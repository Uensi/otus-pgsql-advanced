#!/usr/bin/env python3
"""AI-ассистент (разд. 14 спеки): чат-помощник DBA, объяснение EXPLAIN-планов,
тюнинг-советы. Локальный движок на эвристиках + живой контекст консоли —
без внешних API (работает в закрытой сети). История в assistant_log.jsonl.
"""
import os
import re
import json
import time
from collections import deque
from flask import Blueprint, jsonify, request

bp = Blueprint('assistant', __name__)
BASE = os.path.dirname(os.path.abspath(__file__))
from paths import data_path
LOG_PATH = data_path('assistant_log.jsonl')
DIALOG = deque(maxlen=40)
LOG = deque(maxlen=200)

try:
    with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
        for line in f:
            try: LOG.append(json.loads(line))
            except Exception: pass
except Exception:
    pass


def _app():
    import app as A
    return A


def _log(buf, rec):
    buf.append(rec)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f: f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass


# ---------- живой контекст консоли ----------
def _live_context():
    """Живые данные: агенты, лаг, алерты, bloat — чтобы ответы были предметными."""
    A = _app()
    ctx = {'agents_online': 0, 'agents_total': 0, 'max_lag_s': 0, 'active_alerts': 0,
           'bloat_tables': 0, 'idle_in_tx': 0, 'dbs': []}
    try:
        if A.agent_api is not None:
            with A.agent_api._LOCK:
                agents = dict(A.agent_api.AGENTS)
            ctx['agents_total'] = len(agents)
            now = time.time()
            for _n, a in agents.items():
                if now - a['last_seen'] < 30: ctx['agents_online'] += 1
                m = a.get('metrics') or {}
                conn = m.get('conn') or {}
                ctx['idle_in_tx'] += conn.get('idle_in_tx', 0) or 0
                for d in (m.get('dbs') or []):
                    if d not in ctx['dbs']: ctx['dbs'].append(d)
                bloat = (m.get('db') or {}).get('bloat_count')
                if bloat: ctx['bloat_tables'] += int(bloat)
    except Exception:
        pass
    try:
        import alerts as _al
        ctx['active_alerts'] = len(_al.ACTIVE)
        lsn, _ = A.collect_lsn_lag()
        lags = [v.get('lag', 0) or 0 for v in lsn.values() if v.get('role') not in ('master', 'leader', 'primary')]
        ctx['max_lag_s'] = max(lags) if lags else 0
    except Exception:
        pass
    ctx['dbs'] = ctx['dbs'][:12]
    return ctx


