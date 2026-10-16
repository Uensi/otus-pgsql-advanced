import itertools

import pytest
import requests

from conftest import PASSWORDS, raw_config
from fakes import FakeResponse
from sim import SimCluster
from patroni_console import config as cfgmod
from patroni_console.notify import Notifier, message
from patroni_console.store import Store
from patroni_console.watcher import conditions_from
from patroni_console.web import create_app

TG_TOKEN = '123456:secret-bot-token'


class Outbox:
    """Подменяет requests.Session для каналов уведомлений."""
    def __init__(self):
        self.sent = []
        self.fail = 0           # сколько ближайших отправок уронить

    def session(self):
        box = self

        class S:
            def post(self, url, json=None, headers=None, timeout=None):
                if box.fail:
                    box.fail -= 1
                    raise requests.ConnectionError(f'cannot reach {url}')
                box.sent.append((url, json, headers))
                return FakeResponse(200, {'ok': True})
        return S()


@pytest.fixture
def sim():
    return SimCluster()


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv('TG_TOKEN', TG_TOKEN)
    raw = raw_config(notify=[
        {'type': 'webhook', 'name': 'hook', 'url': 'https://hooks.example/alert', 'min_level': 'info'},
        {'type': 'telegram', 'name': 'tg', 'bot_token': '${TG_TOKEN}', 'chat_id': '-100500', 'min_level': 'critical'},
    ])
    raw['clusters'][0]['etcd'] = {}
    c = cfgmod.parse(raw)
    c.db_path = str(tmp_path / 'w.db')
    return c


@pytest.fixture
def app(cfg, sim, outbox):
    a = create_app(cfg, patroni_session_factory=sim.session, notify_session_factory=outbox.session, ops_sync=True)
    a.testing = True
    return a


def tick(app, n=1):
    w = app.extensions['pc_watcher']
    for _ in range(n):
        w.tick(app.config['PC'].clusters['test'])


def events(app):
    return list(reversed(app.extensions['pc_watcher'].store.events_list('test')))


def test_healthy_cluster_produces_no_events(app):
    tick(app, 3)
    assert events(app) == []


def test_problem_opens_after_confirm_and_resolves(app, sim):
    sim.down.add('n3')
    tick(app)
    assert events(app) == []                     # одна проверка — ещё не проблема
    tick(app)
    ev = events(app)
    assert [(e['kind'], e['code'], e['node']) for e in ev] == [('opened', 'unreachable', 'n3')]
    tick(app, 3)
    assert len(events(app)) == 1                 # пока держится — повторов нет
    sim.down.discard('n3')
    tick(app)
    assert len(events(app)) == 1
    tick(app)
    assert [e['kind'] for e in events(app)] == ['opened', 'resolved']


def test_flapping_does_not_alert(app, sim):
    for _ in range(4):
        sim.down.add('n3'); tick(app)
        sim.down.discard('n3'); tick(app)
    assert events(app) == []


def test_unplanned_leader_change_is_critical(app, sim):
    tick(app)
    sim._promote('n2')
    tick(app)
    lc = [e for e in events(app) if e['kind'] == 'leader_change']
    assert len(lc) == 1 and lc[0]['level'] == 'critical' and 'n1 → n2' in lc[0]['text']


def test_leader_change_by_operation_is_info(app, sim):
    tick(app)
    c = app.test_client()
    r = c.post('/api/auth/login', json={'username': 'oper', 'password': PASSWORDS['oper']})
    csrf = r.get_json()['data']['csrf_token']
    op = c.post('/api/clusters/test/operations', headers={'X-CSRF-Token': csrf},
                json={'kind': 'switchover', 'params': {'candidate': 'n2'}, 'confirm': 'test'}).get_json()['data']
    tick(app)
    lc = [e for e in events(app) if e['kind'] == 'leader_change']
    assert lc[0]['level'] == 'info' and lc[0]['op_id'] == op['id']


def test_scheduled_switchover_is_info(app, sim):
    sim.scheduled = {'at': '2099-01-01T00:00:00+00:00'}
    tick(app)
    sim.scheduled = None
    sim._promote('n3')
    tick(app)
    lc = [e for e in events(app) if e['kind'] == 'leader_change']
    assert lc[0]['level'] == 'info' and 'по расписанию' in lc[0]['text']


def test_restart_does_not_repeat_open_alerts(cfg, sim, outbox):
    sim.down.add('n3')
    a1 = create_app(cfg, patroni_session_factory=sim.session, notify_session_factory=outbox.session)
    tick(a1, 2)
    a2 = create_app(cfg, patroni_session_factory=sim.session, notify_session_factory=outbox.session)
    tick(a2, 3)
    assert len(events(a2)) == 1


