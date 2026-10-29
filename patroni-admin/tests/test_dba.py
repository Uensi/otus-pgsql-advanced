#!/usr/bin/env python3
"""Модульные тесты DBA-функций без живой БД: разбор журналов, время wal-g,
маршруты новых экранов (авторизация, отказ без лидера), LDAP-роли, демо-данные."""
import os
import sys
import tempfile
import types
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ['PATRONI_WEB_CONFIG'] = os.path.join(BASE, 'tests', 'config.test.yaml')  # тестовые пароли, без живых кластеров
os.environ.setdefault('PATRONI_CONSOLE_DATA', tempfile.mkdtemp(prefix='pc-test-'))
os.environ.setdefault('PATRONI_CONSOLE_NO_SAMPLERS', '1')
if 'psycopg2' not in sys.modules or not hasattr(sys.modules['psycopg2'], 'connect'):
    _stub = types.ModuleType('psycopg2')
    _stub.connect = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('pg unreachable'))
    _stub.extensions = types.ModuleType('psycopg2.extensions')
    sys.modules['psycopg2'] = _stub

import app as appmod  # noqa: E402
import logscan  # noqa: E402
import risk  # noqa: E402
import ldap_auth  # noqa: E402
import demo_dba  # noqa: E402

LOG = """2026-10-17 10:00:01.120 MSK [1201] LOG:  checkpoint starting: wal
2026-10-17 10:00:31.004 MSK [1201] LOG:  checkpoint complete: wrote 18211 buffers (0.9%); write=29.8 s, sync=0.41 s, total=30.2 s
2026-10-17 10:01:00.000 MSK [77] app@shop ERROR:  division by zero
2026-10-17 10:01:00.000 MSK [77] app@shop STATEMENT:  SELECT 1/0
2026-10-17 10:01:05.000 MSK [78] app@shop ERROR:  division by zero
2026-10-17 10:02:00.000 MSK [79] [unknown]@shop FATAL:  password authentication failed for user "mallory"
2026-10-17 10:03:00.000 MSK [80] app@shop LOG:  temporary file: path "base/pgsql_tmp/pgsql_tmp80.0", size 1048576
2026-10-17 10:03:00.000 MSK [80] app@shop STATEMENT:  SELECT * FROM t ORDER BY x
2026-10-17 10:04:00.000 MSK [81] app@shop LOG:  process 81 still waiting for ShareLock on transaction 900 after 1000.5 ms
2026-10-17 10:05:00.000 MSK [82] app@shop LOG:  duration: 1500.2 ms  statement: SELECT * FROM t WHERE id = 5
2026-10-17 10:05:01.000 MSK [82] app@shop LOG:  duration: 500.0 ms  statement: SELECT * FROM t WHERE id = 7
2026-10-17 10:06:00.000 MSK [90] LOG:  automatic vacuum of table "shop.public.t": index scans: 1 elapsed: 12.50 s
2026-10-17 10:07:00.000 MSK [91] app@shop ERROR:  deadlock detected
"""


class LogParse(unittest.TestCase):
    def setUp(self):
        self.r = logscan.parse(LOG)

    def test_errors_grouped_by_normalized_text(self):
        div = [e for e in self.r['errors'] if 'division by zero' in e['message']]
        self.assertEqual(len(div), 1)
        self.assertEqual(div[0]['count'], 2)

    def test_auth_failures_and_deadlocks(self):
        self.assertEqual(self.r['auth_failures'], [{'user': 'mallory', 'count': 1}])
        self.assertEqual(self.r['deadlocks'], 1)

    def test_checkpoints_temp_locks_autovacuum(self):
        self.assertEqual(self.r['checkpoints']['wal'], 1)
        self.assertEqual(self.r['checkpoints']['avg_s'], 30.2)
        self.assertEqual(self.r['temp_files']['count'], 1)
        self.assertIn('ORDER BY', self.r['temp_files']['top'][0]['query'])
        self.assertEqual(self.r['lock_waits'][0]['mode'], 'ShareLock')
        self.assertEqual(self.r['autovacuum'][0]['table'], 'shop.public.t')

    def test_slow_queries_normalized(self):
        sq = self.r['slow_queries']
        self.assertEqual(len(sq), 1)            # id = 5 и id = 7 — один и тот же запрос
        self.assertEqual(sq[0]['count'], 2)
        self.assertEqual(sq[0]['max_ms'], 1500.2)

    def test_period(self):
        self.assertEqual(self.r['first'], '2026-10-17 10:00:01')
        self.assertEqual(self.r['last'], '2026-10-17 10:07:00')


class TimeParsing(unittest.TestCase):
    def test_walg_nanoseconds_and_pg_offsets(self):
        a = risk._parse_ts('2026-10-17T02:00:05.123456789Z')
        b = risk._parse_ts('2026-10-17 05:00:05.123456+03')
        self.assertIsNotNone(a)
        self.assertAlmostEqual(a, b, places=3)
        self.assertIsNone(risk._parse_ts('не дата'))
        self.assertIsNone(risk._parse_ts(None))


