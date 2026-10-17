#!/usr/bin/env python3
"""Демо-данные для DBA-экранов: риски, ASH, запросы, checkpoints, прогресс,
аудит индексов, журналы, безопасность, обновления, еженедельный отчёт."""
import math
import random
import time

import logscan

T0 = time.time()
GB = 1 << 30
MB = 1 << 20


def _w(period, phase=0.0):
    return (math.sin((time.time() - T0) / period * 2 * math.pi + phase) + 1) / 2


def risk_summary(lag_fn, nodes, leader):
    now = time.time()
    xid_dbs = [
        {'datname': 'erp_1c', 'xid_age': 1_210_000_000 + int((now - T0) * 900), 'mxid_age': 310_000_000, 'size': 412 * GB},
        {'datname': 'billing', 'xid_age': 420_000_000, 'mxid_age': 12_000_000, 'size': 96 * GB},
        {'datname': 'reports', 'xid_age': 198_000_000, 'mxid_age': 2_000_000, 'size': 31 * GB},
        {'datname': 'postgres', 'xid_age': 198_000_000, 'mxid_age': 1_000, 'size': 9 * MB},
    ]
    rate = 900.0
    for d in xid_dbs:
        d['xid_pct'] = round(100.0 * d['xid_age'] / 2 ** 31, 2)
        d['mxid_pct'] = round(100.0 * d['mxid_age'] / 2 ** 31, 2)
        d['days_left'] = round((2 ** 31 - 3_000_000 - d['xid_age']) / rate / 86400, 1)
    top = [
        {'db': 'erp_1c', 'schema': 'public', 'table': '_accumrg1234', 'xid_age': xid_dbs[0]['xid_age'], 'mxid_age': 290_000_000,
         'size': 182 * GB, 'last_autovacuum': '2026-09-02 03:12:44', 'last_vacuum': None},
        {'db': 'erp_1c', 'schema': 'public', 'table': '_inforg5521', 'xid_age': 980_000_000, 'mxid_age': 40_000_000,
         'size': 41 * GB, 'last_autovacuum': '2026-10-01 01:02:10', 'last_vacuum': None},
        {'db': 'erp_1c', 'schema': 'public', 'table': '_document312', 'xid_age': 610_000_000, 'mxid_age': 1_000_000,
         'size': 12 * GB, 'last_autovacuum': '2026-10-12 22:40:00', 'last_vacuum': None},
        {'db': 'billing', 'schema': 'public', 'table': 'payments', 'xid_age': 420_000_000, 'mxid_age': 1_000,
         'size': 22 * GB, 'last_autovacuum': '2026-10-15 04:00:00', 'last_vacuum': None},
    ]
    wr = {'ok': True, 'level': 'warn', 'node': leader, 'max_pct': xid_dbs[0]['xid_pct'], 'worst_db': 'erp_1c',
          'days_left': xid_dbs[0]['days_left'], 'xid_per_sec': rate, 'dbs': xid_dbs, 'top_tables': top,
          'settings': {'autovacuum_freeze_max_age': '200000000', 'autovacuum_multixact_freeze_max_age': '400000000',
                       'vacuum_failsafe_age': '1600000000', 'autovacuum': 'on'}}
    bk = {'ok': True, 'level': 'warn',
          'issues': [{'level': 'warn', 'text': 'Успешной проверки восстановления не было больше 8 дней'}],
          'last_backup': {'ts': now - 6.2 * 3600, 'name': 'base_000000070000005A00000004', 'node': 'pg-node2', 'full': False},
          'last_backup_age_h': 6.2, 'last_full_age_h': 30.2, 'backups_total': 5, 'agents_online': 3,
          'archiver': {'mode': 'on', 'command': 'wal-g wal-push %p', 'library': '', 'timeout': '60',
                       'stat': {'archived_count': 182_334, 'last_archived_wal': '000000070000005A0000003C',
                                'last_archived_time': time.strftime('%Y-%m-%d %H:%M:%S+03', time.localtime(now - 40)),
                                'failed_count': 2, 'last_failed_wal': '000000070000005800000011',
                                'last_failed_time': '2026-10-09 03:14:00+03', 'stats_reset': '2026-09-01 00:00:00+03'},
                       'ready': 1, 'current_wal': '000000070000005A0000003D'},
          'restore_tests': [{'ts': now - 12 * 86400, 'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - 12 * 86400)),
                             'node': 'pg-node3', 'ok': True,
                             'data': {'backup': 'base_000000070000005800000002', 'duration_s': 2410, 'fetch_s': 1980, 'recovery_s': 380,
                                      'size': 538 * GB, 'consistent_at': '2026-10-05 02:31:07.112+03',
                                      'checked': [{'db': 'erp_1c', 'tables': 8120, 'ok': True}, {'db': 'billing', 'tables': 214, 'ok': True}]},
                             'error': None}],
          'last_restore_ok': None}
    bk['last_restore_ok'] = bk['restore_tests'][-1]
    sl_nodes = [{'node': leader, 'ok': True, 'max_slot_wal_keep_size': '-1', 'recovery': False, 'slots': [
        {'slot_name': n.replace('-', '_'), 'slot_type': 'physical', 'database': None, 'active': True, 'wal_status': 'reserved',
         'safe_wal_size': None, 'restart_lsn': '5A/1F000000', 'retained_bytes': lag_fn(n), 'level': 'ok', 'why': ''}
        for n in nodes if n != leader] + [
        {'slot_name': 'debezium_billing', 'slot_type': 'logical', 'database': 'billing', 'active': False, 'wal_status': 'extended',
         'safe_wal_size': None, 'restart_lsn': '57/0A000000', 'retained_bytes': int(3.4 * GB), 'level': 'warn',
         'why': 'Неактивный слот удерживает 3.4 GB WAL. Ограничения нет (max_slot_wal_keep_size = -1)'}]}]
    reps = []
    for n in nodes:
        if n == leader:
            continue
        lag_s = round(lag_fn(n) / (2.5 * MB), 1)
        reps.append({'application_name': n, 'client': '10.0.10.%d' % (12 + len(reps)), 'state': 'streaming', 'sync_state': 'async',
                     'write_lag_s': round(lag_s * 0.3, 2), 'flush_lag_s': round(lag_s * 0.5, 2), 'replay_lag_s': lag_s,
                     'replay_lag_bytes': lag_fn(n), 'reply_time': time.strftime('%Y-%m-%d %H:%M:%S'),
                     'level': 'warn' if lag_s >= 60 else 'ok'})
    rep = {'ok': True, 'level': 'warn' if any(r['level'] == 'warn' for r in reps) else 'ok', 'replicas': reps,
           'max_replay_lag_s': max([r['replay_lag_s'] for r in reps] or [0])}
    items = [{'level': 'warn', 'area': 'wraparound',
              'text': f"Возраст XID в базе erp_1c: {wr['max_pct']}% от предела, до остановки записи ~{wr['days_left']} дн."},
             {'level': 'warn', 'area': 'backups', 'text': bk['issues'][0]['text']},
             {'level': 'warn', 'area': 'slots', 'text': 'Слот debezium_billing на %s: %s' % (leader, sl_nodes[0]['slots'][-1]['why'])}]
    for r in reps:
        if r['level'] == 'warn':
            items.append({'level': 'warn', 'area': 'replication', 'text': f"Реплика {r['application_name']} отстаёт на {round(r['replay_lag_s'])} с"})
    return {'level': 'warn', 'items': items, 'ts': now, 'wraparound': wr, 'backups': bk,
            'slots': {'ok': True, 'level': 'warn', 'nodes': sl_nodes}, 'replication': rep}


