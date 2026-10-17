#!/usr/bin/env python3
"""Демо-данные для экранов, которым нужна живая БД: схема/ERD, анализатор запросов,
SQL-консоль, соединения, параметры и шаблоны, админ, отчёты PWR, история переключений."""
import time

GB = 1 << 30

PARAMS = {'shared_buffers': '16GB', 'effective_cache_size': '48GB', 'work_mem': '32MB', 'maintenance_work_mem': '2GB',
          'max_connections': '300', 'max_wal_size': '1GB', 'min_wal_size': '80MB', 'checkpoint_completion_target': '0.9',
          'random_page_cost': '4', 'effective_io_concurrency': '1', 'max_worker_processes': '16',
          'max_parallel_workers_per_gather': '2', 'max_parallel_workers': '8', 'wal_buffers': '16MB',
          'autovacuum_max_workers': '3', 'autovacuum_naptime': '1min', 'autovacuum_vacuum_scale_factor': '0.2',
          'default_statistics_target': '100', 'jit': 'on', 'huge_pages': 'try'}


def _col(name, typ, nullable=False):
    return {'name': name, 'type': typ, 'max_length': None, 'nullable': nullable, 'default': None}


def schema(db):
    t = [
        ('public', 'customers', 2 * GB, [_col('id', 'bigint'), _col('name', 'text'), _col('email', 'text'), _col('created_at', 'timestamp with time zone')], ['id']),
        ('public', 'accounts', 6 * GB, [_col('id', 'bigint'), _col('customer_id', 'bigint'), _col('currency', 'character(3)'), _col('balance', 'numeric')], ['id']),
        ('public', 'payments', 22 * GB, [_col('id', 'bigint'), _col('account_id', 'bigint'), _col('amount', 'numeric'), _col('ext_id', 'text'), _col('created_at', 'timestamp with time zone')], ['id']),
        ('public', 'payment_items', 9 * GB, [_col('id', 'bigint'), _col('payment_id', 'bigint'), _col('sku', 'text'), _col('qty', 'integer')], ['id']),
        ('public', 'tariffs', 40 << 20, [_col('id', 'integer'), _col('code', 'text'), _col('rate', 'numeric')], ['id']),
        ('public', 'audit_log', 31 * GB, [_col('id', 'bigint'), _col('actor', 'text'), _col('action', 'text'), _col('payload', 'jsonb'), _col('at', 'timestamp with time zone')], ['id']),
    ]
    tables = [{'name': n, 'schema': s, 'full_name': f'{s}.{n}', 'size': sz, 'columns': cols, 'primary_key': pk,
               'foreign_keys': [], 'indexes': []} for s, n, sz, cols, pk in t]
    rels = [('public.accounts', 'public.customers', 'customer_id'), ('public.payments', 'public.accounts', 'account_id'),
            ('public.payment_items', 'public.payments', 'payment_id')]
    return {'ok': True, 'data': {'db': db, 'tables': tables,
                                 'relationships': [{'source': a, 'target': b, 'from_column': c, 'to_column': 'id', 'constraint': f'{a.split(".")[1]}_{c}_fkey'} for a, b, c in rels]}}


SLOW = [
    ("SELECT p.* FROM payments p JOIN accounts a ON a.id = p.account_id WHERE a.customer_id = $1 ORDER BY p.created_at DESC LIMIT $2", 182_000, 41.2, 980.4, 97.1),
    ("SELECT sum(amount) FROM payments WHERE created_at > now() - interval '1 day' GROUP BY account_id", 240, 1250.4, 4100.0, 62.3),
    ("UPDATE accounts SET balance = balance + $1 WHERE id = $2", 920_000, 3.9, 210.5, 99.8),
    ("SELECT * FROM audit_log WHERE payload ->> 'order_id' = $1", 3_900, 388.0, 1730.2, 71.5),
]


def slow():
    return {'ok': True, 'data': [{'query': q, 'calls': c, 'mean_time': m, 'max_time': mx, 'cache_hit': ch, 'system': False,
                                  'label': 'пользовательский', 'category': 'user'} for q, c, m, mx, ch in SLOW]}


