"""HTTP API и раздача страницы.

Правила доступа:
  * каждый маршрут обязан объявить минимальную роль через @require(...);
    маршрут без объявления отклоняется (закрыто по умолчанию);
  * сессии серверные (SQLite), cookie хранит только идентификатор;
  * изменяющие запросы требуют заголовок X-CSRF-Token из ответа на вход;
  * кластер всегда указывается в пути запроса, «активного кластера» нет.
"""
import json
import logging
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

import hashlib
import threading

from . import dynconf
from . import ops as ops_mod
from . import status as status_mod
from .etcd import EtcdClient
from .notify import Notifier
from .patroni import PatroniClient, PatroniError
from .store import ClusterBusy, IdempotencyConflict, Store
from .watcher import Watcher

log = logging.getLogger('patroni_console')
ROLE_LEVEL = {'viewer': 0, 'operator': 1, 'admin': 2}
MAX_PASSWORD = 256
_RID_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def require(role):
    def deco(fn):
        fn.min_role = role
        return fn
    return deco


class JsonFormatter(logging.Formatter):
    def format(self, rec):
        d = {'ts': round(rec.created, 3), 'level': rec.levelname, 'logger': rec.name, 'msg': rec.getMessage()}
        for k in ('request_id', 'user', 'method', 'path', 'status', 'ms', 'cluster', 'channel', 'attempt', 'error', 'gave_up'):
            if hasattr(rec, k):
                d[k] = getattr(rec, k)
        if rec.exc_info:
            d['exc'] = self.formatException(rec.exc_info)
        return json.dumps(d, ensure_ascii=False)


