#!/usr/bin/env python3
"""Демо-режим: консоль работает без реальных кластеров.

Включается переменной окружения PATRONI_CONSOLE_DEMO=1. Отвечает правдоподобными,
меняющимися во времени данными на основные экраны (кластер, лаг, метрики, хосты,
базы, DBA, DCS, слоты, центр). Любые изменяющие запросы (POST) ничего не делают
и возвращают ok с пометкой «демо» — показывать консоль клиенту безопасно.

Нужен для презентаций, обучения и проверки интерфейса без доступа к Patroni."""
import math
import os
import random
import time
from collections import deque

from flask import jsonify, request, session

import demo_dba
import demo_more

ENABLED = bool(os.environ.get('PATRONI_CONSOLE_DEMO'))

CLUSTER = 'demo-cluster'
NODES = [
    {'name': 'pg-node1', 'host': '10.0.10.11', 'port': 8008},
    {'name': 'pg-node2', 'host': '10.0.10.12', 'port': 8008},
    {'name': 'pg-node3', 'host': '10.0.10.13', 'port': 8008},
]
LEADER = 'pg-node1'
DBS = ['postgres', 'billing', 'erp_1c', 'reports']
T0 = time.time()
_LAG_HIST = deque(maxlen=200)
_PERF = deque(maxlen=200)
_METRICS = {n['name']: deque(maxlen=1200) for n in NODES}
_last_tick = [0.0]
_xact = {n['name']: 1_000_000 + i * 50_000 for i, n in enumerate(NODES)}


def _wave(period, phase=0.0):
    return (math.sin((time.time() - T0) / period * 2 * math.pi + phase) + 1) / 2


def _lag(name):
    if name == LEADER:
        return 0
    if name == 'pg-node2':
        return 0 if _wave(90) < 0.55 else int(_wave(17) * 3 * 1024 * 1024)
    # третья реплика периодически «тормозит» — видно предупреждение в сводке
    return int(_wave(240, 1.3) ** 4 * 60 * 1024 * 1024)


def _tick():
    now = time.time()
    if now - _last_tick[0] < 2.9:
        return
    _last_tick[0] = now
    _LAG_HIST.append({'ts': now, 'nodes': {n['name']: {'lag': _lag(n['name']), 'role': 'leader' if n['name'] == LEADER else 'replica',
                                                        'state': 'running' if n['name'] == LEADER else 'streaming'} for n in NODES}})
    snap = {'ts': now, 'nodes': {}}
    for n in NODES:
        nm = n['name']
        tps = (420 if nm == LEADER else 140) * (0.6 + 0.8 * _wave(120, hash(nm) % 7)) + random.uniform(-15, 15)
        _xact[nm] += int(tps * 3)
        chr_ = round(99.2 - 1.8 * _wave(300, 0.5) + random.uniform(-0.1, 0.1), 2)
        snap['nodes'][nm] = {'total_xact': _xact[nm], 'cache_hit_ratio': chr_}
        _METRICS[nm].append({'ts': now, 'metrics': {
            'rates': {'tps': round(tps, 1), 'cache_hit_ratio': chr_,
                      'tup_inserted_per_s': round(tps * 0.8, 1), 'tup_updated_per_s': round(tps * 0.5, 1),
                      'tup_deleted_per_s': round(tps * 0.05, 1)},
            'conn': {'active': int(8 + 10 * _wave(60)), 'idle': 42, 'idle_in_tx': 1 if _wave(45) > 0.8 else 0},
            'locks_waiting': 1 if _wave(33) > 0.85 else 0, 'long_queries': 1 if _wave(70) > 0.7 else 0,
            'db': {'deadlocks': 2, 'temp_files': 14 + int((now - T0) / 600)},
            'dbs': DBS}})
    _PERF.append(snap)


def _members():
    out = []
    for i, n in enumerate(NODES):
        isl = n['name'] == LEADER
        out.append({'name': n['name'], 'host': n['host'], 'port': n['port'], 'role': 'leader' if isl else 'replica',
                    'state': 'running' if isl else 'streaming', 'timeline': 7, 'lag': _lag(n['name'])})
    return out


