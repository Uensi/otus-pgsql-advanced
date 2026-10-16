from fakes import FakeSession
from patroni_console.etcd import EtcdClient

EPS = ['http://10.0.0.1:2379', 'http://10.0.0.2:2379', 'http://10.0.0.3:2379']
IDS = ['111', '222', '333']


def routes(down=(), alarm=None):
    r = {}
    for ep, mid in zip(EPS, IDS):
        if ep in down:
            continue
        r[('POST', ep + '/v3/maintenance/status')] = (200, {
            'header': {'member_id': mid, 'raft_term': '7'}, 'version': '3.6.4',
            'dbSize': '2097152', 'leader': '111', 'raftIndex': '1234', 'raftTerm': '7'})
        r[('POST', ep + '/v3/maintenance/alarm')] = (200, {'alarms': [{'memberID': mid, 'alarm': alarm}] if alarm else []})
        r[('POST', ep + '/v3/cluster/member/list')] = (200, {'members': [
            {'ID': i, 'name': f'etcd{k}', 'peerURLs': [], 'clientURLs': []} for k, i in enumerate(IDS)]})
    return r


def client(config, **kw):
    s = FakeSession(routes(**kw))
    return EtcdClient(config.clusters['test'].etcd, session=s), s


def test_healthy(config):
    h, _ = client(config)
    h = h.health()
    assert h['quorum'] and h['leader_id'] == '111' and not h['split_leader_view']
    assert [e['is_leader'] for e in h['endpoints']] == [True, False, False]
    assert h['endpoints'][0]['db_size'] == 2097152


def test_one_endpoint_down_keeps_quorum(config):
    c, _ = client(config, down=(EPS[2],))
    h = c.health()
    assert h['quorum']
    assert h['endpoints'][2]['error']


def test_two_down_loses_quorum(config):
    c, _ = client(config, down=(EPS[1], EPS[2]))
    assert not c.health()['quorum']


def test_alarm_marks_endpoint_not_ok(config):
    c, _ = client(config, alarm='NOSPACE')
    e = c.health()['endpoints'][0]
    assert e['alarms'] == ['NOSPACE'] and not e['ok']


def test_never_writes(config):
    c, s = client(config)
    c.health()
    assert all('/kv/' not in url for _, url, _ in s.calls)
