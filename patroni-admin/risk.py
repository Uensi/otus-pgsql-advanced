#!/usr/bin/env python3
"""Риски потери данных и простоя — то, на чём DBA «горит» в продакшене.

1. Wraparound XID / MultiXact: возраст datfrozenxid по базам, самые «старые»
   таблицы, прогноз до остановки записи, VACUUM FREEZE в фоне.
2. RPO: возраст последнего бэкапа, архивация WAL (pg_stat_archiver), очередь
   .ready, результаты автоматических проверок восстановления.
3. Слоты репликации: wal_status (lost/unreserved/extended), safe_wal_size,
   неактивные слоты без max_slot_wal_keep_size — они заполняют диск лидера.
4. Лаг репликации во времени (write/flush/replay_lag из pg_stat_replication).

/api/risk/summary — сводка для экрана «Риски», баннера здоровья и алертов."""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session

import pgx
from paths import data_path

bp = Blueprint('risk', __name__)

XID_LIMIT = 2 ** 31            # возраст, при котором наступает wraparound
XID_STOP = XID_LIMIT - 3_000_000   # PG14+: запись запрещается за ~3 млн до предела
DEFAULTS = {
    'xid_warn_pct': 50, 'xid_crit_pct': 75,
    'backup_warn_h': 26, 'backup_crit_h': 50,
    'restore_test_warn_days': 8,
    'replay_lag_warn_s': 60, 'replay_lag_crit_s': 300,
    'archive_ready_warn': 50,
    'slot_inactive_warn_bytes': 1 << 30, 'slot_inactive_crit_bytes': 10 << 30,
}
CFG_PATH = data_path('risk_config.json')
RESTORE_LOG = data_path('restore_tests.jsonl')
_XID_HIST = deque(maxlen=720)        # (ts, xmax) для оценки скорости расхода XID
_JOBS = {}                           # фоновые VACUUM FREEZE
_JOBS_LOCK = threading.Lock()


def cfg():
    out = dict(DEFAULTS)
    try:
        with open(CFG_PATH, encoding='utf-8') as f:
            out.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- wraparound
def _xmax(c):
    # txid_current() расходует XID — берём xmax снимка, он не расходует
    for sql in ("SELECT pg_snapshot_xmax(pg_current_snapshot())::text::bigint AS x",
                "SELECT txid_snapshot_xmax(txid_current_snapshot()) AS x"):
        try:
            return int(pgx.q1(c, sql)['x'])
        except Exception:
            continue
    return None


def xid_rate():
    """XID в секунду по истории снимков (окно до 1 ч)."""
    h = list(_XID_HIST)
    if len(h) < 2:
        return None
    t0, x0 = h[0]
    for t, x in h:
        if h[-1][0] - t <= 3600:
            t0, x0 = t, x
            break
    t1, x1 = h[-1]
    if t1 - t0 < 30 or x1 < x0:
        return None
    return (x1 - x0) / (t1 - t0)


