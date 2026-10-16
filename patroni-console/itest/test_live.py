"""Интеграционные тесты на живом кластере: etcd 3.6 + Patroni 4 + PostgreSQL.

Стенд поднимается скриптом itest/up.sh (3 ноды на 127.0.0.1) и разрушается
сценариями тестов: switchover, failover, падение лидера, reinit. Запускать
только на этом стенде:

    PC_LIVE=1 python -m pytest -v itest/test_live.py

Тесты идут по порядку и опираются на состояние, оставленное предыдущими.
"""
import os
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

from patroni_console import config as cfgmod, ops  # noqa: E402
from patroni_console.web import create_app  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get('PC_LIVE') != '1', reason='нужен стенд itest/up.sh и PC_LIVE=1')

ROOT = os.environ.get('ITEST_ROOT', '/opt/itest/run')
PATRONICTL = os.environ.get('PATRONICTL_BIN', '/opt/itest/venv/bin/patronictl')
PG_USER = os.environ.get('PG_USER', 'pgtest')
PASS = 'live-admin-password'
NODES = ['n1', 'n2', 'n3']


class LiveTimings(ops.Timings):
    poll = 1.0


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    os.environ['PC_LIVE_SECRET'] = 'l' * 40
    os.environ['PC_LIVE_REST'] = 'itest-rest-pass'
    raw = {
        'web': {'secret_key': '${PC_LIVE_SECRET}', 'cookie_secure': False, 'timeout': 3},
        'watch': {'interval': 2, 'confirm': 2},
        'clusters': [{
            'name': 'itest', 'max_lag_bytes': 16 * 1024 * 1024,
            'rest': {'scheme': 'http', 'username': 'patroni', 'password': '${PC_LIVE_REST}'},
            'nodes': [{'name': n, 'host': '127.0.0.1', 'port': 8008 + i} for i, n in enumerate(NODES)],
            'etcd': {'endpoints': ['http://127.0.0.1:2379', 'http://127.0.0.1:2381', 'http://127.0.0.1:2383']},
        }],
        'users': [{'name': 'admin', 'role': 'admin', 'password_hash': generate_password_hash(PASS)}],
    }
    c = cfgmod.parse(raw)
    c.db_path = str(tmp_path_factory.mktemp('live') / 'live.db')
    a = create_app(c, ops_timings=LiveTimings(), ops_sync=True)
    a.testing = True
    return a


@pytest.fixture(scope='module')
def http(app):
    c = app.test_client()
    r = c.post('/api/auth/login', json={'username': 'admin', 'password': PASS})
    c.csrf = r.get_json()['data']['csrf_token']
    return c


def status(http):
    return http.get('/api/clusters/itest/status').get_json()['data']


