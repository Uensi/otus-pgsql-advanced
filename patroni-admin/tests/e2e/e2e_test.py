#!/usr/bin/env python3
"""Сквозной (E2E) тест Patroni Console на живом стенде (tests/e2e/stand.sh).

Проверяет консоль целиком: API → агент → wal-g / pg_dump → PostgreSQL / Patroni.
Ключевые сценарии: полный и дельта-бэкап, PITR на момент времени с проверкой
данных, обычное восстановление, автоматическая проверка восстановления, клон,
логический дамп, switchover, а также все DBA-экраны (риски, ASH, запросы,
контрольные точки, прогресс, аудит индексов, журналы, безопасность, обновления).

Запуск (внутри WSL/Linux со стендом):
  PYTHONPATH=~/pctest/pylibs python3 tests/e2e/e2e_test.py
Итог: таблица PASS/FAIL и код выхода 0 только если всё прошло."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback

import psycopg2
import requests

ST = os.environ.get('PCTEST_DIR', os.path.expanduser('~/pctest'))
URL = os.environ.get('CONSOLE_URL', 'http://127.0.0.1:15000')
PGBIN = os.environ.get('PGBIN', '/usr/lib/postgresql/16/bin')
HOSTS = {'pgnode1': '127.0.0.2', 'pgnode2': '127.0.0.3'}
PGPW = 'pctest'
RESULTS = []
RUN = time.strftime('%H%M%S')   # метки строк этого прогона: тест не зависит от данных прошлых запусков


# ---------------------------------------------------------------- инфраструктура
class Console:
    def __init__(self, user='admin', pw='e2e-admin'):
        self.s = requests.Session()
        r = self.s.post(URL + '/api/auth/login', json={'user': user, 'password': pw}, timeout=20)
        assert r.status_code == 200 and r.json().get('ok'), f'вход {user}: {r.status_code} {r.text[:200]}'

    def get(self, path, **kw):
        r = self.s.get(URL + path, timeout=kw.pop('timeout', 60), **kw)
        return r.json() if 'json' in r.headers.get('content-type', '') else r

    def post(self, path, body=None, **kw):
        r = self.s.post(URL + path, json=body or {}, timeout=kw.pop('timeout', 120), **kw)
        try:
            return r.json()
        except ValueError:
            return {'ok': False, 'status': r.status_code, 'text': r.text[:300]}

    def wait_cmd(self, node, cid, timeout=900, path='/api/ops/cmd-result'):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = self.get(f'{path}?node={node}&id={cid}')
            if isinstance(j, dict) and j.get('ok') is False:
                return {'ok': False, 'out': j.get('error')}
            if isinstance(j, dict) and j.get('data'):
                return j['data']
            time.sleep(2)
        raise AssertionError(f'команда {cid} на {node} не завершилась за {timeout} с')

    def agent_backups(self, node):
        return (self.get('/api/agent/status')['agents'].get(node) or {}).get('backups') or []


def pg(node_or_host, db='postgres', port=25432):
    host = HOSTS.get(node_or_host, node_or_host)
    c = psycopg2.connect(host=host, port=port, user='postgres', password=PGPW, dbname=db, connect_timeout=5)
    c.autocommit = True
    return c


def one(c, sql, params=None):
    cur = c.cursor(); cur.execute(sql, params); r = cur.fetchone(); cur.close()
    return r[0] if r else None


def run(c, sql, params=None):
    cur = c.cursor(); cur.execute(sql, params); cur.close()


def leader(con):
    for m in con.get('/api/cluster')['data']['members']:
        if m.get('role') in ('leader', 'master', 'primary'):
            return m['name']
    return None


def replica(con):
    for m in con.get('/api/cluster')['data']['members']:
        if m.get('role') not in ('leader', 'master', 'primary'):
            return m['name']
    return None


def test(name):
    def deco(fn):
        def wrapper(*a):
            t0 = time.time()
            try:
                detail = fn(*a)
                RESULTS.append((name, 'PASS', round(time.time() - t0, 1), detail or ''))
                print(f'PASS  {name} ({time.time() - t0:.1f} с) {detail or ""}', flush=True)
            except Exception as e:
                RESULTS.append((name, 'FAIL', round(time.time() - t0, 1), f'{type(e).__name__}: {e}'))
                print(f'FAIL  {name}: {e}', flush=True)
                traceback.print_exc()
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def wait_archived(c, timeout=120):
    """Переключает WAL и ждёт, пока сегмент уйдёт в архив (нужно для PITR)."""
    seg = one(c, 'SELECT pg_walfile_name(pg_switch_wal())')
    t0 = time.time()
    while time.time() - t0 < timeout:
        last = one(c, 'SELECT last_archived_wal FROM pg_stat_archiver')
        if last and last[8:] >= seg[8:]:
            return seg
        time.sleep(1)
    raise AssertionError(f'сегмент {seg} не заархивирован за {timeout} с')


def start_copy(path, port):
    """Запуск восстановленной копии на отдельном порту; ждём выхода из recovery."""
    with open(os.path.join(path, 'postgresql.auto.conf'), 'a') as f:
        f.write(f"\nport = {port}\nlisten_addresses = '127.0.0.1'\nunix_socket_directories = '{path}'\n"
                "hot_standby = off\nshared_buffers = '64MB'\nmax_wal_senders = 0\nmax_replication_slots = 0\nwal_level = minimal\n")
    for fn in ('postmaster.pid',):
        try: os.remove(os.path.join(path, fn))
        except FileNotFoundError: pass
    os.chmod(path, 0o700)
    r = subprocess.run([f'{PGBIN}/pg_ctl', '-D', path, '-l', os.path.join(path, 'e2e.log'), '-w', '-t', '300', 'start'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log = open(os.path.join(path, 'e2e.log'), errors='replace').read()[-3000:]
        raise AssertionError(f'копия не запустилась: {r.stderr or r.stdout}\n{log}')
    for _ in range(300):
        try:
            c = psycopg2.connect(host='127.0.0.1', port=port, user='postgres', password=PGPW, dbname='postgres', connect_timeout=3)
            c.autocommit = True
            if not one(c, 'SELECT pg_is_in_recovery()'):
                c.close(); return
            c.close()
        except Exception:
            pass
        time.sleep(1)
    raise AssertionError('копия не вышла из recovery')


def stop_copy(path):
    subprocess.run([f'{PGBIN}/pg_ctl', '-D', path, '-m', 'fast', '-w', 'stop'], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


def restored_dir(out):
    """«restored to /path» / «pitr prepared at /path» → /path"""
    return str(out).rsplit(' ', 1)[-1].strip()


# ---------------------------------------------------------------- тесты
@test('Служебные проверки /healthz, /readyz, /metrics')
def t_health(con):
    h = requests.get(URL + '/healthz', timeout=10).json()
    r = requests.get(URL + '/readyz', timeout=20).json()
    m = con.get('/metrics')
    assert h['ok'] and r['ok'] and r['patroni_reachable'] == 2, (h, r)
    txt = m.text if hasattr(m, 'text') else str(m)
    assert 'patroni' in txt.lower() or '#' in txt, txt[:200]
    return f"готово: {r['patroni_reachable']}/{r['patroni_total']} узлов Patroni"


@test('Кластер: лидер, реплика в состоянии streaming, LSN')
def t_cluster(con):
    d = con.get('/api/cluster')['data']
    roles = {m['name']: (m['role'], m['state']) for m in d['members']}
    assert len(roles) == 2, roles
    assert sorted(v[0] for v in roles.values()) == ['leader', 'replica'], roles
    assert any(v == ('replica', 'streaming') for v in roles.values()), roles
    lsn = con.get('/api/lsn')['data']
    assert set(lsn) == set(roles), lsn.keys()
    return str(roles)


@test('Безопасность API: RBAC, CSRF, закрытый статус агентов')
def t_rbac(con):
    assert requests.get(URL + '/api/agent/status', timeout=10).status_code == 401
    v = Console('viewer', 'e2e-viewer')
    assert v.s.post(URL + '/api/pause', json={'pause': True}, timeout=10).status_code == 403
    d = Console('dba', 'e2e-dba')
    assert d.s.post(URL + '/api/switchover', json={}, timeout=10).status_code == 403
    r = con.s.post(URL + '/api/pause', json={'pause': True}, headers={'Origin': 'http://evil.example'}, timeout=10)
    assert r.status_code == 403
    assert requests.post(URL + '/api/agent/register', json={'key': 'bad'}, timeout=10).status_code == 403
    hdr = requests.get(URL + '/', timeout=10).headers
    assert hdr.get('X-Frame-Options') == 'DENY' and 'frame-ancestors' in hdr.get('Content-Security-Policy', '')
    return 'viewer/dba ограничены, межсайтовый POST отклонён'


@test('Подготовка данных: база shop, pg_stat_statements')
def t_prepare(con):
    c = pg(leader(con))
    if not one(c, "SELECT 1 FROM pg_database WHERE datname='shop'"):
        run(c, 'CREATE DATABASE shop')
    run(c, 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements')
    c.close()
    s = pg(leader(con), 'shop')
    run(s, 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements')
    run(s, """CREATE TABLE IF NOT EXISTS customers(id int PRIMARY KEY, name text);
              CREATE TABLE IF NOT EXISTS orders(id bigserial PRIMARY KEY, customer_id int REFERENCES customers(id),
                                                batch text, amount numeric, created_at timestamptz DEFAULT now())""")
    run(s, "INSERT INTO customers SELECT g, 'c'||g FROM generate_series(1,1000) g ON CONFLICT DO NOTHING")
    run(s, "INSERT INTO orders(customer_id, batch, amount) SELECT 1+g%1000, 'initial', g FROM generate_series(1,50000) g")
    n = one(s, 'SELECT count(*) FROM orders'); s.close()
    return f'orders: {n} строк'


@test('Полный бэкап wal-g через консоль (с реплики)')
def t_backup_full(con):
    node = replica(con)
    before = len(con.agent_backups(node))
    j = con.post('/api/backups', {'node': node, 'action': 'backup_full'})
    assert j['ok'], j
    res = con.wait_cmd(node, j['data']['cmd_id'], 600)
    assert res['ok'], res
    for _ in range(30):
        bl = con.agent_backups(node)
        if len(bl) > before: break
        time.sleep(2)
    bl = sorted(con.agent_backups(node), key=lambda b: b.get('time') or b.get('start_time') or '')
    assert len(bl) > before, 'список бэкапов не обновился'
    assert bl[-1].get('is_full') is True, bl[-1]
    return f"{bl[-1]['backup_name']} (полный), всего {len(bl)}"


@test('Дельта-бэкап действительно дельта (исправление флага -f)')
def t_backup_delta(con):
    node = replica(con)
    s = pg(leader(con), 'shop')
    run(s, f"INSERT INTO orders(customer_id, batch, amount) SELECT 1, 'pre-delta-' || '{RUN}', g FROM generate_series(1,5000) g"); s.close()
    # бэкап с реплики начинается с точки рестарта; в продакшене КТ лидера идёт раз в checkpoint_timeout
    c = pg(leader(con)); run(c, 'CHECKPOINT'); c.close()
    time.sleep(3)
    before = len(con.agent_backups(node))
    j = con.post('/api/backups', {'node': node, 'action': 'backup_delta'})
    assert j['ok'], j
    res = con.wait_cmd(node, j['data']['cmd_id'], 600)
    assert res['ok'], res
    for _ in range(30):
        if len(con.agent_backups(node)) > before: break
        time.sleep(2)
    bl = sorted(con.agent_backups(node), key=lambda b: b.get('time') or b.get('start_time') or '')
    assert bl[-1].get('is_full') is False, f'ожидался дельта-бэкап, получен: {bl[-1]}'
    return f"{bl[-1]['backup_name']} (дельта)"


@test('PITR: восстановление на момент времени, данные до/после точки')
def t_pitr(con):
    node = replica(con)
    s = pg(leader(con), 'shop')
    run(s, f"INSERT INTO orders(customer_id, batch, amount) SELECT 2, 'before-T-' || '{RUN}', g FROM generate_series(1,777) g")
    t = one(s, 'SELECT clock_timestamp()::text')
    time.sleep(2)
    run(s, f"INSERT INTO orders(customer_id, batch, amount) SELECT 3, 'after-T-' || '{RUN}', g FROM generate_series(1,333) g")
    total_now = one(s, 'SELECT count(*) FROM orders')
    s.close()
    seg = wait_archived(pg(leader(con)))
    name = sorted(con.agent_backups(node), key=lambda b: b.get('time') or '')[-1]['backup_name']
    j = con.post('/api/backups', {'node': node, 'action': 'pitr', 'name': name, 'target_time': t})
    assert j['ok'], j
    res = con.wait_cmd(node, j['data']['cmd_id'], 900)
    assert res['ok'], res
    path = restored_dir(res['out'])
    try:
        start_copy(path, 25701)
        c = psycopg2.connect(host='127.0.0.1', port=25701, user='postgres', password=PGPW, dbname='shop'); c.autocommit = True
        before = one(c, f"SELECT count(*) FROM orders WHERE batch='before-T-' || '{RUN}'")
        after = one(c, f"SELECT count(*) FROM orders WHERE batch='after-T-' || '{RUN}'")
        total = one(c, 'SELECT count(*) FROM orders')
        am = one(c, "SELECT current_setting('archive_mode')")
        c.close()
        assert before == 777 and after == 0, f'before={before} after={after}'
        assert am == 'off', f'archive_mode в копии = {am} (копия писала бы WAL в боевое хранилище)'
        return f'цель {t}: строк до точки 777 ✓, после точки 0 ✓, всего {total} (в боевой {total_now}); WAL до {seg}'
    finally:
        stop_copy(path)


@test('Восстановление (restore) последнего бэкапа запускается (исправление recovery.signal)')
def t_restore(con):
    node = replica(con)
    name = sorted(con.agent_backups(node), key=lambda b: b.get('time') or '')[-1]['backup_name']
    j = con.post('/api/backups', {'node': node, 'action': 'restore', 'name': name})
    assert j['ok'], j
    res = con.wait_cmd(node, j['data']['cmd_id'], 900)
    assert res['ok'], res
    path = restored_dir(res['out'])
    try:
        start_copy(path, 25702)
        c = psycopg2.connect(host='127.0.0.1', port=25702, user='postgres', password=PGPW, dbname='shop'); c.autocommit = True
        n = one(c, f"SELECT count(*) FROM orders WHERE batch='pre-delta-' || '{RUN}'")
        c.close()
        assert n == 5000, f'pre-delta={n}'
        return f'{name}: база открылась, данные дельта-бэкапа на месте (5000 строк)'
    finally:
        stop_copy(path)


@test('Проверка восстановления из экрана «Риски» (restore_test)')
def t_restore_test(con):
    node = replica(con)
    j = con.post('/api/risk/restore-test', {'node': node})
    assert j['ok'], j
    res = con.wait_cmd(node, j['data']['cmd_id'], 900)
    assert res['ok'], res
    d = res['out']['data']
    assert d['ok'] and any(x['db'] == 'shop' and x['ok'] for x in d['checked']), d
    assert d.get('consistent_at'), 'не определён момент, на который восстановлены данные'
    assert not os.path.exists(d['path']), 'временный каталог не удалён'
    time.sleep(1)
    s = con.get('/api/risk/summary?force=1')['data']
    assert s['backups']['last_restore_ok'], 'результат не записан в историю проверок'
    return f"{d['backup']}: скачивание {d['fetch_s']} с, восстановление {d['recovery_s']} с, данные на {d.get('consistent_at')}"


@test('Клон из бэкапа и его инспекция')
def t_clone(con):
    node = replica(con)
    name = sorted(con.agent_backups(node), key=lambda b: b.get('time') or '')[-1]['backup_name']
    j = con.post('/api/backups', {'node': node, 'action': 'clone_restore', 'name': name, 'label': 'e2e'})
    res = con.wait_cmd(node, j['data']['cmd_id'], 900)
    assert res['ok'], res
    path = res['out']['data']['path']
    try:
        j = con.post('/api/backups', {'node': node, 'action': 'clone_inspect', 'path': path})
        ins = con.wait_cmd(node, j['data']['cmd_id'], 120)
        assert ins['ok'], ins
        d = ins['out']['data']
        assert d['exists'] and d['version'] == '16' and d['recovery_signal'], d
        assert (d.get('controldata') or {}).get('status'), f"pg_controldata не прочитан: {d.get('error')}"
        return f"{path}: PG {d['version']}, {d['size'] // 1048576} МБ, controldata: {(d.get('controldata') or {}).get('status')}"
    finally:
        shutil.rmtree(path, ignore_errors=True)


@test('Логический дамп pg_dump: создание, список, удаление')
def t_dump(con):
    node = replica(con)
    j = con.post('/api/dumps', {'node': node, 'action': 'create', 'db': 'shop', 'mode': 'custom', 'compress': 3})
    assert j['ok'], j
    res = con.wait_cmd(node, j['data']['cmd_id'], 600)
    assert res['ok'], res
    meta = res['out']['data']
    path = os.path.join(ST, 'dumps-n' + node[-1], meta['name'])
    toc = subprocess.run([f'{PGBIN}/pg_restore', '--list', path], capture_output=True, text=True)
    assert toc.returncode == 0 and 'TABLE DATA public orders' in toc.stdout, toc.stderr[:300]
    j = con.post('/api/dumps', {'node': node, 'action': 'list'})
    lst = con.wait_cmd(node, j['data']['cmd_id'], 60)
    assert any(x['name'] == meta['name'] for x in lst['out']['data']), lst
    j = con.post('/api/dumps', {'node': node, 'action': 'delete', 'name': meta['name']})
    assert con.wait_cmd(node, j['data']['cmd_id'], 60)['ok']
    return f"{meta['name']} {meta['size'] // 1024} КБ, sha256 {meta.get('sha256', '')[:12]}…, pg_restore --list ✓"


@test('Риски: wraparound, архивация WAL, слоты, лаг во времени')
def t_risk(con):
    c = pg(leader(con))
    run(c, "SELECT pg_create_physical_replication_slot('e2e_stale', true) WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='e2e_stale')")
    try:
        s = con.get('/api/risk/summary?force=1')['data']
        w = s['wraparound']
        assert w['ok'] and any(d['datname'] == 'shop' for d in w['dbs']) and w['top_tables'], w.get('error')
        b = s['backups']
        assert b['archiver']['mode'] == 'on' and b['last_backup_age_h'] is not None and b['last_backup_age_h'] < 1, b
        slots = [x for n in s['slots']['nodes'] for x in (n.get('slots') or [])]
        st = next(x for x in slots if x['slot_name'] == 'e2e_stale')
        assert st['active'] is False and st['wal_status'] in ('reserved', 'extended'), st
        r = s['replication']
        assert r['ok'] and len(r['replicas']) == 1 and r['replicas'][0]['state'] == 'streaming', r
        m = con.get('/api/alerts/rules')['data']
        assert {'risk_xid', 'risk_backup', 'risk_archive', 'risk_slots', 'risk_replay'} <= {x['id'] for x in m}
        return (f"XID {w['max_pct']}%, бэкап {b['last_backup_age_h']} ч назад, архивация {b['archiver']['stat'].get('archived_count')} сегм., "
                f"слот e2e_stale: {st['wal_status']}, replay_lag {r['replicas'][0]['replay_lag_s']}")
    finally:
        run(c, "SELECT pg_drop_replication_slot('e2e_stale')"); c.close()


@test('VACUUM FREEZE из экрана «Риски» снижает возраст таблицы')
def t_freeze(con):
    s = pg(leader(con), 'shop')
    age0 = one(s, "SELECT age(relfrozenxid) FROM pg_class WHERE relname='orders'")
    j = con.post('/api/risk/freeze', {'db': 'shop', 'schema': 'public', 'table': 'orders'})
    assert j['ok'], j
    for _ in range(60):
        jobs = con.get('/api/risk/jobs')['data']
        job = next(x for x in jobs if x['id'] == j['data']['job'])
        if job['state'] != 'running': break
        time.sleep(1)
    assert job['state'] == 'done', job
    age1 = one(s, "SELECT age(relfrozenxid) FROM pg_class WHERE relname='orders'"); s.close()
    assert age1 <= age0, (age0, age1)
    return f'возраст orders: {age0} → {age1}'


def _load(stop, host):
    """Нагрузка с конфликтами блокировок и долгими запросами — для ASH/журналов."""
    def locker():
        c = pg(host, 'shop'); c.autocommit = False
        while not stop.is_set():
            cur = c.cursor(); cur.execute("UPDATE customers SET name=name WHERE id=1"); time.sleep(1.5); c.commit()
        c.close()
    def waiter():
        c = pg(host, 'shop'); c.autocommit = False
        while not stop.is_set():
            cur = c.cursor(); cur.execute("UPDATE customers SET name=name WHERE id=1"); c.commit()
        c.close()
    def reader():
        c = pg(host, 'shop')
        while not stop.is_set():
            run(c, "SELECT count(*), sum(amount) FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.amount > random()*1000")
        c.close()
    ts = [threading.Thread(target=f, daemon=True) for f in (locker, waiter, reader, reader)]
    for t in ts: t.start()
    return ts


@test('ASH: выборка активных сессий видит CPU и ожидания блокировок')
def t_ash(con):
    stop = threading.Event()
    ts = _load(stop, HOSTS[leader(con)])
    time.sleep(12)
    stop.set()
    for t in ts: t.join(5)
    d = con.get('/api/ash?minutes=2')['data']
    ev = {e['key'] for e in d['top_events']}
    assert d['samples'] > 5 and d['avg_active'] > 0, d['samples']
    assert any(e.startswith('Lock') for e in ev), ev
    assert any('orders' in q['query'] for q in d['top_queries']), [q['query'][:60] for q in d['top_queries']]
    return f"снимков {d['samples']}, AAS {d['avg_active']}, ожидания: {', '.join(sorted(ev))}"


@test('История pg_stat_statements: топ запросов за период')
def t_statements(con):
    assert con.post('/api/statements/snapshot')['ok']
    s = pg(leader(con), 'shop')
    for _ in range(30):
        run(s, "SELECT count(*) FROM orders WHERE customer_id = 42")
    s.close()
    time.sleep(1)
    assert con.post('/api/statements/snapshot')['ok']
    r = con.get('/api/statements/report?hours=1')
    assert r['ok'] and r['top'], r
    q = next((x for x in r['top'] if 'customer_id' in x['query'] and 'orders' in x['query']), None)
    assert q and q['calls'] >= 30, [x['query'][:50] for x in r['top']]
    return f"запросов в топе {len(r['top'])}, тестовый: {q['calls']} вызовов, {q['mean_ms']} мс"


@test('Контрольные точки и WAL: счётчики и рекомендации')
def t_ckpt(con):
    c = pg(leader(con)); run(c, 'CHECKPOINT'); c.close()
    r = con.get('/api/perf/checkpoints?minutes=60')
    assert r['ok'] and r['recommendations'] and r['settings']['max_wal_size'], r
    return f"max_wal_size {r['settings']['max_wal_size']}, рекомендаций {len(r['recommendations'])}"


@test('Прогресс долгих операций (pg_stat_progress_vacuum)')
def t_progress(con):
    host = HOSTS[leader(con)]
    s = pg(host, 'shop')
    run(s, "CREATE TABLE IF NOT EXISTS big AS SELECT g AS id, md5(g::text) AS v FROM generate_series(1, 400000) g")
    run(s, "DELETE FROM big WHERE id % 3 = 0")
    def slow_vacuum():
        c = pg(host, 'shop')
        run(c, "SET vacuum_cost_delay = 20"); run(c, "SET vacuum_cost_limit = 10")
        try: run(c, "VACUUM big")
        except Exception: pass
        c.close()
    t = threading.Thread(target=slow_vacuum, daemon=True); t.start()
    seen = None
    for _ in range(40):
        ops = con.get('/api/progress')['data']
        seen = next((o for o in ops if o['kind'] == 'VACUUM' and 'big' in (o.get('target') or '')), None)
        if seen and seen.get('pct') is not None: break
        time.sleep(0.5)
    run(s, "SELECT pg_cancel_backend(pid) FROM pg_stat_activity WHERE query = 'VACUUM big'")
    s.close(); t.join(10)
    assert seen, 'VACUUM не появился в /api/progress'
    return f"{seen['kind']} {seen['target']}: фаза «{seen['phase']}», {seen.get('pct')}%"


@test('Аудит индексов: дубли, перекрытия, FK без индекса, невалидные, неиспользуемые')
def t_idx(con):
    s = pg(leader(con), 'shop')
    run(s, "CREATE TABLE IF NOT EXISTS items(id serial PRIMARY KEY, order_id bigint REFERENCES orders(id), sku text, qty int)")
    run(s, "CREATE INDEX IF NOT EXISTS items_sku ON items(sku)")
    run(s, "CREATE INDEX IF NOT EXISTS items_sku_dup ON items(sku)")
    run(s, "CREATE INDEX IF NOT EXISTS items_sku_qty ON items(sku, qty)")
    run(s, "INSERT INTO items(order_id, sku, qty) VALUES (1, 'A', 1), (2, 'A', 2)")
    try: run(s, "CREATE UNIQUE INDEX CONCURRENTLY items_sku_uq ON items(sku)")
    except Exception: pass
    s.close()
    r = con.get('/api/indexes/audit?db=shop')
    assert r['ok'], r
    c = r['counts']
    assert c['duplicate'] >= 1 and c['overlap'] >= 1 and c['fk_no_index'] >= 1 and c['invalid'] >= 1 and c['unused'] >= 1, c
    assert any('items_sku_uq' in x['index'] for x in r['invalid'])
    assert any('order_id' in x['columns'] for x in r['fk_no_index'])
    return str(c)


@test('Журналы PostgreSQL: агент читает, консоль разбирает')
def t_logs(con):
    node = leader(con)
    c = pg(node)
    try: run(c, 'SELECT 1/0')
    except Exception: pass
    c.close()
    try: psycopg2.connect(host=HOSTS[node], port=25432, user='postgres', password='wrong', dbname='postgres', connect_timeout=3)
    except Exception: pass
    st = con.get(f'/api/logs/settings?node={node}')
    assert st['ok'], st
    j = con.post('/api/logs/scan', {'node': node, 'max_mb': 4})
    assert j['ok'], j
    d = con.wait_cmd(node, j['data']['cmd_id'], 120, path='/api/logs/result')
    assert d.get('lines', 0) > 0, d
    assert any('division by zero' in e['message'] for e in d['errors']), [e['message'] for e in d['errors']][:5]
    assert any(a['user'] == 'postgres' for a in d['auth_failures']), d['auth_failures']
    return (f"{d['lines']} строк, ошибок {len(d['errors'])}, неудачных входов {sum(a['count'] for a in d['auth_failures'])}, "
            f"контрольных точек {d['checkpoints']['count']}, ожиданий блокировок {len(d['lock_waits'])}")


@test('Аудит безопасности и помощник обновлений')
def t_sec_upg(con):
    s = con.get('/api/security/audit')
    assert s['ok'] and 0 <= s['score'] <= 100, s
    titles = [f['title'] for f in s['findings']]
    assert any('SSL' in t for t in titles), titles
    u = con.get('/api/upgrade/check?target=17')
    assert u['ok'] and all(n['ok'] and n['data']['major'] == '16' for n in u['nodes']), u
    assert any(p['title'].startswith('Подготовленные') for p in u['prechecks'])
    return f"оценка {s['score']}/100, находок {len(titles)}; версия {u['nodes'][0]['data']['version']} → актуальная {u['nodes'][0]['data']['latest']}"


@test('Еженедельный отчёт (HTML) и прочие экраны')
def t_weekly(con):
    r = con.s.get(URL + '/report/weekly', timeout=60)
    assert r.status_code == 200 and 'Отчёт о состоянии PostgreSQL' in r.text, r.status_code
    for path in ('/api/db/overview', '/api/dba/summary', '/api/repl/slots', '/api/hosts', '/api/center', '/api/metrics/nodes',
                 '/api/dcs/health', '/api/alerts/active', '/api/admin/roles', '/api/tuning/catalog?ram_gb=8&cpu=4&maxconn=100&disk=ssd'):
        j = con.get(path)
        assert isinstance(j, dict) and j.get('ok'), (path, str(j)[:200])
    return 'отчёт и 10 эндпоинтов экранов ответили ok'


@test('Switchover через консоль и возврат')
def t_switchover(con):
    l0, r0 = leader(con), replica(con)
    def switch(l, c):
        j = con.post('/api/switchover', {'leader': l, 'candidate': c})
        assert j.get('ok'), j
        for _ in range(60):
            time.sleep(2)
            try:
                d = con.get('/api/cluster')['data']['members']
            except Exception:
                continue
            st = {m['name']: (m['role'], m['state']) for m in d}
            if st.get(c, ('',))[0] == 'leader' and st.get(l) == ('replica', 'streaming'):
                return st
        raise AssertionError(f'switchover {l}→{c} не завершился: {st}')
    st1 = switch(l0, r0)
    s = pg(r0, 'shop'); run(s, "INSERT INTO orders(customer_id, batch, amount) VALUES (1, 'after-switchover', 1)"); s.close()
    st2 = switch(r0, l0)
    tl = max(m.get('timeline') or 0 for m in con.get('/api/cluster')['data']['members'])
    return f'{l0}→{r0}→{l0}, timeline {tl}'


@test('Бэкап и архивация продолжаются после switchover')
def t_after_switch(con):
    seg = wait_archived(pg(leader(con)))
    node = replica(con)
    j = con.post('/api/backups', {'node': node, 'action': 'backup_delta'})
    res = con.wait_cmd(node, j['data']['cmd_id'], 600)
    assert res['ok'], res
    return f'сегмент {seg} в архиве, бэкап после смены лидера выполнен'


def main():
    t0 = time.time()
    con = Console()
    for t in (t_health, t_cluster, t_rbac, t_prepare, t_backup_full, t_backup_delta, t_pitr, t_restore,
              t_restore_test, t_clone, t_dump, t_risk, t_freeze, t_ash, t_statements, t_ckpt, t_progress,
              t_idx, t_logs, t_sec_upg, t_weekly, t_switchover, t_after_switch):
        t(con)
    ok = sum(1 for r in RESULTS if r[1] == 'PASS')
    print('\n' + '=' * 100)
    for name, st, sec, det in RESULTS:
        print(f'{st:5} {sec:7.1f} с  {name}' + (f'\n             {det}' if det else ''))
    print('=' * 100)
    print(f'ИТОГО: {ok}/{len(RESULTS)} прошло за {time.time() - t0:.0f} с')
    with open(os.path.join(ST, 'e2e_result.json'), 'w') as f:
        json.dump([{'test': n, 'status': s, 'sec': sec, 'detail': d} for n, s, sec, d in RESULTS], f, ensure_ascii=False, indent=1)
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == '__main__':
    main()
