import pytest

from conftest import PASSWORDS
from sim import SimCluster
from patroni_console import ops
from patroni_console.web import create_app


class Fast(ops.Timings):
    poll = 0
    leader_change = rejoin = restart = reinit = pause = 0.05


@pytest.fixture
def sim():
    return SimCluster()


@pytest.fixture
def app(config, sim):
    a = create_app(config, patroni_session_factory=sim.session, ops_timings=Fast(), ops_sync=True)
    a.testing = True
    return a


def client(app, user):
    c = app.test_client()
    r = c.post('/api/auth/login', json={'username': user, 'password': PASSWORDS[user]})
    c.csrf = r.get_json()['data']['csrf_token']
    return c


def run(c, kind, params=None, key=None, **extra):
    body = {'kind': kind, 'params': params or {}, 'confirm': 'test', **extra}
    h = {'X-CSRF-Token': c.csrf}
    if key:
        h['Idempotency-Key'] = key
    return c.post('/api/clusters/test/operations', json=body, headers=h)


def precheck(c, kind, params=None):
    r = c.post('/api/clusters/test/operations/precheck', json={'kind': kind, 'params': params or {}},
               headers={'X-CSRF-Token': c.csrf})
    return r.get_json()['data']


def codes(checks):
    return {(x['level'], x['code']) for x in checks}


# ---------- switchover ----------
def test_switchover_happy_path(app, sim):
    c = client(app, 'oper')
    r = run(c, 'switchover', {'candidate': 'n2'})
    op = r.get_json()['data']
    assert r.status_code == 202 and op['status'] == 'succeeded', op
    assert op['result'] == {'old_leader': 'n1', 'new_leader': 'n2', 'timeline': 6}
    assert sim.leader == 'n2'
    # запрос ушёл ровно один раз и на старого лидера
    sw = [x for x in sim.calls if x[2] == '/switchover']
    assert len(sw) == 1 and sw[0][1] == 'n1' and sw[0][3] == {'leader': 'n1', 'candidate': 'n2'}


def test_switchover_requires_typed_cluster_name(app, sim):
    c = client(app, 'oper')
    r = run(c, 'switchover', {'candidate': 'n2'}, confirm='wrong')
    assert r.status_code == 400 and sim.leader == 'n1'


def test_switchover_blocked_on_lagging_candidate(app, sim):
    sim.members['n2']['lag'] = 50 * 1024 * 1024
    c = client(app, 'oper')
    assert ('block', 'candidate_lag') in codes(precheck(c, 'switchover', {'candidate': 'n2'})['checks'])
    r = run(c, 'switchover', {'candidate': 'n2'})
    assert r.status_code == 412 and sim.leader == 'n1'
    assert not [x for x in sim.calls if x[2] == '/switchover']


def test_switchover_blocked_on_nofailover_and_sync_mode(app, sim):
    sim.members['n2']['tags'] = {'nofailover': True}
    sim.config['synchronous_mode'] = True
    c = client(app, 'oper')
    cks = codes(precheck(c, 'switchover', {'candidate': 'n2'})['checks'])
    assert ('block', 'candidate_nofailover') in cks and ('block', 'candidate_not_sync') in cks


def test_paused_needs_explicit_candidate(app, sim):
    sim.paused = True
    c = client(app, 'oper')
    assert ('block', 'paused_needs_candidate') in codes(precheck(c, 'switchover')['checks'])
    r = run(c, 'switchover', {'candidate': 'n2'})
    assert r.get_json()['error']['code'] == 'warnings_not_acknowledged'
    r = run(c, 'switchover', {'candidate': 'n2'}, acknowledge_warnings=True)
    assert r.get_json()['data']['status'] == 'succeeded'


def test_patroni_refusal_is_reported_not_retried(app, sim):
    sim.refuse[('POST', '/switchover')] = (412, 'candidate name does not match with sync_standby')
    c = client(app, 'oper')
    op = run(c, 'switchover', {'candidate': 'n3'}).get_json()['data']
    assert op['status'] == 'failed'
    assert op['result']['status'] == 412 and 'sync_standby' in op['result']['body']
    assert len([x for x in sim.calls if x[2] == '/switchover']) == 1