class LdapRoles(unittest.TestCase):
    def test_highest_role_wins(self):
        self.assertEqual(ldap_auth._best_role(['viewer', 'admin', 'dba']), 'admin')
        self.assertIsNone(ldap_auth._best_role(['unknown']))

    def test_disabled_by_default(self):
        ldap_auth.configure({})
        self.assertFalse(ldap_auth.enabled())
        role, why = ldap_auth.authenticate('u', 'p')
        self.assertIsNone(role)


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = appmod.app.test_client()
        r = cls.c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        assert r.status_code == 200

    def test_new_endpoints_require_login(self):
        anon = appmod.app.test_client()
        for p in ('/api/risk/summary', '/api/ash', '/api/statements/report', '/api/perf/checkpoints', '/api/progress',
                  '/api/indexes/audit', '/api/security/audit', '/api/upgrade/check', '/api/logs/settings'):
            self.assertEqual(anon.get(p).status_code, 401, p)
        r = anon.get('/report/weekly')
        self.assertEqual(r.status_code, 302)

    def test_providers_public(self):
        r = appmod.app.test_client().get('/api/auth/providers')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['data']['ldap'])

    def test_risk_summary_without_db(self):
        d = self.c.get('/api/risk/summary').get_json()['data']
        self.assertIn(d['wraparound']['level'], ('unknown', 'ok', 'warn', 'bad'))
        self.assertIn('backups', d)

    def test_ash_empty(self):
        d = self.c.get('/api/ash?minutes=5').get_json()['data']
        self.assertEqual(d['samples'], 0)
        self.assertEqual(len(d['timeline']), 60)

    def test_validation(self):
        self.assertFalse(self.c.post('/api/risk/freeze', json={'db': 'x;drop', 'table': 't'}).get_json()['ok'])
        self.assertFalse(self.c.get('/api/indexes/audit?db=a%20b').get_json()['ok'])
        self.assertFalse(self.c.get('/api/upgrade/check?target=x').get_json()['ok'])
        self.assertFalse(self.c.post('/api/risk/config', json={'xid_warn_pct': 'abc'}).get_json()['ok'])

    def test_viewer_cannot_freeze(self):
        v = appmod.app.test_client()
        v.post('/api/auth/login', json={'user': 'viewer', 'password': 'test-pass'})
        self.assertEqual(v.post('/api/risk/freeze', json={}).status_code, 403)

    def test_risk_alert_rules_seeded(self):
        ids = {r['id'] for r in self.c.get('/api/alerts/rules').get_json()['data']}
        self.assertTrue({'risk_xid', 'risk_backup', 'risk_archive', 'risk_slots', 'risk_replay'} <= ids)


class NodeResolution(unittest.TestCase):
    """На проде имена узлов в config.yaml могут не совпадать с именами членов Patroni."""
    def test_find_by_config_name_member_name_and_host(self):
        first = appmod.NODES[0]
        self.assertIs(appmod.find_node(first['name']), first)
        appmod._MEMBER_HOST['patroni-real-name'] = first['host']
        try:
            self.assertIs(appmod.find_node('patroni-real-name'), first)
        finally:
            appmod._MEMBER_HOST.pop('patroni-real-name', None)
        self.assertIs(appmod.find_node(first['host']), first)
        self.assertIsNone(appmod.find_node('нет-такого'))
        self.assertIsNone(appmod.find_node(''))

    def test_restart_resolves_patroni_member_name(self):
        c = appmod.app.test_client()
        c.post('/api/auth/login', json={'user': 'admin', 'password': 'test-pass'})
        first = appmod.NODES[0]
        appmod._MEMBER_HOST['member-x'] = first['host']
        try:
            j = c.post('/api/ops/reload', json={'node': 'member-x'}).get_json()
            self.assertNotEqual(j.get('error'), 'Нода не найдена')
        finally:
            appmod._MEMBER_HOST.pop('member-x', None)

    def test_human_errors(self):
        h = appmod._human_net_error
        self.assertIn('таймаут', h('connection to server at "x", port 5432 failed: timeout expired'))
        self.assertIn('неверный пароль', h('FATAL:  password authentication failed for user "postgres"'))
        self.assertIn('pg_hba', h('FATAL:  no pg_hba.conf entry for host "10.1.1.1", user "postgres"'))
        self.assertIn('отклонено', h('Connection refused'))

    def test_pg_error_is_remembered_and_shown(self):
        self.assertIsNone(appmod.pg_conn('127.0.0.1', port=1))
        self.assertTrue(appmod.pg_error('127.0.0.1', port=1))
        self.assertIn('Не удалось подключиться к PostgreSQL', appmod.pg_fail_msg())

    def test_connectivity_report(self):
        d = appmod.connectivity()
        self.assertEqual(len(d['nodes']), len(appmod.NODES))
        self.assertEqual(d['level'], 'bad')            # тестовые узлы недоступны
        self.assertTrue(any('REST API Patroni' in i['text'] for i in d['issues']))


class DemoData(unittest.TestCase):
    def test_demo_log_parses(self):
        r = demo_dba.log_report('pg-node1')
        self.assertGreater(r['deadlocks'], 0)
        self.assertTrue(r['auth_failures'])

    def test_demo_ash_shape(self):
        d = demo_dba.ash(15)
        self.assertEqual(len(d['timeline']), 60)
        self.assertTrue(d['top_events'])


if __name__ == '__main__':
    unittest.main()