QUERIES = [
    ("SELECT T1._IDRRef, T1._Fld1234 FROM _AccumRg1234 T1 WHERE T1._Period >= $1 AND T1._Fld1235RRef = $2", 'IO: DataFileRead', 'erp_1c', 'usr1c'),
    ("UPDATE _InfoRg5521 SET _Fld5530 = $1 WHERE _Fld5522RRef = $2", 'Lock: transactionid', 'erp_1c', 'usr1c'),
    ("INSERT INTO payments (account_id, amount, created_at) VALUES ($1, $2, now())", 'CPU', 'billing', 'billing_app'),
    ("SELECT sum(amount) FROM payments WHERE created_at > now() - interval '1 day' GROUP BY account_id", 'IO: BufFileWrite', 'billing', 'report_ro'),
    ("COMMIT", 'IO: WALSync', 'billing', 'billing_app'),
    ("SELECT * FROM _Document312 WHERE _Number = $1", 'CPU', 'erp_1c', 'usr1c'),
]


def ash(minutes=15, buckets=60):
    now = time.time(); frm = now - minutes * 60; width = minutes * 60 / buckets
    tl = []
    for i in range(buckets):
        t = frm + i * width
        ph = t / 60.0
        storm = 1.0 if (int(t) // 300) % 3 == 1 else 0.0       # периодический «шторм» блокировок
        cl = {'CPU': round(2.2 + 1.4 * (math.sin(ph) + 1) / 2, 2), 'IO': round(1.1 + 0.9 * (math.sin(ph / 2 + 1) + 1) / 2, 2),
              'Lock': round(0.1 + 2.8 * storm * (math.sin(ph * 3) + 1) / 2, 2), 'LWLock': round(0.2 + 0.3 * random.random(), 2),
              'Client': round(0.4 + 0.2 * random.random(), 2), 'IdleTx': round(0.3 * storm, 2)}
        tl.append({'ts': t, 'classes': cl, 'aas': round(sum(cl.values()), 2), 'n': 0})
    tot = {}
    for b in tl:
        for k, v in b['classes'].items():
            tot[k] = tot.get(k, 0) + v
    s = sum(tot.values()) or 1
    ev_names = {'CPU': 'CPU', 'IO': 'IO: DataFileRead', 'Lock': 'Lock: transactionid', 'LWLock': 'LWLock: WALWrite',
                'Client': 'Client: ClientRead', 'IdleTx': 'IdleTx'}
    events = sorted(({'key': ev_names[k], 'n': int(v * 60), 'pct': round(100 * v / s, 1), 'aas': round(v / buckets, 2)} for k, v in tot.items()),
                    key=lambda x: -x['n'])
    weights = [0.31, 0.22, 0.17, 0.12, 0.1, 0.08]
    aas = s / buckets
    tq = [{'query': q, 'n': int(w * s * 60), 'pct': round(100 * w, 1), 'aas': round(w * aas, 2), 'db': db, 'user': u, 'top_event': ev}
          for (q, ev, db, u), w in zip(QUERIES, weights)]
    return {'from': frm, 'to': now, 'bucket_s': round(width, 1), 'timeline': tl, 'samples': minutes * 60 * 3,
            'avg_active': round(aas, 2), 'top_events': events, 'top_queries': tq,
            'top_users': [{'key': 'usr1c', 'n': 0, 'pct': 58.0, 'aas': round(aas * .58, 2)}, {'key': 'billing_app', 'n': 0, 'pct': 31.0, 'aas': round(aas * .31, 2)},
                          {'key': 'report_ro', 'n': 0, 'pct': 11.0, 'aas': round(aas * .11, 2)}],
            'top_dbs': [{'key': 'erp_1c', 'n': 0, 'pct': 63.0, 'aas': round(aas * .63, 2)}, {'key': 'billing', 'n': 0, 'pct': 37.0, 'aas': round(aas * .37, 2)}],
            'top_sessions': [{'pid': 48211, 'user': 'usr1c', 'app': '1CV8C', 'n': 410, 'pct': 9.1}, {'pid': 48390, 'user': 'billing_app', 'app': 'billing-api', 'n': 260, 'pct': 5.8}],
            'history_from': now - 3600, 'interval_s': 1, 'errors': {}}


def statements(hours=1):
    now = time.time()
    top = []
    base = [(QUERIES[0][0], 18_220, 41.2, 31.0), (QUERIES[1][0], 92_100, 4.1, 3.9), (QUERIES[3][0], 240, 1250.4, 410.2),
            (QUERIES[2][0], 310_400, 0.42, 0.40), (QUERIES[5][0], 51_000, 2.1, 2.0),
            ("SELECT _IDRRef FROM _Reference88 WHERE _Description ILIKE $1", 3_900, 88.0, 87.1)]
    total = sum(c * m for _, c, m, _ in base)
    for i, (q, c, m, pm) in enumerate(base):
        top.append({'qid': str(1000 + i), 'query': q, 'calls': int(c * hours), 'total_ms': round(c * m * hours, 1), 'mean_ms': m,
                    'rows': c * 3, 'blks_read': int(c * m * 4), 'temp_w': 52_000 if i == 2 else 0,
                    'pct': round(100 * c * m / total, 1), 'prev_mean_ms': pm, 'ratio': round(m / pm, 2)})
    top.sort(key=lambda x: -x['total_ms'])
    reg = [{'qid': '1002', 'query': QUERIES[3][0], 'mean_ms': 1250.4, 'prev_mean_ms': 410.2, 'ratio': 3.0, 'calls': 240, 'total_ms': 300096.0}]
    return {'ok': True, 'from': now - hours * 3600, 'to': now, 'prev_from': now - 2 * hours * 3600, 'node': 'pg-node1',
            'total_ms': round(total * hours, 1), 'top': top, 'regressions': reg, 'snapshots': 144}


def checkpoints():
    now = time.time()
    hist = [{'ts': now - (60 - i) * 60, 'lsn': int(0x5A000000 + i * 31 * MB + random.random() * MB), 'timed': 900 + i // 5, 'req': 210 + i // 3} for i in range(60)]
    return {'ok': True,
            'settings': {'max_wal_size': '1GB', 'min_wal_size': '80MB', 'checkpoint_timeout': '5min', 'checkpoint_completion_target': '0.9',
                         'wal_buffers': '16MB', 'wal_compression': 'off', 'full_page_writes': 'on', 'bgwriter_lru_maxpages': '100',
                         'bgwriter_delay': '200ms', 'shared_buffers': '16GB', 'wal_level': 'replica', 'synchronous_commit': 'on'},
            'counters': {}, 'delta': {}, 'since_reset': False,
            'rates': {'window_s': 3600, 'wal_bytes_per_s': round(31 * MB / 60), 'checkpoints_per_h': 32.0, 'write_ms_per_ckpt': 81_400.0, 'sync_ms_per_ckpt': 420.0},
            'recommendations': [
                {'level': 'warn', 'text': '62% контрольных точек вынужденные (по объёму WAL). Увеличьте max_wal_size (сейчас 1GB), чтобы они шли по checkpoint_timeout.'},
                {'level': 'warn', 'text': '27% грязных страниц пишут сами бэкенды — запросы ждут записи. Настройте bgwriter_lru_maxpages/bgwriter_delay, проверьте shared_buffers и скорость дисков.'},
                {'level': 'info', 'text': '41% записей WAL — полные образы страниц (FPI). Помогают wal_compression = on/lz4 и более редкие контрольные точки.'}],
            'history': hist}


def progress():
    el = (time.time() - T0) % 900
    pct = round(min(99.0, el / 9), 1)
    return [
        {'node': 'pg-node1', 'kind': 'VACUUM', 'pid': 51220, 'datname': 'erp_1c', 'target': '_accumrg1234', 'phase': 'scanning heap',
         'pct': pct, 'done': int(pct * 1e5), 'total': int(1e7), 'usename': 'postgres', 'dur_s': el, 'eta_s': round(el * (100 - pct) / max(pct, 1)),
         'query': 'autovacuum: VACUUM public._accumrg1234 (to prevent wraparound)'},
        {'node': 'pg-node1', 'kind': 'CREATE INDEX', 'pid': 51388, 'datname': 'billing', 'target': 'payments_created_at_idx',
         'phase': 'CREATE INDEX CONCURRENTLY: building index: scanning table', 'pct': round(min(99.0, pct * 1.7 % 100), 1),
         'done': 0, 'total': 0, 'usename': 'dba', 'dur_s': el / 2, 'eta_s': 240, 'query': 'CREATE INDEX CONCURRENTLY ON payments (created_at)'},
    ]


def index_audit(db):
    return {'ok': True, 'db': db, 'stats_reset': '2026-09-01 00:00:00+03', 'reclaimable_bytes': int(14.2 * GB),
            'invalid': [{'schema': 'public', 'table': 'payments', 'index': 'payments_account_ccnew', 'size': int(2.1 * GB),
                         'fix': 'DROP INDEX CONCURRENTLY public.payments_account_ccnew;', 'def': 'CREATE INDEX payments_account_ccnew ON public.payments USING btree (account_id)'}],
            'duplicate': [{'schema': 'public', 'table': '_inforg5521', 'index': '_inforg5521_by_dims_copy', 'duplicate_of': '_inforg5521_by_dims',
                           'size': int(3.3 * GB), 'fix': 'DROP INDEX CONCURRENTLY public._inforg5521_by_dims_copy;',
                           'def': 'CREATE INDEX _inforg5521_by_dims_copy ON public._inforg5521 USING btree (_fld5522rref, _period)'}],
            'overlap': [{'schema': 'public', 'table': 'payments', 'index': 'payments_account_idx', 'covered_by': 'payments_account_created_idx',
                         'size': int(1.8 * GB), 'fix': 'DROP INDEX CONCURRENTLY public.payments_account_idx;',
                         'def': 'CREATE INDEX payments_account_idx ON public.payments USING btree (account_id)'}],
            'fk_no_index': [{'schema': 'public', 'table': 'payment_items', 'constraint': 'payment_items_payment_id_fkey', 'columns': 'payment_id',
                             'references': 'payments', 'table_size': 9 * GB, 'fix': 'CREATE INDEX CONCURRENTLY ON public.payment_items (payment_id);'}],
            'unused': [{'schema': 'public', 'table': '_document312', 'index': '_document312_old_number', 'size': 7 * GB, 'scans': 0,
                        'fix': 'DROP INDEX CONCURRENTLY public._document312_old_number;', 'def': 'CREATE INDEX _document312_old_number ON public._document312 USING btree (_number, _date_time)'}],
            'counts': {'invalid': 1, 'duplicate': 1, 'overlap': 1, 'fk_no_index': 1, 'unused': 1}}


def log_text():
    d = time.strftime('%Y-%m-%d')
    L = []
    for h in range(8, 15):
        t = f'{d} {h:02d}'
        L += [f'{t}:00:01.120 MSK [1201] LOG:  checkpoint starting: time',
              f'{t}:04:31.004 MSK [1201] LOG:  checkpoint complete: wrote 18211 buffers (0.9%); 0 WAL file(s) added, 0 removed, 12 recycled; write=269.8 s, sync=0.41 s, total=270.2 s',
              f'{t}:07:12.551 MSK [1201] LOG:  checkpoint starting: wal',
              f'{t}:09:40.910 MSK [1201] LOG:  checkpoint complete: wrote 40112 buffers (2.0%); write=146.1 s, sync=2.10 s, total=148.4 s',
              f'{t}:12:05.300 MSK [48211] usr1c@erp_1c LOG:  process 48211 still waiting for ShareLock on transaction 912233411 after 1000.112 ms',
              f'{t}:12:05.301 MSK [48211] usr1c@erp_1c DETAIL:  Process holding the lock: 48390. Wait queue: 48211.',
              f'{t}:13:44.010 MSK [48655] report_ro@billing LOG:  temporary file: path "base/pgsql_tmp/pgsql_tmp48655.3", size 734003200',
              f"{t}:13:44.010 MSK [48655] report_ro@billing STATEMENT:  SELECT sum(amount) FROM payments WHERE created_at > now() - interval '1 day' GROUP BY account_id",
              f'{t}:21:00.400 MSK [48211] usr1c@erp_1c LOG:  duration: 3412.551 ms  statement: SELECT T1._IDRRef FROM _AccumRg1234 T1 WHERE T1._Period >= \'2026-10-01\'',
              f'{t}:25:10.000 MSK [51220] LOG:  automatic vacuum of table "erp_1c.public._inforg5521": index scans: 1 pages: 0 removed, 512331 remain; elapsed: 412.11 s',
              f'{t}:31:02.777 MSK [49001] usr1c@erp_1c ERROR:  deadlock detected',
              f'{t}:31:02.777 MSK [49001] usr1c@erp_1c DETAIL:  Process 49001 waits for ShareLock on transaction 912240001; blocked by process 49002.',
              f'{t}:33:00.100 MSK [49120] billing_app@billing ERROR:  duplicate key value violates unique constraint "payments_ext_id_key"',
              f'{t}:33:00.100 MSK [49120] billing_app@billing DETAIL:  Key (ext_id)=(A-{h}771) already exists.',
              f'{t}:40:11.000 MSK [49300] [unknown]@billing FATAL:  password authentication failed for user "admin"',
              f'{t}:40:12.000 MSK [49301] [unknown]@billing FATAL:  password authentication failed for user "admin"',
              f'{t}:47:30.500 MSK [48211] usr1c@erp_1c LOG:  duration: 1210.004 ms  statement: UPDATE _InfoRg5521 SET _Fld5530 = 12 WHERE _Fld5522RRef = \'\\x01\'',
              f'{t}:52:00.000 MSK [49500] usr1c@erp_1c ERROR:  canceling statement due to statement timeout']
    return '\n'.join(L)


def log_report(node):
    rep = logscan.parse(log_text())
    rep.update({'file': '/var/lib/pgsql/16/data/log/postgresql-%s.log' % time.strftime('%a'), 'file_size': 412 * MB,
                'read_bytes': len(log_text()), 'node': node})
    return rep


def log_settings(node):
    vals = {'log_lock_waits': 'on', 'log_temp_files': '0', 'log_autovacuum_min_duration': '10s', 'log_checkpoints': 'on',
            'log_min_duration_statement': '1s', 'log_connections': 'off', 'log_disconnections': 'off', 'logging_collector': 'on'}
    out = []
    for name, (rec, why) in logscan.LOG_SETTINGS.items():
        cur = vals[name]
        out.append({'name': name, 'current': cur, 'recommended': rec, 'why': why, 'ok': cur not in ('off', '-1')})
    return {'ok': True, 'node': node, 'settings': out,
            'info': {'log_directory': 'log', 'log_filename': 'postgresql-%a.log', 'log_line_prefix': '%m [%p] %q%u@%d ', 'log_destination': 'stderr'}}


def security():
    f = [
        {'level': 'bad', 'title': 'pg_hba.conf: вход без пароля (trust) по сети', 'detail': 'Любой, кто достучится до порта, войдёт без пароля под указанной ролью.',
         'fix': 'Замените trust на scram-sha-256 и выполните SELECT pg_reload_conf(); (в Patroni — через postgresql.pg_hba в DCS)',
         'items': ['строка 14: host replication replicator 10.0.10.0/24 trust']},
        {'level': 'warn', 'title': 'Ролей с паролем в формате MD5: 3', 'detail': 'Хеш MD5 позволяет войти, зная только хеш (pass-the-hash). В PostgreSQL 18 MD5 объявлен устаревшим.',
         'fix': "SET password_encryption = 'scram-sha-256'; ALTER ROLE <имя> PASSWORD '<новый пароль>';", 'items': ['usr1c', 'report_ro', 'billing_app']},
        {'level': 'warn', 'title': 'Суперпользователей с правом входа: 3', 'detail': 'Суперпользователь обходит все проверки прав.',
         'fix': 'ALTER ROLE <имя> NOSUPERUSER;', 'items': ['postgres', 'usr1c', 'dba_ivanov']},
        {'level': 'warn', 'title': 'Незашифрованных сетевых подключений: 61', 'detail': 'SSL включён на сервере, но клиенты подключаются без него.',
         'fix': 'В pg_hba.conf замените host на hostssl для сетевых подключений', 'items': ['usr1c с 10.0.20.15 (1CV8C) × 58', 'report_ro с 10.0.30.4 (psql) × 3']},
        {'level': 'info', 'title': 'log_connections = off', 'detail': 'входы в журнал не пишутся — расследовать инцидент будет сложно',
         'fix': 'ALTER SYSTEM SET log_connections = on; SELECT pg_reload_conf();', 'items': []},
        {'level': 'info', 'title': 'Пароли без срока действия: 4 ролей', 'detail': 'Для учётных записей людей обычно требуется регулярная смена пароля.',
         'fix': "ALTER ROLE <имя> VALID UNTIL '2027-01-01';", 'items': ['usr1c', 'report_ro', 'billing_app', 'dba_ivanov']},
    ]
    return {'ok': True, 'score': 100 - 15 - 6 * 3 - 2, 'node': 'pg-node1', 'version_num': 160004, 'findings': f}


def upgrade(target=None):
    target = target or '18'
    nodes = [{'node': n, 'ok': True, 'data': {'version': '16.4', 'major': '16', 'full': 'PostgreSQL 16.4 on x86_64-pc-linux-gnu', 'latest': '16.15',
                                               'behind': True, 'eol': '2028-11-09', 'eol_passed': False, 'eol_soon': False, 'checksums': 'on'}}
             for n in ('pg-node1', 'pg-node2', 'pg-node3')]
    pre = [{'level': 'ok', 'title': 'Подготовленные транзакции (2PC)', 'detail': 'нет'},
           {'level': 'warn', 'title': 'Логические слоты репликации', 'detail': 'pg_upgrade с версии ниже 17 не переносит логические слоты — их придётся пересоздать, подписчиков ресинхронизировать: debezium_billing'},
           {'level': 'info', 'title': 'Расширения', 'detail': f'Все расширения должны быть установлены в новой версии (пакеты для PostgreSQL {target})',
            'items': ['pg_stat_statements 1.10 — billing, erp_1c', 'pg_repack 1.5.0 — erp_1c', 'online_analyze 1.0 — erp_1c']},
           {'level': 'info', 'title': 'Кодировка и локаль', 'detail': 'Новый кластер инициализируйте с теми же кодировкой и локалью', 'items': ['UTF8 / ru_RU.UTF-8']},
           {'level': 'info', 'title': 'Контрольные суммы данных', 'detail': 'сейчас on: новый кластер должен совпадать (initdb --data-checksums)'}]
    return {'ok': True, 'versions_updated': '2026-08-13', 'nodes': nodes, 'current_major': '16', 'target': target, 'prechecks': pre,
            'major_plan': ['Прогоните проверки на копии: восстановите бэкап (экран «Риски» → проверка восстановления) и выполните pg_upgrade --check.',
                           'Patroni: поставьте кластер на паузу (patronictl pause), остановите реплики, затем лидер.',
                           f'На лидере: установите пакеты PostgreSQL {target} и всех расширений, initdb с теми же локалью/кодировкой/checksums.',
                           'pg_upgrade --link --check, затем pg_upgrade --link (минуты вместо часов, но откат только из бэкапа).',
                           'Обновите bin_dir/data_dir в patroni.yml, удалите ключ /initialize в DCS (patronictl remove) и запустите Patroni на лидере.',
                           'Реплики пересоздайте с нового лидера (reinit) или используйте rsync по процедуре из документации pg_upgrade.',
                           'После запуска: vacuumdb --all --analyze-in-stages, сделайте полный бэкап, снимите паузу.'],
            'minor_plan': 'Минорное обновление: обновите пакеты на репликах и перезапустите их, затем switchover и обновление бывшего лидера. Совместимость данных сохраняется, pg_upgrade не нужен.',
            'available_targets': ['13', '14', '15', '16', '17', '18']}


def weekly(days, cluster, members, leader, risk):
    return {'generated': time.strftime('%Y-%m-%d %H:%M'), 'days': days,
            'period': (time.strftime('%d.%m.%Y', time.localtime(time.time() - days * 86400)), time.strftime('%d.%m.%Y')),
            'cluster': cluster, 'members': members, 'leader': leader, 'risk': risk,
            'alerts': [{'rule': 'Лаг реплики во времени', 'severity': 'warning', 'count': 14}, {'rule': 'Много idle-in-transaction', 'severity': 'warning', 'count': 3},
                       {'rule': 'Архивация WAL падает', 'severity': 'critical', 'count': 1}],
            'alerts_total': 18,
            'growth': [{'name': 'erp_1c', 'size': 412 * GB, 'delta': int(10.3 * GB), 'per_day': int(1.47 * GB), 'in_30d': 456 * GB, 'window_days': 7},
                       {'name': 'billing', 'size': 96 * GB, 'delta': int(1.7 * GB), 'per_day': int(0.24 * GB), 'in_30d': 103 * GB, 'window_days': 7},
                       {'name': 'reports', 'size': 31 * GB, 'delta': 0, 'per_day': 0, 'in_30d': 31 * GB, 'window_days': 7}],
            'disks': [{'host': 'pg-node3', 'path': '/var/lib/pgsql', 'used_pct': 83, 'free': 348 * GB, 'days': 2.9},
                      {'host': 'pg-node1', 'path': '/var/lib/pgsql', 'used_pct': 61, 'free': 799 * GB, 'days': 37.5},
                      {'host': 'pg-node2', 'path': '/var/lib/pgsql', 'used_pct': 58, 'free': 860 * GB, 'days': 45.8}],
            'statements': statements(24 * days),
            'security': {'score': 65, 'bad': 1, 'warn': 3, 'top': [f['title'] for f in security()['findings'][:4]]},
            'versions': [{'node': n['node'], **n['data']} for n in upgrade()['nodes']]}
