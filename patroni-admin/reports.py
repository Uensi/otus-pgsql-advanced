#!/usr/bin/env python3
"""PWR-аналог: периодические снимки статистики + отчёты за период."""
import os, json, time, math, threading
from collections import deque
from flask import Blueprint, jsonify, request

bp = Blueprint('reports', __name__)
from paths import data_path
SNAP_PATH = data_path('reports_snapshots.jsonl')
SNAP_INTERVAL = 60            # сек
SNAPS = deque(maxlen=1440)    # ~24 часа
SNAP_FILE_MAX_BYTES = 20 * 1024 * 1024   # ротация файла при 20 МБ

def _app():
    import app as A; return A

def _load():
    try:
        with open(SNAP_PATH, encoding='utf-8', errors='replace') as f:
            for line in f:
                try: SNAPS.append(json.loads(line))
                except Exception: pass
    except Exception: pass

def _persist(rec):
    try:
        if os.path.exists(SNAP_PATH) and os.path.getsize(SNAP_PATH) > SNAP_FILE_MAX_BYTES:
            _rotate()
        with open(SNAP_PATH, 'a', encoding='utf-8') as f: f.write(json.dumps(rec) + '\n')
    except Exception: pass

def _rotate():
    """Файл снимков перерос лимит: оставляем половину последних записей (перепишем файл)."""
    try:
        tail = list(SNAPS)[-max(1, len(SNAPS) // 2):]
        tmp = SNAP_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            for rec in tail: f.write(json.dumps(rec) + '\n')
        os.replace(tmp, SNAP_PATH)
    except Exception: pass

_load()

def take_snapshot():
    A = _app()
    ln = A.leader_node()
    if not ln: return None
    c = A.pg_conn(ln['host'])
    if not c: return None
    try:
        cnt = A.q(c, """
            SELECT sum(xact_commit)::bigint AS commit, sum(xact_rollback)::bigint AS rollback,
                   sum(blks_hit)::bigint AS hit, sum(blks_read)::bigint AS read,
                   sum(tup_returned)::bigint AS ret, sum(tup_fetched)::bigint AS fetch,
                   sum(tup_inserted)::bigint AS ins, sum(tup_updated)::bigint AS upd, sum(tup_deleted)::bigint AS del,
                   sum(temp_files)::bigint AS temp_files, sum(temp_bytes)::bigint AS temp_bytes,
                   sum(deadlocks)::bigint AS deadlocks
            FROM pg_stat_database WHERE datname NOT LIKE 'template%'""")[0]
        dbs = A.q(c, "SELECT datname AS name, pg_database_size(datname) AS size FROM pg_database WHERE datistemplate=false ORDER BY 2 DESC")
        conn = A.q(c, "SELECT count(*) AS c FROM pg_stat_activity")[0]['c']
        top = []
        try:
            top = A.q(c, "SELECT left(query,120) AS query, calls, round(total_exec_time::numeric,1) AS total, round(mean_exec_time::numeric,2) AS mean FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10")
        except Exception: top = []
        rec = {'ts': time.time(), 'counters': {k: int(v or 0) for k, v in cnt.items()},
               'dbs': dbs, 'conn': int(conn), 'top': top}
        SNAPS.append(rec); _persist(rec)
        return rec
    except Exception:
        return None
    finally: c.close()

def _loop():
    while True:
        try: take_snapshot()
        except Exception: pass
        time.sleep(SNAP_INTERVAL)

if not getattr(_loop, '_started', False):
    _loop._started = True
    threading.Thread(target=_loop, daemon=True, name='reports-snapshot').start()

@bp.route('/api/reports/snapshots')
def snap_list():
    return jsonify({'ok': True, 'data': [{'ts': s['ts'], 'conn': s.get('conn')} for s in SNAPS]})

@bp.route('/api/reports/snapshot', methods=['POST'])
def snap_now():
    r = take_snapshot()
    return jsonify({'ok': bool(r), 'data': {'ts': r['ts'] if r else None}})

@bp.route('/api/reports/generate')
def generate():
    try:
        frm = float(request.args.get('from', 0)); to = float(request.args.get('to', time.time()))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'некорректный диапазон дат'})
    if not (math.isfinite(frm) and math.isfinite(to)):
        return jsonify({'ok': False, 'error': 'некорректный диапазон дат'})
    inr = [s for s in SNAPS if frm <= s['ts'] <= to]
    if len(inr) < 2: return jsonify({'ok': False, 'error': 'недостаточно снимков в диапазоне'})
    a, b = inr[0], inr[-1]; dt = max(1.0, b['ts'] - a['ts'])
    ca, cb = a['counters'], b['counters']
    def d(k): return (cb.get(k,0) - ca.get(k,0))
    tps = (d('commit') + d('rollback')) / dt
    hit_ratio = round(100.0 * d('hit') / max(1, d('hit') + d('read')), 2)
    growth = []
    amap = {x['name']: x['size'] for x in a.get('dbs', [])}
    for x in b.get('dbs', []):
        prev = amap.get(x['name'])
        if prev is not None: growth.append({'name': x['name'], 'delta': x['size'] - prev, 'now': x['size']})
    return jsonify({'ok': True, 'data': {
        'from': a['ts'], 'to': b['ts'], 'seconds': dt, 'snapshots': len(inr),
        'tps': round(tps,1), 'rollback_ratio': round(100.0*d('rollback')/max(1,d('commit')+d('rollback')),2),
        'cache_hit': hit_ratio,
        'rates': {k: round(d(k)/dt,1) for k in ('ins','upd','del','ret')},
        'temp_files': d('temp_files'), 'temp_bytes': d('temp_bytes'), 'deadlocks': d('deadlocks'),
        'conn_start': a.get('conn'), 'conn_end': b.get('conn'),
        'db_growth': sorted(growth, key=lambda x: -x['delta']),
        'top_queries': b.get('top', [])
    }})

