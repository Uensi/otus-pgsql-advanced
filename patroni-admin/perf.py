#!/usr/bin/env python3
"""Диагностика производительности.

ASH (Active Session History): раз в секунду снимок активных сессий на каждом узле
  (pg_stat_activity: состояние, событие ожидания, запрос). Отвечает на вопрос
  «что тормозило вчера в 14:30» — история на ASH_KEEP_S секунд в памяти.
История pg_stat_statements: снимки раз в STMT_INTERVAL; топ за период и регрессии
  (среднее время выросло относительно предыдущего такого же окна).
Checkpoints / bgwriter / WAL: доля вынужденных контрольных точек, запись бэкендами,
  переполнение wal_buffers, full page writes — с рекомендациями.
Прогресс долгих операций: pg_stat_progress_* (VACUUM, CREATE INDEX, ANALYZE,
  CLUSTER, basebackup, COPY) в процентах.
Аудит индексов: дубли, перекрывающиеся, FK без индекса, невалидные, неиспользуемые.
История размеров баз (раз в час, 90 дней) — для отчётов и прогноза роста."""
import json
import os
import threading
import time
import zlib
from collections import OrderedDict, deque

from flask import Blueprint, jsonify, request

import pgx
from paths import data_path

bp = Blueprint('perf', __name__)

ASH_INTERVAL = float(os.environ.get('PATRONI_CONSOLE_ASH_INTERVAL', '1'))
ASH_KEEP_S = int(os.environ.get('PATRONI_CONSOLE_ASH_KEEP', '3600'))
ASH_MAX_ROWS = 300
STMT_INTERVAL = 600
STMT_KEEP = 7 * 24 * 3600 // STMT_INTERVAL      # неделя
STMT_TOP = 150
STMT_PATH = data_path('statements_history.jsonl')
SIZES_PATH = data_path('db_sizes.jsonl')
CKPT_INTERVAL = 60

_ASH = {}                 # node -> deque[(ts, [row tuples])]
_QTEXT = OrderedDict()    # qhash -> текст запроса (LRU)
_ASH_LOCK = threading.Lock()
_STMTS = deque(maxlen=STMT_KEEP)    # {'ts','node','rows':[[qid,calls,total_ms,rows,blks_read,temp_w]]}
_STMT_TEXT = OrderedDict()
_CKPT = deque(maxlen=24 * 60)       # снимки счётчиков контрольных точек/WAL
_SIZES = deque(maxlen=24 * 90)
_STATE = {'ash_errors': {}, 'started': False}

# ASH-строка: (pid, user, db, app, state, wait_type, wait_event, qhash, backend_type, dur_s)
CPU = 'CPU'


def _remember(store, key, text, limit=5000):
    store[key] = text
    store.move_to_end(key)
    while len(store) > limit:
        store.popitem(last=False)


# ---------------------------------------------------------------- ASH
def _ash_sample(c):
    rows = pgx.q(c, """
        SELECT pid, usename, datname, application_name, state, wait_event_type, wait_event,
               left(query, 1000) AS query, backend_type,
               EXTRACT(EPOCH FROM (now() - COALESCE(query_start, backend_start)))::float AS dur
        FROM pg_stat_activity
        WHERE pid <> pg_backend_pid()
          AND COALESCE(wait_event_type, '') <> 'Activity'
          AND (state IN ('active', 'idle in transaction', 'idle in transaction (aborted)', 'fastpath function call')
               OR (backend_type <> 'client backend' AND wait_event_type IS NOT NULL))
        LIMIT %s""", (ASH_MAX_ROWS,))
    out = []
    for r in rows:
        txt = r.get('query') or ''
        qh = zlib.crc32(txt.encode('utf-8', 'replace')) if txt else 0
        if qh:
            _remember(_QTEXT, qh, txt)
        out.append((r['pid'], r.get('usename') or '', r.get('datname') or '', r.get('application_name') or '',
                    r.get('state') or '', r.get('wait_event_type') or '', r.get('wait_event') or '', qh,
                    r.get('backend_type') or '', round(r.get('dur') or 0, 1)))
    return out


