#!/usr/bin/env python3
import time, threading, hmac
from flask import Blueprint, jsonify, request

bp = Blueprint('agent', __name__)
AGENTS = {}
COMMANDS = {}
_LOCK = threading.Lock()

def _get_key():
    try:
        from app import CONFIG
        return CONFIG.get('agents', {}).get('key', '') or ''
    except Exception:
        return ''

def _key_ok(provided):
    return hmac.compare_digest(provided or '', _get_key())

@bp.route('/api/agent/register', methods=['POST'])
def register():
    b = request.json or {}
    if not _key_ok(b.get('key')): return jsonify({'ok': False, 'error': 'bad key'}), 403
    node = b.get('node')
    with _LOCK:
        old = AGENTS.get(node, {})
        AGENTS[node] = {'last_seen': time.time(), 'caps': b.get('caps', []),
                        'pending': old.get('pending', []), 'results': old.get('results', {}),
                        'delay': old.get('delay'), 'delay_ts': old.get('delay_ts', 0),
                        'backups': old.get('backups', []), 'last': old.get('last'),
                        'metrics': old.get('metrics'),
                        'host': b.get('host') or old.get('host')}
    return jsonify({'ok': True})

@bp.route('/api/agent/poll', methods=['POST'])
def poll():
    b = request.json or {}
    if not _key_ok(b.get('key')): return jsonify({'ok': False, 'error': 'bad key'}), 403
    node = b.get('node')
    with _LOCK:
        ag = AGENTS.get(node)
        if not ag: return jsonify({'ok': True, 'commands': []})
        ag['last_seen'] = time.time()
        if b.get('delay') is not None:
            ag['delay'] = b.get('delay'); ag['delay_ts'] = time.time()
        if b.get('backups') is not None:
            ag['backups'] = b.get('backups')
        if b.get('metrics') is not None:
            ag['metrics'] = b.get('metrics')
        if b.get('os') is not None:
            ag['os'] = b.get('os')
        cmds = ag['pending']; ag['pending'] = []
    return jsonify({'ok': True, 'commands': cmds})

@bp.route('/api/agent/result', methods=['POST'])
def result():
    b = request.json or {}
    if not _key_ok(b.get('key')): return jsonify({'ok': False, 'error': 'bad key'}), 403
    node = b.get('node')
    with _LOCK:
        ag = AGENTS.get(node)
        if not ag: return jsonify({'ok': False, 'error': 'нода не зарегистрирована'}), 404
        ag['results'][b.get('cmd_id')] = {'ok': b.get('ok'), 'out': b.get('out'), 'ts': time.time()}
        ag['last'] = {'ok': b.get('ok'), 'out': b.get('out'), 'ts': time.time()}
        ag['last_seen'] = time.time()
    _notify_agent_event(node, b.get('cmd_id') or '', b.get('action') or '',
                        bool(b.get('ok')), b.get('out'))
    if b.get('action') == 'restore_test':
        try:
            import risk
            risk.record_restore_test(node, bool(b.get('ok')), b.get('out'))
        except Exception:
            pass
    return jsonify({'ok': True})


_BACKUP_ACTIONS = {'backup_full', 'backup_delta', 'rotate', 'verify_backup',
                   'dump_create', 'dump_rotate', 'delete_backup', 'restore_test'}

def _notify_agent_event(node, cmd_id, action, ok, out):
    """Уведомление на e-mail о завершении бэкапа/дампов (тихо при ошибках)."""
    if action not in _BACKUP_ACTIONS:
        return
    try:
        import notify
        import time as _t
        ts = _t.strftime('%Y-%m-%d %H:%M:%S')
        name = {'backup_full': 'полный бэкап (wal-g)',
                'backup_delta': 'дельта-бэкап (wal-g)',
                'rotate': 'ротация бэкапов',
                'verify_backup': 'проверка бэкапа (verify)',
                'dump_create': 'логический дамп (pg_dump)',
                'dump_rotate': 'ротация дампов',
                'delete_backup': 'удаление бэкапа',
                'restore_test': 'проверка восстановления из бэкапа'}.get(action, action)
        if ok:
            title = f'✅ Завершён: {name} · узел {node}'
            snippet = (str(out) or '')[:2000]
        else:
            title = f'❌ Ошибка: {name} · узел {node}'
            snippet = (str(out) or 'см. журнал агента')[:2000]
        body = (f"Событие: {title}\n"
                f"Узел: {node} · время: {ts}\n"
                f"Статус: {'OK' if ok else 'ОШИБКА'}\n"
                f"Результат: {snippet}")
        notify.notify_direct(title, body, severity='warning' if not ok else 'info')
    except Exception:
        pass

@bp.route('/api/agent/status')
def status():
    now = time.time()
    with _LOCK:
        out = {n: {'online': (now - a['last_seen'] < 30), 'last_seen': a['last_seen'],
                   'delay': a.get('delay'), 'backups': a.get('backups') or [],
                   'last': a.get('last'), 'metrics': a.get('metrics')}
               for n, a in AGENTS.items()}
    return jsonify({'ok': True, 'agents': out})

def agent_online(node, max_age=30):
    with _LOCK:
        ag = AGENTS.get(node)
        return bool(ag and time.time() - ag['last_seen'] < max_age)

def agent_delay(node, max_age=30):
    with _LOCK:
        ag = AGENTS.get(node)
        if ag and ag.get('delay') is not None and time.time() - ag.get('delay_ts', 0) < max_age:
            return ag['delay']
    return None

def agent_metrics(node):
    with _LOCK:
        ag = AGENTS.get(node)
        return ag.get('metrics') if ag else None

def enqueue_agent(node, action, **params):
    with _LOCK:
        ag = AGENTS.get(node)
        if not ag: return None
        cmd_id = f"{node}-{int(time.time()*1000)}"
        ag['pending'].append({'id': cmd_id, 'action': action, 'params': params})
        return cmd_id

def agent_read_config(node, timeout=10):
    """Прочитать patroni.yml с ноды через агента. Возвращает (dict, error)."""
    cid = enqueue_agent(node, 'read_patroni_config')
    if cid is None: return None, 'нода не зарегистрирована'
    t0 = time.time()
    while time.time() - t0 < timeout:
        with _LOCK:
            ag = AGENTS.get(node)
            res = (ag or {}).get('results', {}).get(cid)
            if res:
                if res.get('ok'):
                    return ((res.get('out') or {}).get('data', {}) or {}).get('config') or {}, None
                return None, res.get('out') or 'error'
        time.sleep(0.2)
    return None, 'timeout'

