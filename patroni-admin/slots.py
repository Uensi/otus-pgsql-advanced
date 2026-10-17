#!/usr/bin/env python3
"""Мониторинг слотов репликации: лаг, активность, опасные (зависшие) слоты."""
import re
from flask import Blueprint, jsonify, request

bp = Blueprint('slots', __name__)
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
DANGER_BYTES = 1024 * 1024 * 1024   # 1 GB — порог «опасно» для неактивного слота

def _app():
    import app as A; return A

@bp.route('/api/repl/slots')
def slots_list():
    A = _app()
    out = []
    for n in A.NODES:
        c = A.pg_conn(n['host'])
        if not c:
            out.append({'node': n['name'], 'ok': False, 'slots': [], 'repl': []}); continue
        try:
            slots = A.q(c, """
                SELECT slot_name, slot_type, active, plugin, restart_lsn::text,
                       CASE WHEN pg_is_in_recovery()
                            THEN pg_wal_lsn_diff(pg_last_wal_replay_lsn(), restart_lsn)
                            ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) END AS lag_bytes
                FROM pg_replication_slots ORDER BY slot_name""")
            repl = []
            try:
                repl = A.q(c, """
                    SELECT client_addr::text AS client, application_name, state,
                           pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn) AS sent_lag,
                           pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag
                    FROM pg_stat_replication ORDER BY client_addr""")
            except Exception: repl = []
            for s in slots:
                lag = s.get('lag_bytes') or 0
                s['danger'] = (not s['active']) and lag > DANGER_BYTES
                s['lag_bytes'] = lag
            out.append({'node': n['name'], 'ok': True, 'slots': slots, 'repl': repl})
        except Exception as e:
            out.append({'node': n['name'], 'ok': False, 'slots': [], 'repl': [], 'error': str(e)})
        finally: c.close()
    return jsonify({'ok': True, 'data': out})

@bp.route('/api/repl/slots/drop', methods=['POST'])
def slots_drop():
    A = _app()
    b = request.json or {}; node = b.get('node'); name = b.get('name', '')
    if not _IDENT.match(name or ''): return jsonify({'ok': False, 'error': 'недопустимое имя слота'})
    t = next((n for n in A.NODES if n['name'] == node), None)
    if not t: return jsonify({'ok': False, 'error': 'нода не найдена'})
    c = A.pg_conn(t['host'])
    if not c: return jsonify({'ok': False, 'error': 'нет подключения'})
    try:
        cur = c.cursor()
        cur.execute("SELECT active FROM pg_replication_slots WHERE slot_name=%s", (name,))
        row = cur.fetchone()
        if not row: cur.close(); return jsonify({'ok': False, 'error': 'слот не найден'})
        if row[0]: cur.close(); return jsonify({'ok': False, 'error': 'слот активен — удаление опасно'})
        cur.execute('SELECT pg_drop_replication_slot(%s)', (name,)); cur.close()
        return jsonify({'ok': True, 'data': {'message': f'слот {name} удалён'}})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})
    finally: c.close()

