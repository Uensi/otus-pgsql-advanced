#!/usr/bin/env python3
"""Тесты платформенных модулей (Этапы 1–6): хосты, дампы, обслуживание, шаблоны,
уведомления, DCS, AI-ассистент. PostgreSQL недоступен — psycopg2 стаб (как в test_app).
Запуск: python -X utf8 -m unittest discover -s tests -p 'test_*.py'"""
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

# --- стаб psycopg2 до импорта app ---
_stub = types.ModuleType('psycopg2')
_stub.connect = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('pg unreachable'))
_stub.extensions = types.ModuleType('psycopg2.extensions')
_stub.extensions.ISOLATION_LEVEL_AUTOCOMMIT = 'AUTOCOMMIT'
sys.modules['psycopg2'] = _stub

import app as appmod          # noqa: E402


def login(client, role='admin'):
    pwd = {'admin': 'test-pass', 'dba': 'test-pass', 'viewer': 'test-pass'}[role]
    return client.post('/api/auth/login', json={'user': role, 'password': pwd})


class HostsTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_hosts_list_ok(self):
        r = self.c.get('/api/hosts')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))
        self.assertIsInstance(r.get_json().get('data'), list)

    def test_disk_history_ok(self):
        r = self.c.get('/api/hosts/disk-history')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))

    def test_service_admin_only(self):
        c = appmod.app.test_client()
        login(c, 'dba')
        r = c.post('/api/hosts/service', json={'node': 'x', 'service': 'patroni', 'action': 'restart'})
        self.assertEqual(r.status_code, 403)


class StandaloneTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_standalone_list_ok(self):
        r = self.c.get('/api/hosts/standalone')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))


class DumpsTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_dump_bad_action(self):
        r = self.c.post('/api/dumps', json={'node': 'x', 'action': 'hack'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('недопустимое действие', r.get_json().get('error', ''))

    def test_dump_schedule_roundtrip(self):
        r = self.c.post('/api/dump-schedule', json={'enabled': True, 'time': '04:00', 'db': 'app', 'mode': 'custom', 'rotate_days': 5})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))
        r2 = self.c.get('/api/dump-schedule')
        j = r2.get_json()
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(j.get('ok'))
        self.assertEqual(j['data']['time'], '04:00')
        self.assertEqual(j['data']['db'], 'app')
        # сбросим, чтобы не мешать другим тестам
        self.c.post('/api/dump-schedule', json={'enabled': False})
        self.c.post('/api/backup-schedule', json={'enabled': False})

    def test_dump_schedule_admin_only(self):
        c = appmod.app.test_client()
        login(c, 'dba')
        r = c.post('/api/dump-schedule', json={'enabled': True})
        self.assertEqual(r.status_code, 403)

    def test_dump_bad_dbname(self):
        # агент офлайн, но валидация имени идёт после — здесь проверим через офлайн-ветку
        r = self.c.post('/api/dumps', json={'node': 'off', 'action': 'create', 'db': 'x; DROP'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))

    def test_dump_nfs_admin_only(self):
        c = appmod.app.test_client()
        login(c, 'dba')
        r = c.post('/api/dumps', json={'node': 'x', 'action': 'nfs_mount', 'share': 'a:/b'})
        self.assertEqual(r.status_code, 403)

    def test_dump_report_bad_email(self):
        c = appmod.app.test_client()
        login(c, 'admin')
        r = c.post('/api/dumps', json={'node': 'x', 'action': 'report', 'to': 'not-an-email'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('e-mail', r.get_json().get('error', ''))

    def test_cmd_result_bad_id(self):
        r = self.c.get('/api/ops/cmd-result?node=x&id=bad')
        self.assertEqual(r.status_code, 400)

    def test_clone_inspect_whitelisted(self):
        # агент офлайн -> не "недопустимое действие", значит действие разрешено
        r = self.c.post('/api/backups', json={'node': 'x', 'action': 'clone_inspect', 'path': '/var/lib/pgsql/restore/x'})
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        self.assertFalse(j.get('ok'))
        self.assertNotIn('недопустимое действие', j.get('error', ''))


class RepackTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_repack_bad_table(self):
        r = self.c.post('/api/ops/repack', json={'db': 'postgres', 'schema': 'public', 'table': 'x; DROP'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('некорректная таблица', r.get_json().get('error', ''))

    def test_repack_bad_method(self):
        r = self.c.post('/api/ops/repack', json={'db': 'postgres', 'schema': 'public', 'table': 'acc', 'method': 'nope'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))


class TemplatesTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_templates_list(self):
        r = self.c.get('/api/templates')
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(j.get('ok'))
        keys = {t['key'] for t in j['data']['templates']}
        self.assertTrue({'1c-high', 'oltp', 'olap', 'safety'} <= keys)

    def test_apply_requires_confirm(self):
        r = self.c.post('/api/templates/apply', json={'name': 'oltp'})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('подтвердите', r.get_json().get('error', ''))

    def test_apply_unknown(self):
        r = self.c.post('/api/templates/apply', json={'name': 'nope', 'confirm': True})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))

    def test_apply_admin_only(self):
        c = appmod.app.test_client()
        login(c, 'dba')
        r = c.post('/api/templates/apply', json={'name': 'oltp', 'confirm': True})
        self.assertEqual(r.status_code, 403)

    def test_history_ok(self):
        r = self.c.get('/api/templates/history')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_config_roundtrip(self):
        r = self.c.post('/api/notify/config', json={
            'email': {'enabled': True, 'to': 'dba@example.com', 'method': 'msmtp'},
            'telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
            'routes': {'critical': ['email'], 'warning': [], 'info': []}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))
        r2 = self.c.get('/api/notify/config')
        j = r2.get_json()
        self.assertEqual(j['data']['email']['to'], 'dba@example.com')
        self.assertEqual(j['data']['routes']['critical'], ['email'])

    def test_admin_only(self):
        c = appmod.app.test_client()
        login(c, 'dba')
        r = c.post('/api/notify/config', json={'email': {}})
        self.assertEqual(r.status_code, 403)

    def test_log_ok(self):
        r = self.c.get('/api/notify/log')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))


class DcsTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_health_ok(self):
        r = self.c.get('/api/dcs/health')
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(j.get('ok'))
        self.assertIn('etcd', j['data'])
        self.assertIn('consul', j['data'])

    def test_config_bad_url(self):
        r = self.c.post('/api/dcs/config', json={'etcd': ['not-http']})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json().get('ok'))

    def test_config_admin_only(self):
        c = appmod.app.test_client()
        login(c, 'dba')
        r = c.post('/api/dcs/config', json={'etcd': []})
        self.assertEqual(r.status_code, 403)

    def test_discover_admin_only(self):
        c = appmod.app.test_client()
        login(c, 'dba')
        r = c.post('/api/dcs/discover', json={})
        self.assertEqual(r.status_code, 403)

    def test_discover_ok(self):
        c = appmod.app.test_client()
        login(c, 'admin')
        r = c.post('/api/dcs/discover', json={})
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(j.get('ok'))
        self.assertIn('found', j.get('data', {}))

    def test_patroni_config_get(self):
        r = self.c.get('/api/dcs/patroni-config')
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        # не падает: либо ok с данными, либо ok=False с error (нет ключа в DCS)
        self.assertIn('ok', j)

    def test_patroni_config_bad_body(self):
        r = self.c.post('/api/dcs/patroni-config', json={'config': 'это не yaml: [object'})
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(j.get('ok'))
        self.assertIn('error', j)

    def test_patroni_config_admin_only(self):
        c = appmod.app.test_client()
        login(c, 'dba')
        r = c.post('/api/dcs/patroni-config', json={'config': 'postgresql:\n  parameters:\n    max_connections: 100\n'})
        self.assertEqual(r.status_code, 403)


class AssistantTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_chat_topic(self):
        r = self.c.post('/api/assistant/chat', json={'message': 'почему вырос лаг репликации?'})
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(j.get('ok'))
        self.assertEqual(j['data']['topic'], 'lag')
        self.assertIn('лаг', j['data']['reply'])

    def test_chat_config_topic(self):
        r = self.c.post('/api/assistant/chat', json={'message': 'как настроить shared_buffers?'})
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(j['data']['topic'], 'config')

    def test_chat_unknown(self):
        r = self.c.post('/api/assistant/chat', json={'message': 'привет'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))

    def test_explain_plan(self):
        plan = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "t", "Rows": 1000, "Plans": []}}]
        r = self.c.post('/api/assistant/explain', json={'query': 'select * from t', 'plan': plan})
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(j.get('ok'))
        self.assertIn('Seq Scan', j.get('text', ''))

    def test_chat_viewer_forbidden(self):
        c = appmod.app.test_client()
        login(c, 'viewer')
        r = c.post('/api/assistant/chat', json={'message': 'привет'})
        self.assertEqual(r.status_code, 403)

    def test_history_ok(self):
        r = self.c.get('/api/assistant/history')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('ok'))

    def test_diagnose_ok(self):
        r = self.c.post('/api/assistant/diagnose', json={})
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(j.get('ok'))
        d = j.get('data', {})
        self.assertIn('summary', d)
        self.assertIn('issues', d)
        self.assertIn('context', d)


class MetricsExportTest(unittest.TestCase):
    def setUp(self):
        self.c = appmod.app.test_client()
        login(self.c, 'admin')

    def test_zabbix_export(self):
        r = self.c.get('/api/metrics/export?format=zabbix')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/plain', r.content_type)

    def test_json_export(self):
        r = self.c.get('/api/metrics/export?format=json')
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(j.get('ok'))
        self.assertIsInstance(j.get('data'), list)

    def test_prometheus_has_cluster(self):
        r = self.c.get('/metrics')
        self.assertEqual(r.status_code, 200)
        body = r.data.decode('utf-8', 'replace')
        self.assertIn('patroni_web_cluster_up 1', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)