def test_lost_response_is_verified_by_state(app, sim):
    sim.read_timeout_on.add('/switchover')
    c = client(app, 'oper')
    op = run(c, 'switchover', {'candidate': 'n2'}).get_json()['data']
    assert op['status'] == 'succeeded' and sim.leader == 'n2'
    assert len([x for x in sim.calls if x[2] == '/switchover']) == 1


def test_old_leader_not_rejoining_is_warning(app, sim):
    sim.old_leader_rejoins = False
    c = client(app, 'oper')
    op = run(c, 'switchover', {'candidate': 'n2'}).get_json()['data']
    assert op['status'] == 'warning' and 'n1' in op['result']['warning']


def test_scheduled_switchover_and_cancel(app, sim):
    c = client(app, 'oper')
    op = run(c, 'switchover', {'candidate': 'n2', 'scheduled_at': '2099-01-01T03:00:00+03:00'}).get_json()['data']
    assert op['status'] == 'scheduled' and sim.scheduled and sim.leader == 'n1'
    assert ('block', 'already_scheduled') in codes(precheck(c, 'switchover', {'candidate': 'n3'})['checks'])
    op = run(c, 'cancel_switchover').get_json()['data']
    assert op['status'] == 'cancelled' and sim.scheduled is None


def test_scheduled_time_must_have_timezone_and_be_future(app):
    c = client(app, 'oper')
    assert ('block', 'bad_time') in codes(precheck(c, 'switchover', {'candidate': 'n2', 'scheduled_at': '2099-01-01T03:00'})['checks'])
    assert ('block', 'time_in_past') in codes(precheck(c, 'switchover', {'candidate': 'n2', 'scheduled_at': '2000-01-01T03:00:00+00:00'})['checks'])


# ---------- failover ----------
def test_failover_is_admin_only(app):
    c = client(app, 'oper')
    assert run(c, 'failover', {'candidate': 'n2'}).status_code == 403


def test_failover_with_healthy_leader_requires_force(app, sim):
    c = client(app, 'admin')
    r = run(c, 'failover', {'candidate': 'n2'})
    assert r.get_json()['error']['code'] == 'force_required' and sim.leader == 'n1'
    op = run(c, 'failover', {'candidate': 'n2'}, force=True).get_json()['data']
    assert op['status'] == 'succeeded' and sim.leader == 'n2'


def test_failover_when_leader_is_down(app, sim):
    sim.members['n1'].update(role='replica', state='stopped')
    sim.down.add('n1')
    sim.members['n3']['lag'] = 4096
    c = client(app, 'admin')
    pc = precheck(c, 'failover', {'candidate': 'n3'})
    assert ('warn', 'data_loss') in codes(pc['checks']) and not pc['needs_force']
    op = run(c, 'failover', {'candidate': 'n3'}, acknowledge_warnings=True).get_json()['data']
    # лидера до операции не было, поэтому ждать возврата старого лидера некого
    assert op['status'] == 'succeeded' and op['result'] == {'old_leader': None, 'new_leader': 'n3', 'timeline': 6}
    assert [x[1] for x in sim.calls if x[2] == '/failover'] == ['n3']


def test_failover_unknown_candidate_rejected(app):
    c = client(app, 'admin')
    r = run(c, 'failover', {'candidate': 'n9'})
    assert r.status_code == 400


# ---------- пауза ----------
def test_pause_and_resume(app, sim):
    c = client(app, 'oper')
    assert run(c, 'pause', acknowledge_warnings=True).get_json()['data']['status'] == 'succeeded'
    assert sim.paused
    assert run(c, 'resume').get_json()['data']['status'] == 'succeeded'
    assert not sim.paused
    patches = [x[3] for x in sim.calls if x[2] == '/config' and x[0] == 'PATCH']
    assert patches == [{'pause': True}, {'pause': None}]


# ---------- рестарты ----------
def test_restart_leader_needs_admin_force(app, sim):
    c = client(app, 'oper')
    assert run(c, 'restart', {'node': 'n1'}, acknowledge_warnings=True).status_code == 403
    assert not [x for x in sim.calls if x[2] == '/restart']