def wraparound():
    def run(c, node):
        x = _xmax(c)
        if x is not None:
            _XID_HIST.append((time.time(), x))
        dbs = pgx.q(c, """
            SELECT datname, age(datfrozenxid)::bigint AS xid_age,
                   mxid_age(datminmxid)::bigint AS mxid_age,
                   pg_database_size(datname) AS size
            FROM pg_database WHERE datallowconn ORDER BY age(datfrozenxid) DESC""")
        st = {k: pgx.setting(c, k) for k in ('autovacuum_freeze_max_age', 'autovacuum_multixact_freeze_max_age',
                                              'vacuum_failsafe_age', 'autovacuum')}
        return {'node': node['name'], 'dbs': dbs, 'settings': st}
    base = pgx.on_leader(run)
    if not base:
        return {'ok': False, 'level': 'unknown', 'error': 'лидер недоступен'}
    c = cfg()
    rate = xid_rate()
    top = []
    for d in base['dbs'][:3]:   # самые «старые» базы: ищем таблицы, держащие возраст
        rows = pgx.on_leader(lambda cc, n: pgx.try_q(cc, """
            SELECT n.nspname AS schema, c.relname AS table, age(c.relfrozenxid)::bigint AS xid_age,
                   mxid_age(c.relminmxid)::bigint AS mxid_age, pg_total_relation_size(c.oid) AS size,
                   s.last_autovacuum::text AS last_autovacuum, s.last_vacuum::text AS last_vacuum
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
            WHERE c.relkind IN ('r','m','t') AND c.relfrozenxid <> '0'
            ORDER BY age(c.relfrozenxid) DESC LIMIT 8"""), db=d['datname']) or []
        for r in rows:
            r['db'] = d['datname']
        top.extend(rows)
    top.sort(key=lambda r: -(r.get('xid_age') or 0))
    for d in base['dbs']:
        d['xid_pct'] = round(100.0 * (d['xid_age'] or 0) / XID_LIMIT, 2)
        d['mxid_pct'] = round(100.0 * (d['mxid_age'] or 0) / XID_LIMIT, 2)
        left = XID_STOP - max(d['xid_age'] or 0, d['mxid_age'] or 0)
        d['days_left'] = round(left / rate / 86400, 1) if rate and rate > 0 else None
    worst = max(base['dbs'], key=lambda d: max(d['xid_pct'], d['mxid_pct'])) if base['dbs'] else None
    pct = max(worst['xid_pct'], worst['mxid_pct']) if worst else 0
    level = 'bad' if pct >= c['xid_crit_pct'] else ('warn' if pct >= c['xid_warn_pct'] else 'ok')
    if (base['settings'].get('autovacuum') or 'on') == 'off':
        level = pgx.worst([level, 'warn'])
    return {'ok': True, 'level': level, 'node': base['node'], 'max_pct': pct,
            'worst_db': worst['datname'] if worst else None,
            'days_left': worst['days_left'] if worst else None,
            'xid_per_sec': round(rate, 2) if rate else None,
            'dbs': base['dbs'], 'top_tables': top[:15], 'settings': base['settings']}


def _freeze_worker(jid, node, db, schema, table):
    def setj(**kw):
        with _JOBS_LOCK:
            _JOBS[jid].update(kw)
    c = pgx.conn(node, db)
    if not c:
        setj(state='error', error='нет подключения', finished=time.time())
        return
    try:
        cur = c.cursor()
        cur.execute('VACUUM (FREEZE, ANALYZE) %s.%s' % (_qi(schema), _qi(table)))
        cur.close()
        setj(state='done', finished=time.time())
    except Exception as e:
        setj(state='error', error=str(e), finished=time.time())
    finally:
        c.close()
        pgx.cache_clear('risk:')


def _qi(name):
    return '"' + name.replace('"', '""') + '"'


@bp.route('/api/risk/freeze', methods=['POST'])
def freeze():
    b = request.json or {}
    db, schema, table = b.get('db') or '', b.get('schema') or 'public', b.get('table') or ''
    if not (pgx.ident_ok(db) and pgx.ident_ok(schema) and pgx.ident_ok(table)):
        return jsonify({'ok': False, 'error': 'некорректное имя базы/схемы/таблицы'})
    ld = pgx.leader()
    if not ld:
        return jsonify({'ok': False, 'error': 'лидер недоступен'})
    jid = 'fz-%d' % int(time.time() * 1000)
    with _JOBS_LOCK:
        if any(j['state'] == 'running' and j['target'] == f'{db}.{schema}.{table}' for j in _JOBS.values()):
            return jsonify({'ok': False, 'error': 'VACUUM FREEZE этой таблицы уже выполняется'})
        _JOBS[jid] = {'id': jid, 'target': f'{db}.{schema}.{table}', 'node': ld['name'], 'state': 'running',
                      'started': time.time(), 'user': session.get('user')}
    threading.Thread(target=_freeze_worker, args=(jid, ld, db, schema, table), daemon=True).start()
    return jsonify({'ok': True, 'data': {'job': jid, 'message': f'VACUUM FREEZE {schema}.{table} запущен на {ld["name"]}'}})


@bp.route('/api/risk/jobs')
def jobs():
    with _JOBS_LOCK:
        return jsonify({'ok': True, 'data': sorted(_JOBS.values(), key=lambda j: -j['started'])[:20]})