def _ash_loop():
    conns = {}
    backoff = {}
    while True:
        t0 = time.time()
        try:
            for n in pgx.nodes():
                name = n['name']
                if backoff.get(name, 0) > t0:
                    continue
                c = conns.get(name)
                try:
                    if c is None or c.closed:
                        c = pgx.conn(n)
                        if c is None:
                            raise RuntimeError('нет подключения')
                        conns[name] = c
                    rows = _ash_sample(c)
                    with _ASH_LOCK:
                        dq = _ASH.setdefault(name, deque(maxlen=int(ASH_KEEP_S / max(ASH_INTERVAL, 0.2))))
                        dq.append((t0, rows))
                    _STATE['ash_errors'].pop(name, None)
                except Exception as e:
                    _STATE['ash_errors'][name] = str(e)[:200]
                    backoff[name] = t0 + 30
                    try:
                        if c is not None:
                            c.close()
                    except Exception:
                        pass
                    conns.pop(name, None)
        except Exception:
            pass
        time.sleep(max(0.05, ASH_INTERVAL - (time.time() - t0)))


def wait_class(r):
    """Класс ожидания для графика: CPU (активна, ничего не ждёт) или wait_event_type."""
    if r[4] in ('idle in transaction', 'idle in transaction (aborted)') and not r[5]:
        return 'IdleTx'
    if not r[5]:
        return CPU
    return r[5]


def ash_report(node=None, minutes=15, frm=None, to=None, buckets=60):
    now = time.time()
    to = to or now
    frm = frm or to - minutes * 60
    with _ASH_LOCK:
        series = {k: [s for s in v if frm <= s[0] <= to] for k, v in _ASH.items() if not node or k == node}
    samples = sorted((s for v in series.values() for s in v), key=lambda s: s[0])
    span = max(1.0, to - frm)
    width = span / buckets
    nodes_n = max(1, len(series))
    tl = [{'ts': frm + i * width, 'n': 0, 'classes': {}} for i in range(buckets)]
    seen_samples = [set() for _ in range(buckets)]
    events, queries, users, dbs, sess = {}, {}, {}, {}, {}
    total = 0
    for ts, rows in samples:
        bi = min(buckets - 1, int((ts - frm) / width))
        seen_samples[bi].add(round(ts, 1))
        for r in rows:
            if r[8] != 'client backend' and r[4] == '':
                cls = r[5] or CPU
            else:
                cls = wait_class(r)
            tl[bi]['classes'][cls] = tl[bi]['classes'].get(cls, 0) + 1
            total += 1
            ev = cls if cls in (CPU, 'IdleTx') else f'{r[5]}: {r[6]}'
            events[ev] = events.get(ev, 0) + 1
            if r[7]:
                q = queries.setdefault(r[7], {'n': 0, 'events': {}, 'db': r[2], 'user': r[1]})
                q['n'] += 1
                q['events'][ev] = q['events'].get(ev, 0) + 1
            users[r[1] or '—'] = users.get(r[1] or '—', 0) + 1
            dbs[r[2] or '—'] = dbs.get(r[2] or '—', 0) + 1
            sk = (r[0], r[1], r[3])
            sess[sk] = sess.get(sk, 0) + 1
    # AAS (average active sessions) = строк в бакете / число снимков в бакете (на узел)
    # снимки всех узлов в одном цикле имеют общий ts — число раундов = число различных ts
    for i, b in enumerate(tl):
        cnt = max(1, len(seen_samples[i]))
        b['classes'] = {c: round(v / cnt, 2) for c, v in b['classes'].items()}
        b['aas'] = round(sum(b['classes'].values()), 2)
    n_samples = max(1, len({round(s[0], 1) for s in samples}))

    def top(d, limit=10):
        return sorted(({'key': k, 'n': v, 'pct': round(100.0 * v / max(1, total), 1),
                        'aas': round(v / n_samples, 2)} for k, v in d.items()), key=lambda x: -x['n'])[:limit]
    tq = []
    for qh, q in sorted(queries.items(), key=lambda kv: -kv[1]['n'])[:15]:
        tq.append({'query': _QTEXT.get(qh, '?'), 'n': q['n'], 'pct': round(100.0 * q['n'] / max(1, total), 1),
                   'aas': round(q['n'] / n_samples, 2), 'db': q['db'], 'user': q['user'],
                   'top_event': max(q['events'].items(), key=lambda kv: kv[1])[0] if q['events'] else ''})
    ts_ = [{'pid': k[0], 'user': k[1], 'app': k[2], 'n': v, 'pct': round(100.0 * v / max(1, total), 1)}
           for k, v in sorted(sess.items(), key=lambda kv: -kv[1])[:10]]
    with _ASH_LOCK:
        oldest = min((v[0][0] for v in _ASH.values() if v), default=None)
    return {'from': frm, 'to': to, 'bucket_s': round(width, 1), 'timeline': tl, 'samples': len(samples),
            'avg_active': round(total / n_samples, 2) if samples else 0,
            'top_events': top(events), 'top_queries': tq, 'top_users': top(users, 8), 'top_dbs': top(dbs, 8),
            'top_sessions': ts_, 'history_from': oldest, 'interval_s': ASH_INTERVAL,
            'errors': dict(_STATE['ash_errors'])}