def _lsn():
    base = int(0x5A000000 + (time.time() - T0) * 120_000)
    out = {}
    for n in NODES:
        nm = n['name']; lag = _lag(nm)
        hx = lambda v: '%X/%08X' % (v >> 32, v & 0xFFFFFFFF)
        out[nm] = {'role': 'leader' if nm == LEADER else 'replica', 'state': 'running' if nm == LEADER else 'streaming',
                   'location': base if nm == LEADER else None, 'received': hx(base - lag), 'replayed': hx(base - lag - (4096 if lag else 0)),
                   'timeline': 7, 'lag': lag, 'pending_restart': nm == 'pg-node3', 'pending_restart_reason': 'shared_buffers: 8GB → 12GB',
                   'delay': '0'}
    return out


def _agents():
    now = time.time()
    return {n['name']: {'online': True, 'last_seen': now - 2, 'delay': '0', 'last': {'ok': True, 'out': 'backup-push завершён'},
                        'backups': [{'backup_name': 'base_000000070000005A000000%02X' % i, 'is_full': i % 3 == 0,
                                     'start_time': time.strftime('%Y-%m-%dT02:00:00', time.localtime(now - 86400 * (5 - i))),
                                     'end_lsn': '5A/%08X' % (i * 0x100000)} for i in range(5)],
                        'metrics': None} for n in NODES}


def _hosts():
    out = []
    for i, n in enumerate(NODES):
        used = [61, 58, 83][i]
        out.append({'kind': 'agent', 'name': n['name'], 'online': True, 'cpus': 16, 'loadavg': [round(2 + _wave(50, i), 2), 2.1, 1.9],
                    'cpu_used_pct': round(18 + 30 * _wave(80, i), 1), 'cpu_iowait_pct': round(1.5 + 2 * _wave(40, i), 1),
                    'mem': {'total': 64 * 2**30, 'available': 21 * 2**30, 'swap_used': 0}, 'uptime_s': 86400 * 41 + 3600 * i,
                    'disks': [{'path': '/var/lib/pgsql', 'used_pct': used, 'total': 2 * 2**40, 'free': int(2 * 2**40 * (100 - used) / 100),
                               'forecast': {'hours': [900, 1100, 70][i], 'slope_mb_h': [180, 150, 820][i]}},
                              {'path': '/', 'used_pct': 34, 'total': 100 * 2**30, 'free': 66 * 2**30, 'forecast': None}],
                    'services': {'postgresql': 'active', 'patroni': 'active', 'etcd': 'active'},
                    'pg_version': 'PostgreSQL 16.4', 'kernel_warnings': ['vm.swappiness=60 — рекомендуется 1–10'] if i == 2 else []})
    return out


def _risk():
    return demo_dba.risk_summary(_lag, [n['name'] for n in NODES], LEADER)


def weekly_data(days=7):
    return demo_dba.weekly(days, CLUSTER, _members(), LEADER, _risk())


_DEMO_CMDS = {}


def _demo_post(path):
    """Изменяющие запросы в демо ничего не делают; длительные операции имитируются."""
    b = request.get_json(silent=True) or {}
    node = b.get('node') or LEADER
    if path == '/api/logs/scan':
        cid = f'{node}-{int(time.time() * 1000)}'
        _DEMO_CMDS[cid] = {'kind': 'log', 'node': node, 'ready': time.time() + 2}
        return {'ok': True, 'data': {'cmd_id': cid, 'node': node}}
    if path == '/api/risk/restore-test':
        node = 'pg-node3'
        cid = f'{node}-{int(time.time() * 1000)}'
        _DEMO_CMDS[cid] = {'kind': 'restore', 'node': node, 'ready': time.time() + 6}
        return {'ok': True, 'data': {'cmd_id': cid, 'node': node}}
    if path == '/api/dumps' and b.get('action') == 'list':
        cid = f'{node}-{int(time.time() * 1000)}'
        _DEMO_CMDS[cid] = {'kind': 'dumps', 'node': node, 'ready': time.time()}
        return {'ok': True, 'data': {'cmd_id': cid}}
    if path == '/api/assistant/diagnose':
        r = _risk()
        issues = [{'sev': 'warning', 'what': i['text'], 'detail': ''} for i in r['items']]
        if _lag('pg-node3') > 10 * 2**20:
            issues.insert(0, {'sev': 'warning', 'what': 'pg-node3 отстаёт от лидера', 'detail': 'проверьте нагрузку на диск реплики и сеть'})
        return {'ok': True, 'data': {'summary': f'Найдено проблем: {len(issues)} (критичных нет)', 'issues': issues}}
    if path == '/api/assistant/chat':
        return {'ok': True, 'data': demo_more.ai_reply(b.get('message') or '')}
    if path == '/api/assistant/config-advice':
        return {'ok': True, 'data': {'tips': demo_more.AI_TIPS}}
    if path == '/api/analyze-query':
        return demo_more.analyze(b.get('query') or '')
    if path == '/api/querylab/exec':
        return demo_more.exec_sql(b.get('sql'))
    if path == '/api/risk/freeze':
        return {'ok': True, 'data': {'job': 'demo', 'message': f"демо: VACUUM FREEZE {b.get('schema')}.{b.get('table')} не выполняется"}}
    return {'ok': True, 'data': {'message': 'демо-режим: действие не выполняется', 'summary': 'демо-режим: действие не выполняется'}}