def current():
    now = time.time()
    return {'ok': True, 'data': [
        {'pid': 48211, 'usename': 'billing_app', 'application_name': 'billing-api', 'client': '10.0.20.15', 'state': 'active',
         'query': SLOW[1][0], 'duration_ms': 3410.2 + (now % 60) * 100},
        {'pid': 48390, 'usename': 'report_ro', 'application_name': 'psql', 'client': '10.0.30.4', 'state': 'idle in transaction',
         'query': 'SELECT count(*) FROM payments WHERE created_at > $1', 'duration_ms': 95_400.0}]}


def analyze(query):
    import app as A
    plan = [{'Plan': {'Node Type': 'Limit', 'Plan Rows': 50, 'Total Cost': 182340.5, 'Plans': [
        {'Node Type': 'Sort', 'Sort Key': ['p.created_at DESC'], 'Plan Rows': 1210, 'Total Cost': 182337.4, 'Plans': [
            {'Node Type': 'Hash Join', 'Join Type': 'Inner', 'Plan Rows': 1210, 'Total Cost': 182001.0, 'Plans': [
                {'Node Type': 'Seq Scan', 'Relation Name': 'payments', 'Alias': 'p', 'Plan Rows': 41_200_000, 'Total Cost': 151000.0,
                 'Filter': '(amount > 0)'},
                {'Node Type': 'Hash', 'Plan Rows': 12, 'Plans': [
                    {'Node Type': 'Index Scan', 'Relation Name': 'accounts', 'Index Name': 'accounts_customer_idx', 'Alias': 'a', 'Plan Rows': 12}]}]}]}]}}]
    issues = []
    try:
        A._plan_node(plan[0]['Plan'], issues)
    except Exception:
        pass
    if not issues:
        issues = [{'type': 'Seq Scan', 'severity': 'high', 'message': 'Полное чтение payments (41 млн строк) ради ~1200 строк результата',
                   'suggestion': 'Индекс по (account_id, created_at DESC)'}]
    recs = [{'type': 'index', 'table': 'payments', 'columns': ['account_id', 'created_at'],
             'reason': 'Соединение по account_id и сортировка по created_at без подходящего индекса',
             'sql': 'CREATE INDEX CONCURRENTLY payments_account_created_idx ON payments (account_id, created_at DESC);'}]
    rwr = []
    try:
        rwr = A._rewrite(query)
    except Exception:
        pass
    summary = [{'sev': 'high', 'text': 'Создайте индекс payments(account_id, created_at DESC): уйдёт полное чтение 22 ГБ и сортировка'},
               {'sev': 'medium', 'text': 'Выбирайте нужные колонки вместо p.* — меньше чтения TOAST и сетевого трафика'}]
    return {'ok': True, 'data': {'query': query, 'plan': plan, 'plan_error': None,
                                 'classification': {'system': False, 'label': 'пользовательский запрос', 'category': 'user',
                                                    'explanation': 'Запрос приложения к пользовательским таблицам — оптимизация имеет смысл.'},
                                 'recommendations_summary': summary, 'plan_issues': issues, 'index_recommendations': recs,
                                 'rewrite_suggestions': rwr or [{'problem': 'SELECT p.*', 'suggestion': 'перечислите нужные колонки'}],
                                 'tables': ['payments', 'accounts'], 'table_stats': {}, 'tables_count': 2, 'issues_count': len(issues),
                                 'indexes_count': 1, 'rewrites_count': 1}}


def exec_sql(sql):
    s = (sql or '').strip().lower()
    if not s.startswith(('select', 'with', 'show', 'explain')):
        return {'ok': True, 'data': {'message': 'демо-режим: изменяющие команды не выполняются', 'execution_time': 0.4}}
    return {'ok': True, 'data': {'columns': ['datname', 'size', 'connections', 'xact_commit'], 'rows': [
        {'datname': 'erp_1c', 'size': '412 GB', 'connections': 61, 'xact_commit': 91216375},
        {'datname': 'billing', 'size': '96 GB', 'connections': 24, 'xact_commit': 40114213},
        {'datname': 'reports', 'size': '31 GB', 'connections': 6, 'xact_commit': 1204590}], 'row_count': 3, 'execution_time': 3.82}}


