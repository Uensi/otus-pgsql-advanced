#!/usr/bin/env python3
"""Аудит безопасности кластера и помощник обновлений PostgreSQL.

Безопасность (/api/security/audit): суперпользователи, хеши md5 вместо SCRAM,
пароли без срока, правила pg_hba.conf (trust/md5/password для всех адресов),
SSL и незашифрованные подключения, CREATE в схеме public, журналирование входов.
Итог — оценка 0–100 и список находок с исправлением.

Обновления (/api/upgrade/check): отставание от последней минорной версии,
окончание поддержки (EOL) и предварительные проверки pg_upgrade для перехода
на новую мажорную версию."""
import json
import os
from datetime import date

from flask import Blueprint, jsonify, request

import pgx

bp = Blueprint('secaudit', __name__)
VERSIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pg_versions.json')
_W = {'bad': 15, 'warn': 6, 'info': 1}


def _finding(level, title, detail='', fix='', items=None):
    return {'level': level, 'title': title, 'detail': detail, 'fix': fix, 'items': items or []}


def security_audit():
    def run(c, node):
        f = []
        v = pgx.server_version_num(c)
        supers = pgx.try_q(c, "SELECT rolname FROM pg_roles WHERE rolsuper AND rolcanlogin ORDER BY 1")
        names = [r['rolname'] for r in supers]
        extra = [n for n in names if n not in ('postgres',)]
        if len(extra) > 1:
            f.append(_finding('warn', f'Суперпользователей с правом входа: {len(names)}',
                              'Суперпользователь обходит все проверки прав. Приложениям и людям нужны отдельные роли с минимальными правами.',
                              'ALTER ROLE <имя> NOSUPERUSER; — выдайте нужные права через GRANT/предопределённые роли (pg_monitor, pg_read_all_data…)', names))
        pe = pgx.setting(c, 'password_encryption')
        if pe == 'md5':
            f.append(_finding('warn', 'password_encryption = md5',
                              'Новые пароли хешируются устаревшим MD5. SCRAM-SHA-256 устойчив к перехвату и подбору по хешу.',
                              "ALTER SYSTEM SET password_encryption = 'scram-sha-256'; SELECT pg_reload_conf(); затем смените пароли ролей"))
        md5 = pgx.try_q(c, "SELECT rolname FROM pg_authid WHERE rolpassword LIKE 'md5%' ORDER BY 1")
        if md5:
            f.append(_finding('warn', f'Ролей с паролем в формате MD5: {len(md5)}',
                              'Хеш MD5 позволяет войти, зная только хеш (pass-the-hash). В PostgreSQL 18 MD5 объявлен устаревшим.',
                              "SET password_encryption = 'scram-sha-256'; ALTER ROLE <имя> PASSWORD '<новый пароль>';",
                              [r['rolname'] for r in md5]))
        nopw = pgx.try_q(c, "SELECT rolname FROM pg_authid WHERE rolcanlogin AND rolpassword IS NULL AND rolname NOT LIKE 'pg\\_%' ORDER BY 1")
        noexp = pgx.try_q(c, "SELECT rolname FROM pg_authid WHERE rolcanlogin AND rolpassword IS NOT NULL AND rolvaliduntil IS NULL "
                             "AND NOT rolsuper AND rolname NOT LIKE 'pg\\_%' ORDER BY 1")
        if noexp:
            f.append(_finding('info', f'Пароли без срока действия: {len(noexp)} ролей',
                              'Для учётных записей людей обычно требуется регулярная смена пароля (политики ИБ).',
                              "ALTER ROLE <имя> VALID UNTIL '2027-01-01';", [r['rolname'] for r in noexp]))
        hba = pgx.try_q(c, "SELECT line_number, type, database, user_name, address, netmask, auth_method, error "
                           "FROM pg_hba_file_rules ORDER BY line_number")
        if not hba:
            f.append(_finding('info', 'pg_hba.conf не проверен', 'Нужны права суперпользователя (представление pg_hba_file_rules).'))
        trust, weak, errs = [], [], []
        for r in hba:
            addr = r.get('address') or ''
            wide = r['type'] != 'local' and (addr in ('all', '0.0.0.0', '::', 'samenet') or r.get('netmask') in ('0.0.0.0', '::'))
            line = f"строка {r['line_number']}: {r['type']} {','.join(r.get('database') or [])} {','.join(r.get('user_name') or [])} {addr or ''} {r.get('auth_method')}"
            if r.get('error'):
                errs.append(line + ' — ' + r['error'])
            elif r.get('auth_method') == 'trust' and r['type'] != 'local':
                trust.append(line)
            elif r.get('auth_method') in ('password', 'md5') and wide:
                weak.append(line)
        if trust:
            f.append(_finding('bad', 'pg_hba.conf: вход без пароля (trust) по сети',
                              'Любой, кто достучится до порта, войдёт без пароля под указанной ролью.',
                              'Замените trust на scram-sha-256 и выполните SELECT pg_reload_conf(); (в Patroni — через postgresql.pg_hba в DCS)', trust))
        if weak:
            f.append(_finding('warn', 'pg_hba.conf: md5/password для любых адресов',
                              'Широкий доступ (0.0.0.0/0, all) с устаревшим методом. Ограничьте сети и используйте scram-sha-256.',
                              'host all all 10.0.0.0/8 scram-sha-256', weak))
        if errs:
            f.append(_finding('bad', 'Ошибки в pg_hba.conf', 'Строки с ошибками не применятся при перезагрузке конфигурации.', '', errs))
        ssl = pgx.setting(c, 'ssl')
        if ssl != 'on':
            f.append(_finding('warn', 'SSL выключен', 'Пароли (кроме SCRAM) и данные передаются по сети открытым текстом.',
                              "ssl = on, ssl_cert_file/ssl_key_file; в pg_hba — hostssl вместо host"))
        plain = pgx.try_q(c, """SELECT a.usename, a.client_addr::text AS addr, a.application_name, count(*) AS n
                                FROM pg_stat_activity a JOIN pg_stat_ssl s ON s.pid = a.pid
                                WHERE NOT s.ssl AND a.client_addr IS NOT NULL AND a.client_addr <> '127.0.0.1' AND a.client_addr <> '::1'
                                GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 20""")
        if plain and ssl == 'on':
            f.append(_finding('warn', f'Незашифрованных сетевых подключений: {sum(r["n"] for r in plain)}',
                              'SSL включён на сервере, но клиенты подключаются без него.',
                              'В pg_hba.conf замените host на hostssl для сетевых подключений',
                              [f"{r['usename']} с {r['addr']} ({r['application_name'] or '—'}) × {r['n']}" for r in plain]))
        pub = []
        for db in pgx.databases(c)[:30]:
            cc = pgx.conn(node, db)
            if not cc:
                continue
            try:
                r = pgx.try_q(cc, "SELECT has_schema_privilege('public', 'public', 'CREATE') AS c")
                if r and r[0]['c']:
                    pub.append(db)
            finally:
                cc.close()
        if pub:
            f.append(_finding('warn', 'Любой пользователь может создавать объекты в схеме public',
                              'Классический вектор атаки через подмену функций (CVE-2018-1058). В PostgreSQL 15+ по умолчанию закрыто.',
                              'REVOKE CREATE ON SCHEMA public FROM PUBLIC; (в каждой базе)', pub))
        for name, why in (('log_connections', 'входы в журнал не пишутся — расследовать инцидент будет сложно'),
                          ('log_disconnections', 'не видно длительности сессий')):
            if pgx.setting(c, name) != 'on':
                f.append(_finding('info', f'{name} = off', why, f'ALTER SYSTEM SET {name} = on; SELECT pg_reload_conf();'))
        if pgx.setting(c, 'listen_addresses') == '*':
            f.append(_finding('info', "listen_addresses = '*'", 'Сервер слушает все интерфейсы; доступ ограничивает только pg_hba.conf и межсетевой экран.',
                              'Укажите конкретные адреса интерфейсов'))
        ext = pgx.try_q(c, "SELECT extname FROM pg_extension WHERE extname IN ('adminpack', 'dblink', 'file_fdw', 'plpython3u', 'plperlu', 'pg_execute_server_program')")
        if ext:
            f.append(_finding('info', 'Установлены расширения с доступом к ФС/сети', 'Проверьте, кому выданы права на их использование.', '',
                              [r['extname'] for r in ext]))
        if nopw:
            f.append(_finding('info', f'Ролей с правом входа без пароля: {len(nopw)}',
                              'Вход возможен только по peer/cert/trust — убедитесь, что это намеренно.', '', [r['rolname'] for r in nopw]))
        return {'node': node['name'], 'version_num': v, 'findings': f}
    res = pgx.on_leader(run)
    if not res:
        return {'ok': False, 'error': 'лидер недоступен'}
    score = max(0, 100 - sum(_W.get(x['level'], 0) for x in res['findings']))
    order = {'bad': 0, 'warn': 1, 'info': 2}
    res['findings'].sort(key=lambda x: order.get(x['level'], 3))
    return {'ok': True, 'score': score, **res}