# ---------- библиотека знаний ----------
KB = {
    'lag': {
        'keywords': ['лаг', 'lag', 'отставан', 'репликац', 'реплик'],
        'answer': ('Лаг репликации — это отставание реплики от лидера (по WAL). '
                   'Текущий макс. лаг: {max_lag_s} с. Причины: тяжёлые запросы на реплике '
                   '(проверь «Блокировки» → long running), медленный диск, отсутствие индексов, '
                   'сетевая задержка. Смотри экран «Кластер» → график лага и «Хосты» → CPU/RAM.'),
    },
    'bloat': {
        'keywords': ['bloat', 'раздут', 'фрагментац', 'dead', 'мертв'],
        'answer': ('Раздутие (bloat) — это мёртвые версии строк после UPDATE/DELETE. '
                   'Сейчас раздутых: {bloat_tables}. Лечится: autovacuum (scale_factor ниже), '
                   'при сильном раздутии — экран «Обслуживание» → pg_repack (онлайн) '
                   'или VACUUM FULL в окно обслуживания.'),
    },
    'idle': {
        'keywords': ['idle', 'висящ', 'транзакц', 'in transaction', 'неактивн'],
        'answer': ('Idle-in-transaction — открытые транзакции, держащие snapshot и мешающие '
                   'vacuum. Сейчас: {idle_in_tx}. Смотри DBA → Соединения: pid, usename, '
                   'длительность. Меры: idle_in_transaction_session_timeout, statement_timeout, '
                   'автокоммит в приложении.'),
    },
    'index': {
        'keywords': ['индекс', 'index', 'быстродейств', 'тормоз', 'медленн', 'seq scan'],
        'answer': ('Медленные запросы чаще всего из-за Seq Scan на больших таблицах. '
                   'Вставь запрос в «SQL-консоль» → EXPLAIN: анализатор покажет рекомендации '
                   'по индексам. Общие правила: индексы по условиям WHERE и JOIN, покрывающие '
                   '(INCLUDE), чистка неиспользуемых (DBA → Индексы).'),
    },
    'backup': {
        'keywords': ['бэкап', 'backup', 'резервн', 'восстанов', 'pg_dump', 'wal'],
        'answer': ('Резервное копирование: физические бэкапы wal-g (экран «Бэкапы») и '
                   'логические дампы pg_dump там же (панель «Логические дампы»). '
                   'Регулярно проверяй: verify (wal-g) и sha256 дампов. Ротация настроена '
                   'по TTL.'),
    },
    'config': {
        'keywords': ['конфиг', 'настройк', 'tuning', 'тюнинг', 'парамет', 'shared_buffers', 'work_mem'],
        'answer': ('Тюнинг начинается с шаблонов (Обслуживание → Шаблоны): 1С, OLTP, OLAP. '
                   'Базовые правила: shared_buffers ≈ 25% ОЗУ, effective_cache_size ≈ 70% ОЗУ, '
                   'work_mem до 64–256 МБ, maintenance_work_mem 256 МБ – 1 ГБ. Применение '
                   'через Patroni перезапустит PG при необходимости.'),
    },
    'connection': {
        'keywords': ['соединен', 'connection', 'сессий', 'conn', 'max_connections'],
        'answer': ('Заканчиваются соединения? Смотри DBA → Соединения: активные, idle, '
                   'idle-in-transaction. Не увеличивай max_connections бездумно — каждый слот '
                   'это память. Лучше пул (pgbouncer) и statement_timeout.'),
    },
    'patroni': {
        'keywords': ['patroni', 'лидер', 'leader', 'свитч', 'switchover', 'failover', 'кластер'],
        'answer': ('Patroni управляет лидером и репликами, автофейловер. Экран «Операции»: '
                   'switchover, failover, restart. «Кластер» — состав и роли. Проверяй DCS '
                   '(etcd/consul) на экране «Кластер»: без здорового DCS Patroni не выберет лидера.'),
    },
    'vacuum': {
        'keywords': ['vacuum', 'autovacuum', 'очистка', 'analyze'],
        'answer': ('Vacuum/autovacuum убирают мёртвые строки и обновляют статистику. '
                   'Если autovacuum не успевает: уменьши scale_factor (0.05/0.02), подними '
                   'autovacuum_max_workers, поставь VACUUM FULL в окно в планировщике '
                   '«Обслуживание».'),
    },
}


def _answer_unknown(q, ctx):
    low = q.lower()
    if 'select' in low or 'query' in low:
        return 'Я могу помочь с оптимизацией запроса: вставь его в «SQL-консоль» и нажми EXPLAIN — анализатор даст план и рекомендации.'
    return ('Я локальный ассистент DBA по жизненному циклу PostgreSQL. Спроси про: '
            'лаг репликации, раздутие (bloat), idle-транзакции, индексы, бэкапы, '
            'конфигурацию (тюнинг), соединения, Patroni, вакуум. Или пришли мне '
            'EXPLAIN-план/запрос для разбора.')


# ---------- интерпретация EXPLAIN ----------
_NODE_HINTS = {
    'Seq Scan': 'полное сканирование таблицы — на большой таблице это медленно',
    'Index Scan': 'чтение по индексу — хорошо',
    'Index Only Scan': 'чтение только индекса — оптимально',
    'Bitmap Heap Scan': 'чтение по бит-карте — приемлемо',
    'Bitmap Index Scan': 'бит-карта по индексу — приемлемо',
    'Hash Join': 'соединение через хэш — ок, если хэш в памяти',
    'Nested Loop': 'вложенный цикл — ок по малому набору, плохо на больших',
    'Merge Join': 'соединение слиянием — хорошо на отсортированных входах',
    'Sort': 'сортировка — часто узкое место, проверь ORDER BY/индексы',
    'Aggregate': 'агрегация — узкое место при большом вводе',
    'Limit': 'Limit — ок, удобно для топ-N',
    'CTE Scan': 'чтение CTE — иногда материализация мешает',
    'Subquery Scan': 'подзапрос',
    'Gather': 'параллельная сборка',
    'Gather Merge': 'параллельное слияние',
    'Function Scan': 'обход функции',
    'Append': 'объединение (UNION)',
    'Foreign Scan': 'внешняя таблица (fdw)',
    'Result': 'тривиальный узел (константы)',
    'Materialize': 'материализация подзапроса — память',
}


def _walk_plan(node, out):
    """Рекурсивный обход дерева плана EXPLAIN (JSON)."""
    if not isinstance(node, dict):
        return
    node_type = node.get('Node Type') or '?'
    extra = {}
    for k in ('Relation Name', 'Schema', 'Alias', 'Total Cost', 'Rows',
              'Actual Rows', 'Filter', 'Index Name', 'Actual Total Time'):
        if k in node:
            extra[k] = node[k]
    out.append({'node': node_type, 'extra': extra})
    for ch in (node.get('Plans') or []):
        _walk_plan(ch, out)


