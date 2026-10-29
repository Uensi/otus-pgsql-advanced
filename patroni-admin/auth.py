#!/usr/bin/env python3
"""RBAC + аудит: сессии, роли (viewer/dba/admin), журнал действий, хеши паролей."""
import os, json, time
from collections import deque
from flask import Blueprint, jsonify, request, session, Response

try:
    from werkzeug.security import generate_password_hash, check_password_hash
    HAS_WERKZEUG = True
except ImportError:
    HAS_WERKZEUG = False

bp = Blueprint('auth', __name__)
USERS = []
AUDIT = deque(maxlen=2000)
from paths import data_path
AUDIT_PATH = data_path('audit.jsonl')
AUDIT_FILE_MAX_BYTES = 10 * 1024 * 1024   # ротация аудит-файла при 10 МБ

# простейший rate-limit логина: >10 неудачных попыток с одного IP за 15 минут
_LOGIN_ATTEMPTS = {}
_LOGIN_LOCK = __import__('threading').Lock()
_LOGIN_MAX = 10
_LOGIN_WINDOW = 900

ROLE_LEVEL = {'viewer': 0, 'dba': 1, 'admin': 2}
ADMIN_ONLY = ['/api/ops/', '/api/switchover', '/api/failover', '/api/reinitialize', '/api/pause',
              '/api/dba/kill', '/api/dba/cancel', '/api/dba/kill-user-sessions', '/api/dba/drop-index',
              '/api/backups/delete_backup', '/api/backups/set_wal_archiving',
              '/api/center/select', '/api/backup-schedule', '/api/dump-schedule', '/api/admin/', '/api/hosts/service',
              '/api/templates/apply', '/api/maint-schedule',
              '/api/notify/config', '/api/dcs/config', '/api/dcs/discover', '/api/dcs/patroni-config']

def _load_audit():
    try:
        with open(AUDIT_PATH, encoding='utf-8', errors='replace') as f:
            for line in f:
                try: AUDIT.append(json.loads(line))
                except Exception: pass
    except Exception: pass

def _rotate_audit():
    """Аудит-файл перерос лимит: оставляем половину последних записей."""
    try:
        tail = list(AUDIT)[-max(1, len(AUDIT) // 2):]
        tmp = AUDIT_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            for rec in tail: f.write(json.dumps(rec) + '\n')
        os.replace(tmp, AUDIT_PATH)
    except Exception: pass

def audit(user, method, path, extra=''):
    rec = {'ts': time.time(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
           'user': user or '?', 'method': method, 'path': path, 'extra': extra}
    AUDIT.append(rec)
    try:
        if os.path.exists(AUDIT_PATH) and os.path.getsize(AUDIT_PATH) > AUDIT_FILE_MAX_BYTES:
            _rotate_audit()
        with open(AUDIT_PATH, 'a', encoding='utf-8') as f: f.write(json.dumps(rec) + '\n')
    except Exception: pass

TRUST_PROXY = False
_LDAP_HINT = None
import ldap_auth

def init_app(app, config):
    global USERS, TRUST_PROXY, _LDAP_HINT
    ldap_auth.configure(config)
    _LDAP_HINT = ((config.get('ldap') or {}).get('hint'))
    TRUST_PROXY = bool((config.get('web', {}) or {}).get('trust_proxy', False))
    raw_users = config.get('users') or [{'name': 'admin', 'password': 'admin', 'role': 'admin'}]
    USERS = []
    for u in raw_users:
        rec = dict(u)
        # автохешируем пароли, если они ещё не захешированы (pbkdf2:/scrypt:)
        if HAS_WERKZEUG:
            pwd = rec.get('password', '')
            if pwd and not (pwd.startswith('pbkdf2:') or pwd.startswith('scrypt:')):
                rec['_password_hash'] = generate_password_hash(pwd)
                rec.pop('password', None)
            else:
                rec['_password_hash'] = pwd
                rec.pop('password', None)
        USERS.append(rec)
    secret = config.get('web', {}).get('secret') or os.environ.get('PATRONI_CONSOLE_SECRET')
    if not secret:
        import warnings
        warnings.warn('web.secret не задан — секретный ключ сессий генерируется случайно при каждом старте '
                      '(сёссии сбрасываются). Задайте web.secret в config.yaml или PATRONI_CONSOLE_SECRET.')
    app.secret_key = secret or os.urandom(32).hex()
    # cookie-политика: защита от CSRF-подобных подмен и фиксации сессии
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=bool(config.get('web', {}).get('cookie_secure', False)),
        PERMANENT_SESSION_LIFETIME=__import__('datetime').timedelta(hours=12),
    )
    _load_audit()

    metrics_token = (config.get('web', {}) or {}).get('metrics_token') or os.environ.get('PATRONI_CONSOLE_METRICS_TOKEN')

    @app.before_request
    def _auth_gate():
        path = request.path
        # агенты аутентифицируются общим ключом в теле запроса (agent_api._key_ok);
        # /api/agent/status — для браузера и требует сессии, как остальной API
        if path in AGENT_KEY_PATHS: return None
        if path in PUBLIC_PATHS or path.startswith('/static/'): return None
        if path == '/metrics':
            if metrics_token and request.headers.get('Authorization', '') != 'Bearer ' + metrics_token \
                    and request.args.get('token') != metrics_token:
                return Response('unauthorized\n', 401, {'Content-Type': 'text/plain'})
            return None
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and path.startswith('/api/') and _cross_site():
            return jsonify({'ok': False, 'error': 'запрос с чужого сайта отклонён (CSRF)'}), 403
        if path in ('/api/auth/login', '/login'): return None
        if not session.get('user'):
            if path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'unauthorized'}), 401
            return None
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and path.startswith('/api/'):
            role = session.get('role', 'viewer')
            if ROLE_LEVEL.get(role, 0) < 1:
                return jsonify({'ok': False, 'error': 'недостаточно прав (viewer)'}), 403
            if role == 'dba' and any(path.startswith(p) for p in ADMIN_ONLY):
                return jsonify({'ok': False, 'error': 'требуется роль admin'}), 403
            audit(session.get('user'), request.method, path)
        return None