def test_restart_replica(app, sim):
    sim.members['n2']['pending_restart'] = True
    c = client(app, 'oper')
    op = run(c, 'restart', {'node': 'n2'}).get_json()['data']
    assert op['status'] == 'succeeded' and not sim.members['n2']['pending_restart']


def test_restart_failure_reported(app, sim):
    sim.members['n2']['pending_restart'] = True
    sim.restart_heals = False
    c = client(app, 'oper')
    op = run(c, 'restart', {'node': 'n2'}).get_json()['data']
    assert op['status'] == 'failed'


def test_rolling_restart_switches_leader_first(app, sim):
    for m in sim.members.values():
        m['pending_restart'] = True
    c = client(app, 'oper')
    op = run(c, 'rolling_restart', {'only_pending': True}).get_json()['data']
    assert op['status'] == 'succeeded', op
    order = [(x[1], x[2]) for x in sim.calls if x[0] in ('POST',)]
    assert order == [('n2', '/restart'), ('n3', '/restart'), ('n1', '/switchover'), ('n1', '/restart')]
    assert sim.leader != 'n1' and not any(m['pending_restart'] for m in sim.members.values())


def test_rolling_restart_stops_on_first_failure(app, sim):
    for m in sim.members.values():
        m['pending_restart'] = True
    sim.restart_heals = False
    c = client(app, 'oper')
    op = run(c, 'rolling_restart').get_json()['data']
    assert op['status'] == 'failed' and op['result']['failed_on'] == 'n2'
    assert len([x for x in sim.calls if x[2] == '/restart']) == 1


# ---------- reinit ----------
def test_reinit_replica_admin(app, sim):
    sim.members['n3'].update(state='start failed', lag=None)
    c = client(app, 'admin')
    op = run(c, 'reinitialize', {'node': 'n3'}, acknowledge_warnings=True).get_json()['data']
    assert op['status'] == 'succeeded'


def test_reinit_leader_blocked(app):
    c = client(app, 'admin')
    assert run(c, 'reinitialize', {'node': 'n1'}, acknowledge_warnings=True).status_code == 412


# ---------- общее ----------
def test_idempotency_key_replays_without_second_call(app, sim):
    c = client(app, 'oper')
    a = run(c, 'switchover', {'candidate': 'n2'}, key='k1').get_json()['data']
    b = run(c, 'switchover', {'candidate': 'n2'}, key='k1')
    assert b.status_code == 200 and b.get_json()['data']['id'] == a['id']
    assert len([x for x in sim.calls if x[2] == '/switchover']) == 1
    # тот же ключ с другими параметрами — конфликт
    assert run(c, 'switchover', {'candidate': 'n3'}, key='k1').status_code == 409


def test_unknown_params_rejected(app):
    c = client(app, 'oper')
    assert run(c, 'pause', {'node': 'n1'}).status_code == 400


def test_viewer_cannot_run(app):
    c = client(app, 'viewer')
    assert run(c, 'pause').status_code == 403


def test_operations_are_audited_and_listed(app):
    c = client(app, 'oper')
    run(c, 'switchover', {'candidate': 'n2'})
    ops_list = c.get('/api/clusters/test/operations').get_json()['data']
    assert ops_list[0]['kind'] == 'switchover' and ops_list[0]['log']
    a = client(app, 'admin')
    rows = a.get('/api/audit').get_json()['data']
    assert any(r['action'] == 'op:switchover' and r['detail'] == 'запуск' for r in rows)
    assert any(r['action'] == 'op:switchover' and r['detail'].startswith('succeeded') for r in rows)


def test_one_operation_per_cluster(config, sim):
    from patroni_console.store import ClusterBusy, Store
    s = Store(config.db_path)
    s.op_create('test', 'pause', {}, 'oper')
    with pytest.raises(ClusterBusy):
        s.op_create('test', 'resume', {}, 'oper')
    s.op_interrupt_stale()
    s.op_create('test', 'resume', {}, 'oper')
