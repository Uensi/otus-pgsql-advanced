#!/usr/bin/env python3
"""Smoke-тесты Patroni Console: запуск без PostgreSQL (psycopg2 замокан),
проверка маршрутов, авторизации, RBAC и валидации ввода.
Запуск:  python -m unittest discover -s tests -p 'test_*.py'  (или python tests/test_app.py)"""
import os
import sys
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ['PATRONI_WEB_CONFIG'] = os.path.join(BASE, 'tests', 'config.test.yaml')  # тестовые пароли, без живых кластеров
import tempfile
os.environ.setdefault('PATRONI_CONSOLE_DATA', tempfile.mkdtemp(prefix='pc-test-'))  # тесты не трогают рабочие данные
os.environ.setdefault('PATRONI_CONSOLE_NO_SAMPLERS', '1')


class PGStub:
    """Заглушка psycopg2: подключения к БД в тестах недоступны."""
    def cursor(self): raise RuntimeError('pg недоступен в smoke-тестах')
    def close(self): pass


sys.modules.setdefault('psycopg2', type(sys)('psycopg2_stub'))
# подмена только модуля: приложение должно импортироваться без бинарного psycopg2
import types
_stub_mod = types.ModuleType('psycopg2')
_stub_mod.connect = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('pg unreachable'))
_stub_mod.extensions = types.ModuleType('psycopg2.extensions')
_stub_mod.extensions.ISOLATION_LEVEL_AUTOCOMMIT = 'AUTOCOMMIT'
sys.modules['psycopg2'] = _stub_mod

import app as appmod          # noqa: E402


class PatroniConsoleSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = appmod.app.test_client()

    def test_index_returns_html(self):
        r = self.app.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Patroni Console', r.data)

    def test_unauthorized_json(self):
        c = appmod.app.test_client()
        r = c.get('/api/audit')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json().get('ok'), False)
        self.assertEqual(r.get_json().get('error'), 'unauthorized')

    def test_login_and_audit(self):
        r = self.app.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))
        r2 = self.app.get('/api/audit')
        self.assertEqual(r2.status_code, 200)
        self.assertIsInstance(r2.get_json().get('data'), list)

    def test_login_bad_password(self):
        r = self.app.post('/api/auth/login', json={'user': 'admin', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)

    def test_viewer_rbac(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'viewer', 'password': 'test-pass'})
        r = c.post('/api/pause', json={'pause': True})
        self.assertEqual(r.status_code, 403)
        r2 = c.post('/api/dba/vacuum', json={'table': 'x'})
        self.assertEqual(r2.status_code, 403)

    def test_vacuum_injection_rejected(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        r = c.post('/api/dba/vacuum', json={'table': 'users; DROP TABLE users;--'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('недопустимый идентификатор', r.get_json().get('error', ''))

    def test_drop_index_injection_rejected(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        r = c.post('/api/dba/drop-index', json={'index': 'ix; DELETE FROM users;--'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('недопустимый идентификатор', r.get_json().get('error', ''))

    def test_delay_hours_validation(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'dba', 'password': 'test-pass'})
        r = c.post('/api/dba/delay-replica', json={'node': 'patroni-cluster1', 'hours': 'abc'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('hours', r.get_json().get('error', ''))

    def test_grant_without_role_validation(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        r = c.post('/api/admin/roles', json={'action': 'grant', 'name': 'someuser'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('роль не указана', r.get_json().get('error', ''))

    def test_metrics_history_garbage_param(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        r = c.get('/api/metrics/history?points=abc')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))

    def test_reports_generate_garbage_range(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        r = c.get('/api/reports/generate?from=abc')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))

    def test_metrics_prometheus(self):
        r = self.app.get('/metrics')
        self.assertEqual(r.status_code, 200)
        body = r.data.decode('utf-8', 'replace')
        # стабильные части формата присутствуют всегда (агентов может не быть)
        self.assertIn('# HELP patroni_web_cluster_up', body)
        self.assertIn('patroni_web_cluster_up 1', body)
        self.assertIn('# TYPE patroni_console_repl_lag_bytes gauge', body)
        self.assertIn('text/plain', r.content_type)

    def test_routes_registered_without_agent(self):
        """Ключевые модули зарегистрированы даже если agent_api не импортирован."""
        from werkzeug.routing import Map
        rules = {str(r) for r in appmod.app.url_map.iter_rules()}
        for need in ('/api/auth/login', '/api/audit', '/api/alerts/rules',
                     '/api/reports/snapshots', '/api/repl/slots', '/api/admin/nodes'):
            self.assertTrue(any(r.startswith(need) for r in rules), f'missing {need}')

    def test_show_param_whitelist(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        # недопустимый параметр отклоняется ещё до подключения к БД
        r = c.get('/api/querylab/show?param=server_version&node=patroni-cluster1')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('не поддерживается', r.get_json().get('error', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)