def test_notifications_routed_by_level_and_retried(app, sim, outbox):
    notifier = app.extensions['pc_notifier']
    notifier.clock = itertools.count(2e9, 1e5).__next__   # время быстро идёт: повтор уже «пора»
    sim.members['n1'].update(role='replica', state='stopped')   # нет лидера: critical
    outbox.fail = 1
    tick(app, 2)
    first = notifier.drain()                 # одна отправка упала
    assert notifier.drain() == 1             # повтор прошёл
    assert notifier.drain() == 0
    tg = [b for u, b, _ in outbox.sent if 'telegram' in u]
    hooks = [b for u, b, _ in outbox.sent if 'hooks' in u]
    assert len(tg) == 1 and 'нет лидера' in tg[0]['text']          # в telegram только critical
    assert first + 1 == len(outbox.sent) == len(hooks) + 1
    nl = next(b for b in hooks if b['code'] == 'no_leader')
    assert nl['event_id'] and nl['level'] == 'critical'


def test_warning_goes_only_to_channels_that_want_it(app, sim, outbox):
    sim.down.add('n3')                       # warning: telegram (critical) не получает
    tick(app, 2)
    app.extensions['pc_notifier'].drain()
    assert [u for u, _, _ in outbox.sent] == ['https://hooks.example/alert']


def test_token_not_leaked_in_errors(app, sim, outbox):
    notifier = app.extensions['pc_notifier']
    sim.members['n1'].update(role='replica', state='stopped')
    outbox.fail = 2
    tick(app, 2)
    notifier.drain()
    evs = app.extensions['pc_watcher'].store.events_list('test')
    errs = [n['last_error'] for e in evs for n in e['notifications'] if n['last_error']]
    assert errs and all(TG_TOKEN not in x for x in errs)


def test_gives_up_after_max_attempts(cfg, outbox, tmp_path):
    store = Store(cfg.db_path)
    store.event_add('test', 'info', 'info', 'test', 'x', channels=['hook'])
    n = Notifier(store, cfg.channels, session_factory=outbox.session, clock=itertools.count(2e9, 1e5).__next__)
    outbox.fail = 100
    for _ in range(10):
        n.drain()
    st = store.events_list('test')[0]['notifications'][0]
    assert st['status'] == 'failed' and st['attempts'] == 6


def test_events_api_and_notify_test(app, outbox):
    c = app.test_client()
    r = c.post('/api/auth/login', json={'username': 'viewer', 'password': PASSWORDS['viewer']})
    data = c.get('/api/clusters/test/events').get_json()['data']
    assert data['events'] == [] and [ch['name'] for ch in data['channels']] == ['hook', 'tg']
    csrf = r.get_json()['data']['csrf_token']
    assert c.post('/api/clusters/test/notify-test', headers={'X-CSRF-Token': csrf}).status_code == 403
    a = app.test_client()
    r = a.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORDS['admin']})
    assert a.post('/api/clusters/test/notify-test',
                  headers={'X-CSRF-Token': r.get_json()['data']['csrf_token']}).status_code == 202
    app.extensions['pc_notifier'].drain()
    assert len(outbox.sent) == 2             # тест уходит во все каналы независимо от уровня


def test_etcd_conditions():
    snap = {'warnings': [{'level': 'info', 'code': 'pending_restart', 'node': 'n2', 'text': 'x'}]}
    etcd = {'configured': True, 'quorum': False, 'split_leader_view': False,
            'endpoints': [{'endpoint': 'http://e1:2379', 'error': 'таймаут'},
                          {'endpoint': 'http://e2:2379', 'error': None, 'alarms': ['NOSPACE']}]}
    assert set(conditions_from(snap, etcd)) == {'etcd_quorum:', 'etcd_endpoint:http://e1:2379', 'etcd_alarm:NOSPACE'}


def test_message_format():
    e = {'kind': 'resolved', 'level': 'warning', 'cluster': 'prod', 'node': 'n3', 'text': 'устранено: лаг'}
    assert message(e) == '✅ [prod] n3: устранено: лаг'
    e['text'] = 'REST API недоступен: n3: HTTP 503'
    assert message(e) == '✅ [prod] REST API недоступен: n3: HTTP 503'


def test_bad_channel_config(monkeypatch):
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse(raw_config(notify=[{'type': 'telegram', 'bot_token': 'plain-token', 'chat_id': '1'}]))
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.parse(raw_config(notify=[{'type': 'webhook', 'url': 'ftp://x'}]))
