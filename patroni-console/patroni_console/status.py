"""Снимок состояния кластера Patroni 4: состав, роли, лаг, таймлайн, предупреждения.

Снимок строится из двух источников:
  * GET /patroni на каждой ноде из конфига — доступность и взгляд самой ноды;
  * GET /cluster с любой доступной ноды — состав кластера по данным DCS.
Расхождения между ними и есть то, что оператору нужно увидеть в первую очередь.
"""
import time
from concurrent.futures import ThreadPoolExecutor

from .patroni import PatroniError

LEADER_ROLES = {'leader', 'primary', 'standby_leader', 'master'}
REPLICA_OK_STATES = {'streaming', 'running'}


def _lag(v):
    """Patroni отдаёт lag числом байт или строкой 'unknown'."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def human_bytes(n):
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if abs(n) < 1024 or unit == 'ГБ':
            return f'{n:.0f} {unit}' if unit == 'Б' else f'{n:.1f} {unit}'
        n /= 1024


def _probe(client, node):
    t0 = time.monotonic()
    try:
        data = client.node_status(node)
        return node.name, {'reachable': True, 'data': data or {},
                           'ms': round((time.monotonic() - t0) * 1000)}
    except PatroniError as e:
        return node.name, {'reachable': False, 'error': str(e), 'status': e.status,
                           'ms': round((time.monotonic() - t0) * 1000)}


def collect(cluster, client, pool=None):
    own_pool = pool is None
    pool = pool or ThreadPoolExecutor(max_workers=max(2, len(cluster.nodes)))
    try:
        probes = dict(pool.map(lambda n: _probe(client, n), cluster.nodes))
    finally:
        if own_pool:
            pool.shutdown(wait=False)

    view, view_from, view_err = None, None, []
    # /cluster спрашиваем сначала у доступных нод, начиная с той, что считает себя лидером
    order = sorted(cluster.nodes, key=lambda n: (
        not probes[n.name]['reachable'],
        (probes[n.name].get('data') or {}).get('role') not in LEADER_ROLES))
    for n in order:
        if not probes[n.name]['reachable']:
            continue
        try:
            view = client.cluster_view(n)
            view_from = n.name
            break
        except PatroniError as e:
            view_err.append(str(e))
    return build(cluster, probes, view, view_from, view_err)


def build(cluster, probes, view, view_from=None, view_err=()):
    """Чистая функция: из сырых ответов — снимок. Отдельно, чтобы тестировать без сети."""
    warnings = []

    def warn(level, code, text, node=None):
        warnings.append({'level': level, 'code': code, 'text': text, 'node': node})

    dcs_members = {m.get('name'): m for m in (view or {}).get('members') or []}
    members = []
    for n in cluster.nodes:
        p = probes.get(n.name) or {'reachable': False, 'error': 'нет данных'}
        d = p.get('data') or {}
        m = dcs_members.get(n.name) or {}
        role = m.get('role') or d.get('role')
        state = m.get('state') or d.get('state')
        tags = m.get('tags') or d.get('tags') or {}
        members.append({
            'name': n.name,
            'url': n.url,
            'reachable': p['reachable'],
            'error': p.get('error'),
            'response_ms': p.get('ms'),
            'in_dcs': n.name in dcs_members,
            'role': role,
            'state': state,
            'timeline': m.get('timeline') or d.get('timeline'),
            'lag': _lag(m.get('lag')),
            'lag_unknown': 'lag' in m and _lag(m.get('lag')) is None,
            'receive_lag': _lag(m.get('receive_lag')),
            'replay_lag': _lag(m.get('replay_lag')),
            'lsn': m.get('lsn'),
            'host': m.get('host'),
            'port': m.get('port'),
            'tags': tags,
            'pending_restart': bool(m.get('pending_restart') or d.get('pending_restart')),
            'pending_restart_reason': d.get('pending_restart_reason'),
            'patroni_version': (d.get('patroni') or {}).get('version'),
            'postgres_version': d.get('server_version'),
            'dcs_last_seen': d.get('dcs_last_seen'),
        })
    # члены в DCS, которых нет в нашем конфиге
    known = {n.name for n in cluster.nodes}
    for name, m in dcs_members.items():
        if name not in known:
            members.append({'name': name, 'url': None, 'reachable': None, 'in_dcs': True,
                            'role': m.get('role'), 'state': m.get('state'),
                            'timeline': m.get('timeline'), 'lag': _lag(m.get('lag')),
                            'tags': m.get('tags') or {}, 'unconfigured': True})
            warn('warning', 'unconfigured_member', f'{name} есть в DCS, но не описан в конфиге консоли', name)

    leaders = [m for m in members if m.get('in_dcs') and m.get('role') in LEADER_ROLES]
    paused = bool((view or {}).get('pause')) or any(
        (probes.get(n.name) or {}).get('data', {}).get('pause') for n in cluster.nodes)

    if view is None:
        warn('critical', 'no_dcs_view', 'ни одна нода не отдала /cluster: состав кластера неизвестен')
    elif not leaders:
        warn('critical', 'no_leader', 'в кластере нет лидера')
    elif len(leaders) > 1:
        warn('critical', 'multiple_leaders', 'несколько нод с ролью лидера: ' + ', '.join(m['name'] for m in leaders))
    if paused:
        warn('warning', 'paused', 'кластер на паузе: автоматический failover выключен')
    if (view or {}).get('scheduled_switchover'):
        warn('info', 'scheduled_switchover', 'запланирован switchover')

    leader = leaders[0] if len(leaders) == 1 else None
    timelines = {m['timeline'] for m in members if m.get('in_dcs') and m.get('timeline') is not None}

    for m in members:
        if m.get('unconfigured'):
            continue
        name = m['name']
        if not m['reachable']:
            warn('critical' if m is leader else 'warning', 'unreachable', f'REST API недоступен: {m.get("error")}', name)
        if not m['in_dcs']:
            warn('warning', 'not_in_dcs', 'нода не зарегистрирована в DCS', name)
        if m is leader or m.get('role') in LEADER_ROLES:
            if m.get('state') not in (None, 'running'):
                warn('critical', 'leader_state', f'лидер в состоянии {m["state"]}', name)
        elif m['in_dcs']:
            if m.get('state') not in REPLICA_OK_STATES:
                warn('warning', 'replica_state', f'реплика в состоянии {m.get("state")}', name)
            if m['lag_unknown']:
                warn('warning', 'lag_unknown', 'лаг неизвестен', name)
            elif m['lag'] is not None and m['lag'] > cluster.max_lag_bytes:
                warn('warning', 'lag', f'лаг {human_bytes(m["lag"])} выше порога {human_bytes(cluster.max_lag_bytes)}', name)
            if leader and m.get('timeline') is not None and leader.get('timeline') is not None \
                    and m['timeline'] != leader['timeline']:
                warn('warning', 'timeline', f'таймлайн {m["timeline"]}, у лидера {leader["timeline"]}', name)
        if m.get('pending_restart'):
            warn('info', 'pending_restart', 'нужен рестарт для применения параметров', name)

    candidates = [m for m in members
                  if m.get('in_dcs') and m.get('role') not in LEADER_ROLES
                  and m.get('state') in REPLICA_OK_STATES
                  and not (m.get('tags') or {}).get('nofailover')
                  and not (m.get('tags') or {}).get('failover_priority') == 0]
    if leader and not candidates:
        warn('warning', 'no_failover_candidate', 'нет реплики, пригодной для failover')

    level_rank = {'critical': 2, 'warning': 1, 'info': 0}
    worst = max((level_rank[w['level']] for w in warnings), default=-1)
    health = {2: 'critical', 1: 'degraded'}.get(worst, 'ok')

    return {
        'cluster': cluster.name,
        'scope': (view or {}).get('scope'),
        'collected_at': time.time(),
        'view_from': view_from,
        'view_errors': list(view_err),
        'health': health,
        'leader': leader['name'] if leader else None,
        'paused': paused,
        'timelines': sorted(timelines),
        'scheduled_switchover': (view or {}).get('scheduled_switchover'),
        'failover_candidates': [m['name'] for m in candidates],
        'members': members,
        'warnings': warnings,
    }
