#!/usr/bin/env python3
"""Общие хелперы для DBA-модулей: лидер кластера, подключения, кэш, версии PostgreSQL.

Модули risk/perf/secaudit/logscan получают доступ к состоянию app.py лениво
(через _app()), чтобы не было циклических импортов."""
import re
import threading
import time

LEADER_ROLES = ('leader', 'master', 'primary', 'standby_leader')
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]{0,62}$')
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def app():
    import app as A
    return A


def ident_ok(name):
    return bool(name) and bool(_IDENT.match(name))


def cached(key, ttl, fn):
    """Результат fn() кэшируется на ttl секунд (тяжёлые запросы к каталогу не чаще)."""
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now + ttl, val)
    return val


def cache_clear(prefix=''):
    with _CACHE_LOCK:
        for k in [k for k in _CACHE if k.startswith(prefix)]:
            _CACHE.pop(k, None)


def nodes():
    return list(app().NODES)


def node_by_name(name):
    return app().find_node(name)


def leader():
    """Узел-лидер по кэшу опроса Patroni (без лишних REST-запросов)."""
    A = app()
    try:
        lsn, _ = A.collect_lsn_lag()
        for n in A.NODES:
            if (lsn.get(n['name']) or {}).get('role') in LEADER_ROLES:
                return n
    except Exception:
        pass
    try:
        return A.leader_node()
    except Exception:
        return None


def conn(node, db=None):
    if not node:
        return None
    return app().pg_conn(node['host'], database=db) if db else app().pg_conn(node['host'])


def q(c, sql, params=None):
    return app().q(c, sql, params)


def q1(c, sql, params=None):
    rows = q(c, sql, params)
    return rows[0] if rows else {}


def try_q(c, sql, params=None, default=None):
    """Запрос, который может не поддерживаться версией/правами: ошибка → default."""
    try:
        return q(c, sql, params)
    except Exception:
        return [] if default is None else default


def server_version_num(c):
    try:
        return int(q1(c, "SELECT current_setting('server_version_num')::int AS v")['v'])
    except Exception:
        return 0


def setting(c, name, default=None):
    try:
        return q1(c, "SELECT current_setting(%s, true) AS v", (name,)).get('v', default)
    except Exception:
        return default


def databases(c):
    return [r['datname'] for r in try_q(c, "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY pg_database_size(datname) DESC")]


def on_leader(fn, db=None):
    """fn(conn, node) на лидере; None, если лидер недоступен."""
    ld = leader()
    c = conn(ld, db)
    if not c:
        return None
    try:
        return fn(c, ld)
    finally:
        try:
            c.close()
        except Exception:
            pass


def on_each_node(fn):
    """fn(conn, node) на каждом узле параллельно → [{'node','ok','data'|'error'}]."""
    A = app()
    def run(n):
        c = conn(n)
        if not c:
            return {'node': n['name'], 'ok': False, 'error': 'нет подключения к PostgreSQL'}
        try:
            return {'node': n['name'], 'ok': True, 'data': fn(c, n)}
        except Exception as e:
            return {'node': n['name'], 'ok': False, 'error': str(e)}
        finally:
            try:
                c.close()
            except Exception:
                pass
    futs = [A.EXECUTOR.submit(run, n) for n in A.NODES]
    return [f.result() for f in futs]


def worst(levels):
    order = {'bad': 3, 'warn': 2, 'info': 1, 'ok': 0, 'unknown': 0}
    best = 'ok'
    for lv in levels:
        if order.get(lv, 0) > order.get(best, 0):
            best = lv
    return best


def fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024