def wait_for(pred, timeout=90, every=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def healthy(http):
    s = status(http)
    ok = s['leader'] and all(m['reachable'] and m['in_dcs'] and m['state'] in ('running', 'streaming')
                             for m in s['members'])
    return s if ok and not s['paused'] else None


def run(http, kind, params=None, **extra):
    r = http.post('/api/clusters/itest/operations', headers={'X-CSRF-Token': http.csrf},
                  json={'kind': kind, 'params': params or {}, 'confirm': 'itest', **extra})
    body = r.get_json()
    assert body['ok'], body
    return body['data']


def tick(app, n=1, pause=2.0):
    w = app.extensions['pc_watcher']
    for _ in range(n):
        w.tick(app.config['PC'].clusters['itest'])
        time.sleep(pause)


def events(app):
    return app.extensions['pc_watcher'].store.events_list('itest', 200)


def node_dir(name):
    return os.path.join(ROOT, name)


def kill_node(name):
    """Жёсткое падение ноды: SIGKILL patroni и postmaster."""
    with open(os.path.join(node_dir(name), 'patroni.pid')) as f:
        os.kill(int(f.read().strip()), signal.SIGKILL)
    pm = os.path.join(node_dir(name), 'data', 'postmaster.pid')
    if os.path.exists(pm):
        with open(pm) as f:
            os.kill(int(f.readline().strip()), signal.SIGKILL)


def start_node(name):
    yml = os.path.join(ROOT, f'{name}.yml')
    d = node_dir(name)
    subprocess.run(['su', PG_USER, '-c', f'nohup /opt/itest/venv/bin/patroni {yml} >> {d}/patroni.log 2>&1 & '
                                         f'echo $! > {d}/patroni.pid'], check=True)


# ------------------------------------------------------------------ сценарии
def test_01_cluster_visible(app, http):
    s = wait_for(lambda: healthy(http), 120)
    assert s, 'стенд не поднялся'
    assert s['health'] == 'ok' and len(s['members']) == 3
    assert {m['patroni_version'] for m in s['members']} == {'4.0.4'}
    e = http.get('/api/clusters/itest/etcd').get_json()['data']
    assert e['quorum'] and len(e['members']) == 3
    assert {ep['version'] for ep in e['endpoints']} >= {'3.6.4'}
    tick(app, 2, 0.5)              # базовое состояние наблюдателя


def test_02_switchover(app, http):
    s = status(http)
    old = s['leader']
    cand = next(m['name'] for m in s['members'] if m['name'] != old)
    op = run(http, 'switchover', {'candidate': cand})
    assert op['status'] == 'succeeded', op
    assert op['result']['new_leader'] == cand and op['result']['old_leader'] == old
    assert wait_for(lambda: healthy(http), 60)
    tick(app, 1, 0.5)
    lc = [e for e in events(app) if e['kind'] == 'leader_change']
    assert lc and lc[0]['level'] == 'info' and lc[0]['op_id'] == op['id']


def test_03_switchover_blocked_on_unknown_candidate(http):
    r = http.post('/api/clusters/itest/operations', headers={'X-CSRF-Token': http.csrf},
                  json={'kind': 'switchover', 'params': {'candidate': 'nX'}, 'confirm': 'itest'})
    assert r.status_code == 400


def test_04_config_change_and_rollback(app, http):
    op = run(http, 'config', {'changes': {'postgresql': {'parameters': {'work_mem': '16MB'}}}})
    assert op['status'] == 'succeeded', op
    vid = op['result']['version']
    cfg = http.get('/api/clusters/itest/config').get_json()['data']
    assert cfg['postgresql']['parameters']['work_mem'] == '16MB'
    op2 = run(http, 'config', {'rollback_to': vid}, acknowledge_warnings=True)
    assert op2['status'] == 'succeeded', op2
    cfg = http.get('/api/clusters/itest/config').get_json()['data']
    assert 'work_mem' not in cfg['postgresql']['parameters']


def test_05_restart_param_then_rolling_restart(app, http):
    op = run(http, 'config', {'changes': {'postgresql': {'parameters': {'max_connections': 150}}}},
             acknowledge_warnings=True)
    assert op['status'] == 'succeeded' and op['result']['needs_restart'] == ['max_connections']
    s = wait_for(lambda: (lambda x: x if all(m['pending_restart'] for m in x['members']) else None)(status(http)), 60)
    assert s, 'Patroni не отметил pending_restart'
    op = run(http, 'rolling_restart', {'only_pending': True})
    assert op['status'] == 'succeeded', op
    s = wait_for(lambda: healthy(http), 60)
    assert s and not any(m['pending_restart'] for m in s['members'])


def test_06_pause_resume(http):
    assert run(http, 'pause', acknowledge_warnings=True)['status'] == 'succeeded'
    assert status(http)['paused']
    assert run(http, 'resume')['status'] == 'succeeded'
    assert not status(http)['paused']


def test_07_restart_replica(http):
    s = status(http)
    rep = next(m['name'] for m in s['members'] if m['name'] != s['leader'])
    op = run(http, 'restart', {'node': rep}, acknowledge_warnings=True)   # нода не ждёт рестарта: предупреждение
    assert op['status'] == 'succeeded', op


def test_08_unplanned_failover_detected(app, http):
    """Лидер падает (SIGKILL): Patroni сам выбирает нового, наблюдатель шлёт critical."""
    tick(app, 1, 0.5)
    s = status(http)
    old = s['leader']
    kill_node(old)
    new = wait_for(lambda: (lambda x: x['leader'] if x['leader'] and x['leader'] != old else None)(status(http)), 90)
    assert new, 'новый лидер не выбран'
    tick(app, 3, 1)
    lc = [e for e in events(app) if e['kind'] == 'leader_change']
    assert lc[0]['level'] == 'critical' and f'{old} → {new}' in lc[0]['text']
    assert any(e['kind'] == 'opened' and e['code'] == 'unreachable' and e['node'] == old for e in events(app))
    start_node(old)
    assert wait_for(lambda: healthy(http), 120), 'старый лидер не вернулся'
    tick(app, 3, 1)
    assert any(e['kind'] == 'resolved' and e['node'] == old for e in events(app))


def test_09_forced_failover(http):
    s = status(http)
    cand = next(m['name'] for m in s['members'] if m['name'] != s['leader'])
    r = http.post('/api/clusters/itest/operations', headers={'X-CSRF-Token': http.csrf},
                  json={'kind': 'failover', 'params': {'candidate': cand}, 'confirm': 'itest'})
    assert r.get_json()['error']['code'] == 'force_required'
    op = run(http, 'failover', {'candidate': cand}, force=True, acknowledge_warnings=True)
    assert op['status'] in ('succeeded', 'warning'), op
    assert status(http)['leader'] == cand
    assert wait_for(lambda: healthy(http), 120)


def test_10_reinit_replica(http):
    s = status(http)
    rep = next(m['name'] for m in s['members'] if m['name'] != s['leader'])
    op = run(http, 'reinitialize', {'node': rep}, acknowledge_warnings=True)
    assert op['status'] == 'succeeded', op
    assert wait_for(lambda: healthy(http), 60)


def test_11_external_config_change_detected(app, http):
    tick(app, 1, 0.5)
    subprocess.run([PATRONICTL, '-c', os.path.join(ROOT, 'n1.yml'), 'edit-config', '--force',
                    '--set', 'maximum_lag_on_failover=2097152'], check=True, capture_output=True)
    ev = wait_for(lambda: (tick(app, 1, 0.5), [e for e in events(app) if e['code'] == 'config_external'])[1], 30)
    assert ev and 'maximum_lag_on_failover' in ev[0]['text']


def test_12_etcd_member_down(app, http):
    with open(os.path.join(ROOT, 'etcd3.pid')) as f:
        pid = int(f.read().strip())
    os.kill(pid, signal.SIGSTOP)        # зависший член etcd
    try:
        tick(app, 3, 1)
        e = http.get('/api/clusters/itest/etcd').get_json()['data']
        assert e['quorum']               # 2 из 3 — кворум есть
        assert any(ev['code'] == 'etcd_endpoint' and ev['kind'] == 'opened' for ev in events(app))
        assert healthy(http)            # Patroni продолжает работать
    finally:
        os.kill(pid, signal.SIGCONT)
    tick(app, 3, 1)
    assert any(ev['code'] == 'etcd_endpoint' and ev['kind'] == 'resolved' for ev in events(app))