def _validate_params(kind, raw, cluster):
    """Белый список параметров для каждого вида операции."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError('params должен быть объектом')
    names = {n.name for n in cluster.nodes}
    allowed = {
        'switchover': {'candidate', 'scheduled_at'}, 'failover': {'candidate'},
        'restart': {'node'}, 'reinitialize': {'node'}, 'rolling_restart': {'only_pending'},
        'pause': set(), 'resume': set(), 'cancel_switchover': set(),
        'config': {'changes', 'rollback_to', 'base_hash'},
    }[kind]
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f'лишние параметры: {", ".join(sorted(extra))}')
    p = {}
    for key in ('candidate', 'node'):
        if key in raw and raw[key] not in (None, ''):
            if raw[key] not in names:
                raise ValueError(f'{key}: ноды {raw[key]!r} нет в конфиге кластера')
            p[key] = raw[key]
    if kind in ('failover',) and 'candidate' not in p:
        raise ValueError('candidate обязателен')
    if kind in ('restart', 'reinitialize') and 'node' not in p:
        raise ValueError('node обязателен')
    if raw.get('scheduled_at'):
        p['scheduled_at'] = str(raw['scheduled_at'])[:40]
    if kind == 'config':
        if raw.get('rollback_to') not in (None, ''):
            if raw.get('changes'):
                raise ValueError('укажите либо changes, либо rollback_to')
            try:
                p['rollback_to'] = int(raw['rollback_to'])
            except (TypeError, ValueError):
                raise ValueError('rollback_to должен быть номером версии')
        else:
            p['changes'] = dynconf.validate(raw.get('changes'))
        if raw.get('base_hash'):
            p['base_hash'] = str(raw['base_hash'])[:32]
    if kind == 'rolling_restart':
        p['only_pending'] = bool(raw.get('only_pending', True))
    return p


def create_app(config, store=None, patroni_session_factory=None, etcd_session_factory=None,
               ops_timings=None, ops_sync=False, notify_session_factory=None):
    """Фабрика приложения. Импорт модуля ничего не запускает и в сеть не ходит.
    ops_sync=True выполняет операции в потоке запроса (для тестов).
    Наблюдатель и отправщик уведомлений создаются, но не запускаются:
    это делает start_background() (см. wsgi.py)."""
    app = Flask(__name__, static_folder=None)
    app.config['PC'] = config
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
    # с HTTPS cookie получает префикс __Host-: только этот хост, только Secure, путь /
    cookie_name = '__Host-pc_session' if config.cookie_secure else 'pc_session'
    # фиктивный хеш тем же методом, что у пользователей: время проверки не выдаёт, есть ли логин
    methods = [u.password_hash.split('$', 1)[0] for u in config.users.values()]
    dummy_hash = generate_password_hash(secrets.token_hex(16), method=max(set(methods), key=methods.count))
    status_cache = {}
    status_lock = threading.Lock()
    store = store or Store(config.db_path)
    store.op_interrupt_stale()
    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix='probe')
    static_dir = os.path.join(os.path.dirname(__file__), 'static')

    def patroni_client(cluster):
        s = patroni_session_factory() if patroni_session_factory else None
        return PatroniClient(cluster, timeout=config.timeout, session=s)

    def etcd_client(cluster):
        s = etcd_session_factory() if etcd_session_factory else None
        return EtcdClient(cluster.etcd, timeout=config.timeout, session=s)

    notifier = Notifier(store, config.channels, session_factory=notify_session_factory)
    watcher = Watcher(store, config, patroni_client, etcd_client, pool=pool, on_event=notifier.wake)
    app.extensions['pc_watcher'] = watcher
    app.extensions['pc_notifier'] = notifier

    def current_role(username):
        u = config.users.get(username)
        return u.role if u else None

    def remote():
        # X-Forwarded-For не доверяем: консоль должна стоять за прокси,
        # который сам подставляет remote_addr (ProxyFix настраивается явно).
        return request.remote_addr or '?'

    def err(code, message, http=400, **extra):
        body = {'ok': False, 'error': {'code': code, 'message': message, **extra}}
        return jsonify(body), http

    def get_cluster(name):
        return config.clusters.get(name)

    # ---------- конвейер запроса ----------
    @app.before_request
    def _gate():
        rid = request.headers.get('X-Request-Id', '')
        g.request_id = rid if _RID_RE.match(rid) else secrets.token_hex(8)
        g.t0 = time.monotonic()
        g.user = None
        view = app.view_functions.get(request.endpoint) if request.endpoint else None
        if view is None:
            return None  # 404/405 отдаст Flask
        min_role = getattr(view, 'min_role', None)
        if min_role is None:
            log.error('маршрут без объявленной роли', extra={'path': request.path})
            return err('forbidden', 'маршрут не настроен', 403)
        if min_role == 'public':
            return None
        sess = store.get_session(request.cookies.get(cookie_name), current_role)
        if not sess:
            return err('unauthorized', 'требуется вход', 401)
        g.user = sess
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(token.encode('utf-8', 'replace'), sess['csrf'].encode()):
                return err('csrf', 'неверный CSRF-токен', 403)
        if ROLE_LEVEL[sess['role']] < ROLE_LEVEL[min_role]:
            store.audit(request.endpoint, 'denied', sess['username'], remote(),
                        cluster=(request.view_args or {}).get('cluster'), request_id=g.request_id)
            return err('forbidden', f'требуется роль {min_role}', 403)
        return None

    @app.after_request
    def _after(resp):
        resp.headers['X-Request-Id'] = g.get('request_id', '')
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        resp.headers['X-Frame-Options'] = 'DENY'
        resp.headers['Referrer-Policy'] = 'no-referrer'
        resp.headers['Content-Security-Policy'] = "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        if request.is_secure:
            resp.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if request.path.startswith('/api/'):
            resp.headers['Cache-Control'] = 'no-store'
        log.info('request', extra={
            'request_id': g.get('request_id'), 'user': (g.get('user') or {}).get('username'),
            'method': request.method, 'path': request.path, 'status': resp.status_code,
            'ms': round((time.monotonic() - g.get('t0', time.monotonic())) * 1000)})
        return resp

    # ---------- служебное ----------
    @app.get('/healthz')
    @require('public')
    def healthz():
        # здоровье процесса консоли, а не кластеров
        try:
            store.audit_list(1)
            return jsonify({'ok': True})
        except Exception:
            log.exception('healthz: база недоступна')
            return jsonify({'ok': False}), 503

    @app.get('/')
    @require('public')
    def index():
        return send_from_directory(static_dir, 'index.html')

    @app.get('/static/<path:name>')
    @require('public')
    def static_files(name):
        return send_from_directory(static_dir, name)

    # ---------- вход ----------
    @app.post('/api/auth/login')
    @require('public')
    def login():
        b = request.get_json(silent=True) or {}
        username = str(b.get('username') or '')[:64]
        password = str(b.get('password') or '')[:MAX_PASSWORD]
        keys = [f'user:{username}', f'addr:{remote()}']
        if store.login_blocked(keys):
            # без записи в аудит: иначе поток запросов раздувает базу
            return err('throttled', 'слишком много попыток, подождите', 429)
        # попытка учитывается до проверки пароля: параллельные запросы не проскочат лимит
        store.login_failed(keys)
        u = config.users.get(username)
        ok = check_password_hash(u.password_hash if u else dummy_hash, password) and u is not None
        if not ok:
            store.audit('login', 'denied', username, remote(), request_id=g.request_id)
            return err('bad_credentials', 'неверный логин или пароль', 401)
        store.login_ok(keys)
        sid, csrf = store.create_session(u.name, u.role, remote())
        store.audit('login', 'ok', u.name, remote(), request_id=g.request_id)
        resp = jsonify({'ok': True, 'data': {'username': u.name, 'role': u.role, 'csrf_token': csrf}})
        resp.set_cookie(cookie_name, sid, httponly=True, secure=config.cookie_secure, samesite='Strict', path='/',
                        max_age=12 * 3600)
        return resp

    @app.post('/api/auth/logout')
    @require('viewer')
    def logout():
        store.delete_session(request.cookies.get(cookie_name))
        store.audit('logout', 'ok', g.user['username'], remote(), request_id=g.request_id)
        resp = jsonify({'ok': True})
        resp.delete_cookie(cookie_name, path='/', secure=config.cookie_secure, samesite='Strict')
        return resp

    @app.get('/api/auth/me')
    @require('viewer')
    def me():
        return jsonify({'ok': True, 'data': {'username': g.user['username'], 'role': g.user['role'],
                                             'csrf_token': g.user['csrf']}})

    # ---------- кластеры ----------
    @app.get('/api/clusters')
    @require('viewer')
    def clusters():
        return jsonify({'ok': True, 'data': [
            {'name': c.name, 'nodes': [n.name for n in c.nodes], 'etcd_configured': bool(c.etcd.endpoints)}
            for c in config.clusters.values()]})

    @app.get('/api/clusters/<cluster>/status')
    @require('viewer')
    def cluster_status(cluster):
        c = get_cluster(cluster)
        if not c:
            return err('not_found', 'кластер не найден', 404)
        # короткий кэш: много открытых вкладок не превращаются в шквал запросов к нодам
        with status_lock:
            hit = status_cache.get(cluster)
        if hit and time.monotonic() - hit[0] < 2.0:
            return jsonify({'ok': True, 'data': hit[1]})
        snap = status_mod.collect(c, patroni_client(c), pool)
        with status_lock:
            status_cache[cluster] = (time.monotonic(), snap)
        return jsonify({'ok': True, 'data': snap})

    def _from_any_node(c, fn, leader=None):
        # сначала лидер: кэш DCS на репликах отстаёт до loop_wait секунд,
        # и /config с реплики сразу после изменения показывает старое значение
        if leader is None:
            leader = (store.watch_state_get(c.name) or {}).get('leader')
        errors = []
        for n in sorted(c.nodes, key=lambda n: n.name != leader):
            try:
                return fn(n), None
            except PatroniError as e:
                errors.append(str(e))
        return None, errors

    @app.get('/api/clusters/<cluster>/history')
    @require('viewer')
    def cluster_history(cluster):
        c = get_cluster(cluster)
        if not c:
            return err('not_found', 'кластер не найден', 404)
        pc = patroni_client(c)
        data, errors = _from_any_node(c, pc.history)
        if data is None:
            return err('unavailable', 'ни одна нода не ответила', 502, details=errors)
        # [timeline, lsn, reason, timestamp, new_leader] — последние сверху
        rows = []
        for h in data:
            h = list(h) + [None] * (5 - len(h))
            rows.append({'timeline': h[0], 'lsn': h[1], 'reason': h[2], 'timestamp': h[3], 'new_leader': h[4]})
        return jsonify({'ok': True, 'data': list(reversed(rows))})

    @app.get('/api/clusters/<cluster>/config')
    @require('viewer')
    def cluster_config(cluster):
        c = get_cluster(cluster)
        if not c:
            return err('not_found', 'кластер не найден', 404)
        pc = patroni_client(c)
        data, errors = _from_any_node(c, pc.dynamic_config)
        if data is None:
            return err('unavailable', 'ни одна нода не ответила', 502, details=errors)
        return jsonify({'ok': True, 'data': data if g.user['role'] == 'admin' else dynconf.redact(data)})

    @app.get('/api/clusters/<cluster>/etcd')
    @require('viewer')
    def cluster_etcd(cluster):
        c = get_cluster(cluster)
        if not c:
            return err('not_found', 'кластер не найден', 404)
        return jsonify({'ok': True, 'data': etcd_client(c).health()})

    # ---------- операции ----------
    def _prepare(cluster):
        """Общая часть precheck и запуска: кластер, вид, параметры, свежий снимок, проверки."""
        c = get_cluster(cluster)
        if not c:
            return None, err('not_found', 'кластер не найден', 404)
        b = request.get_json(silent=True) or {}
        kind = b.get('kind')
        if kind not in ops_mod.KINDS:
            return None, err('bad_request', f'неизвестная операция; допустимы: {", ".join(ops_mod.KINDS)}')
        if ROLE_LEVEL[g.user['role']] < ROLE_LEVEL[ops_mod.KINDS[kind]]:
            store.audit(f'op:{kind}', 'denied', g.user['username'], remote(), cluster=cluster, request_id=g.request_id)
            return None, err('forbidden', f'операция {kind} требует роли {ops_mod.KINDS[kind]}', 403)
        try:
            params = _validate_params(kind, b.get('params'), c)
        except ValueError as e:
            return None, err('bad_request', str(e))
        pc = patroni_client(c)
        snap = status_mod.collect(c, pc, pool)
        dyn, _ = _from_any_node(c, pc.dynamic_config, snap.get('leader'))
        preview = None
        if kind == 'config':
            if dyn is None:
                return None, err('no_config', 'не удалось прочитать /config кластера', 502)
            if 'rollback_to' in params:
                v = store.config_version(cluster, params['rollback_to'])
                if not v:
                    return None, err('not_found', 'версия конфига не найдена', 404)
                params['changes'] = dynconf.patch_to(dyn, v['before'])
                if not params['changes']:
                    return None, err('no_changes', 'текущий конфиг уже совпадает с этой версией', 412)
            new = dynconf.merge(dyn, params['changes'])
            preview = {'diff': dynconf.diff(dyn, new), 'base_hash': dynconf.config_hash(dyn),
                       'changes': params['changes']}
        cks = ops_mod.checks(kind, snap, dyn, params)
        if dyn is None:
            cks.insert(0, {'level': 'block', 'code': 'no_config', 'text': 'не удалось прочитать /config кластера'})
        return {'cluster': c, 'kind': kind, 'params': params, 'body': b, 'snap': snap, 'checks': cks,
                'preview': preview}, None

    @app.post('/api/clusters/<cluster>/operations/precheck')
    @require('operator')
    def op_precheck(cluster):
        ctx, e = _prepare(cluster)
        if e:
            return e
        ok, code, msg = ops_mod.gate(ctx['checks'], True, True, g.user['role'])
        return jsonify({'ok': True, 'data': {
            'kind': ctx['kind'], 'params': ctx['params'], 'checks': ctx['checks'],
            'can_run': ok or code in ('force_required',),
            'needs_force': any(c['level'] == 'force' for c in ctx['checks']),
            'needs_ack': any(c['level'] == 'warn' for c in ctx['checks']),
            'leader': ctx['snap']['leader'], 'failover_candidates': ctx['snap']['failover_candidates'],
            'preview': ctx['preview']}})

    @app.post('/api/clusters/<cluster>/operations')
    @require('operator')
    def op_start(cluster):
        # повтор с тем же Idempotency-Key отвечаем сразу, не трогая кластер
        key = (request.headers.get('Idempotency-Key') or '')[:100] or None
        b0 = request.get_json(silent=True) or {}
        c0 = get_cluster(cluster)
        rhash = None
        if c0 and b0.get('kind') in ops_mod.KINDS:
            try:
                rhash = hashlib.sha256(json.dumps(
                    [b0['kind'], _validate_params(b0['kind'], b0.get('params'), c0)], sort_keys=True).encode()).hexdigest()
            except ValueError:
                pass
        kind0 = b0.get('kind')
        if kind0 in ops_mod.KINDS and ROLE_LEVEL[g.user['role']] < ROLE_LEVEL[ops_mod.KINDS[kind0]]:
            store.audit(f'op:{kind0}', 'denied', g.user['username'], remote(), cluster=cluster, request_id=g.request_id)
            return err('forbidden', f'операция {kind0} требует роли {ops_mod.KINDS[kind0]}', 403)
        if key and rhash:
            prev, prev_hash = store.op_by_key(cluster, key, g.user['username'])
            if prev_hash == 'foreign':
                return err('idempotency_conflict', 'ключ уже использован другим пользователем', 409)
            if prev:
                if prev_hash != rhash:
                    return err('idempotency_conflict', 'ключ уже использован для другой операции', 409)
                return jsonify({'ok': True, 'data': prev}), 200
        ctx, e = _prepare(cluster)
        if e:
            return e
        b = ctx['body']
        if b.get('confirm') != cluster:
            return err('confirm_required', 'для подтверждения введите имя кластера', 400)
        ok, code, msg = ops_mod.gate(ctx['checks'], bool(b.get('acknowledge_warnings')), bool(b.get('force')),
                                     g.user['role'])
        if not ok:
            store.audit(f'op:{ctx["kind"]}', 'denied', g.user['username'], remote(), cluster=cluster,
                        params=ctx['params'], detail=code, request_id=g.request_id)
            return err(code, msg, 403 if code == 'forbidden' else 412, checks=ctx['checks'])
        try:
            op, created = store.op_create(cluster, ctx['kind'], ctx['params'], g.user['username'], key, rhash)
        except ClusterBusy as ex:
            return err('cluster_busy', 'на кластере уже выполняется операция', 409, active=ex.active)
        except IdempotencyConflict:
            return err('idempotency_conflict', 'ключ уже использован для другой операции', 409)
        if not created:
            return jsonify({'ok': True, 'data': op}), 200
        store.audit(f'op:{ctx["kind"]}', 'ok', g.user['username'], remote(), cluster=cluster,
                    params={'operation_id': op['id'], **ctx['params'], 'force': bool(b.get('force'))},
                    detail='запуск', request_id=g.request_id)
        runner = ops_mod.Runner(store, ctx['cluster'], patroni_client(ctx['cluster']), ops_timings)

        def run_and_forget_status(o):
            try:
                runner.run(o)
            finally:
                # операция поменяла кластер: следующий запрос статуса не должен попасть в старый кэш
                with status_lock:
                    status_cache.pop(cluster, None)
        if ops_sync:
            run_and_forget_status(op)
        else:
            threading.Thread(target=run_and_forget_status, args=(op,), name=f'op-{op["id"]}', daemon=True).start()
        return jsonify({'ok': True, 'data': store.op_get(op['id'])}), 202

    @app.get('/api/clusters/<cluster>/operations')
    @require('viewer')
    def op_list(cluster):
        if not get_cluster(cluster):
            return err('not_found', 'кластер не найден', 404)
        return jsonify({'ok': True, 'data': store.op_list(cluster)})

    @app.get('/api/operations/<int:op_id>')
    @require('viewer')
    def op_get(op_id):
        op = store.op_get(op_id)
        if not op or op['cluster'] not in config.clusters:
            return err('not_found', 'операция не найдена', 404)
        return jsonify({'ok': True, 'data': op})

    @app.get('/api/clusters/<cluster>/config/versions')
    @require('viewer')
    def config_versions(cluster):
        if not get_cluster(cluster):
            return err('not_found', 'кластер не найден', 404)
        out = []
        admin = g.user['role'] == 'admin'
        for v in store.config_versions(cluster):
            before, after = (v['before'], v['after']) if admin else (dynconf.redact(v['before']), dynconf.redact(v['after']))
            out.append({k: v[k] for k in ('id', 'ts', 'source', 'username', 'op_id')} |
                       {'diff': dynconf.diff(before, after)})
        return jsonify({'ok': True, 'data': out})

    # ---------- события ----------
    @app.get('/api/clusters/<cluster>/events')
    @require('viewer')
    def events_list(cluster):
        if not get_cluster(cluster):
            return err('not_found', 'кластер не найден', 404)
        try:
            limit = min(max(int(request.args.get('limit', 100)), 1), 500)
        except ValueError:
            return err('bad_request', 'limit должен быть числом')
        open_ = sorted(store.conditions_get(cluster).values(), key=lambda c: c['opened_at'])
        return jsonify({'ok': True, 'data': {
            'events': store.events_list(cluster, limit),
            'open': open_,
            'watcher': {**(watcher.state.get(cluster) or {}), 'interval': config.watch.interval,
                        'running': watcher._thread is not None},
            'channels': [{'name': c.name, 'type': c.type, 'min_level': c.min_level} for c in config.channels],
        }})

    @app.post('/api/clusters/<cluster>/notify-test')
    @require('admin')
    def notify_test(cluster):
        if not get_cluster(cluster):
            return err('not_found', 'кластер не найден', 404)
        if not config.channels:
            return err('no_channels', 'каналы уведомлений не настроены (секция notify в конфиге)', 412)
        ev = store.event_add(cluster, 'info', 'info', 'test', f'тестовое уведомление от {g.user["username"]}',
                             channels=[c.name for c in config.channels])
        store.audit('notify_test', 'ok', g.user['username'], remote(), cluster, {}, None, g.request_id)
        notifier.wake()
        return jsonify({'ok': True, 'data': ev}), 202

    @app.get('/api/audit')
    @require('admin')
    def audit_list():
        try:
            limit = min(max(int(request.args.get('limit', 200)), 1), 1000)
        except ValueError:
            return err('bad_request', 'limit должен быть числом')
        return jsonify({'ok': True, 'data': store.audit_list(limit)})

    @app.errorhandler(Exception)
    def _unhandled(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return err(e.name.lower().replace(' ', '_'), e.description, e.code)
        log.exception('необработанная ошибка', extra={'request_id': g.get('request_id')})
        return err('internal', 'внутренняя ошибка, см. журнал по request_id', 500, request_id=g.get('request_id'))

    return app


def start_background(app):
    """Запускает наблюдатель и отправщик уведомлений. Вызывать один раз на процесс."""
    app.extensions['pc_notifier'].start()
    app.extensions['pc_watcher'].start()
