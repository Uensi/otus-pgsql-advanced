#!/usr/bin/env python3
"""Реестр хостов и standalone-инстансы (разд. 4, 6 спеки).

Источники данных:
  * агенты (агент шлёт ОС-метрики: load, CPU, RAM, диски, kernel, службы);
  * секция `standalone:` в config.yaml (необязательна): одиночные инстансы
    без Patroni — порт, PGDATA, хост.
"""
import os
import time
from collections import deque
from flask import Blueprint, jsonify, request

bp = Blueprint('hosts', __name__)

# диск: точка монтирования -> история (ts, used) для прогноза
DISK_HISTORY = {}
HIST_MAX = 2880   # ~24 ч при снятии раз в 30 с

def _app():
    import app as A
    return A


def _agents():
    A = _app()
    if A.agent_api is None:
        return {}
    with A.agent_api._LOCK:
        return {n: dict(a) for n, a in A.agent_api.AGENTS.items()}


def _config():
    A = _app()
    return A.CONFIG


def _standalone_list():
    """Одиночные инстансы из config (опциональная секция `standalone:`)."""
    out = []
    for s in (_config().get('standalone') or []):
        out.append({
            'name': s.get('name') or s.get('host'),
            'host': s.get('host'),
            'port': s.get('port', 5432),
            'user': s.get('user') or _config().get('postgres', {}).get('user', 'postgres'),
            'database': s.get('database') or _config().get('postgres', {}).get('database', 'postgres'),
        })
    return out


def _probe_standalone(s):
    """Проверка одиночного инстанса: порт + базовая инфа из pg_stat_database."""
    A = _app()
    res = {'name': s['name'], 'host': s['host'], 'port': s['port'],
           'up': False, 'databases': [], 'error': None}
    c = A.pg_conn(s['host'], database=s['database'], port=s['port'], user=s['user'])
    if not c:
        res['error'] = 'нет подключения'
        return res
    try:
        res['up'] = True
        v = A.q(c, "SELECT current_setting('server_version') AS v")
        res['version'] = v[0]['v'] if v else None
        res['databases'] = A.q(c, """
            SELECT d.datname AS name, pg_database_size(d.datname) AS size,
                   (SELECT count(*) FROM pg_stat_activity s WHERE s.datname=d.datname) AS conn
            FROM pg_database d WHERE d.datistemplate=false ORDER BY 2 DESC""")
    except Exception as e:
        res['error'] = str(e)
    finally:
        c.close()
    return res


def _disk_forecast(history):
    """Линейный прогноз: за сколько часов диск заполнится при текущем темпе роста."""
    pts = [h for h in history if h.get('used') is not None]
    if len(pts) < 3:
        return None
    n = len(pts)
    ts0 = pts[0]['ts']; dt = pts[-1]['ts'] - ts0
    if dt <= 0:
        return None
    # простая линейная регрессия used ~ t
    mx = dt / 2.0
    my = sum(p['used'] for p in pts) / n
    num = sum((p['ts'] - ts0 - mx) * (p['used'] - my) for p in pts)
    den = sum((p['ts'] - ts0 - mx) ** 2 for p in pts)
    if den == 0:
        return None
    slope = num / den  # байт/сек
    if slope <= 0:
        return {'hours': None, 'note': 'рост не обнаружен'}
    free = pts[-1].get('free')
    if free is None:
        return None
    secs = free / slope
    return {'hours': round(secs / 3600, 1), 'slope_mb_h': round(slope * 3600 / 1048576, 2)}


def _record_disks(agent_name, os_info):
    """Пишем в историю использование дисков данного хоста."""
    for d in (os_info or {}).get('disks') or []:
        key = f"{agent_name}:{d.get('path')}"
        hist = DISK_HISTORY.setdefault(key, deque(maxlen=HIST_MAX))
        hist.append({'ts': time.time(), 'used': d.get('used'), 'free': d.get('free'),
                     'total': d.get('total'), 'used_pct': d.get('used_pct')})