@bp.route('/api/ash')
def api_ash():
    node = request.args.get('node') or None
    try:
        minutes = max(1, min(int(request.args.get('minutes', 15)), ASH_KEEP_S // 60))
        frm = float(request.args['from']) if request.args.get('from') else None
        to = float(request.args['to']) if request.args.get('to') else None
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'некорректный период'})
    return jsonify({'ok': True, 'data': ash_report(node, minutes, frm, to)})


# ---------------------------------------------------------------- pg_stat_statements
def _stmt_snapshot():
    def run(c, n):
        v = pgx.server_version_num(c)
        tcol = 'total_exec_time' if v >= 130000 else 'total_time'
        rows = pgx.q(c, f"""
            SELECT queryid::text AS qid, left(query, 2000) AS query, calls, {tcol} AS total_ms, rows,
                   shared_blks_read, temp_blks_written
            FROM pg_stat_statements WHERE queryid IS NOT NULL
            ORDER BY {tcol} DESC LIMIT %s""", (STMT_TOP,))
        return rows
    ld = pgx.leader()
    if not ld:
        return None
    rows = pgx.on_leader(run)
    if not rows:
        return None
    rec = {'ts': time.time(), 'node': ld['name'],
           'rows': [[r['qid'], int(r['calls'] or 0), round(float(r['total_ms'] or 0), 2), int(r['rows'] or 0),
                     int(r['shared_blks_read'] or 0), int(r['temp_blks_written'] or 0)] for r in rows]}
    for r in rows:
        _remember(_STMT_TEXT, r['qid'], r['query'] or '', 20000)
    _STMTS.append(rec)
    try:
        with open(STMT_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps({**rec, 'texts': {r['qid']: (r['query'] or '')[:2000] for r in rows}}, ensure_ascii=False) + '\n')
        if os.path.getsize(STMT_PATH) > 60 * 1024 * 1024:
            _rewrite_stmt_file()
    except Exception:
        pass
    return rec


