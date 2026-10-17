#!/usr/bin/env python3
"""Тесты защиты: авторизация агентского статуса, CSRF, служебные проверки, заголовки."""
import os
import sys
import types
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ['PATRONI_WEB_CONFIG'] = os.path.join(BASE, 'tests', 'config.test.yaml')  # тестовые пароли, без живых кластеров
import tempfile
os.environ.setdefault('PATRONI_CONSOLE_DATA', tempfile.mkdtemp(prefix='pc-test-'))  # тесты не трогают рабочие данные
os.environ.setdefault('PATRONI_CONSOLE_NO_SAMPLERS', '1')
if 'psycopg2' not in sys.modules or not hasattr(sys.modules['psycopg2'], 'connect'):
    _stub = types.ModuleType('psycopg2')
    _stub.connect = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('pg unreachable'))
    _stub.extensions = types.ModuleType('psycopg2.extensions')
    sys.modules['psycopg2'] = _stub

import app as appmod  # noqa: E402


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()

    def login(self):
        r = self.c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        self.assertEqual(r.status_code, 200)

    def test_agent_status_requires_session(self):
        self.assertEqual(self.c.get('/api/agent/status').status_code, 401)
        self.login()
        self.assertEqual(self.c.get('/api/agent/status').status_code, 200)

    def test_agent_key_endpoints_stay_public_but_check_key(self):
        r = self.c.post('/api/agent/register', json={'key': 'wrong'})
        self.assertEqual(r.status_code, 403)

    def test_cross_site_post_rejected(self):
        self.login()
        r = self.c.post('/api/pause', json={'pause': True}, headers={'Origin': 'http://evil.example'})
        self.assertEqual(r.status_code, 403)
        r = self.c.post('/api/pause', json={'pause': True}, headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(r.status_code, 403)

    def test_same_origin_post_allowed_through_csrf_check(self):
        self.login()
        # выход из сессии — изменяющий запрос без побочных эффектов на данные
        r = self.c.post('/api/auth/logout', json={}, headers={'Origin': 'http://localhost', 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(r.status_code, 200)

    def test_healthz_public(self):
        r = self.c.get('/healthz')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])

    def test_security_headers(self):
        r = self.c.get('/')
        self.assertEqual(r.headers.get('X-Frame-Options'), 'DENY')
        self.assertEqual(r.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertIn("frame-ancestors 'none'", r.headers.get('Content-Security-Policy', ''))
        self.assertEqual(self.c.get('/api/auth/me').headers.get('Cache-Control'), 'no-store')

    def test_index_uses_versioned_local_assets(self):
        html = self.c.get('/').get_data(as_text=True)
        self.assertIn('/static/ui-kit.js?v=', html)
        self.assertNotIn('cdn.jsdelivr.net', html)


if __name__ == '__main__':
    unittest.main()