def connections(nodes):
    by = [{'usename': 'usr1c', 'application_name': '1CV8C', 'state': 'active', 'cnt': 14},
          {'usename': 'usr1c', 'application_name': '1CV8C', 'state': 'idle', 'cnt': 47},
          {'usename': 'billing_app', 'application_name': 'billing-api', 'state': 'idle', 'cnt': 22},
          {'usename': 'report_ro', 'application_name': 'psql', 'state': 'idle in transaction', 'cnt': 1}]
    bn = [{'name': n, 'ok': True, 'total': 84 if i == 0 else 6, 'max': 300, 'by_user': by if i == 0 else by[2:3]} for i, n in enumerate(nodes)]
    return {'ok': True, 'data': {'by_node': bn, 'total_all': sum(x['total'] for x in bn), 'max_connections': 300}}


def config_recs():
    return {'ok': True, 'data': {'current': PARAMS}}


def templates():
    import templates as T
    return {'ok': True, 'data': {'templates': [{'key': k, 'name': t['name'], 'description': t['description'], 'params': t['params']}
                                               for k, t in T.TEMPLATES.items()],
                                 'current': {'postgresql': {'parameters': PARAMS}}}}


def admin_extensions():
    return {'ok': True, 'data': {'installed': [{'extname': 'pg_stat_statements', 'extversion': '1.10', 'schema': 'public'},
                                               {'extname': 'pg_repack', 'extversion': '1.5.0', 'schema': 'public'},
                                               {'extname': 'pgcrypto', 'extversion': '1.3', 'schema': 'public'}],
                                 'available': [{'name': 'pg_trgm', 'default_version': '1.6', 'comment': 'триграммы для LIKE/ILIKE-поиска', 'installed_version': None},
                                               {'name': 'pg_buffercache', 'default_version': '1.4', 'comment': 'содержимое shared_buffers', 'installed_version': None},
                                               {'name': 'amcheck', 'default_version': '1.3', 'comment': 'проверка целостности индексов', 'installed_version': None}]}}


def admin_roles():
    def r(n, su=False, cdb=False, cr=False, rep=False, login=True, member=()):
        return {'rolname': n, 'rolsuper': su, 'rolcreatedb': cdb, 'rolcreaterole': cr, 'rolreplication': rep,
                'rolcanlogin': login, 'rolconnlimit': -1, 'member_of': list(member)}
    return {'ok': True, 'data': [r('postgres', su=True, cdb=True, cr=True, rep=True), r('replicator', rep=True),
                                 r('usr1c', cdb=True), r('billing_app', member=['app_rw']), r('report_ro', member=['app_ro']),
                                 r('app_rw', login=False), r('app_ro', login=False)]}


def admin_ts():
    return {'ok': True, 'data': [{'spcname': 'pg_default', 'location': '', 'size': 539 * GB},
                                 {'spcname': 'fast_nvme', 'location': '/mnt/nvme/pg_ts', 'size': 41 * GB}]}


def config_diff():
    return {'ok': True, 'data': {'diff': [{'name': 'shared_buffers', 'a': '16GB', 'source_a': 'configuration file', 'b': '12GB', 'source_b': 'configuration file'},
                                          {'name': 'work_mem', 'a': '32MB', 'source_a': 'configuration file', 'b': '64MB', 'source_b': 'override'}]}}


def patroni_diff():
    return {'ok': True, 'data': {'same': 41, 'diff': [{'path': 'postgresql.parameters.shared_buffers', 'a': '16GB', 'b': '12GB'},
                                                      {'path': 'tags.nofailover', 'a': None, 'b': True, 'only_b': True}]}}


def report_snapshots():
    now = time.time()
    return {'ok': True, 'data': [{'ts': now - (24 - i) * 3600, 'conn': 80 + i} for i in range(25)]}


def report_generate(frm, to):
    return {'ok': True, 'data': {'from': frm, 'to': to, 'seconds': to - frm, 'snapshots': 1440, 'tps': 612.4, 'rollback_ratio': 0.31,
                                 'cache_hit': 98.6, 'rates': {'ins': 410.2, 'upd': 288.7, 'del': 21.4, 'ret': 182344.0},
                                 'temp_files': 214, 'temp_bytes': 61 * GB, 'deadlocks': 2, 'conn_start': 81, 'conn_end': 104,
                                 'db_growth': [{'name': 'erp_1c', 'delta': int(1.47 * GB), 'now': 412 * GB}, {'name': 'billing', 'delta': int(0.24 * GB), 'now': 96 * GB}],
                                 'top_queries': [{'query': q[:120], 'calls': c, 'total': round(c * m, 1), 'mean': m} for q, c, m, _, _ in SLOW]}}