def _rewrite_stmt_file():
    tmp = STMT_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for rec in list(_STMTS)[-len(_STMTS) // 2:]:
            f.write(json.dumps({**rec, 'texts': {r[0]: _STMT_TEXT.get(r[0], '')[:2000] for r in rec['rows']}}, ensure_ascii=False) + '\n')
    os.replace(tmp, STMT_PATH)


def _load_stmt():
    try:
        with open(STMT_PATH, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                for k, v in (rec.pop('texts', None) or {}).items():
                    _remember(_STMT_TEXT, k, v, 20000)
                if time.time() - rec.get('ts', 0) < STMT_KEEP * STMT_INTERVAL:
                    _STMTS.append(rec)
    except Exception:
        pass


def _nearest(ts):
    best = None
    for s in _STMTS:
        if best is None or abs(s['ts'] - ts) < abs(best['ts'] - ts):
            best = s
    return best


def _delta(a, b):
    """Разница счётчиков между снимками a (раньше) и b. Сброс статистики → запрос пропускается."""
    am = {r[0]: r for r in a['rows']}
    complete = len(a['rows']) < STMT_TOP      # в снимке a все запросы — отсутствие значит «ещё не выполнялся»
    out = {}
    for r in b['rows']:
        p = am.get(r[0])
        if p is None:
            # запрос появился после снимка a (или был вне топа): база — ноль
            p = [r[0], 0, 0.0, 0, 0, 0]
            if not complete and r[1] > 1000:
                continue                      # давно существующий запрос вне топа — счётчики не за период
        dc, dt = r[1] - p[1], r[2] - p[2]
        if dc <= 0 or dt < 0:
            continue
        out[r[0]] = {'calls': dc, 'total_ms': round(dt, 1), 'mean_ms': round(dt / dc, 3),
                     'rows': r[3] - p[3], 'blks_read': r[4] - p[4], 'temp_w': r[5] - p[5]}
    return out


def statements_report(frm, to):
    a, b = _nearest(frm), _nearest(to)
    if not a or not b or a is b:
        return {'ok': False, 'error': 'недостаточно снимков pg_stat_statements за период '
                                       '(снимок раз в %d мин; нужно расширение pg_stat_statements)' % (STMT_INTERVAL // 60)}
    cur = _delta(a, b)
    span = b['ts'] - a['ts']
    pa, pb = _nearest(a['ts'] - span), a
    prev = _delta(pa, pb) if pa and pa is not pb else {}
    total = sum(x['total_ms'] for x in cur.values()) or 1
    top = []
    for qid, x in sorted(cur.items(), key=lambda kv: -kv[1]['total_ms'])[:30]:
        p = prev.get(qid)
        ratio = round(x['mean_ms'] / p['mean_ms'], 2) if p and p['mean_ms'] > 0 else None
        top.append({'qid': qid, 'query': _STMT_TEXT.get(qid, '?'), **x, 'pct': round(100.0 * x['total_ms'] / total, 1),
                    'prev_mean_ms': p['mean_ms'] if p else None, 'ratio': ratio})
    regressions = []
    for qid, x in cur.items():
        p = prev.get(qid)
        if p and p['mean_ms'] > 0 and x['calls'] >= 10 and p['calls'] >= 10 and x['mean_ms'] / p['mean_ms'] >= 2 \
                and x['total_ms'] >= 1000:
            regressions.append({'qid': qid, 'query': _STMT_TEXT.get(qid, '?'), 'mean_ms': x['mean_ms'],
                                'prev_mean_ms': p['mean_ms'], 'ratio': round(x['mean_ms'] / p['mean_ms'], 1),
                                'calls': x['calls'], 'total_ms': x['total_ms']})
    regressions.sort(key=lambda r: -r['total_ms'])
    return {'ok': True, 'from': a['ts'], 'to': b['ts'], 'prev_from': pa['ts'] if pa else None,
            'node': b.get('node'), 'total_ms': round(total, 1), 'top': top, 'regressions': regressions[:20],
            'snapshots': len(_STMTS)}


@bp.route('/api/statements/report')
def api_statements():
    try:
        hours = float(request.args.get('hours', 1))
        to = float(request.args.get('to') or time.time())
        frm = float(request.args.get('from') or (to - hours * 3600))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'некорректный период'})
    return jsonify(statements_report(frm, to))


@bp.route('/api/statements/snapshot', methods=['POST'])
def api_stmt_snapshot():
    rec = _stmt_snapshot()
    return jsonify({'ok': bool(rec), 'error': None if rec else 'pg_stat_statements недоступен на лидере'})


# ---------------------------------------------------------------- checkpoints / WAL
def _ckpt_counters(c):
    v = pgx.server_version_num(c)
    out = {'version': v}
    if v >= 170000:
        r = pgx.q1(c, """SELECT num_timed AS timed, num_requested AS req, write_time, sync_time,
                                buffers_written AS buf_ckpt FROM pg_stat_checkpointer""")
        out.update(r)
        out.update(pgx.q1(c, "SELECT buffers_clean, maxwritten_clean, buffers_alloc FROM pg_stat_bgwriter"))
        out.update((pgx.try_q(c, "SELECT sum(writes)::bigint AS buf_backend, sum(fsyncs)::bigint AS backend_fsync "
                                 "FROM pg_stat_io WHERE backend_type='client backend' AND object='relation'") or [{}])[0])
    else:
        out.update(pgx.q1(c, """SELECT checkpoints_timed AS timed, checkpoints_req AS req,
                                       checkpoint_write_time AS write_time, checkpoint_sync_time AS sync_time,
                                       buffers_checkpoint AS buf_ckpt, buffers_clean, maxwritten_clean,
                                       buffers_backend AS buf_backend, buffers_backend_fsync AS backend_fsync,
                                       buffers_alloc FROM pg_stat_bgwriter"""))
    if v >= 140000:
        out.update((pgx.try_q(c, "SELECT wal_records, wal_fpi, wal_bytes::bigint AS wal_bytes, wal_buffers_full FROM pg_stat_wal") or [{}])[0])
    out['lsn'] = (pgx.try_q(c, "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint AS b") or [{'b': None}])[0]['b']
    return {k: (float(v_) if isinstance(v_, (int, float)) else v_) for k, v_ in out.items()}


def _ckpt_sample():
    def run(c, n):
        return _ckpt_counters(c)
    r = pgx.on_leader(run)
    if r:
        _CKPT.append({'ts': time.time(), **r})


def checkpoints_report(minutes=60):
    def settings(c, n):
        names = ('max_wal_size', 'min_wal_size', 'checkpoint_timeout', 'checkpoint_completion_target', 'wal_buffers',
                 'wal_compression', 'full_page_writes', 'bgwriter_lru_maxpages', 'bgwriter_delay', 'shared_buffers',
                 'wal_level', 'synchronous_commit')
        return {k: pgx.setting(c, k) for k in names}, _ckpt_counters(c)
    res = pgx.on_leader(settings)
    if not res:
        return {'ok': False, 'error': 'лидер недоступен'}
    st, now_c = res
    now = time.time()
    base = next((s for s in _CKPT if now - s['ts'] <= minutes * 60), None)
    d = {}
    span = None
    if base:
        span = now - base['ts']
        for k, v in now_c.items():
            if isinstance(v, float) and isinstance(base.get(k), float):
                d[k] = v - base[k]
    src = d if span and span >= 120 else now_c
    timed, req = src.get('timed') or 0, src.get('req') or 0
    total_ck = timed + req
    recs = []
    if total_ck and req / total_ck > 0.2:
        recs.append({'level': 'warn', 'text': f'{round(100 * req / total_ck)}% контрольных точек вынужденные (по объёму WAL). '
                                              f'Увеличьте max_wal_size (сейчас {st.get("max_wal_size")}), чтобы они шли по checkpoint_timeout.'})
    bc, bb, bcl = src.get('buf_ckpt') or 0, src.get('buf_backend') or 0, src.get('buffers_clean') or 0
    allw = bc + bb + bcl
    if allw and bb / allw > 0.2:
        recs.append({'level': 'warn', 'text': f'{round(100 * bb / allw)}% грязных страниц пишут сами бэкенды — запросы ждут записи. '
                                              'Настройте bgwriter_lru_maxpages/bgwriter_delay, проверьте shared_buffers и скорость дисков.'})
    if (src.get('maxwritten_clean') or 0) > 0 and span:
        recs.append({'level': 'info', 'text': 'bgwriter упирается в bgwriter_lru_maxpages (maxwritten_clean > 0) — можно увеличить.'})
    if (src.get('wal_buffers_full') or 0) > 0:
        recs.append({'level': 'warn', 'text': f'wal_buffers переполнялся {int(src["wal_buffers_full"])} раз — увеличьте wal_buffers (сейчас {st.get("wal_buffers")}).'})
    if src.get('wal_records') and src.get('wal_fpi') and src['wal_fpi'] / src['wal_records'] > 0.3:
        recs.append({'level': 'info', 'text': f'{round(100 * src["wal_fpi"] / src["wal_records"])}% записей WAL — полные образы страниц (FPI). '
                                              'Помогают wal_compression = on/lz4 и более редкие контрольные точки.'})
    if not recs:
        recs.append({'level': 'ok', 'text': 'Контрольные точки и запись WAL в норме.'})
    rates = None
    if span and span >= 120:
        rates = {'window_s': round(span), 'wal_bytes_per_s': round((d.get('lsn') or 0) / span, 1),
                 'checkpoints_per_h': round(total_ck / span * 3600, 2),
                 'write_ms_per_ckpt': round((d.get('write_time') or 0) / total_ck, 1) if total_ck else None,
                 'sync_ms_per_ckpt': round((d.get('sync_time') or 0) / total_ck, 1) if total_ck else None}
    hist = [{'ts': s['ts'], 'lsn': s.get('lsn'), 'timed': s.get('timed'), 'req': s.get('req')} for s in _CKPT if now - s['ts'] <= minutes * 60]
    return {'ok': True, 'settings': st, 'counters': now_c, 'delta': d if span else None, 'rates': rates,
            'recommendations': recs, 'history': hist, 'since_reset': not (span and span >= 120)}


@bp.route('/api/perf/checkpoints')
def api_checkpoints():
    try:
        minutes = max(5, min(int(request.args.get('minutes', 60)), 24 * 60))
    except (TypeError, ValueError):
        minutes = 60
    return jsonify(checkpoints_report(minutes))


# ---------------------------------------------------------------- прогресс операций
_PROGRESS_SQL = [
    ('VACUUM', 130000 - 40000, """SELECT p.pid, p.datname, p.relid::oid AS relid, p.phase,
        CASE WHEN p.heap_blks_total > 0 THEN round(100.0 * p.heap_blks_scanned / p.heap_blks_total, 1) END AS pct,
        p.heap_blks_scanned AS done, p.heap_blks_total AS total FROM pg_stat_progress_vacuum p"""),
    ('ANALYZE', 130000, """SELECT p.pid, p.datname, p.relid::oid AS relid, p.phase,
        CASE WHEN p.sample_blks_total > 0 THEN round(100.0 * p.sample_blks_scanned / p.sample_blks_total, 1) END AS pct,
        p.sample_blks_scanned AS done, p.sample_blks_total AS total FROM pg_stat_progress_analyze p"""),
    ('CREATE INDEX', 120000, """SELECT p.pid, p.datname, COALESCE(NULLIF(p.index_relid, 0), p.relid)::oid AS relid,
        p.command || ': ' || p.phase AS phase,
        CASE WHEN p.blocks_total > 0 THEN round(100.0 * p.blocks_done / p.blocks_total, 1)
             WHEN p.tuples_total > 0 THEN round(100.0 * p.tuples_done / p.tuples_total, 1) END AS pct,
        COALESCE(NULLIF(p.blocks_done, 0), p.tuples_done) AS done, COALESCE(NULLIF(p.blocks_total, 0), p.tuples_total) AS total
        FROM pg_stat_progress_create_index p"""),
    ('CLUSTER', 120000, """SELECT p.pid, p.datname, p.relid::oid AS relid, p.command || ': ' || p.phase AS phase,
        CASE WHEN p.heap_blks_total > 0 THEN round(100.0 * p.heap_blks_scanned / p.heap_blks_total, 1) END AS pct,
        p.heap_blks_scanned AS done, p.heap_blks_total AS total FROM pg_stat_progress_cluster p"""),
    ('BASEBACKUP', 130000, """SELECT p.pid, NULL::name AS datname, NULL::oid AS relid, p.phase,
        CASE WHEN p.backup_total > 0 THEN round(100.0 * p.backup_streamed / p.backup_total, 1) END AS pct,
        p.backup_streamed AS done, p.backup_total AS total FROM pg_stat_progress_basebackup p"""),
    ('COPY', 140000, """SELECT p.pid, p.datname, NULLIF(p.relid, 0)::oid AS relid, p.command || ' ' || p.type AS phase,
        CASE WHEN p.bytes_total > 0 THEN round(100.0 * p.bytes_processed / p.bytes_total, 1) END AS pct,
        p.bytes_processed AS done, p.bytes_total AS total FROM pg_stat_progress_copy p"""),
]


def _resolve_names(node, ops):
    """relid → schema.table: regclass разрешается только в базе, где лежит таблица."""
    by_db = {}
    for op in ops:
        if op.get('relid') and op.get('datname'):
            by_db.setdefault(op['datname'], set()).add(int(op['relid']))
    for db, oids in by_db.items():
        c = pgx.conn(node, db)
        if not c:
            continue
        try:
            names = {r['oid']: r['name'] for r in pgx.try_q(c, """SELECT c.oid::bigint AS oid, n.nspname || '.' || c.relname AS name
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = ANY(%s::oid[])""", (list(oids),))}
        finally:
            c.close()
        for op in ops:
            if op.get('datname') == db and op.get('relid') and int(op['relid']) in names:
                op['target'] = names[int(op['relid'])]


def progress():
    def run(c, n):
        v = pgx.server_version_num(c)
        ops = []
        for kind, minv, sql in _PROGRESS_SQL:
            if v < minv:
                continue
            for r in pgx.try_q(c, f"""SELECT x.*, a.usename, EXTRACT(EPOCH FROM now() - a.query_start)::float AS dur_s,
                                             left(a.query, 300) AS query
                                      FROM ({sql}) x LEFT JOIN pg_stat_activity a ON a.pid = x.pid"""):
                r['kind'] = kind
                r['target'] = {'BASEBACKUP': 'pg_basebackup', 'COPY': '(запрос)'}.get(kind, f"oid {r.get('relid')}") if r.get('relid') is None or kind == 'BASEBACKUP' else f"oid {r['relid']}"
                if r.get('pct') is not None and r.get('dur_s') and r['pct'] > 0:
                    r['eta_s'] = round(r['dur_s'] * (100 - float(r['pct'])) / float(r['pct']))
                ops.append(r)
        _resolve_names(n, ops)
        return ops
    out = []
    for r in pgx.on_each_node(run):
        for op in (r.get('data') or []):
            op['node'] = r['node']
            out.append(op)
    return out


@bp.route('/api/progress')
def api_progress():
    return jsonify({'ok': True, 'data': progress()})


# ---------------------------------------------------------------- аудит индексов
_IDX_SQL = {
    'invalid': """
        SELECT n.nspname AS schema, t.relname AS table, i.relname AS index, pg_relation_size(i.oid) AS size,
               format('DROP INDEX CONCURRENTLY %I.%I;', n.nspname, i.relname) AS fix,
               pg_get_indexdef(i.oid) AS def
        FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid
        JOIN pg_namespace n ON n.oid = i.relnamespace
        WHERE NOT x.indisvalid OR NOT x.indisready""",
    'duplicate': """
        SELECT n.nspname AS schema, t.relname AS table, i1.relname AS index, i2.relname AS duplicate_of,
               pg_relation_size(i1.oid) AS size,
               format('DROP INDEX CONCURRENTLY %I.%I;', n.nspname, i1.relname) AS fix,
               pg_get_indexdef(i1.oid) AS def
        FROM pg_index a JOIN pg_index b ON a.indrelid = b.indrelid AND a.indexrelid > b.indexrelid
             AND a.indkey::text = b.indkey::text AND a.indclass::text = b.indclass::text
             AND COALESCE(pg_get_expr(a.indexprs, a.indrelid), '') = COALESCE(pg_get_expr(b.indexprs, b.indrelid), '')
             AND COALESCE(pg_get_expr(a.indpred, a.indrelid), '') = COALESCE(pg_get_expr(b.indpred, b.indrelid), '')
        JOIN pg_class i1 ON i1.oid = a.indexrelid JOIN pg_class i2 ON i2.oid = b.indexrelid
        JOIN pg_class t ON t.oid = a.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE NOT a.indisprimary AND NOT a.indisunique
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')""",
    'overlap': """
        SELECT n.nspname AS schema, t.relname AS table, i1.relname AS index, i2.relname AS covered_by,
               pg_relation_size(i1.oid) AS size,
               format('DROP INDEX CONCURRENTLY %I.%I;', n.nspname, i1.relname) AS fix,
               pg_get_indexdef(i1.oid) AS def
        FROM pg_index a JOIN pg_index b ON a.indrelid = b.indrelid AND a.indexrelid <> b.indexrelid
             AND a.indnkeyatts < b.indnkeyatts
             AND (string_to_array(b.indkey::text, ' '))[1:a.indnkeyatts] = (string_to_array(a.indkey::text, ' '))[1:a.indnkeyatts]
             AND a.indexprs IS NULL AND b.indexprs IS NULL AND a.indpred IS NULL AND b.indpred IS NULL
        JOIN pg_class i1 ON i1.oid = a.indexrelid JOIN pg_class i2 ON i2.oid = b.indexrelid
        JOIN pg_am am1 ON am1.oid = i1.relam JOIN pg_am am2 ON am2.oid = i2.relam
        JOIN pg_class t ON t.oid = a.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE am1.amname = 'btree' AND am2.amname = 'btree' AND NOT a.indisunique AND NOT a.indisprimary
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')""",
    'fk_no_index': """
        SELECT n.nspname AS schema, t.relname AS table, c.conname AS constraint,
               (SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY k.ord)
                  FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS columns,
               c.confrelid::regclass::text AS references, pg_relation_size(t.oid) AS table_size,
               format('CREATE INDEX CONCURRENTLY ON %I.%I (%s);', n.nspname, t.relname,
                      (SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY k.ord)
                         FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord)
                         JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum)) AS fix
        FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE c.contype = 'f' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND NOT EXISTS (SELECT 1 FROM pg_index x WHERE x.indrelid = c.conrelid
                          AND (x.indkey::int2[])[0:array_length(c.conkey, 1) - 1] @> c.conkey
                          AND (x.indkey::int2[])[0:array_length(c.conkey, 1) - 1] <@ c.conkey)
        ORDER BY pg_relation_size(t.oid) DESC""",
    'unused': """
        SELECT s.schemaname AS schema, s.relname AS table, s.indexrelname AS index, pg_relation_size(s.indexrelid) AS size,
               s.idx_scan AS scans, format('DROP INDEX CONCURRENTLY %I.%I;', s.schemaname, s.indexrelname) AS fix,
               pg_get_indexdef(s.indexrelid) AS def
        FROM pg_stat_user_indexes s JOIN pg_index x ON x.indexrelid = s.indexrelid
        WHERE s.idx_scan = 0 AND NOT x.indisunique AND NOT x.indisprimary
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = s.indexrelid)
        ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT 50""",
}


def index_audit(db):
    def run(c, n):
        out = {k: pgx.try_q(c, sql) for k, sql in _IDX_SQL.items()}
        out['stats_reset'] = (pgx.try_q(c, "SELECT stats_reset::text AS r FROM pg_stat_database WHERE datname = current_database()") or [{'r': None}])[0]['r']
        return out
    res = pgx.on_leader(run, db=db)
    if res is None:
        return {'ok': False, 'error': 'лидер недоступен или нет базы ' + db}
    waste = sum(r.get('size') or 0 for k in ('invalid', 'duplicate', 'overlap', 'unused') for r in res[k])
    return {'ok': True, 'db': db, **res, 'reclaimable_bytes': waste,
            'counts': {k: len(res[k]) for k in _IDX_SQL}}


@bp.route('/api/indexes/audit')
def api_index_audit():
    db = request.args.get('db') or ''
    if not db:
        db = (pgx.on_leader(lambda c, n: (pgx.databases(c) or ['postgres'])[0]) or 'postgres')
    if not pgx.ident_ok(db):
        return jsonify({'ok': False, 'error': 'некорректное имя базы'})
    return jsonify(index_audit(db))


# ---------------------------------------------------------------- размеры баз (для отчётов)
def _size_sample():
    rows = pgx.on_leader(lambda c, n: pgx.q(c, "SELECT datname AS name, pg_database_size(datname) AS size "
                                               "FROM pg_database WHERE datallowconn AND NOT datistemplate"))
    if not rows:
        return
    rec = {'ts': time.time(), 'dbs': {r['name']: int(r['size']) for r in rows}}
    _SIZES.append(rec)
    try:
        with open(SIZES_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
    except Exception:
        pass


def _load_sizes():
    try:
        with open(SIZES_PATH, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    _SIZES.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass


def size_growth(days=7):
    """Рост баз за days суток и прогноз на 30 дней (линейный)."""
    if len(_SIZES) < 2:
        return []
    last = _SIZES[-1]
    base = next((s for s in _SIZES if last['ts'] - s['ts'] <= days * 86400), _SIZES[0])
    span = max(3600.0, last['ts'] - base['ts'])
    out = []
    for name, size in last['dbs'].items():
        prev = base['dbs'].get(name)
        if prev is None:
            continue
        per_day = (size - prev) / span * 86400
        out.append({'name': name, 'size': size, 'delta': size - prev, 'per_day': round(per_day),
                    'in_30d': round(size + per_day * 30), 'window_days': round(span / 86400, 1)})
    return sorted(out, key=lambda x: -x['delta'])


# ---------------------------------------------------------------- фоновые задачи
def _bg_loop():
    last_stmt = last_ckpt = last_size = 0
    while True:
        now = time.time()
        try:
            if now - last_ckpt >= CKPT_INTERVAL:
                last_ckpt = now
                _ckpt_sample()
            if now - last_stmt >= STMT_INTERVAL:
                last_stmt = now
                _stmt_snapshot()
            if now - last_size >= 3600:
                last_size = now
                _size_sample()
        except Exception:
            pass
        time.sleep(15)


def start():
    if _STATE['started'] or os.environ.get('PATRONI_CONSOLE_NO_SAMPLERS'):
        return
    _STATE['started'] = True
    _load_stmt()
    _load_sizes()
    threading.Thread(target=_ash_loop, daemon=True, name='ash-sampler').start()
    threading.Thread(target=_bg_loop, daemon=True, name='perf-history').start()