def _explain_post(plan_json):
    """Разбор EXPLAIN (FORMAT JSON) → человеческий текст."""
    try:
        root = plan_json[0].get('Plan', {}) if isinstance(plan_json, list) else plan_json.get('Plan', {})
    except Exception:
        root = {}
    nodes = []
    try:
        _walk_plan(root, nodes)
    except Exception:
        return {'ok': True, 'text': 'Не удалось разобрать план — покажи сырой JSON в консоли.'}
    if not nodes:
        return {'ok': True, 'text': 'План пустой или не распознан.'}
    lines = ['**Разбор плана (EXPLAIN)**', '']
    seen = {}
    for n in nodes:
        t = n['node']
        seen[t] = seen.get(t, 0) + 1
        hint = _NODE_HINTS.get(t)
        if hint and seen[t] == 1:
            lines.append(f'• {t} — {hint}')
    lines.append('')
    lines.append('**Возможные узкие места:**')
    warns = [n for n in nodes if n['node'] in ('Seq Scan', 'Sort', 'Nested Loop', 'Aggregate')]
    if warns:
        for w in warns[:6]:
            rel = w['extra'].get('Relation Name') or ''
            rows = w['extra'].get('Actual Rows') or w['extra'].get('Rows') or ''
            part = f' на {rel}' if rel else ''
            part += f' (~{rows} строк)' if rows else ''
            lines.append(f'— {w["node"]}{part}')
    else:
        lines.append('Явных узких мест не обнаружено — запрос выглядит эффективным.')
    lines.append('')
    lines.append('**Что сделать:**')
    lines.append('1. Seq Scan по большой таблице → создать индекс по условиям WHERE/JOIN.')
    lines.append('2. Обновите статистику: ANALYZE, если оценки строк далеки от реальности.')
    lines.append('3. Для тяжёлых запросов смотрите «SQL-консоль» → EXPLAIN + рекомендации анализатора.')
    return {'ok': True, 'text': '\n'.join(lines)}


# ---------- чат ----------
def _classify(q):
    low = q.lower()
    for key, kb in KB.items():
        if any(kw in low for kw in kb['keywords']):
            return key
    return None


def chat(message):
    ctx = _live_context()
    key = _classify(message)
    if key:
        try:
            ans = KB[key]['answer'].format(**ctx)
        except Exception:
            ans = KB[key]['answer']
    else:
        ans = _answer_unknown(message, ctx)
    t = time.time()
    rec = {'ts': t, 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'role': 'user',
           'msg': message[:500], 'reply': ans[:2000], 'topic': key}
    _log(LOG, rec)
    DIALOG.append(rec)
    return jsonify({'ok': True, 'data': {'reply': ans, 'context': ctx, 'topic': key}})


# ---------- Flask-роуты ----------
@bp.route('/api/assistant/chat', methods=['POST'])
def api_chat():
    msg = (request.json or {}).get('message', '') or ''
    if len(msg) > 2000:
        return jsonify({'ok': False, 'error': 'слишком длинное сообщение'})
    if not msg.strip():
        return jsonify({'ok': False, 'error': 'пустое сообщение'})
    return chat(msg)