@bp.route('/api/security/audit')
def api_security():
    return jsonify(security_audit())


# ---------------------------------------------------------------- обновления
def versions():
    try:
        with open(VERSIONS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'latest': {}, 'eol': {}, 'updated': None}


def _vtuple(s):
    try:
        return tuple(int(x) for x in str(s).split('.')[:2])
    except ValueError:
        return (0, 0)


def upgrade_check(target=None):
    vs = versions()
    today = date.today().isoformat()
    def per_node(c, n):
        num = pgx.server_version_num(c)
        full = pgx.q1(c, "SELECT version() AS v")['v']
        major = str(num // 10000)
        cur = f"{major}.{num % 10000}"
        latest = vs['latest'].get(major)
        eol = vs['eol'].get(major)
        stale = bool(latest and _vtuple(cur) > _vtuple(latest))   # справочник старее установленной версии
        if stale:
            latest = cur
        return {'version': cur, 'major': major, 'full': full, 'latest': latest,
                'behind': bool(latest and _vtuple(latest) > _vtuple(cur)),
                'eol': eol, 'eol_passed': bool(eol and eol <= today),
                'eol_soon': bool(eol and not eol <= today and (date.fromisoformat(eol) - date.today()).days < 365),
                'checksums': pgx.setting(c, 'data_checksums'), 'versions_stale': stale}
    nodes = pgx.on_each_node(per_node)
    majors = sorted({r['data']['major'] for r in nodes if r.get('ok')})
    cur_major = majors[-1] if majors else None
    target = target or (str(max(int(k) for k in vs['latest'])) if vs['latest'] else None)

    def prechecks(c, node):
        out = []
        v = pgx.server_version_num(c)
        pt = pgx.q1(c, "SELECT count(*) AS n FROM pg_prepared_xacts")['n']
        out.append({'level': 'bad' if pt else 'ok', 'title': 'Подготовленные транзакции (2PC)',
                    'detail': f'{pt} шт. — pg_upgrade требует 0: завершите COMMIT/ROLLBACK PREPARED' if pt else 'нет'})
        ls = pgx.try_q(c, "SELECT slot_name FROM pg_replication_slots WHERE slot_type = 'logical'")
        if ls:
            out.append({'level': 'warn' if v < 170000 else 'info', 'title': 'Логические слоты репликации',
                        'detail': ('pg_upgrade с версии ниже 17 не переносит логические слоты — их придётся пересоздать, подписчиков ресинхронизировать: '
                                   if v < 170000 else 'переносятся pg_upgrade (17+), если подписчики догнали: ') + ', '.join(r['slot_name'] for r in ls)})
        ext_all = {}
        regs = []
        for db in pgx.databases(c)[:30]:
            cc = pgx.conn(node, db)
            if not cc:
                continue
            try:
                for r in pgx.try_q(cc, "SELECT extname, extversion FROM pg_extension WHERE extname <> 'plpgsql'"):
                    ext_all.setdefault(f"{r['extname']} {r['extversion']}", []).append(db)
                for r in pgx.try_q(cc, """SELECT n.nspname || '.' || c.relname || '.' || a.attname AS col, t.typname
                        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                        JOIN pg_type t ON t.oid = a.atttypid
                        WHERE c.relkind IN ('r','m') AND a.attnum > 0 AND NOT a.attisdropped
                          AND t.typname IN ('regproc','regprocedure','regoper','regoperator','regconfig','regdictionary','regnamespace','regcollation')
                          AND n.nspname NOT IN ('pg_catalog','information_schema') LIMIT 20"""):
                    regs.append(f"{db}: {r['col']} ({r['typname']})")
            finally:
                cc.close()
        out.append({'level': 'info' if ext_all else 'ok', 'title': 'Расширения',
                    'detail': 'Все расширения должны быть установлены в новой версии (пакеты для PostgreSQL ' + str(target) + ')' if ext_all else 'только встроенные',
                    'items': [f"{k} — {', '.join(v)}" for k, v in sorted(ext_all.items())]})
        if regs:
            out.append({'level': 'bad', 'title': 'Колонки с типами reg*', 'detail': 'pg_upgrade не поддерживает хранение OID в этих типах — замените на text/oid',
                        'items': regs})
        if v < 120000:
            oids = pgx.try_q(c, "SELECT count(*) AS n FROM pg_class WHERE relhasoids")
            if oids and oids[0]['n']:
                out.append({'level': 'bad', 'title': 'Таблицы WITH OIDS', 'detail': f"{oids[0]['n']} таблиц — уберите: ALTER TABLE … SET WITHOUT OIDS"})
        enc = pgx.try_q(c, "SELECT datname, pg_encoding_to_char(encoding) AS enc, datcollate FROM pg_database WHERE datallowconn")
        out.append({'level': 'info', 'title': 'Кодировка и локаль', 'detail': 'Новый кластер инициализируйте с теми же кодировкой и локалью (initdb --locale/--encoding), иначе pg_upgrade откажет',
                    'items': sorted({f"{r['enc']} / {r['datcollate']}" for r in enc})})
        cs = pgx.setting(c, 'data_checksums')
        out.append({'level': 'info', 'title': 'Контрольные суммы данных', 'detail': f'сейчас {cs}: новый кластер должен совпадать (initdb {"--data-checksums" if cs == "on" else "без --data-checksums"}; в PostgreSQL 18 включены по умолчанию)'})
        return out
    checks = pgx.on_leader(prechecks) or []
    plan = [
        'Прогоните проверки на копии: восстановите бэкап (экран «Риски» → проверка восстановления) и выполните pg_upgrade --check.',
        'Patroni: поставьте кластер на паузу (patronictl pause), остановите реплики, затем лидер.',
        f'На лидере: установите пакеты PostgreSQL {target} и всех расширений, initdb с теми же локалью/кодировкой/checksums.',
        'pg_upgrade --link --check, затем pg_upgrade --link (минуты вместо часов, но откат только из бэкапа).',
        'Обновите bin_dir/data_dir в patroni.yml, удалите ключ /initialize в DCS (patronictl remove) и запустите Patroni на лидере.',
        'Реплики пересоздайте с нового лидера (reinit) или используйте rsync по процедуре из документации pg_upgrade.',
        'После запуска: vacuumdb --all --analyze-in-stages, сделайте полный бэкап, снимите паузу.',
    ]
    minor_plan = 'Минорное обновление: обновите пакеты на репликах и перезапустите их, затем switchover и обновление бывшего лидера. Совместимость данных сохраняется, pg_upgrade не нужен.'
    return {'ok': True, 'versions_updated': vs.get('updated'), 'nodes': nodes, 'current_major': cur_major,
            'target': target, 'prechecks': checks, 'major_plan': plan, 'minor_plan': minor_plan,
            'available_targets': sorted(vs['latest'].keys(), key=int)}


@bp.route('/api/upgrade/check')
def api_upgrade():
    t = request.args.get('target')
    if t and not t.isdigit():
        return jsonify({'ok': False, 'error': 'некорректная версия'})
    return jsonify(upgrade_check(t))