def ops_history():
    return {'ok': True, 'data': [{'timeline': 5, 'lsn': '4F/1A000000', 'time': '2026-08-14 02:11:07', 'new_leader': 'pg-node2', 'reason': 'плановый switchover (обновление ОС)'},
                                 {'timeline': 6, 'lsn': '53/0C000000', 'time': '2026-08-14 02:40:52', 'new_leader': 'pg-node1', 'reason': 'возврат лидера'},
                                 {'timeline': 7, 'lsn': '57/2E000000', 'time': '2026-09-29 11:03:20', 'new_leader': 'pg-node1', 'reason': 'failover: потеря связи с DCS на pg-node1, авто'}]}


DCS_CONFIG = """loop_wait: 10
ttl: 30
retry_timeout: 10
maximum_lag_on_failover: 1048576
synchronous_mode: false
postgresql:
  use_pg_rewind: true
  parameters:
    max_connections: 300
    shared_buffers: 16GB
    wal_level: replica
    archive_mode: 'on'
    archive_command: wal-g wal-push %p
    max_wal_size: 1GB
"""


def dcs_config():
    return {'ok': True, 'data': {'backend': 'etcd3', 'url': 'http://10.0.10.21:2379', 'key': '/service/demo-cluster/config', 'config': DCS_CONFIG}}


AI_TIPS = [
    'max_wal_size = 1GB мало для текущей записи (≈530 КБ/с): 62% контрольных точек вынужденные. Поднимите до 8GB.',
    'wal_compression = off: включите lz4 — FPI составляют 41% WAL, объём архива и репликации заметно уменьшится.',
    'autovacuum_vacuum_scale_factor = 0.2 для таблиц на 100+ млн строк слишком велик: задайте 0.02 точечно для _accumrg1234 и payments.',
    'random_page_cost = 4 рассчитан на HDD; на SSD/NVMe поставьте 1.1 — планировщик чаще выберет индексы.',
    'effective_io_concurrency = 1: для SSD 200, для NVMe 256–1000.',
]


def ai_reply(msg):
    m = msg.lower()
    if 'лаг' in m or 'реплик' in m:
        topic, text = 'replication', (
            'Сейчас отстаёт pg-node3 (десятки МБ WAL, replay ~6 с), pg-node2 в норме.\n'
            'Типичные причины: 1) медленный диск реплики — посмотрите iowait на экране «Хосты» (у pg-node3 диск занят на 83%);\n'
            '2) долгие запросы на реплике с hot_standby_feedback — экран «Активные сессии», узел pg-node3;\n'
            '3) всплеск записи на лидере — экран «Производительность» → «Контрольные точки и WAL».\n'
            'Если лаг растёт и не догоняется — не делайте failover на эту реплику: потеряете хвост транзакций.')
    elif 'bloat' in m or 'вакуум' in m or 'vacuum' in m:
        topic, text = 'vacuum', (
            'Раздуты _accumrg1234 (38% мёртвых строк, 18 ГБ) и _inforg5521 (21%).\n'
            'Автовакуум не успевает: scale_factor 0.2 для таблицы на 3 млн строк = 600 тыс. мёртвых строк до запуска.\n'
            'Рекомендую: ALTER TABLE _accumrg1234 SET (autovacuum_vacuum_scale_factor = 0.02); и pg_repack в окно обслуживания (экран «Обслуживание»).')
    elif 'индекс' in m or 'запрос' in m:
        topic, text = 'queries', (
            'Больше всего времени уходит на SELECT по _AccumRg1234 (37% общего времени, IO: DataFileRead).\n'
            'Регрессия: агрегат по payments за сутки стал в 3 раза медленнее (410 → 1250 мс) и пишет временные файлы — '
            'поднимите work_mem для роли report_ro или добавьте индекс payments(created_at) INCLUDE (account_id, amount).')
    else:
        topic, text = 'general', ('Могу разобрать лаг репликации, bloat и вакуум, тяжёлые запросы и индексы, бэкапы и конфигурацию. '
                                  'Спросите, например: «почему вырос лаг репликации?»')
    return {'reply': text, 'topic': topic}