def _kernel_warnings(kernel, mem_total, pg_version):
    """Эвристики: неоптимальные параметры ядра (разд. 4 спеки)."""
    w = []
    if kernel.get('vm.swappiness') not in (None, '1', '10'):
        w.append(f"vm.swappiness={kernel.get('vm.swappiness')}: для PostgreSQL обычно 1 или 10")
    if kernel.get('vm.overcommit_memory') not in (None, '2'):
        w.append(f"vm.overcommit_memory={kernel.get('vm.overcommit_memory')}: рекомендуется 2 (строгий учёт)")
    thp = kernel.get('transparent_hugepage') or ''
    if '[always]' in thp:
        w.append('transparent_hugepage=always: для PostgreSQL рекомендуется never/madvise')
    if kernel.get('vm.dirty_bytes') in (None, '0') and kernel.get('vm.dirty_background_bytes') in (None, '0'):
        w.append('vm.dirty_bytes / vm.dirty_background_bytes не заданы (используются проценты)')
    for k, v in kernel.items():
        if k.startswith('io_scheduler_') and v and 'none' in v and '[' not in v:
            pass
    return w


@bp.route('/api/hosts')
def hosts_list():
    """Сводка по всем хостам: агенты (ОС-метрики) + standalone."""
    now = time.time()
    agents = _agents()
    out = []
    for name, ag in agents.items():
        os_info = ag.get('os') or ag.get('host') or {}
        _record_disks(name, os_info)
        disks = []
        for d in os_info.get('disks') or []:
            key = f"{name}:{d.get('path')}"
            hist = DISK_HISTORY.get(key)
            fc = _disk_forecast(list(hist)) if hist else None
            dd = dict(d)
            dd['forecast'] = fc
            disks.append(dd)
        kernel = os_info.get('kernel') or {}
        out.append({
            'name': name,
            'kind': 'agent',
            'online': bool(ag.get('last_seen') and now - ag['last_seen'] < 30),
            'last_seen': ag.get('last_seen'),
            'hostname': os_info.get('hostname'),
            'cpus': os_info.get('cpus'),
            'loadavg': os_info.get('loadavg'),
            'cpu_used_pct': os_info.get('cpu_used_pct'),
            'cpu_iowait_pct': os_info.get('cpu_iowait_pct'),
            'uptime_s': os_info.get('uptime_s'),
            'mem': os_info.get('mem'),
            'disks': disks,
            'kernel': kernel,
            'kernel_warnings': _kernel_warnings(kernel, (os_info.get('mem') or {}).get('total'), os_info.get('pg_version')),
            'services': os_info.get('services'),
            'pg_version': os_info.get('pg_version'),
        })
    for s in _standalone_list():
        out.append({'name': s['name'], 'kind': 'standalone', 'online': None, **_probe_standalone(s)})
    return jsonify({'ok': True, 'data': out})


@bp.route('/api/hosts/standalone')
def standalone_list():
    return jsonify({'ok': True, 'data': [_probe_standalone(s) for s in _standalone_list()]})


@bp.route('/api/hosts/disk-history')
def disk_history():
    key = request.args.get('key')
    if not key:
        return jsonify({'ok': True, 'data': {k: list(v)[-300:] for k, v in DISK_HISTORY.items()}})
    return jsonify({'ok': True, 'data': list(DISK_HISTORY.get(key, []))})


@bp.route('/api/hosts/service', methods=['POST'])
def host_service():
    """Управление службой на хосте через агента (белый список в агенте)."""
    A = _app()
    b = request.json or {}
    node = b.get('node'); service = b.get('service'); action = b.get('action', 'status')
    if A.agent_api is None or not A.agent_api.agent_online(node):
        return jsonify({'ok': False, 'error': 'агент офлайн на этой ноде'})
    cid = A.agent_api.enqueue_agent(node, 'service', service=service, action=action)
    return jsonify({'ok': True, 'data': {'cmd_id': cid}})
