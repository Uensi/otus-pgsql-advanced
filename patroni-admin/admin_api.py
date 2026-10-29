#!/usr/bin/env python3
"""Админ-модуль: расширения, роли, табличные пространства, diff pg_settings и patroni.yml."""
import re
from flask import Blueprint, jsonify, request

bp = Blueprint('admin', __name__)
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def _app():
    import app as A; return A

def qid(name):
    if not _IDENT.match(name or ''): raise ValueError(f'недопустимый идентификатор: {name}')
    return '"' + name + '"'

def _leader_conn():
    A = _app()
    ln = A.leader_node()
    if not ln: return None, 'лидер не найден'
    c = A.pg_conn(ln['host'])
    if not c: return None, 'нет подключения к лидеру'
    return c, None

# ---------- Расширения ----------
@bp.route('/api/admin/extensions')
def ext_list():
    A = _app(); c, err = _leader_conn()
    if not c: return jsonify({'ok': False, 'error': err})
    try:
        inst = A.q(c, "SELECT e.extname, e.extversion, n.nspname AS schema FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace ORDER BY e.extname")
        avail = A.q(c, "SELECT name, default_version, installed_version, comment FROM pg_available_extensions ORDER BY name")
        return jsonify({'ok': True, 'data': {'installed': inst, 'available': avail}})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})
    finally: c.close()

@bp.route('/api/admin/extensions', methods=['POST'])
def ext_mod():
    A = _app(); b = request.json or {}; act = b.get('action'); name = b.get('name','')
    try: ident = qid(name)
    except ValueError as e: return jsonify({'ok': False, 'error': str(e)})
    c, err = _leader_conn()
    if not c: return jsonify({'ok': False, 'error': err})
    try:
        cur = c.cursor()
        if act == 'create': cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ident}")
        elif act == 'drop': cur.execute(f"DROP EXTENSION IF EXISTS {ident} CASCADE")
        else: cur.close(); return jsonify({'ok': False, 'error': 'недопустимое действие'})
        cur.close(); return jsonify({'ok': True, 'data': {'message': f'{act} {name} выполнено'}})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})
    finally: c.close()

# ---------- Роли ----------
@bp.route('/api/admin/roles')
def roles_list():
    A = _app(); c, err = _leader_conn()
    if not c: return jsonify({'ok': False, 'error': err})
    try:
        rows = A.q(c, """
            SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolcanlogin, r.rolreplication,
                   r.rolconnlimit, r.rolvaliduntil::text,
                   (SELECT array_agg(b.rolname) FROM pg_auth_members m JOIN pg_roles b ON b.oid=m.roleid WHERE m.member=r.oid) AS member_of
            FROM pg_roles r ORDER BY r.rolname""")
        return jsonify({'ok': True, 'data': rows})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})
    finally: c.close()

@bp.route('/api/admin/roles', methods=['POST'])
def roles_mod():
    A = _app(); b = request.json or {}; act = b.get('action'); name = b.get('name','')
    try: ident = qid(name)
    except ValueError as e: return jsonify({'ok': False, 'error': str(e)})
    # валидация параметров ДО подключения к лидеру
    if act in ('grant', 'revoke'):
        target = b.get('role', '')
        if not target: return jsonify({'ok': False, 'error': 'роль не указана'})
        try: qtarget = qid(target)
        except ValueError as e: return jsonify({'ok': False, 'error': str(e)})
    elif act not in ('create', 'drop'):
        return jsonify({'ok': False, 'error': 'недопустимое действие'})
    c, err = _leader_conn()
    if not c: return jsonify({'ok': False, 'error': err})
    try:
        cur = c.cursor()
        if act == 'create':
            opts = ['LOGIN' if b.get('login', True) else 'NOLOGIN']
            if b.get('superuser'): opts.append('SUPERUSER')
            if b.get('createdb'): opts.append('CREATEDB')
            if b.get('createrole'): opts.append('CREATEROLE')
            if b.get('replication'): opts.append('REPLICATION')
            sql = f"CREATE ROLE {ident} WITH " + ' '.join(opts)
            if b.get('password'): sql += " PASSWORD '" + str(b['password']).replace("'", "''") + "'"
            cur.execute(sql)
        elif act == 'drop': cur.execute(f"DROP ROLE IF EXISTS {ident}")
        elif act == 'grant': cur.execute(f"GRANT {qtarget} TO {ident}")
        elif act == 'revoke': cur.execute(f"REVOKE {qtarget} FROM {ident}")
        cur.close(); return jsonify({'ok': True, 'data': {'message': f'{act} {name} выполнено'}})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})
    finally: c.close()