# ---------------------------------------------------------------- RPO / бэкапы
def _parse_ts(s):
    if not s:
        return None
    import re
    try:
        s = str(s).strip().replace('Z', '+00:00').replace(' ', 'T', 1)
        s = re.sub(r'(\.\d{6})\d+', r'\1', s)          # wal-g пишет наносекунды
        if re.search(r'[+-]\d\d$', s):                   # PostgreSQL: +03 → +03:00
            s += ':00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def restore_tests(limit=20):
    out = []
    try:
        with open(RESTORE_LOG, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out[-limit:]


def record_restore_test(node, ok, out):
    """Вызывается из agent_api при результате команды restore_test."""
    data = out.get('data') if isinstance(out, dict) else None
    rec = {'ts': time.time(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'node': node, 'ok': bool(ok),
           'data': data, 'error': None if ok else str(out)[:2000]}
    try:
        with open(RESTORE_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass
    pgx.cache_clear('risk:')


def backups():
    A = pgx.app()
    c = cfg()
    now = time.time()
    newest = None; newest_full = None; total = 0; online = 0
    ag = A.agent_api
    if ag is not None:
        with ag._LOCK:
            agents = {k: dict(v) for k, v in ag.AGENTS.items()}
        for name, a in agents.items():
            if now - a.get('last_seen', 0) < 30:
                online += 1
            for b in a.get('backups') or []:
                ts = _parse_ts(b.get('finish_time') or b.get('time') or b.get('start_time'))
                if ts is None:
                    continue
                total += 1
                if newest is None or ts > newest['ts']:
                    newest = {'ts': ts, 'name': b.get('backup_name'), 'node': name, 'full': bool(b.get('is_full'))}
                if b.get('is_full') and (newest_full is None or ts > newest_full):
                    newest_full = ts
    arch = pgx.on_leader(lambda cc, n: {
        'mode': pgx.setting(cc, 'archive_mode'), 'command': pgx.setting(cc, 'archive_command'),
        'library': pgx.setting(cc, 'archive_library'), 'timeout': pgx.setting(cc, 'archive_timeout'),
        'stat': (pgx.try_q(cc, """SELECT archived_count, last_archived_wal, last_archived_time::text AS last_archived_time,
                                         failed_count, last_failed_wal, last_failed_time::text AS last_failed_time,
                                         stats_reset::text AS stats_reset FROM pg_stat_archiver""") or [{}])[0],
        'ready': (pgx.try_q(cc, "SELECT count(*) AS n FROM pg_ls_archive_statusdir() WHERE name LIKE '%.ready'") or [{'n': None}])[0]['n'],
        'current_wal': (pgx.try_q(cc, "SELECT pg_walfile_name(pg_current_wal_lsn()) AS w") or [{'w': None}])[0]['w'],
    })
    issues = []
    age_h = round((now - newest['ts']) / 3600, 1) if newest else None
    if newest is None:
        issues.append(('bad' if online else 'warn',
                       'Нет ни одного бэкапа wal-g' if online else 'Нет данных о бэкапах: агенты не на связи'))
    elif age_h >= c['backup_crit_h']:
        issues.append(('bad', f'Последний бэкап {age_h} ч назад — RPO нарушен'))
    elif age_h >= c['backup_warn_h']:
        issues.append(('warn', f'Последний бэкап {age_h} ч назад'))
    if arch:
        st = arch.get('stat') or {}
        if (arch.get('mode') or 'off') == 'off':
            issues.append(('warn', 'archive_mode = off: WAL не архивируется, PITR невозможен'))
        else:
            lf, la = _parse_ts(st.get('last_failed_time')), _parse_ts(st.get('last_archived_time'))
            if lf and (not la or lf > la):
                issues.append(('bad', f"Архивация WAL падает: {st.get('last_failed_wal')} не отправлен"))
            if arch.get('ready') is not None and arch['ready'] >= c['archive_ready_warn']:
                issues.append(('warn', f"В очереди на архивацию {arch['ready']} WAL-сегментов"))
            if la and now - la > 3600 * 6:
                issues.append(('warn', 'Последний WAL заархивирован более 6 ч назад'))
    tests = restore_tests()
    last_ok = next((t for t in reversed(tests) if t.get('ok')), None)
    if not tests:
        issues.append(('info', 'Проверка восстановления ни разу не выполнялась'))
    else:
        if not tests[-1].get('ok'):
            issues.append(('bad', 'Последняя проверка восстановления завершилась ошибкой'))
        if not last_ok or now - last_ok['ts'] > c['restore_test_warn_days'] * 86400:
            issues.append(('warn', f"Успешной проверки восстановления не было больше {c['restore_test_warn_days']} дней"))
    level = pgx.worst([i[0] for i in issues]) if issues else 'ok'
    return {'ok': True, 'level': level, 'issues': [{'level': l, 'text': t} for l, t in issues],
            'last_backup': newest, 'last_backup_age_h': age_h,
            'last_full_age_h': round((now - newest_full) / 3600, 1) if newest_full else None,
            'backups_total': total, 'agents_online': online, 'archiver': arch,
            'restore_tests': tests[-10:], 'last_restore_ok': last_ok}


@bp.route('/api/risk/restore-test', methods=['POST'])
def restore_test():
    """Проверка восстановления: агент разворачивает последний бэкап во временный
    каталог, поднимает PostgreSQL на отдельном порту, проверяет и удаляет."""
    A = pgx.app()
    node = (request.json or {}).get('node') or ''
    if not node:
        cand = [n['name'] for n in A.NODES if A.agent_api and A.agent_api.agent_online(n['name'])]
        ld = (pgx.leader() or {}).get('name')
        reps = [n for n in cand if n != ld]
        node = (reps or cand or [''])[0]
    if not node or A.agent_api is None or not A.agent_api.agent_online(node):
        return jsonify({'ok': False, 'error': 'нет узла с агентом на связи'})
    target = (request.json or {}).get('target_time')
    cid = A.agent_api.enqueue_agent(node, 'restore_test', target_time=target)
    return jsonify({'ok': True, 'data': {'cmd_id': cid, 'node': node}})


# ---------------------------------------------------------------- слоты
def slots():
    c = cfg()
    def run(cc, n):
        v = pgx.server_version_num(cc)
        cols = "wal_status, safe_wal_size," if v >= 130000 else "NULL::text AS wal_status, NULL::bigint AS safe_wal_size,"
        rows = pgx.q(cc, f"""
            SELECT slot_name, slot_type, database, active, {cols} restart_lsn::text,
                   CASE WHEN pg_is_in_recovery() THEN pg_wal_lsn_diff(pg_last_wal_replay_lsn(), restart_lsn)
                        ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) END AS retained_bytes
            FROM pg_replication_slots ORDER BY slot_name""")
        return {'slots': rows, 'max_slot_wal_keep_size': pgx.setting(cc, 'max_slot_wal_keep_size'),
                'recovery': pgx.q1(cc, 'SELECT pg_is_in_recovery() AS r')['r']}
    out = []; levels = []
    for r in pgx.on_each_node(run):
        if not r['ok']:
            out.append(r); continue
        d = r['data']
        for s in d['slots']:
            ret = s.get('retained_bytes') or 0
            lv, why = 'ok', ''
            ws = s.get('wal_status')
            if ws == 'lost':
                lv, why = 'bad', 'WAL для слота уже удалён — потребитель не сможет продолжить, нужен reinit/пересоздание'
            elif ws == 'unreserved':
                lv, why = 'bad', 'Слот вот-вот потеряет WAL (превышен max_slot_wal_keep_size)'
            elif not s.get('active') and ret >= c['slot_inactive_crit_bytes']:
                lv, why = 'bad', f'Неактивный слот удерживает {pgx.fmt_bytes(ret)} WAL — диск лидера заполняется'
            elif not s.get('active') and ret >= c['slot_inactive_warn_bytes']:
                lv, why = 'warn', f'Неактивный слот удерживает {pgx.fmt_bytes(ret)} WAL'
            elif ws == 'extended':
                lv, why = 'warn', 'Слот удерживает WAL сверх max_wal_size'
            if lv != 'ok' and str(d.get('max_slot_wal_keep_size')) in ('-1', '-1MB'):
                why += '. Ограничения нет (max_slot_wal_keep_size = -1)'
            s.update({'level': lv, 'why': why, 'retained_bytes': ret})
            levels.append(lv)
        out.append({'node': r['node'], 'ok': True, **d})
    return {'ok': True, 'level': pgx.worst(levels) if levels else 'ok', 'nodes': out}


# ---------------------------------------------------------------- лаг во времени
def replication():
    c = cfg()
    def run(cc, n):
        return pgx.q(cc, """
            SELECT application_name, client_addr::text AS client, state, sync_state,
                   EXTRACT(EPOCH FROM write_lag)::float AS write_lag_s,
                   EXTRACT(EPOCH FROM flush_lag)::float AS flush_lag_s,
                   EXTRACT(EPOCH FROM replay_lag)::float AS replay_lag_s,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes,
                   reply_time::text AS reply_time
            FROM pg_stat_replication ORDER BY application_name""")
    rows = pgx.on_leader(run)
    if rows is None:
        return {'ok': False, 'level': 'unknown', 'error': 'лидер недоступен', 'replicas': []}
    levels = []
    for r in rows:
        lag = r.get('replay_lag_s')
        lv = 'ok'
        if lag is not None:
            lv = 'bad' if lag >= c['replay_lag_crit_s'] else ('warn' if lag >= c['replay_lag_warn_s'] else 'ok')
        if r.get('state') != 'streaming':
            lv = pgx.worst([lv, 'warn'])
        r['level'] = lv
        levels.append(lv)
    return {'ok': True, 'level': pgx.worst(levels) if levels else 'ok', 'replicas': rows,
            'max_replay_lag_s': max([r['replay_lag_s'] or 0 for r in rows] or [0])}


# ---------------------------------------------------------------- сводка
def summary(force=False):
    def build():
        parts = {}
        for key, fn in (('wraparound', wraparound), ('backups', backups), ('slots', slots), ('replication', replication)):
            try:
                parts[key] = fn()
            except Exception as e:
                parts[key] = {'ok': False, 'level': 'unknown', 'error': str(e)}
        items = []
        w = parts['wraparound']
        if w.get('level') in ('warn', 'bad'):
            dl = f", до остановки записи ~{w['days_left']} дн." if w.get('days_left') is not None else ''
            items.append({'level': w['level'], 'area': 'wraparound',
                          'text': f"Возраст XID в базе {w.get('worst_db')}: {w.get('max_pct')}% от предела{dl}"})
        for i in parts['backups'].get('issues', []):
            if i['level'] in ('warn', 'bad'):
                items.append({'level': i['level'], 'area': 'backups', 'text': i['text']})
        for nd in parts['slots'].get('nodes', []):
            for s in nd.get('slots', []) or []:
                if s.get('level') in ('warn', 'bad'):
                    items.append({'level': s['level'], 'area': 'slots', 'text': f"Слот {s['slot_name']} на {nd['node']}: {s['why']}"})
        for r in parts['replication'].get('replicas', []):
            if r.get('level') in ('warn', 'bad') and r.get('replay_lag_s') is not None:
                items.append({'level': r['level'], 'area': 'replication',
                              'text': f"Реплика {r['application_name']} отстаёт на {round(r['replay_lag_s'])} с"})
        return {'level': pgx.worst([i['level'] for i in items]) if items else 'ok', 'items': items,
                'ts': time.time(), **parts}
    if force:
        pgx.cache_clear('risk:')
    return pgx.cached('risk:summary', 15, build)


def metrics():
    """Метрики для правил алертов."""
    s = summary()
    out = {}
    w = s.get('wraparound') or {}
    if w.get('ok'):
        out['xid_age_pct'] = w.get('max_pct')
    b = s.get('backups') or {}
    if b.get('last_backup_age_h') is not None:
        out['backup_age_h'] = b['last_backup_age_h']
    arch = (b.get('archiver') or {}).get('stat') or {}
    lf, la = _parse_ts(arch.get('last_failed_time')), _parse_ts(arch.get('last_archived_time'))
    out['archive_failing'] = 1 if lf and (not la or lf > la) else 0
    out['slots_at_risk'] = sum(1 for nd in (s.get('slots') or {}).get('nodes', []) for x in nd.get('slots', []) or []
                               if x.get('level') in ('warn', 'bad'))
    rep = s.get('replication') or {}
    if rep.get('ok'):
        out['replay_lag_s'] = rep.get('max_replay_lag_s') or 0
    return out


@bp.route('/api/risk/summary')
def api_summary():
    return jsonify({'ok': True, 'data': summary(force=request.args.get('force') == '1')})


@bp.route('/api/risk/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify({'ok': True, 'data': cfg()})
    b = request.json or {}
    cur = cfg()
    for k, v in b.items():
        if k in DEFAULTS:
            try:
                cur[k] = type(DEFAULTS[k])(v)
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': f'некорректное значение {k}'})
    with open(CFG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cur, f, indent=1)
    pgx.cache_clear('risk:')
    return jsonify({'ok': True, 'data': cur})
