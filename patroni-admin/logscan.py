#!/usr/bin/env python3
"""Разбор журналов PostgreSQL (в духе pgBadger) через агента.

Агент читает хвост текущего лога (pg_current_logfile) и отдаёт текст; разбор — здесь:
ошибки по типам, deadlock'и, контрольные точки, временные файлы, ожидания
блокировок, автовакуум, медленные запросы, неудачные входы. Плюс проверка
настроек логирования: без log_lock_waits/log_temp_files/... часть разделов пуста."""
import re

from flask import Blueprint, jsonify, request

import pgx

bp = Blueprint('logscan', __name__)

_SEV = re.compile(r'\b(LOG|ERROR|FATAL|PANIC|WARNING|NOTICE|DETAIL|STATEMENT|HINT|CONTEXT|QUERY|INFO|DEBUG\d?):\s{1,2}(.*)$')
_TS = re.compile(r'(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)')
_NORM = [(re.compile(r"'(?:[^']|'')*'"), "'?'"), (re.compile(r'"[^"]*"'), '"?"'),
         (re.compile(r'\b\d+(\.\d+)?\b'), '?'), (re.compile(r'\s+'), ' ')]

LOG_SETTINGS = {
    'log_lock_waits': ('on', 'ожидания блокировок дольше deadlock_timeout'),
    'log_temp_files': ('0', 'временные файлы (сортировки/хеши не влезли в work_mem)'),
    'log_autovacuum_min_duration': ('10s', 'долгие автовакуумы'),
    'log_checkpoints': ('on', 'контрольные точки и их длительность'),
    'log_min_duration_statement': ('1s', 'медленные запросы'),
    'log_connections': ('on', 'подключения (аудит)'),
    'log_disconnections': ('on', 'длительность сессий'),
    'logging_collector': ('on', 'запись логов в файлы (иначе агенту нечего читать)'),
}


def normalize(msg):
    for rx, rep in _NORM:
        msg = rx.sub(rep, msg)
    return msg.strip()[:300]


def parse(text):
    """Разбирает текст журнала. Формат префикса строки не важен: ищем «SEVERITY:  сообщение»."""
    errors, slow, temp, locks, autovac, auth = {}, {}, [], [], [], {}
    ck = {'time': 0, 'wal': 0, 'other': 0, 'durations': [], 'max_s': 0.0}
    deadlocks = 0; lines = 0; first = last = None
    by_sev = {}
    pending_temp = None
    for raw in text.splitlines():
        lines += 1
        m = _SEV.search(raw)
        if not m:
            continue
        ts = _TS.search(raw)
        if ts:
            first = first or ts.group(1)
            last = ts.group(1)
        sev, msg = m.group(1), m.group(2)
        if sev == 'STATEMENT':
            if pending_temp is not None:
                pending_temp['statement'] = msg[:500]
                pending_temp = None
            continue
        if sev in ('DETAIL', 'HINT', 'CONTEXT', 'QUERY'):
            continue
        by_sev[sev] = by_sev.get(sev, 0) + 1
        if sev in ('ERROR', 'FATAL', 'PANIC', 'WARNING'):
            if 'deadlock detected' in msg:
                deadlocks += 1
            am = re.search(r'(?:password|ldap|md5|scram-sha-256|ident|peer|cert) authentication failed for user "([^"]*)"', msg) \
                or re.search(r'no pg_hba.conf entry for host "([^"]*)"', msg)
            if am:
                auth[am.group(1)] = auth.get(am.group(1), 0) + 1
            key = (sev, normalize(msg))
            e = errors.setdefault(key, {'severity': sev, 'message': key[1], 'count': 0, 'example': msg[:500], 'last': None})
            e['count'] += 1
            e['last'] = ts.group(1) if ts else e['last']
            continue
        if msg.startswith('checkpoint starting:'):
            reason = msg.split(':', 1)[1]
            if 'time' in reason:
                ck['time'] += 1
            elif 'wal' in reason or 'xlog' in reason:
                ck['wal'] += 1
            else:
                ck['other'] += 1
            continue
        cm = re.search(r'checkpoint complete:.*total=([\d.]+) s', msg)
        if cm:
            d = float(cm.group(1)); ck['durations'].append(d); ck['max_s'] = max(ck['max_s'], d)
            continue
        tm = re.search(r'temporary file: path "([^"]+)", size (\d+)', msg)
        if tm:
            pending_temp = {'size': int(tm.group(2)), 'time': ts.group(1) if ts else None, 'statement': None}
            temp.append(pending_temp)
            continue
        lm = re.search(r'process (\d+) (still waiting|acquired) for (\w+) on (.+?) after ([\d.]+) ms', msg)
        if lm:
            locks.append({'pid': int(lm.group(1)), 'state': lm.group(2), 'mode': lm.group(3), 'object': lm.group(4)[:200],
                          'ms': float(lm.group(5)), 'time': ts.group(1) if ts else None})
            continue
        vm = re.search(r'automatic (vacuum|analyze) of table "([^"]+)".*?elapsed: ([\d.]+) s', msg, re.S)
        if vm:
            autovac.append({'kind': vm.group(1), 'table': vm.group(2), 'elapsed_s': float(vm.group(3)),
                            'time': ts.group(1) if ts else None})
            continue
        dm = re.search(r'duration: ([\d.]+) ms\s+(?:statement|execute [^:]*|parse [^:]*|bind [^:]*):\s*(.*)', msg)
        if dm:
            q = normalize(dm.group(2))
            s = slow.setdefault(q, {'query': q, 'count': 0, 'total_ms': 0.0, 'max_ms': 0.0, 'example': dm.group(2)[:500]})
            ms = float(dm.group(1)); s['count'] += 1; s['total_ms'] += ms; s['max_ms'] = max(s['max_ms'], ms)
    durs = ck.pop('durations')
    ck['count'] = ck['time'] + ck['wal'] + ck['other']
    ck['avg_s'] = round(sum(durs) / len(durs), 1) if durs else None
    for s in slow.values():
        s['avg_ms'] = round(s['total_ms'] / s['count'], 1); s['total_ms'] = round(s['total_ms'], 1)
    temp_total = sum(t['size'] for t in temp)
    tstmt = {}
    for t in temp:
        k = normalize(t['statement'] or '(запрос не залогирован)')
        x = tstmt.setdefault(k, {'query': k, 'count': 0, 'bytes': 0, 'max': 0})
        x['count'] += 1; x['bytes'] += t['size']; x['max'] = max(x['max'], t['size'])
    return {
        'lines': lines, 'first': first, 'last': last, 'by_severity': by_sev, 'deadlocks': deadlocks,
        'errors': sorted(errors.values(), key=lambda e: -e['count'])[:30],
        'checkpoints': ck,
        'temp_files': {'count': len(temp), 'bytes': temp_total,
                       'top': sorted(tstmt.values(), key=lambda x: -x['bytes'])[:15]},
        'lock_waits': sorted(locks, key=lambda x: -x['ms'])[:30],
        'autovacuum': sorted(autovac, key=lambda x: -x['elapsed_s'])[:20],
        'slow_queries': sorted(slow.values(), key=lambda x: -x['total_ms'])[:20],
        'auth_failures': sorted(({'user': k, 'count': v} for k, v in auth.items()), key=lambda x: -x['count'])[:20],
    }