AGENT_KEY_PATHS = ('/api/agent/register', '/api/agent/poll', '/api/agent/result')
PUBLIC_PATHS = ('/healthz', '/readyz', '/favicon.ico', '/api/auth/providers')

def _cross_site():
    """Защита от CSRF без токенов: браузер всегда помечает межсайтовый запрос
    заголовками Sec-Fetch-Site / Origin. Скрипты и curl их не шлют — им не мешаем."""
    sfs = request.headers.get('Sec-Fetch-Site')
    if sfs and sfs not in ('same-origin', 'none'):
        return True
    origin = request.headers.get('Origin')
    if origin and origin != 'null':
        from urllib.parse import urlparse
        return urlparse(origin).netloc != request.host
    return False

def _login_blocked(ip):
    with _LOGIN_LOCK:
        now = time.time()
        _LOGIN_ATTEMPTS[ip] = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _LOGIN_WINDOW]
        return len(_LOGIN_ATTEMPTS[ip]) >= _LOGIN_MAX

def _login_fail(ip):
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())

def _login_reset(ip):
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop(ip, None)

@bp.route('/api/auth/login', methods=['POST'])
def login():
    b = request.json or {}
    u = b.get('user'); p = b.get('password')
    # X-Forwarded-For подделывается клиентом: доверяем ему только за своим reverse-proxy
    ip = (request.headers.get('X-Forwarded-For', '') if TRUST_PROXY else '').split(',')[0].strip() or request.remote_addr or '?'
    if _login_blocked(ip):
        return jsonify({'ok': False, 'error': 'слишком много попыток входа, подождите'}), 429
    for rec in USERS:
        if rec.get('name') != u: continue
        ok = False
        if HAS_WERKZEUG:
            ok = check_password_hash(rec['_password_hash'], p or '')
        else:
            ok = (rec.get('password') == p)
        if ok:
            return _start_session(u, rec.get('role', 'viewer'), 'local', ip)
    # локального пользователя нет или пароль не подошёл — пробуем домен (LDAP/AD)
    if ldap_auth.enabled() and u and not any(r.get('name') == u for r in USERS):
        role, why = ldap_auth.authenticate(u, p)
        if role:
            return _start_session(u, role, 'ldap', ip)
        audit(u, 'LOGIN_FAIL', '/api/auth/login', 'ldap: ' + (why or ''))
    _login_fail(ip)
    return jsonify({'ok': False, 'error': 'неверный логин или пароль'}), 401

def _start_session(u, role, source, ip):
    session.clear()                     # защита от фиксации сессии
    session.permanent = True
    session['user'] = u; session['role'] = role; session['auth'] = source
    _login_reset(ip)
    audit(u, 'LOGIN', '/api/auth/login', source)
    return jsonify({'ok': True, 'data': {'user': u, 'role': role, 'auth': source}})

@bp.route('/api/auth/providers')
def providers():
    """Публично: какие способы входа включены (подсказка на форме входа)."""
    return jsonify({'ok': True, 'data': {'local': True, 'ldap': ldap_auth.enabled(),
                                         'ldap_hint': (_LDAP_HINT or 'доменная учётная запись') if ldap_auth.enabled() else None}})

@bp.route('/api/auth/logout', methods=['POST'])
def logout():
    audit(session.get('user'), 'LOGOUT', '')
    session.clear()
    return jsonify({'ok': True})

@bp.route('/api/auth/me')
def me():
    if not session.get('user'): return jsonify({'ok': True, 'data': {'user': None}})
    return jsonify({'ok': True, 'data': {'user': session['user'], 'role': session.get('role', 'viewer'),
                                         'demo': bool(os.environ.get('PATRONI_CONSOLE_DEMO'))}})

@bp.route('/api/audit')
def audit_list():
    if session.get('role') != 'admin': return jsonify({'ok': False, 'error': 'только admin'}), 403
    return jsonify({'ok': True, 'data': list(AUDIT)[-500:]})

