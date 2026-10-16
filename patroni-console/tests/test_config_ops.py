import pytest

from conftest import PASSWORDS
from sim import SimCluster
from patroni_console import dynconf, ops
from patroni_console.web import create_app


class Fast(ops.Timings):
    poll = 0
    leader_change = rejoin = restart = reinit = pause = config = 0.05


@pytest.fixture
def sim():
    s = SimCluster()
    s.config['postgresql'] = {'use_pg_rewind': True, 'parameters': {'max_connections': 200, 'work_mem': '4MB'}}
    return s


@pytest.fixture
def app(config, sim):
    config.clusters['test'].etcd.endpoints = []
    a = create_app(config, patroni_session_factory=sim.session, ops_timings=Fast(), ops_sync=True)
    a.testing = True
    return a


def login(app, user='admin'):
    c = app.test_client()
    r = c.post('/api/auth/login', json={'username': user, 'password': PASSWORDS[user]})
    c.csrf = r.get_json()['data']['csrf_token']
    return c


def precheck(c, params):
    return c.post('/api/clusters/test/operations/precheck', headers={'X-CSRF-Token': c.csrf},
                  json={'kind': 'config', 'params': params})


def run(c, params, **extra):
    return c.post('/api/clusters/test/operations', headers={'X-CSRF-Token': c.csrf},
                  json={'kind': 'config', 'params': params, 'confirm': 'test', **extra})


def codes(cks):
    return {(x['level'], x['code']) for x in cks}


# ---- чистые функции ----
def test_merge_and_patch_to_roundtrip():
    a = {'ttl': 30, 'postgresql': {'parameters': {'work_mem': '4MB', 'x': 1}}}
    b = {'ttl': 40, 'postgresql': {'parameters': {'work_mem': '8MB'}}, 'failsafe_mode': True}
    assert dynconf.merge(a, dynconf.patch_to(a, b)) == b


def test_validate_rejects_typos_and_bad_types():
    for bad in ({'tll': 30}, {'ttl': '30'}, {'ttl': 5}, {'postgresql': {'parametrs': {}}},
                {'pause': True}, {'synchronous_mode': 'yes'}, {}):
        with pytest.raises(ValueError):
            dynconf.validate(bad)
    dynconf.validate({'synchronous_mode': 'quorum', 'postgresql': {'parameters': {'work_mem': None}}})


def test_checks():
    cur = {'ttl': 30, 'loop_wait': 10, 'retry_timeout': 10, 'postgresql': {'parameters': {'max_connections': 200}}}
    assert ('block', 'timing') in codes(dynconf.checks(cur, {'retry_timeout': 15}))
    cks = codes(dynconf.checks(cur, {'postgresql': {'parameters': {'max_connections': 100, 'port': 6000}}}))
    assert {('warn', 'needs_restart'), ('warn', 'decrease_max_connections'), ('block', 'patroni_owned')} <= cks
    assert ('block', 'no_changes') in codes(dynconf.checks(cur, {'ttl': 30}))
    assert ('block', 'config_changed') in codes(dynconf.checks(cur, {'ttl': 40}, base_hash='deadbeef'))
    assert ('force', 'standby_cluster') in codes(dynconf.checks(cur, {'standby_cluster': {'host': 'x'}}))


# ---- через API ----
def test_preview_shows_diff_and_hash(app):
    c = login(app)
    d = precheck(c, {'changes': {'postgresql': {'parameters': {'work_mem': '16MB'}}}}).get_json()['data']
    assert d['preview']['diff'] == [{'path': 'postgresql.parameters.work_mem', 'old': '4MB', 'new': '16MB'}]
    assert d['can_run'] and not d['needs_ack'] and d['preview']['base_hash']


def test_config_change_applied_and_versioned(app, sim):
    c = login(app)
    op = run(c, {'changes': {'postgresql': {'parameters': {'work_mem': '16MB'}}}}).get_json()['data']
    assert op['status'] == 'succeeded', op
    assert sim.config['postgresql']['parameters']['work_mem'] == '16MB'
    patches = [x[3] for x in sim.calls if x[0] == 'PATCH']
    assert patches == [{'postgresql': {'parameters': {'work_mem': '16MB'}}}]
    v = c.get('/api/clusters/test/config/versions').get_json()['data']
    assert v[0]['source'] == 'console' and v[0]['op_id'] == op['id'] and v[0]['username'] == 'admin'


def test_restart_param_needs_ack_and_reports_restart(app, sim):
    c = login(app)
    ch = {'changes': {'postgresql': {'parameters': {'max_connections': 300}}}}
    assert run(c, ch).get_json()['error']['code'] == 'warnings_not_acknowledged'
    op = run(c, ch, acknowledge_warnings=True).get_json()['data']
    assert op['status'] == 'succeeded' and op['result']['needs_restart'] == ['max_connections']


def test_rollback_restores_previous_state(app, sim):
    c = login(app)
    before = dict(sim.config, postgresql=dict(sim.config['postgresql']))
    op = run(c, {'changes': {'ttl': 40, 'failsafe_mode': True}}).get_json()['data']
    vid = op['result']['version']
    d = precheck(c, {'rollback_to': vid}).get_json()['data']
    assert {r['path'] for r in d['preview']['diff']} == {'ttl', 'failsafe_mode'}
    op2 = run(c, {'rollback_to': vid}, acknowledge_warnings=True).get_json()['data']
    assert op2['status'] == 'succeeded' and sim.config == before


def test_patch_not_applied_is_failed(app, sim):
    sim.config_patch_ignored = True
    c = login(app)
    op = run(c, {'changes': {'ttl': 40}}).get_json()['data']
    assert op['status'] == 'failed' and op['result']['mismatch']


def test_config_is_admin_only_and_typos_rejected(app):
    o = login(app, 'oper')
    assert run(o, {'changes': {'ttl': 40}}).status_code == 403
    a = login(app)
    r = run(a, {'changes': {'maximum_lag_on_falover': 1}})
    assert r.status_code == 400 and 'неизвестный ключ' in r.get_json()['error']['message']


def test_stale_preview_blocked(app, sim):
    c = login(app)
    h = precheck(c, {'changes': {'ttl': 40}}).get_json()['data']['preview']['base_hash']
    sim.config['loop_wait'] = 7                      # кто-то поменял конфиг между предпросмотром и запуском
    r = run(c, {'changes': {'ttl': 40}, 'base_hash': h})
    assert r.status_code == 412 and ('block', 'config_changed') in codes(r.get_json()['error']['checks'])


def test_watcher_detects_external_change(app, sim):
    w = app.extensions['pc_watcher']
    cl = app.config['PC'].clusters['test']
    w.tick(cl)
    sim.config['maximum_lag_on_failover'] = 999          # patronictl edit-config мимо консоли
    w.tick(cl)
    evs = w.store.events_list('test')
    assert evs[0]['code'] == 'config_external' and 'maximum_lag_on_failover' in evs[0]['text']
    assert w.store.config_versions('test')[0]['source'] == 'external'
    w.tick(cl)
    assert len([e for e in w.store.events_list('test') if e['code'] == 'config_external']) == 1


def test_watcher_ignores_console_change(app, sim):
    w = app.extensions['pc_watcher']
    cl = app.config['PC'].clusters['test']
    w.tick(cl)
    run(login(app), {'changes': {'ttl': 40}})
    w.tick(cl)
    assert not [e for e in w.store.events_list('test') if e['code'] == 'config_external']