def settings_check(node_name=None):
    node = pgx.node_by_name(node_name) if node_name else pgx.leader()
    c = pgx.conn(node)
    if not c:
        return {'ok': False, 'error': 'нет подключения'}
    try:
        out = []
        for name, (rec, why) in LOG_SETTINGS.items():
            cur = pgx.setting(c, name)
            bad = False
            if name == 'log_temp_files':
                bad = cur in (None, '-1')
            elif name in ('log_autovacuum_min_duration', 'log_min_duration_statement'):
                bad = cur in (None, '-1')
            else:
                bad = cur != 'on'
            out.append({'name': name, 'current': cur, 'recommended': rec, 'why': why, 'ok': not bad})
        info = {k: pgx.setting(c, k) for k in ('log_directory', 'log_filename', 'log_line_prefix', 'log_destination')}
        return {'ok': True, 'node': node['name'], 'settings': out, 'info': info}
    finally:
        c.close()


@bp.route('/api/logs/settings')
def api_settings():
    return jsonify(settings_check(request.args.get('node')))


@bp.route('/api/logs/scan', methods=['POST'])
def api_scan():
    A = pgx.app()
    b = request.json or {}
    node = b.get('node') or (pgx.leader() or {}).get('name')
    try:
        mb = max(1, min(int(b.get('max_mb', 8)), 64))
    except (TypeError, ValueError):
        mb = 8
    if A.agent_api is None or not A.agent_api.agent_online(node):
        return jsonify({'ok': False, 'error': f'агент на {node} не на связи — журнал читает агент'})
    cid = A.agent_api.enqueue_agent(node, 'log_tail', max_mb=mb)
    return jsonify({'ok': True, 'data': {'cmd_id': cid, 'node': node}})


@bp.route('/api/logs/result')
def api_result():
    """Результат команды log_tail, уже разобранный."""
    A = pgx.app()
    node = request.args.get('node') or ''
    cid = request.args.get('id') or ''
    if A.agent_api is None or not cid.startswith(node + '-'):
        return jsonify({'ok': False, 'error': 'некорректный запрос'}), 400
    with A.agent_api._LOCK:
        res = ((A.agent_api.AGENTS.get(node) or {}).get('results') or {}).get(cid)
    if res is None:
        return jsonify({'ok': True, 'data': None})
    if not res.get('ok'):
        return jsonify({'ok': False, 'error': str(res.get('out'))[:2000]})
    out = res.get('out') or {}
    d = out.get('data') if isinstance(out, dict) else None
    if not d:
        return jsonify({'ok': False, 'error': 'агент вернул пустой ответ'})
    report = parse(d.get('text') or '')
    report.update({'file': d.get('file'), 'file_size': d.get('size'), 'read_bytes': len(d.get('text') or ''),
                   'node': node})
    return jsonify({'ok': True, 'data': report})
