import copy

from conftest import NODES, healthy_view, node_views
from fakes import FakeSession, patroni_routes
from patroni_console import status
from patroni_console.patroni import PatroniClient


def snap(config, view=None, views=None, down=()):
    c = config.clusters['test']
    s = FakeSession(patroni_routes(NODES, view if view is not None else healthy_view(),
                                   views or node_views(), down))
    return status.collect(c, PatroniClient(c, session=s))


def codes(st):
    return {(w['code'], w['node']) for w in st['warnings']}


def test_healthy_cluster(config):
    st = snap(config)
    assert st['health'] == 'ok', st['warnings']
    assert st['leader'] == 'n1'
    assert st['failover_candidates'] == ['n2', 'n3']
    assert st['timelines'] == [5]
    n3 = next(m for m in st['members'] if m['name'] == 'n3')
    assert n3['lag'] == 4096 and n3['patroni_version'] == '4.0.4'


def test_unknown_lag_is_warning_not_crash(config):
    v = healthy_view()
    v['members'][2]['lag'] = 'unknown'
    st = snap(config, v)
    assert ('lag_unknown', 'n3') in codes(st)
    assert st['health'] == 'degraded'


def test_lag_over_threshold(config):
    v = healthy_view()
    v['members'][2]['lag'] = 50 * 1024 * 1024
    assert ('lag', 'n3') in codes(snap(config, v))


def test_no_leader_is_critical(config):
    v = healthy_view()
    v['members'][0]['role'] = 'replica'
    v['members'][0]['state'] = 'stopped'
    st = snap(config, v)
    assert st['health'] == 'critical'
    assert ('no_leader', None) in codes(st)
    assert st['leader'] is None


def test_leader_unreachable_but_dcs_view_from_replica(config):
    st = snap(config, down=('n1',))
    assert ('unreachable', 'n1') in codes(st)
    assert st['health'] == 'critical'
    assert st['view_from'] in ('n2', 'n3')


def test_all_nodes_down(config):
    st = snap(config, down=('n1', 'n2', 'n3'))
    assert ('no_dcs_view', None) in codes(st)
    assert st['health'] == 'critical'


def test_paused_and_scheduled_switchover(config):
    v = healthy_view()
    v['pause'] = True
    v['scheduled_switchover'] = {'at': '2026-10-17T03:00:00+03:00', 'from': 'n1', 'to': 'n2'}
    c = codes(snap(config, v))
    assert ('paused', None) in c and ('scheduled_switchover', None) in c


def test_timeline_mismatch_and_nofailover(config):
    v = healthy_view()
    v['members'][1]['timeline'] = 4
    v['members'][1]['tags'] = {'nofailover': True}
    v['members'][2]['tags'] = {'failover_priority': 0}
    st = snap(config, v)
    assert ('timeline', 'n2') in codes(st)
    assert ('no_failover_candidate', None) in codes(st)


def test_member_in_dcs_but_not_in_config(config):
    v = healthy_view()
    extra = copy.deepcopy(v['members'][2]); extra['name'] = 'n4'
    v['members'].append(extra)
    assert ('unconfigured_member', 'n4') in codes(snap(config, v))


def test_replica_not_streaming(config):
    v = healthy_view()
    v['members'][2]['state'] = 'starting'
    assert ('replica_state', 'n3') in codes(snap(config, v))