def _demo_cmd_result():
    cid = request.args.get('id', '')
    c = _DEMO_CMDS.get(cid)
    if not c or time.time() < c['ready']:
        return {'ok': True, 'data': None}
    if c['kind'] == 'dumps':
        now = time.time()
        return {'ok': True, 'data': {'ok': True, 'out': {'data': [
            {'name': f'billing_{time.strftime("%Y%m%d", time.localtime(now - d * 86400))}_030000.dump', 'size': int((9.4 - d * 0.1) * 2**30),
             'mtime': now - d * 86400, 'is_dir': False} for d in range(5)] + [
            {'name': 'erp_1c_20261016_031500.d', 'size': 61 * 2**30, 'mtime': now - 86400, 'is_dir': True}]}}}
    if c['kind'] == 'restore':
        return {'ok': True, 'data': {'ok': True, 'out': {'data': {
            'backup': 'base_000000070000005A00000004', 'duration_s': 2290.4, 'fetch_s': 1874.2, 'recovery_s': 401.7, 'size': 538 * 2**30,
            'consistent_at': time.strftime('%Y-%m-%d %H:%M:%S+03', time.localtime(time.time() - 6.2 * 3600)),
            'checked': [{'db': 'erp_1c', 'tables': 8120, 'ok': True}, {'db': 'billing', 'tables': 214, 'ok': True}], 'ok': True}}}}
    return {'ok': True, 'data': {'ok': True, 'out': {'data': {'text': ''}}}}