# ---------- Табличные пространства ----------
@bp.route('/api/admin/tablespaces')
def ts_list():
    A = _app(); c, err = _leader_conn()
    if not c: return jsonify({'ok': False, 'error': err})
    try:
        rows = A.q(c, "SELECT spcname, pg_tablespace_location(oid) AS location, pg_tablespace_size(oid) AS size FROM pg_tablespace ORDER BY spcname")
        return jsonify({'ok': True, 'data': rows})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})
    finally: c.close()

@bp.route('/api/admin/tablespaces', methods=['POST'])
def ts_mod():
    A = _app(); b = request.json or {}; act = b.get('action'); name = b.get('name',''); loc = b.get('location','')
    try: ident = qid(name)
    except ValueError as e: return jsonify({'ok': False, 'error': str(e)})
    c, err = _leader_conn()
    if not c: return jsonify({'ok': False, 'error': err})
    try:
        cur = c.cursor()
        if act == 'create':
            if not loc: cur.close(); return jsonify({'ok': False, 'error': 'location обязателен'})
            cur.execute(f"CREATE TABLESPACE {ident} LOCATION '" + loc.replace("'", "''") + "'")
        elif act == 'drop': cur.execute(f"DROP TABLESPACE IF EXISTS {ident}")
        else: cur.close(); return jsonify({'ok': False, 'error': 'недопустимое действие'})
        cur.close(); return jsonify({'ok': True, 'data': {'message': f'{act} {name} выполнено'}})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})
    finally: c.close()

# ---------- Diff pg_settings ----------
@bp.route('/api/admin/config-diff')
def cfg_diff():
    A = _app()
    a = request.args.get('a'); b = request.args.get('b')
    na = A.find_node(a)
    nb = A.find_node(b)
    if not na or not nb: return jsonify({'ok': False, 'error': 'выберите две ноды'})
    def settings(host):
        c = A.pg_conn(host)
        if not c: return None
        try:
            rows = A.q(c, "SELECT name, setting, source FROM pg_settings")
            return {r['name']: (r['setting'], r['source']) for r in rows}
        finally: c.close()
    sa = settings(na['host']); sb = settings(nb['host'])
    if sa is None or sb is None: return jsonify({'ok': False, 'error': 'нет связи с одной из нод'})
    diff = []
    for name in sorted(set(sa) | set(sb)):
        va = sa.get(name); vb = sb.get(name)
        if (va[0] if va else None) != (vb[0] if vb else None):
            diff.append({'name': name, 'a': va[0] if va else None, 'b': vb[0] if vb else None,
                         'source_a': va[1] if va else None, 'source_b': vb[1] if vb else None})
    return jsonify({'ok': True, 'data': {'a': a, 'b': b, 'diff': diff}})

# ---------- Diff patroni.yml ----------
_NODE_SPECIFIC = {'name','connect_address','listen','restapi.connect_address','restapi.listen','postgresql.listen','postgresql.connect_address'}

def _flatten(d, prefix=''):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)): out.update(_flatten(v, path))
            else: out[path] = v
    elif isinstance(d, list):
        for i, v in enumerate(d):
            path = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)): out.update(_flatten(v, path))
            else: out[path] = v
    return out

def _is_node_specific(path):
    for ns in _NODE_SPECIFIC:
        if path == ns or path.endswith('.' + ns): return True
    return False

@bp.route('/api/admin/patroni-diff')
def patroni_diff():
    A = _app(); ag = A.agent_api
    if ag is None: return jsonify({'ok': False, 'error': 'агент недоступен'})
    a = request.args.get('a'); b = request.args.get('b')
    na = A.find_node(a)
    nb = A.find_node(b)
    if not na or not nb: return jsonify({'ok': False, 'error': 'выберите две ноды'})
    ca, erra = ag.agent_read_config(a)
    cb, errb = ag.agent_read_config(b)
    if ca is None: return jsonify({'ok': False, 'error': f'{a}: {erra}'})
    if cb is None: return jsonify({'ok': False, 'error': f'{b}: {errb}'})
    fa = _flatten(ca); fb = _flatten(cb)
    diff = []; same = 0
    for path in sorted(set(fa) | set(fb)):
        if _is_node_specific(path): continue
        va = fa.get(path); vb = fb.get(path)
        na_ = None if va in (None, '') else va
        nb_ = None if vb in (None, '') else vb
        if na_ == nb_: same += 1; continue
        diff.append({'path': path, 'a': va, 'b': vb, 'only_a': path not in fb, 'only_b': path not in fa})
    return jsonify({'ok': True, 'data': {'a': a, 'b': b, 'diff': diff, 'same': same}})

@bp.route('/api/admin/nodes')
def admin_nodes():
    A = _app()
    return jsonify({'ok': True, 'data': [n['name'] for n in A.NODES]})

