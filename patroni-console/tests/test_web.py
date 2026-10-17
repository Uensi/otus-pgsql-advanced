import pytest

from conftest import NODES, PASSWORDS, healthy_view, node_views
from fakes import FakeSession, patroni_routes
from patroni_console.web import create_app


@pytest.fixture
def app(config):
    routes = patroni_routes(NODES, healthy_view(), node_views())
    routes[('GET', NODES[0][1] + '/history')] = (200, [[1, 25165984, 'no recovery target specified', '2026-10-01T10:00:00+03:00', 'n2'],
                                                      [2, 33554592, 'no recovery target specified', '2026-10-02T10:00:00+03:00', 'n1']])
    a = create_app(config, patroni_session_factory=lambda: FakeSession(routes),
                   etcd_session_factory=lambda: FakeSession({}))
    a.testing = True
    return a


def login(client, user):
    r = client.post('/api/auth/login', json={'username': user, 'password': PASSWORDS[user]})
    assert r.status_code == 200, r.get_json()
    return r.get_json()['data']['csrf_token']


def test_api_requires_login(app):
    c = app.test_client()
    assert c.get('/api/clusters').status_code == 401
    assert c.get('/api/clusters/test/status').status_code == 401


def test_public_routes(app):
    c = app.test_client()
    assert c.get('/healthz').status_code == 200
    assert c.get('/').status_code == 200


def test_every_route_declares_role(app):
    for rule in app.url_map.iter_rules():
        view = app.view_functions[rule.endpoint]
        assert getattr(view, 'min_role', None) or rule.endpoint == 'static', rule


def test_wrong_password_and_throttle(app):
    c = app.test_client()
    for _ in range(10):
        r = c.post('/api/auth/login', json={'username': 'admin', 'password': 'nope'})
        assert r.status_code == 401
    r = c.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORDS['admin']})
    assert r.status_code == 429


def test_status_flow(app):
    c = app.test_client(); login(c, 'viewer')
    r = c.get('/api/clusters/test/status')
    d = r.get_json()['data']
    assert d['leader'] == 'n1' and d['health'] == 'ok'
    assert c.get('/api/clusters/nope/status').status_code == 404


def test_history_newest_first(app):
    c = app.test_client(); login(c, 'viewer')
    rows = c.get('/api/clusters/test/history').get_json()['data']
    assert [r['timeline'] for r in rows] == [2, 1] and rows[0]['new_leader'] == 'n1'


def test_etcd_unreachable_reported_not_crashing(app):
    c = app.test_client(); login(c, 'viewer')
    d = c.get('/api/clusters/test/etcd').get_json()['data']
    assert d['configured'] and not d['quorum']
    assert all(e['error'] for e in d['endpoints'])


def test_audit_admin_only(app):
    c = app.test_client(); login(c, 'oper')
    assert c.get('/api/audit').status_code == 403
    c2 = app.test_client(); login(c2, 'admin')
    rows = c2.get('/api/audit').get_json()['data']
    assert any(r['action'] == 'login' and r['username'] == 'oper' for r in rows)
    assert any(r['outcome'] == 'denied' and r['username'] == 'oper' for r in rows)


def test_logout_requires_csrf_and_kills_session(app):
    c = app.test_client(); token = login(c, 'viewer')
    assert c.post('/api/auth/logout', json={}).status_code == 403
    assert c.post('/api/auth/logout', json={}, headers={'X-CSRF-Token': token}).status_code == 200
    assert c.get('/api/clusters').status_code == 401


def test_role_change_in_config_kills_session(app, config):
    c = app.test_client(); login(c, 'oper')
    config.users['oper'].role = 'viewer'
    assert c.get('/api/clusters').status_code == 401


def test_security_headers(app):
    r = app.test_client().get('/')
    assert "default-src 'self'" in r.headers['Content-Security-Policy']
    assert r.headers['X-Frame-Options'] == 'DENY'


def test_favicon_served_without_login(app):
    c = app.test_client()
    for path in ('/favicon.ico', '/static/favicon.svg'):
        r = c.get(path)
        assert r.status_code == 200 and r.mimetype == 'image/svg+xml', path