def _routes():
    _tick()
    return {
        '/api/risk/summary': lambda: {'ok': True, 'data': _risk()},
        '/api/schema': lambda: demo_more.schema(request.args.get('db') or 'billing'),
        '/api/querylab/slow': demo_more.slow,
        '/api/querylab/current': demo_more.current,
        '/api/querylab/show': lambda: {'ok': True, 'data': {'node': request.args.get('node'), 'param': request.args.get('param', '').upper(),
                                                             'value': 'on' if request.args.get('param', '').lower() == 'archive_mode' else '0'}},
        '/api/dba/connections': lambda: demo_more.connections([n['name'] for n in NODES]),
        '/api/dba/config-recs': demo_more.config_recs,
        '/api/templates': demo_more.templates,
        '/api/admin/extensions': demo_more.admin_extensions,
        '/api/admin/roles': demo_more.admin_roles,
        '/api/admin/tablespaces': demo_more.admin_ts,
        '/api/admin/nodes': lambda: {'ok': True, 'data': [n['name'] for n in NODES]},
        '/api/admin/config-diff': demo_more.config_diff,
        '/api/admin/patroni-diff': demo_more.patroni_diff,
        '/api/reports/snapshots': demo_more.report_snapshots,
        '/api/reports/generate': lambda: demo_more.report_generate(float(request.args.get('from') or time.time() - 86400), float(request.args.get('to') or time.time())),
        '/api/ops/history': demo_more.ops_history,
        '/api/dcs/patroni-config': demo_more.dcs_config,
        '/api/risk/jobs': lambda: {'ok': True, 'data': []},
        '/api/risk/config': lambda: {'ok': True, 'data': __import__('risk').DEFAULTS},
        '/api/ash': lambda: {'ok': True, 'data': demo_dba.ash(int(request.args.get('minutes', 15)))},
        '/api/statements/report': lambda: demo_dba.statements(float(request.args.get('hours', 1))),
        '/api/perf/checkpoints': lambda: demo_dba.checkpoints(),
        '/api/progress': lambda: {'ok': True, 'data': demo_dba.progress()},
        '/api/indexes/audit': lambda: demo_dba.index_audit(request.args.get('db') or 'erp_1c'),
        '/api/logs/settings': lambda: demo_dba.log_settings(request.args.get('node') or LEADER),
        '/api/logs/result': lambda: ({'ok': True, 'data': demo_dba.log_report(request.args.get('node') or LEADER)}
                                     if _demo_cmd_result()['data'] is not None else {'ok': True, 'data': None}),
        '/api/ops/cmd-result': _demo_cmd_result,
        '/api/security/audit': lambda: demo_dba.security(),
        '/api/upgrade/check': lambda: demo_dba.upgrade(request.args.get('target')),
        '/api/report/weekly/schedule': lambda: {'ok': True, 'data': {'enabled': True, 'weekday': 0, 'time': '09:00', 'last_sent': None}},
        '/api/cluster': lambda: {'ok': True, 'data': {'members': _members(), 'scope': CLUSTER, 'pause': False}},
        '/api/lsn': lambda: {'ok': True, 'data': _lsn()},
        '/api/lag-history': lambda: {'ok': True, 'data': list(_LAG_HIST)},
        '/api/agent/status': lambda: {'ok': True, 'agents': _agents()},
        '/api/perf/snapshot': lambda: {'ok': True, 'data': {}},
        '/api/perf/tps-history': lambda: {'ok': True, 'data': list(_PERF)},
        '/api/perf/cache-history': lambda: {'ok': True, 'data': list(_PERF)},
        '/api/metrics/nodes': lambda: {'ok': True, 'data': [n['name'] for n in NODES]},
        '/api/metrics/history': lambda: {'ok': True, 'data': list(_METRICS.get(request.args.get('node'), []))[-int(request.args.get('points', 600)):]},
        '/api/alerts/active': lambda: {'ok': True, 'data': [{'rule': 'Лаг реплики'}] if _lag('pg-node3') > 10 * 2**20 else []},
        '/api/center/clusters': lambda: {'ok': True, 'data': [{'name': CLUSTER, 'active': True}, {'name': 'demo-reporting', 'active': False}]},
        '/api/center': lambda: {'ok': True, 'data': [
            {'name': CLUSTER, 'active': True, 'up': True, 'nodes': 3, 'leader': LEADER, 'max_lag': max(_lag(n['name']) for n in NODES),
             'agents_online': 3, 'members': _members()},
            {'name': 'demo-reporting', 'active': False, 'up': True, 'nodes': 2, 'leader': 'rep-node1', 'max_lag': 0, 'agents_online': 2,
             'members': [{'name': 'rep-node1', 'role': 'leader', 'state': 'running'}, {'name': 'rep-node2', 'role': 'replica', 'state': 'streaming'}]}]},
        '/api/hosts': lambda: {'ok': True, 'data': _hosts()},
        '/api/databases': lambda: {'ok': True, 'data': DBS},
        '/api/db/overview': lambda: {'ok': True, 'data': [
            {'datname': 'erp_1c', 'size': 412 * 2**30, 'connections': 61, 'cache_hit': 98.7, 'xact_commit': 91_204_331, 'xact_rollback': 12_044, 'growth_rate': 210 * 2**20, 'encoding': 'UTF8'},
            {'datname': 'billing', 'size': 96 * 2**30, 'connections': 24, 'cache_hit': 99.4, 'xact_commit': 40_111_902, 'xact_rollback': 2_311, 'growth_rate': 35 * 2**20, 'encoding': 'UTF8'},
            {'datname': 'reports', 'size': 31 * 2**30, 'connections': 6, 'cache_hit': 86.2, 'xact_commit': 1_204_500, 'xact_rollback': 90, 'growth_rate': 0, 'encoding': 'UTF8'},
            {'datname': 'postgres', 'size': 9 * 2**20, 'connections': 3, 'cache_hit': 99.9, 'xact_commit': 902_331, 'xact_rollback': 4, 'growth_rate': None, 'encoding': 'UTF8'}]},
        '/api/dba/summary': lambda: {'ok': True, 'data': {
            'alerts': [{'type': 'warning', 'msg': 'erp_1c: таблица _accumrg1234 — 38% мёртвых строк, нужен VACUUM'}],
            'idle': [{}] if _wave(45) > 0.8 else [], 'long': [], 'bloat': [{}, {}], 'unused': [
                {'schemaname': 'public', 't': '_document312', 'ix': '_document312_old_number', 'sz': 7 * 2**30},
                {'schemaname': 'public', 't': 'payments', 'ix': 'payments_account_idx', 'sz': int(1.8 * 2**30)},
                {'schemaname': 'public', 't': 'audit_log', 'ix': 'audit_log_actor_idx', 'sz': 3 * 2**30}], 'vacuum': [
                {'schemaname': 'public', 'relname': '_accumrg1234', 'n_dead_tup': 1_830_220, 'last_autovacuum': '2026-10-16 22:14:05'}],
            'deadlocks': [{'datname': 'erp_1c', 'deadlocks': 2, 'stats_reset': '2026-09-01 00:00:00'}]}},
        '/api/dba/bloat': lambda: {'ok': True, 'data': {'tables': [
            {'schemaname': 'public', 't': '_accumrg1234', 'n_dead_tup': 1_830_220, 'n_live_tup': 2_990_000, 'pct': 38, 'sz': 18 * 2**30},
            {'schemaname': 'public', 't': '_inforg5521', 'n_dead_tup': 420_000, 'n_live_tup': 1_600_000, 'pct': 21, 'sz': 4 * 2**30}]}},
        '/api/dba/locks': lambda: {'ok': True, 'data': [], 'meta': {'checked': 3, 'total': 3}},
        '/api/dcs/config': lambda: {'ok': True, 'data': {'etcd': ['http://10.0.10.21:2379', 'http://10.0.10.22:2379', 'http://10.0.10.23:2379'], 'consul': []}},
        '/api/dcs/health': lambda: {'ok': True, 'data': {'configured': True,
            'etcd': [{'url': 'http://10.0.10.21:2379', 'ok': True, 'leader': 2, 'members': [{'name': 'etcd1', 'id': 1}, {'name': 'etcd2', 'id': 2}, {'name': 'etcd3', 'id': 3}]}],
            'consul': [], 'sync': {'ok': True, 'synchronous_standby_names': '',
                                   'replicas': [{'app': n['name'], 'state': 'streaming', 'sync_state': 'async', 'lag_bytes': _lag(n['name'])} for n in NODES if n['name'] != LEADER]}}},
        '/api/repl/slots': lambda: {'ok': True, 'data': [{'node': LEADER, 'ok': True,
            'slots': [{'slot_name': n['name'].replace('-', '_'), 'slot_type': 'physical', 'active': True, 'lag_bytes': _lag(n['name']), 'restart_lsn': '5A/1F000000'} for n in NODES if n['name'] != LEADER],
            'repl': [{'client': n['host'], 'application_name': n['name'], 'state': 'streaming', 'sent_lag': 0, 'replay_lag': _lag(n['name'])} for n in NODES if n['name'] != LEADER]}]},
    }


def init_app(app):
    if not ENABLED:
        return

    @app.before_request
    def _demo():
        path = request.path
        if not path.startswith('/api/') or path.startswith('/api/auth/'):
            return None
        if not session.get('user') and path not in ('/api/agent/register', '/api/agent/poll', '/api/agent/result'):
            return None   # пусть отработает обычная проверка авторизации
        if request.method != 'GET':
            return jsonify(_demo_post(path))
        fn = _routes().get(path)
        if fn:
            return jsonify(fn())
        return None