@bp.route('/api/assistant/explain', methods=['POST'])
def api_explain():
    b = request.json or {}
    query = b.get('query', '') or ''
    plan = b.get('plan')
    if not plan:
        return jsonify({'ok': False, 'error': 'не передан план'})
    res = _explain_post(plan)
    _log(LOG, {'ts': time.time(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
               'role': 'user', 'msg': 'explain ' + query[:200], 'reply': res['text'][:800], 'topic': 'explain'})
    DIALOG.append({'ts': time.time(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                   'role': 'user', 'msg': 'explain ' + query[:200], 'reply': res['text'][:500], 'topic': 'explain'})
    return jsonify(res)


@bp.route('/api/assistant/config-advice', methods=['POST'])
def api_advice():
    """Совет по тюнингу на основе текущего конфига (с Patroni /config)."""
    A = _app()
    cur = None
    for n in A.NODES:
        r = A.patroni_request(n, 'GET', '/config')
        if r['ok']:
            cur = r['data']
            break
    if not cur:
        return jsonify({'ok': False, 'error': 'конфиг недоступен (лидер не отвечает)'})
    params = (cur.get('postgresql', {}) or {}).get('parameters', {}) or {}
    tips = []
    for k in ('shared_buffers', 'work_mem', 'effective_cache_size', 'max_connections',
              'maintenance_work_mem', 'checkpoint_completion_target', 'random_page_cost',
              'autovacuum_max_workers', 'autovacuum_naptime'):
        v = params.get(k)
        if v is None or v == '':
            tips.append(f'• {k} не задан (дефолт) — стоит явно указать, см. шаблоны «Обслуживание».')
        else:
            tips.append(f'• {k} = {v} — приемлемо; уточняйте по шаблонам (Обслуживание → Шаблоны).')
    _log(LOG, {'ts': time.time(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
               'role': 'user', 'msg': 'config-advice', 'reply': '; '.join(tips)[:800], 'topic': 'config'})
    return jsonify({'ok': True, 'data': {'tips': tips, 'keys': list(params.keys())[:60]}})


@bp.route('/api/assistant/history')
def history_get():
    return jsonify({'ok': True, 'data': {'dialog': list(DIALOG)[-30:], 'log': list(LOG)[-100:]}})


@bp.route('/api/assistant/clear', methods=['POST'])
def clear_dialog():
    global DIALOG
    DIALOG = deque(maxlen=40)
    return jsonify({'ok': True})


# ---------- AI-диагностика по живому снимку ----------
def _live_diag():
    """Структурированный анализ живой ситуации: тревоги, лаг, раздутие, idle и т.п."""
    ctx = _live_context()
    issues = []
    # 1. Алерты
    try:
        import alerts as _al
        act = _al.ACTIVE
        rules = {r.get('id'): r for r in _al.RULES}
        for rid, a in list(act.items())[:20]:
            rule = rules.get(rid) or {}
            sev = rule.get('severity') or 'warning'
            name = rule.get('name') or rid
            issues.append({'sev': 'critical' if sev == 'critical' else 'warning',
                           'what': f'алерт «{name}»', 'detail': str(a.get('value') or '')[:120]})
    except Exception:
        pass
    # 2. Агенты офлайн
    try:
        A = _app()
        with A.agent_api._LOCK:
            agents = dict(A.agent_api.AGENTS)
        now = time.time()
        for n, ag in agents.items():
            if now - (ag.get('last_seen') or 0) > 30:
                issues.append({'sev': 'critical', 'what': f'агент на {n} офлайн',
                               'detail': 'последний контакт >30с назад'})
    except Exception:
        pass
    # 3. Лаг
    if ctx.get('max_lag_s') and ctx['max_lag_s'] > 30:
        issues.append({'sev': 'warning' if ctx['max_lag_s'] < 120 else 'critical',
                       'what': f'лаг репликации {ctx["max_lag_s"]} сек',
                       'detail': 'проверь нагрузку на репликах и сеть'})
    # 4. Idle-in-tx
    if ctx.get('idle_in_tx', 0) > 20:
        issues.append({'sev': 'warning', 'what': f'{ctx["idle_in_tx"]} idle-in-transaction',
                       'detail': 'смотри DBA → Соединения; рассмотри idle_in_transaction_session_timeout'})
    # 5. Раздутие
    if ctx.get('bloat_tables', 0) > 10:
        issues.append({'sev': 'warning', 'what': f'{ctx["bloat_tables"]} таблиц с раздутием',
                       'detail': 'экраны «Обслуживание» → pg_repack / планировщик'})
    # 6. Диски (прогноз)
    try:
        import hosts as _h
        for key, hist in _h.DISK_HISTORY.items():
            fc = _h._disk_forecast(list(hist))
            if fc and fc.get('hours') is not None and fc['hours'] <= 24:
                issues.append({'sev': 'critical' if fc['hours'] <= 6 else 'warning',
                               'what': f'диск {key} заполнится через {fc["hours"]} ч',
                               'detail': f'темп ~{fc.get("slope_mb_h", "?")} МБ/ч'})
    except Exception:
        pass
    # 7. Kernel-предупреждения
    try:
        import hosts as _h
        for n, ag in _h._agents().items():
            for w in _h._kernel_warnings((ag.get('os') or {}).get('kernel') or {},
                                         ((ag.get('os') or {}).get('mem') or {}).get('total'), None):
                issues.append({'sev': 'info', 'what': f'ядро на {n}: {w}', 'detail': ''})
    except Exception:
        pass
    issues.sort(key=lambda x: {'critical': 0, 'warning': 1, 'info': 2}.get(x['sev'], 3))
    ok_count = len([i for i in issues if i['sev'] == 'info'])
    summary = ('Всё в норме ✓' if not issues else
               f'найдено проблем: {len([i for i in issues if i["sev"] != "info"])}, '
               f'замечаний: {ok_count}')
    return {'summary': summary, 'issues': issues[:30], 'context': ctx}


@bp.route('/api/assistant/diagnose', methods=['POST'])
def api_diagnose():
    return jsonify({'ok': True, 'data': _live_diag()})