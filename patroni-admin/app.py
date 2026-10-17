#!/usr/bin/env python3
import os, re, time, math, threading, functools, requests, psycopg2, json as _json
from datetime import datetime, timedelta
from decimal import Decimal
from collections import deque
from flask import Flask, render_template, jsonify, request, Blueprint, Response, session
from concurrent.futures import ThreadPoolExecutor, as_completed

# PyYAML может отсутствовать в локальном окружении — используем минимальный парсер.
try:
    import yaml
    _HAS_YAML = True
except ImportError:
    import _yaml_min as yaml
    _HAS_YAML = False
if not hasattr(yaml, 'YAMLError'):
    yaml.YAMLError = Exception

try:
    import agent_api
except Exception:
    agent_api = None
import auth
import admin_api
import alerts
import reports
import slots
import hosts
import templates
import notify
import dcs
import assistant
import tuning
from paths import data_path

app = Flask(__name__)
CONFIG_PATH = os.environ.get('PATRONI_WEB_CONFIG', 'config.yaml')
with open(CONFIG_PATH, encoding='utf-8') as f: CONFIG = yaml.safe_load(f)
auth.init_app(app, CONFIG)

def _load_clusters():
    cs = CONFIG.get('clusters')
    if cs: return cs
    return [CONFIG['cluster']]
CLUSTERS = _load_clusters()
CLUSTER_NAME = CLUSTERS[0]['name']; NODES = CLUSTERS[0]['nodes']

EXECUTOR = ThreadPoolExecutor(max_workers=10)
LAG_HISTORY = deque(maxlen=200); LSN_CACHE = {}
TPS_HISTORY = deque(maxlen=200); CACHE_HISTORY = deque(maxlen=200); SIZE_HISTORY = deque(maxlen=2880)
DELAY_CACHE = {}
PG = CONFIG.get('postgres', {}); PG_USER, PG_PASS = PG.get('user','postgres'), PG.get('password','')
PG_DB, PG_PORT = PG.get('database','postgres'), PG.get('port',5432)
_CACHE = {}; _CACHE_LOCK = threading.Lock()

def ttl_cache(key, seconds):
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **kw):
            now = time.time()
            with _CACHE_LOCK:
                hit = _CACHE.get(key)
                if hit and hit[0] > now: return hit[1]
            res = fn(*a, **kw)
            with _CACHE_LOCK: _CACHE[key] = (now + seconds, res)
            return res
        return wrap
    return deco

def patroni_request(node, method='GET', path='/', **kw):
    url = f"http://{node['host']}:{node['port']}{path}"
    try:
        resp = requests.request(method, url, timeout=10, **kw)
        data = {}
        if resp.content:
            try: data = resp.json()
            except (ValueError, requests.exceptions.JSONDecodeError):
                raw = resp.text.strip(); data = {'_raw_response': raw} if raw else {}
        return {'ok': resp.ok, 'status_code': resp.status_code, 'data': data, 'error': None}
    except Exception as e:
        return {'ok': False, 'status_code': 0, 'data': {}, 'error': str(e)}

def pg_conn(host, database=None, port=None, user=None, password=None):
    try:
        c = psycopg2.connect(host=host, port=port or PG_PORT, user=user or PG_USER,
                             password=password or PG_PASS, database=database or PG_DB, connect_timeout=5)
        c.autocommit = True; return c
    except Exception: return None

def leader_node():
    for node in NODES:
        r = patroni_request(node, 'GET', '/patroni')
        if r['ok'] and r['data'].get('role') in ('master','leader','primary'): return node
    return NODES[0] if NODES else None

def q(conn, sql, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params); cols = [d[0] for d in cur.description] if cur.description else []
        out = []
        for row in cur.fetchall():
            d = {}
            for i, col in enumerate(cols):
                v = row[i]
                if isinstance(v, (datetime, timedelta)): v = str(v)
                elif isinstance(v, Decimal): v = float(v)
                d[col] = v
            out.append(d)
        cur.close(); return out
    except Exception:
        cur.close(); raise

def fb(b):
    if b is None or b == '': return '—'
    try: n = int(b)
    except Exception: return '—'
    if n < 1024: return f"{n} B"
    if n < 1048576: return f"{n/1024:.1f} KB"
    if n < 1073741824: return f"{n/1024/1024:.2f} MB"
    return f"{n/1073741824:.3f} GB"

def get_node_delay(name, host):
    now = time.time()
    hit = DELAY_CACHE.get(host)
    if hit and now - hit[0] < 15: return hit[1]
    v = None
    c = pg_conn(host)
    if c:
        try:
            cur = c.cursor(); cur.execute("SHOW recovery_min_apply_delay"); v = cur.fetchone()[0]; cur.close()
        except Exception: v = None
        finally:
            try: c.close()
            except Exception: pass
    if v in (None, '', '?') and agent_api is not None:
        ad = agent_api.agent_delay(name)
        if ad not in (None, '', '?'): v = ad
    if v in (None, '', '?'): v = '0'
    DELAY_CACHE[host] = (now, v)
    return v

def _is_sys(name):
    n = (name or '').lower().strip().strip('"')
    if n.startswith('pg_'): return True
    if '.' in n and n.split('.')[0] in ('pg_catalog','information_schema'): return True
    return False

_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
def _qid(name):
    """Валидация SQL-идентификатора: возвращает имя в кавычках или бросает ValueError."""
    if not _IDENT_RE.match(name or ''): raise ValueError(f'недопустимый идентификатор: {name!r}')
    return '"' + name + '"'

_MON_NEEDLES = ['pg_catalog','pg_stat_','pg_locks','pg_class','pg_indexes','pg_tables','pg_namespace','pg_extension','pg_database','pg_settings','pg_replication','pg_stat_statements','information_schema']
def _is_monitoring_query(sql):
    s = (sql or '').strip().lower()
    if not s: return True
    if s.startswith(('show ','set ','reset ','discard ','select 1','select 1 ')): return True
    return any(n in s for n in _MON_NEEDLES)

def _is_sys_query(sql):
    s = (sql or '').lower(); return ('pg_catalog.' in s) or ('information_schema.' in s)

def _classify_query(sql):
    """Классификация запроса: user / system / monitoring. Возвращает словарь с
    меткой и пояснением. «Служебный» = системный каталог/каталоги pg_*;
    «мониторинг» = обращения к pg_stat_*/pg_locks и т.п. (обычно агент/консоль)."""
    s = (sql or '').strip()
    if not s:
        return {'category': 'system', 'label': 'пустой запрос', 'system': True,
                'explanation': 'Пустой запрос.'}
    low = s.lower()
    if low.startswith(('show ', 'set ', 'reset ', 'discard ', 'checkpoint', 'vacuum ', 'analyze ')):
        return {'category': 'system', 'label': 'служебная команда', 'system': True,
                'explanation': 'Команда настройки/обслуживания (SHOW/SET/VACUUM/ANALYZE и т.п.), '
                               'не является пользовательским DML/запросом данных.'}
    if _is_sys_query(s):
        return {'category': 'system', 'label': 'служебный (системный каталог)', 'system': True,
                'explanation': 'Запрос к системному каталогу (pg_catalog / information_schema).'}
    if _is_monitoring_query(s):
        return {'category': 'monitoring', 'label': 'мониторинг (pg_stat_*)', 'system': True,
                'explanation': 'Запрос к представлениям мониторинга (pg_stat_*, pg_locks, '
                               'pg_settings и т.п.) — обычно генерируется консолью/агентом.'}
    # системные таблицы по имени (pg_*, кроме пользовательских)
    tbls = _tables(s)
    if tbls and all(_is_sys(t) for t in tbls):
        return {'category': 'system', 'label': 'служебный (pg_*)', 'system': True,
                'explanation': 'Все таблицы запроса — системные (pg_*).'}
    return {'category': 'user', 'label': 'пользовательский', 'system': False,
            'explanation': 'Обычный запрос к пользовательским данным — подлежит оптимизации.'}

def _strip_leading_comments(sql):
    s = sql
    while True:
        t = s.lstrip()
        if t.startswith('/*'):
            end = t.find('*/')
            if end == -1: return t
            s = t[end+2:]; continue
        if t.startswith('--'):
            nl = t.find('\n'); return '' if nl == -1 else t[nl+1:]
        return t

def _deparam(sql): return re.sub(r'\$\d+', 'NULL', sql)

def _plan_node(node, issues, depth=0):
    if not node: return
    t = node.get('Node Type',''); rel = node.get('Relation Name','')
    if t == 'Seq Scan' and not _is_sys(rel):
        ar = node.get('Actual Rows',0); fl = node.get('Filter','')
        issues.append({'type':'seq_scan','severity':'high' if ar>1000 else 'medium','table':rel,'rows':ar,'filter':fl,'message':f'Seq Scan {rel} ({ar} строк)','suggestion':f'Индекс по: {fl}' if fl else f'Индекс для {rel}'})
    if t == 'Index Scan' and not _is_sys(rel) and node.get('Actual Rows',0)>10000 and node.get('Actual Rows',0)>node.get('Plan Rows',0)*2:
        issues.append({'type':'inefficient_index','severity':'medium','table':rel,'rows':node.get('Actual Rows',0),'message':'Index Scan возвращает много строк','suggestion':'Составной индекс?'})
    if t == 'Nested Loop' and node.get('Actual Rows',0)>100000:
        issues.append({'type':'nested_loop','severity':'high','rows':node.get('Actual Rows',0),'message':'Nested Loop с большим числом строк','suggestion':'Индексы по условиям JOIN'})
    if t == 'Hash Join' and node.get('Actual Rows',0)>500000:
        issues.append({'type':'hash_join_large','severity':'medium','rows':node.get('Actual Rows',0),'message':'Hash Join с большим результатом','suggestion':'Индексы для условий соединения'})
    if t == 'Sort':
        ar = node.get('Actual Rows',0); sm = node.get('Sort Method','')
        if 'external' in sm.lower(): issues.append({'type':'external_sort','severity':'high','rows':ar,'message':f'Сортировка на диске ({sm})','suggestion':'work_mem или индекс по ORDER BY'})
        elif ar>100000: issues.append({'type':'large_sort','severity':'medium','rows':ar,'message':f'Сортировка {ar} строк','suggestion':'Индекс по ORDER BY'})
    if t in ('Aggregate','GroupAggregate','HashAggregate') and node.get('Actual Rows',0)>1000000:
        issues.append({'type':'large_aggregate','severity':'medium','rows':node.get('Actual Rows',0),'message':'Агрегация большого числа строк','suggestion':'Материализованное представление'})
    for ch in node.get('Plans', []): _plan_node(ch, issues, depth+1)

def _tables(query):
    s = set()
    for m in re.finditer(r'\bFROM\s+["\']?(\w+)["\']?', query, re.I): s.add(m.group(1))
    for m in re.finditer(r'\bJOIN\s+["\']?(\w+)["\']?', query, re.I): s.add(m.group(1))
    for m in re.finditer(r'\bUPDATE\s+["\']?(\w+)["\']?', query, re.I): s.add(m.group(1))
    for m in re.finditer(r'\bINTO\s+["\']?(\w+)["\']?', query, re.I): s.add(m.group(1))
    kw = {'SELECT','FROM','WHERE','AND','OR','NOT','NULL','TRUE','FALSE','AS','ON','IN','EXISTS','BETWEEN','LIKE','IS','SET','VALUES','GROUP','ORDER','HAVING','LIMIT','OFFSET','UNION','ALL','DISTINCT','CASE','WHEN','THEN','ELSE','END','INSERT','UPDATE','DELETE','CREATE','DROP','ALTER','INDEX','TABLE','VIEW','WITH','RETURNING','ORDINALITY'}
    return {x for x in s if x.upper() not in kw and not _is_sys(x)}

def _where(query):
    conds = []; wm = re.search(r'\bWHERE\s+(.+?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|$)', query, re.I | re.S)
    if not wm: return conds
    w = wm.group(1)
    for m in re.finditer(r'(\w+)\s*=\s*[^,\s]', w): conds.append({'column':m.group(1),'operator':'=','type':'equality'})
    for m in re.finditer(r'(\w+)\s*(>=|<=|>|<)\s*[^,\s]', w): conds.append({'column':m.group(1),'operator':m.group(2),'type':'range'})
    for m in re.finditer(r'(\w+)\s+IN\s*\(', w, re.I): conds.append({'column':m.group(1),'operator':'IN','type':'in_list'})
    for m in re.finditer(r"(\w+)\s+LIKE\s+'([^']*)'", w, re.I):
        pat = m.group(2); ct = 'like_both' if (pat.startswith('%') and pat.endswith('%')) else ('like_prefix' if pat.startswith('%') else 'like_suffix')
        conds.append({'column':m.group(1),'operator':'LIKE','type':ct,'pattern':pat})
    for m in re.finditer(r'(\w+)\s+IS\s+(NOT\s+)?NULL', w, re.I): conds.append({'column':m.group(1),'operator':'IS NULL','type':'null_check'})
    for m in re.finditer(r'(\w+)\s+BETWEEN\s+\S+\s+AND\s+\S+', w, re.I): conds.append({'column':m.group(1),'operator':'BETWEEN','type':'range'})
    for m in re.finditer(r'(\w+)\s*\(\s*(\w+)\s*\)', w): conds.append({'column':m.group(2),'operator':'FUNC','type':'functional','function':m.group(1)})
    return conds

def _orderby(query):
    o = []; m = re.search(r'\bORDER\s+BY\s+(.+?)(?:\bLIMIT\b|\bOFFSET\b|$)', query, re.I | re.S)
    if not m: return o
    for p in m.group(1).split(','):
        p = p.strip(); mm = re.match(r'(\w+)(?:\s+(ASC|DESC))?', p, re.I)
        if mm: o.append({'column':mm.group(1),'direction':(mm.group(2) or 'ASC').upper()})
    return o

def _groupby(query):
    g = []; m = re.search(r'\bGROUP\s+BY\s+(.+?)(?:\bHAVING\b|\bORDER\b|\bLIMIT\b|$)', query, re.I | re.S)
    if not m: return g
    for col in m.group(1).split(','):
        col = col.strip()
        if col and re.match(r'^\w+$', col): g.append(col)
    return g

def _tstats(c, t):
    try:
        r = q(c, "SELECT reltuples::bigint, pg_total_relation_size(oid), pg_relation_size(oid), pg_indexes_size(oid) FROM pg_class WHERE relname=%s AND relnamespace=(SELECT oid FROM pg_namespace WHERE nspname='public')", (t,))
        return {'est_rows':r[0][0],'total_size':r[0][1],'table_size':r[0][2],'indexes_size':r[0][3]} if r else None
    except Exception: return None

def _eidx(c, t):
    try: return q(c, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename=%s AND schemaname='public'", (t,))
    except Exception: return []

def _recommend(query, c):
    recs = []; ex = _tables(query); wc = _where(query); ob = _orderby(query); gb = _groupby(query)
    for t in ex:
        if _is_sys(t): continue
        have = set()
        for ix in _eidx(c, t):
            m = re.search(r'ON.*\((.+)\)', ix['indexdef'])
            if m: have.update(x.strip().strip('"').lower() for x in m.group(1).split(','))
        for cd in wc:
            col = cd['column'].lower()
            if col in have or _is_sys(col): continue
            if cd['type'] in ('equality','in_list','null_check'):
                recs.append({'type':'index','table':t,'columns':[col],'reason':f"WHERE {cd['column']} {cd['operator']}",'sql':f'CREATE INDEX CONCURRENTLY idx_{t}_{col} ON {t} ({col});','impact':'high'})
            elif cd['type'] == 'range':
                recs.append({'type':'index','table':t,'columns':[col],'reason':f"Диапазон {cd['column']} {cd['operator']}",'sql':f'CREATE INDEX CONCURRENTLY idx_{t}_{col} ON {t} ({col});','impact':'high'})
            elif cd['type'] == 'like_suffix':
                recs.append({'type':'index','table':t,'columns':[col],'reason':f"LIKE с префиксом % по {cd['column']}",'sql':f'-- нужен pg_trgm:\nCREATE EXTENSION IF NOT EXISTS pg_trgm;\nCREATE INDEX CONCURRENTLY idx_{t}_{col}_trgm ON {t} USING gin ({col} gin_trgm_ops);','impact':'medium','note':'Требуется расширение pg_trgm'})
            elif cd['type'] == 'functional':
                fn = cd.get('function','')
                recs.append({'type':'functional_index','table':t,'columns':[col],'function':fn,'reason':f'Функция {fn}() в условии','sql':f'CREATE INDEX CONCURRENTLY idx_{t}_{fn}_{col} ON {t} ({fn}({col}));','impact':'high'})
        for o in ob:
            col = o['column'].lower()
            if col in have or _is_sys(col): continue
            recs.append({'type':'index','table':t,'columns':[col],'reason':f"ORDER BY {col} {o['direction']}",'sql':f"CREATE INDEX CONCURRENTLY idx_{t}_{col} ON {t} ({col} {o['direction']});",'impact':'medium'})
        for col in gb:
            cl = col.lower()
            if cl in have or _is_sys(cl): continue
            recs.append({'type':'index','table':t,'columns':[cl],'reason':f"GROUP BY {col}",'sql':f'CREATE INDEX CONCURRENTLY idx_{t}_{cl} ON {t} ({cl});','impact':'medium'})
    seen = set(); u = []
    for r in recs:
        k = (r['table'], tuple(r['columns']), r['type'])
        if k not in seen: seen.add(k); u.append(r)
    return u

def _rewrite(query):
    s = []
    if re.search(r'\bSELECT\s+\*\s+FROM\b', query, re.I): s.append({'problem':'SELECT * тянет все колонки','suggestion':'Укажите только нужные','example':'-- вместо SELECT * FROM users\nSELECT id, name, email FROM users'})
    if re.search(r'\b(\w+)\s+NOT\s+IN\s*\(\s*SELECT', query, re.I): s.append({'problem':'NOT IN с подзапросом медленный','suggestion':'NOT EXISTS или LEFT JOIN … IS NULL','example':'SELECT t1.* FROM t1 LEFT JOIN t2 ON t1.id=t2.id WHERE t2.id IS NULL'})
    if re.search(r'\bWHERE\s+\w+=\S+\s+OR\s+\w+=\S+', query, re.I): s.append({'problem':'OR на разных колонках ломает индексы','suggestion':'UNION ALL двух запросов','example':'SELECT * FROM t WHERE a=1\nUNION ALL\nSELECT * FROM t WHERE b=2 AND a<>1'})
    if re.search(r'\bCOUNT\s*\(\s*\*\s*\).*\bLEFT\s+JOIN\b', query, re.I | re.S): s.append({'problem':'COUNT(*) с LEFT JOIN может дать неверный результат','suggestion':'COUNT(table.column)','example':'SELECT a.id, COUNT(b.id) FROM a LEFT JOIN b ON a.id=b.a_id GROUP BY a.id'})
    if re.search(r'\bDISTINCT\b', query, re.I) and re.search(r'\bGROUP\s+BY\b', query, re.I): s.append({'problem':'DISTINCT с GROUP BY избыточен','suggestion':'GROUP BY уже даёт уникальность','example':'SELECT category, COUNT(*) FROM t GROUP BY category'})
    if re.search(r'\bSELECT\s+.*\(\s*SELECT\s+', query, re.I | re.S) and not re.search(r'\bFROM\s+\(\s*SELECT\b', query, re.I): s.append({'problem':'Коррелированный подзапрос в SELECT','suggestion':'JOIN с агрегацией','example':'SELECT u.name, COUNT(o.id) FROM users u LEFT JOIN orders o ON o.user_id=u.id GROUP BY u.id, u.name'})
    if re.search(r"LIKE\s+'%", query, re.I): s.append({'problem':'LIKE с ведущим % не использует B-tree','suggestion':'pg_trgm + GIN','example':"CREATE EXTENSION IF NOT EXISTS pg_trgm;\nCREATE INDEX … USING gin (col gin_trgm_ops);"})
    ic = re.search(r"(\w+)\s*=\s*'(\d+)'", query, re.I)
    if ic: s.append({'problem':f'Неявное приведение типа: {ic.group(1)} сравнивается со строкой','suggestion':'Используйте правильный тип','example':f"WHERE {ic.group(1)} = {ic.group(2)}  -- без кавычек"})
    if re.search(r'\bWHERE\s+\w+\s*\(\s*\w+\s*\)', query, re.I): s.append({'problem':'Функция в WHERE — индекс не работает','suggestion':'Функциональный индекс','example':'CREATE INDEX idx_lower_email ON users (LOWER(email));'})
    return s

def _summarize(issues, recs, rwr):
    """Собирает человекочитаемые рекомендации «что сделать» из плана, индексов и
    переписывания — для отображения в UI как единый список действий."""
    out = []
    for it in issues:
        sev = it.get('severity', 'medium')
        out.append({'sev': sev, 'text': it.get('message', '') + ((' — ' + it.get('suggestion', '')) if it.get('suggestion') else '')})
    for r in recs:
        if r.get('type') in ('index', 'functional_index'):
            out.append({'sev': r.get('impact', 'high'), 'text': f"Создать индекс: {r.get('reason', '')} — {r.get('sql', '')}"})
        else:
            out.append({'sev': 'medium', 'text': r.get('reason', '')})
    for s in rwr:
        out.append({'sev': 'medium', 'text': s.get('problem', '') + ' → ' + s.get('suggestion', '')})
    return out

bp_cluster = Blueprint('cluster', __name__)
_LSN_CACHE_LOCK = threading.Lock()
_LSN_CACHE_TTL = 3.0   # секунд: верхний предел частоты полного опроса Patroni
_LSN_CACHE_LAST = {'ts': 0, 'value': None}

def collect_lsn_lag(force=False):
    """Полный опрос /patroni + /cluster по нодам. Кэшируется на _LSN_CACHE_TTL сек:
    heartbeat браузера (cluster+lsn+lag-history) не должен долбить REST API Patroni."""
    global LSN_CACHE
    now = time.time()
    if not force:
        with _LSN_CACHE_LOCK:
            if _LSN_CACHE_LAST['value'] is not None and now - _LSN_CACHE_LAST['ts'] < _LSN_CACHE_TTL:
                return _LSN_CACHE_LAST['value']
    snap = {'ts': now, 'nodes': {}}
    futs = {EXECUTOR.submit(patroni_request, n, 'GET', '/patroni'): n for n in NODES}
    for f in as_completed(futs):
        n = futs[f]; r = f.result()
        if not r['ok']: continue
        d = r['data']; name = n['name']; xl = d.get('xlog', {}) or {}
        LSN_CACHE[name] = {'role':d.get('role','unknown'),'received':xl.get('received_location'),'replayed':xl.get('replayed_location'),'location':xl.get('location'),'timeline':xl.get('timeline'),'lag':0,'state':d.get('state','unknown'),'pending_restart':d.get('pending_restart',False),'pending_restart_reason':d.get('pending_restart_reason',''),'delay':get_node_delay(n['name'], n['host'])}
    cf = {EXECUTOR.submit(patroni_request, n, 'GET', '/cluster'): n for n in NODES}
    cd = None
    for f in as_completed(cf):
        r = f.result()
        if r['ok']: cd = r['data']; break
    if cd and 'members' in cd:
        for m in cd['members']:
            nm = m.get('name')
            if nm and nm in LSN_CACHE:
                LSN_CACHE[nm]['lag'] = m.get('lag', 0)
                snap['nodes'][nm] = {'lag':LSN_CACHE[nm]['lag'],'role':LSN_CACHE[nm]['role'],'state':LSN_CACHE[nm]['state']}
    if snap['nodes']: LAG_HISTORY.append(snap)
    res = (LSN_CACHE, list(LAG_HISTORY))
    with _LSN_CACHE_LOCK:
        _LSN_CACHE_LAST['ts'] = time.time(); _LSN_CACHE_LAST['value'] = res
    return res

def cluster_state():
    futs = {EXECUTOR.submit(patroni_request, n, 'GET', '/cluster'): n for n in NODES}
    for f in as_completed(futs):
        r = f.result()
        if r['ok']:
            collect_lsn_lag(); return r['data']
    st = []
    f2 = {EXECUTOR.submit(patroni_request, n, 'GET', '/patroni'): n for n in NODES}
    for f in as_completed(f2):
        n = f2[f]; r = f.result(); d = r['data']
        d.update({'node_name':n['name'],'host':n['host'],'port':n['port'],'reachable':r['ok']}); st.append(d)
    collect_lsn_lag(); return {'members': st}

@bp_cluster.route('/api/cluster')
def api_cluster():
    try: return jsonify({'ok': True, 'data': cluster_state()})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)}), 500

@bp_cluster.route('/api/lsn')
def api_lsn():
    try:
        lsn, _ = collect_lsn_lag(); return jsonify({'ok': True, 'data': lsn})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)}), 500

@bp_cluster.route('/api/lag-history')
def api_lag_history():
    try:
        _, h = collect_lsn_lag(); return jsonify({'ok': True, 'data': h})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)}), 500

bp_repl = Blueprint('replication', __name__)
def _first_ok(method, path, **kw):
    """Шлёт запрос по нодам, пока одна не ответит ok. Возвращает ответ первой успешной."""
    last = None
    for n in NODES:
        r = patroni_request(n, method, path, **kw)
        if r['ok']: return r
        last = r
    return last or {'ok': False, 'status_code': 0, 'data': {}, 'error': 'нет доступных нод'}

@bp_repl.route('/api/pause', methods=['POST'])
def api_pause():
    p = (request.json or {}).get('pause', True)
    return jsonify(_first_ok('PATCH', '/config', json={'pause': True} if p else {'pause': None}))

@bp_repl.route('/api/switchover', methods=['POST'])
def api_switchover():
    b = request.json or {}; l, c = b.get('leader'), b.get('candidate')
    if not l or not c: return jsonify({'ok': False, 'error': 'leader и candidate обязательны'})
    r = _first_ok('POST', '/switchover', json={'leader': l, 'candidate': c})
    if r.get('ok'):
        _notify_failover('switchover', l, c)
    return jsonify(r)

@bp_repl.route('/api/failover', methods=['POST'])
def api_failover():
    b = request.json or {}; c = b.get('candidate')
    old = (b.get('old_leader') or '').strip()
    for n in NODES:
        if old and n['name'] == old: continue
        r = patroni_request(n, 'POST', '/failover', json={'candidate': c, 'leader': old} if (c or old) else {})
        if r['ok']:
            _notify_failover('failover', old or 'неизвестен', c or 'авто')
            return jsonify(r)
    return jsonify({'ok': False, 'error': 'Нет доступных нод'})

def _notify_failover(kind, old_leader, new_leader):
    """Уведомление на e-mail о switchover/failover (тихо при ошибках)."""
    try:
        import notify
        import time as _t
        ts = _t.strftime('%Y-%m-%d %H:%M:%S')
        label = 'Switchover' if kind == 'switchover' else 'FAILOVER'
        title = f"🔄 {label}: лидер {old_leader} → {new_leader}"
        body = (f"Тип: {label}\n"
                f"Старый лидер: {old_leader}\n"
                f"Новый лидер: {new_leader}\n"
                f"Время: {ts}\n"
                f"Кластер: {CLUSTER_NAME}")
        notify.notify_direct(title, body, severity='warning' if kind == 'failover' else 'info')
    except Exception:
        pass

@bp_repl.route('/api/reinitialize', methods=['POST'])
def api_reinit():
    b = request.json or {}; nm = b.get('node')
    t = next((n for n in NODES if n['name'] == nm), None)
    if not t: return jsonify({'ok': False, 'error': 'Нода не найдена'})
    return jsonify(patroni_request(t, 'POST', '/reinitialize', json={'force': b.get('force', False)}))

bp_perf = Blueprint('performance', __name__)
def _perf_node(n):
    """Снимает TPS/cache-метрику с одной ноды (для распараллеливания)."""
    c = pg_conn(n['host'])
    if not c: return None
    try:
        r1 = q(c, "SELECT xact_commit+xact_rollback AS t FROM pg_stat_database WHERE datname=%s", (PG_DB,))
        r2 = q(c, "SELECT round(100.0*blks_hit/nullif(blks_hit+blks_read,0),2) AS c FROM pg_stat_database WHERE datname=%s", (PG_DB,))
        return (n['name'], {'total_xact': r1[0]['t'] if r1 else 0, 'cache_hit_ratio': r2[0]['c'] if r2 else 0})
    except Exception: return None
    finally: c.close()

@bp_perf.route('/api/perf/snapshot')
def api_perf_snapshot():
    try:
        snap = {'ts': time.time(), 'nodes': {}}
        futs = [EXECUTOR.submit(_perf_node, n) for n in NODES]
        for f in as_completed(futs, timeout=12):
            r = f.result()
            if r: snap['nodes'][r[0]] = r[1]
        if snap['nodes']: TPS_HISTORY.append(snap); CACHE_HISTORY.append(snap)
        return jsonify({'ok': True, 'data': snap})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})

@bp_perf.route('/api/perf/tps-history')
def api_tps():
    try: return jsonify({'ok': True, 'data': list(TPS_HISTORY)})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})

@bp_perf.route('/api/perf/cache-history')
def api_cache():
    try: return jsonify({'ok': True, 'data': list(CACHE_HISTORY)})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})

@bp_perf.route('/api/perf/db-size')
@ttl_cache('perf.db-size', 20)
def api_size():
    try:
        ln = leader_node()
        if not ln: return {'ok': False, 'error': 'Лидер не найден'}
        c = pg_conn(ln['host'])
        if not c: return {'ok': False, 'error': 'Не удалось подключиться'}
        try:
            cur = q(c, "SELECT datname, pg_database_size(datname) AS size FROM pg_database WHERE datistemplate=false ORDER BY 2 DESC")
            SIZE_HISTORY.append({'ts': time.time(), 'databases': {d['datname']: d['size'] for d in cur}})
            return {'ok': True, 'data': {'current': cur, 'history': list(SIZE_HISTORY)[-100:]}}
        finally: c.close()
    except Exception as e: return {'ok': False, 'error': str(e)}

bp_schema = Blueprint('schema', __name__)
@bp_schema.route('/api/databases')
def api_databases():
    try:
        ln = leader_node()
        if not ln: return jsonify({'ok': False, 'error': 'Лидер не найден'})
        c = pg_conn(ln['host'])
        if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
        try:
            rows = q(c, "SELECT datname FROM pg_database WHERE datistemplate=false AND datname NOT IN ('template0','template1') ORDER BY datname")
            names = [r['datname'] for r in rows]
            if PG_DB in names: names.remove(PG_DB); names.insert(0, PG_DB)
            return jsonify({'ok': True, 'data': names})
        finally: c.close()
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})

@bp_schema.route('/api/db/overview')
def api_db_overview():
    """Обзор баз данных: размер, соединения, активность, кэш, рост."""
    try:
        ln = leader_node()
        if not ln: return jsonify({'ok': False, 'error': 'Лидер не найден'})
        c = pg_conn(ln['host'])
        if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
        try:
            rows = q(c, "SELECT d.datname, pg_database_size(d.datname) AS size, "
                        "s.numbackends AS connections, s.xact_commit, s.xact_rollback, "
                        "s.tup_inserted, s.tup_updated, s.tup_deleted, s.tup_returned, "
                        "s.blks_hit, s.blks_read, "
                        "round(100.0*s.blks_hit/nullif(s.blks_hit+s.blks_read,0),2) AS cache_hit, "
                        "s.deadlocks, s.temp_files, pg_encoding_to_char(d.encoding) AS encoding "
                        "FROM pg_database d LEFT JOIN pg_stat_database s ON s.datid=d.oid "
                        "WHERE d.datistemplate=false AND d.datname NOT IN ('template0','template1') "
                        "ORDER BY 2 DESC")
            # рост из истории размеров (SIZE_HISTORY: {ts, databases:{name:size}})
            growth = {}
            if len(SIZE_HISTORY) >= 2:
                first = SIZE_HISTORY[0]
                last = SIZE_HISTORY[-1]
                dt_h = max(0.0, (last['ts'] - first['ts']) / 3600.0)
                for name, sz in last['databases'].items():
                    prev = first['databases'].get(name)
                    if prev is not None:
                        delta = sz - prev
                        growth[name] = {'delta': delta,
                                        'rate_per_hour': round(delta / dt_h, 1) if dt_h > 0 else 0}
            for r in rows:
                g = growth.get(r['datname'])
                r['growth_delta'] = g['delta'] if g else None
                r['growth_rate'] = g['rate_per_hour'] if g else None
            return jsonify({'ok': True, 'data': rows})
        finally: c.close()
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})

def collect_database_schema(db=None):
    ln = leader_node()
    if not ln: return {'ok': False, 'error': 'Лидер не найден', 'tables': [], 'relationships': []}
    c = pg_conn(ln['host'], database=db or PG_DB)
    if not c: return {'ok': False, 'error': f'Не удалось подключиться к базе {db or PG_DB}', 'tables': [], 'relationships': []}
    try:
        cur = q(c, "SELECT schemaname, tablename, pg_total_relation_size(schemaname||'.'||tablename) AS s FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') ORDER BY 1,2")
        T = {}
        for r in cur:
            fn = f"{r['schemaname']}.{r['tablename']}"
            T[fn] = {'name':r['tablename'],'schema':r['schemaname'],'full_name':fn,'size':r['s'],'columns':[],'primary_key':[],'foreign_keys':[],'indexes':[]}
        for r in q(c, "SELECT table_schema||'.'||table_name AS fn, column_name, data_type, character_maximum_length, is_nullable, column_default FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1,2,ordinal_position"):
            if r['fn'] in T:
                ci = {'name':r['column_name'],'type':r['data_type'],'max_length':r['character_maximum_length'],'nullable':r['is_nullable']=='YES','default':r['column_default']}
                if r['character_maximum_length']: ci['type'] = f"{r['data_type']}({r['character_maximum_length']})"
                T[r['fn']]['columns'].append(ci)
        for r in q(c, "SELECT tc.table_schema||'.'||tc.table_name AS fn, kcu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema WHERE tc.constraint_type='PRIMARY KEY' AND tc.table_schema NOT IN ('pg_catalog','information_schema')"):
            if r['fn'] in T: T[r['fn']]['primary_key'].append(r['column_name'])
        rels = []
        for r in q(c, "SELECT tc.table_schema||'.'||tc.table_name AS ft, kcu.column_name AS fc, ccu.table_schema||'.'||ccu.table_name AS tt, ccu.column_name AS tc2, tc.constraint_name AS cn FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name=tc.constraint_name AND ccu.table_schema=tc.table_schema WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema NOT IN ('pg_catalog','information_schema')"):
            if r['ft'] in T:
                T[r['ft']]['foreign_keys'].append({'column':r['fc'],'references_table':r['tt'],'references_column':r['tc2'],'constraint_name':r['cn']})
            rels.append({'source':r['ft'],'target':r['tt'],'from_column':r['fc'],'to_column':r['tc2'],'constraint':r['cn']})
        for r in q(c, "SELECT schemaname||'.'||tablename AS fn, indexname, indexdef FROM pg_indexes WHERE schemaname NOT IN ('pg_catalog','information_schema')"):
            if r['fn'] in T: T[r['fn']]['indexes'].append({'name':r['indexname'],'definition':r['indexdef']})
        return {'ok': True, 'db': db or PG_DB, 'tables': list(T.values()), 'relationships': rels}
    except Exception as e: return {'ok': False, 'error': str(e), 'tables': [], 'relationships': []}
    finally: c.close()

@bp_schema.route('/api/schema')
def api_schema():
    db = (request.args.get('db') or '').strip() or None
    res = collect_database_schema(db)
    if not res.get('ok'): return jsonify({'ok': False, 'error': res.get('error') or 'не удалось прочитать схему'})
    return jsonify({'ok': True, 'data': {'db': res.get('db'), 'tables': res.get('tables', []), 'relationships': res.get('relationships', [])}})

@bp_schema.route('/api/analyze-query', methods=['POST'])
def api_analyze():
    try:
        raw = (request.json or {}).get('query', '') or ''; query = raw.strip()
        if not query: return jsonify({'ok': False, 'error': 'Запрос не указан'})
        cleaned = _strip_leading_comments(query)
        cl = _classify_query(cleaned)
        if re.match(r'^(SHOW|SET|RESET|DISCARD)\b', cleaned, re.I):
            return jsonify({'ok': True, 'data': {'query':query,'plan':None,'plan_error':'Это команда настройки/сессии (SHOW/SET/RESET/DISCARD).','classification':cl,'recommendations_summary':[],'plan_issues':[],'index_recommendations':[],'rewrite_suggestions':[],'tables':[],'table_stats':{},'tables_count':0,'issues_count':0,'indexes_count':0,'rewrites_count':0}})
        if not re.match(r'^(SELECT|INSERT|UPDATE|DELETE|WITH|EXPLAIN)\b', cleaned, re.I):
            return jsonify({'ok': False, 'error': 'Анализируются SELECT / INSERT / UPDATE / DELETE / WITH. DDL/DCL не анализируются.'})
        if _is_sys_query(cleaned):
            return jsonify({'ok': True, 'data': {'query':query,'plan':None,'plan_error':'Это служебный запрос к системному каталогу.','classification':cl,'recommendations_summary':[],'plan_issues':[],'index_recommendations':[],'rewrite_suggestions':[],'tables':[],'table_stats':{},'tables_count':0,'issues_count':0,'indexes_count':0,'rewrites_count':0}})
        ln = leader_node()
        if not ln: return jsonify({'ok': False, 'error': 'Лидер не найден'})
        c = pg_conn(ln['host'])
        if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
        plan = None; plan_error = None
        try:
            cur = c.cursor(); cur.execute("EXPLAIN (FORMAT JSON) " + _deparam(cleaned)); plan = cur.fetchone()[0]; cur.close()
        except Exception as e: plan_error = str(e)
        issues = []
        if plan: _plan_node(plan[0].get('Plan', {}), issues)
        recs = _recommend(cleaned, c)
        for r in recs:
            if r['type'] in ('index','functional_index'):
                st = _tstats(c, r['table'])
                if st:
                    est = int(st['table_size'] * 0.15 * len(r['columns']))
                    r['impact_estimate'] = {'table_rows':st['est_rows'],'table_size':st['table_size'],'estimated_index_size':est,'size_increase_percent':round(100*est/max(1,st['total_size']),1)}
        rwr = _rewrite(cleaned); tbl = list(_tables(cleaned)); ts = {t: s for t in tbl if (s := _tstats(c, t))}
        c.close()
        summary = _summarize(issues, recs, rwr)
        return jsonify({'ok': True, 'data': {'query':query,'plan':plan,'plan_error':plan_error,'classification':cl,'recommendations_summary':summary,'plan_issues':issues,'index_recommendations':recs,'rewrite_suggestions':rwr,'tables':tbl,'table_stats':ts,'tables_count':len(tbl),'issues_count':len(issues),'indexes_count':len(recs),'rewrites_count':len(rwr)}})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)}), 500

bp_ql = Blueprint('querylab', __name__)
@bp_ql.route('/api/querylab/slow')
@ttl_cache('ql.slow', 15)
def api_slow():
    try:
        ln = leader_node()
        if not ln: return {'ok': False, 'error': 'Лидер не найден'}
        c = pg_conn(ln['host'])
        if not c: return {'ok': False, 'error': 'Не удалось подключиться'}
        try:
            has = q(c, "SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements'")
            if not has: return {'ok': False, 'error': 'pg_stat_statements не установлен'}
            rows = q(c, "SELECT queryid::text AS queryid, query, calls, round(total_exec_time::numeric,2) AS total_time, round(mean_exec_time::numeric,2) AS mean_time, round(min_exec_time::numeric,2) AS min_time, round(max_exec_time::numeric,2) AS max_time, rows, shared_blks_hit AS blks_hit, shared_blks_read AS blks_read, temp_blks_read AS temp_blks, round(100.0*shared_blks_hit/nullif(shared_blks_hit+shared_blks_read,0),2) AS cache_hit FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) AND calls>=5 ORDER BY mean_time DESC LIMIT 100")
            rows = [r for r in rows if not _is_monitoring_query(r.get('query', ''))]
            rows = rows[:50]
            # проставляем классификацию: служебный / пользовательский
            for r in rows:
                cl = _classify_query(r.get('query', ''))
                r['category'] = cl['category']
                r['system'] = cl['system']
                r['label'] = cl['label']
            return {'ok': True, 'data': rows}
        finally: c.close()
    except Exception as e: return {'ok': False, 'error': str(e)}

@bp_ql.route('/api/querylab/current')
def api_current():
    try:
        ln = leader_node()
        if not ln: return {'ok': False, 'error': 'Лидер не найден'}
        c = pg_conn(ln['host'])
        if not c: return {'ok': False, 'error': 'Не удалось подключиться'}
        try:
            rows = q(c, "SELECT pid, usename, application_name, client_addr, state, query_start, query, extract(epoch from (now()-query_start))*1000 AS duration_ms FROM pg_stat_activity WHERE state<>'idle' AND pid<>pg_backend_pid() AND datname=current_database() AND extract(epoch from (now()-query_start))*1000>=100 ORDER BY query_start LIMIT 50")
            for r in rows:
                r['client'] = str(r.get('client_addr')) if r.get('client_addr') else 'local'; r['duration_ms'] = round(r.get('duration_ms') or 0, 2)
            return {'ok': True, 'data': rows}
        finally: c.close()
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})

@bp_ql.route('/api/querylab/show')
def api_show():
    """Безопасный GET для SHOW-параметров конкретной ноды (из фиксированного списка).
    Используется фронтом для отображения delay/archive_mode без прав на запись."""
    param = (request.args.get('param') or '').upper().strip()
    node = (request.args.get('node') or '').strip()
    allowed = {'RECOVERY_MIN_APPLY_DELAY', 'ARCHIVE_MODE'}
    if param not in allowed: return jsonify({'ok': False, 'error': 'параметр не поддерживается'})
    t = next((n for n in NODES if n['name'] == node), None)
    if not t: return jsonify({'ok': False, 'error': 'Нода не найдена'})
    c = pg_conn(t['host'])
    if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
    try:
        rows = q(c, f"SHOW {param}")
        value = rows[0].get(param.lower()) if rows else None
        return jsonify({'ok': True, 'data': {'node': node, 'param': param, 'value': value}})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})
    finally: c.close()

@bp_ql.route('/api/querylab/exec', methods=['POST'])
def api_exec():
    b = request.json or {}; sql = b.get('sql', '').strip(); nm = b.get('node')
    db = (b.get('db') or b.get('database') or '').strip() or None
    if not sql: return jsonify({'ok': False, 'error': 'SQL не указан'})
    up = sql.upper()
    for cmd in ['DROP ','TRUNCATE ','ALTER SYSTEM ','CREATE ROLE ','ALTER ROLE ','REVOKE ']:
        if up.startswith(cmd): return jsonify({'ok': False, 'error': f'{cmd.strip()} запрещено'})
    t = next((n for n in NODES if n['name'] == nm), None)
    if not t: return jsonify({'ok': False, 'error': 'Нода не найдена'})
    c = pg_conn(t['host'], database=db)
    if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
    try:
        cur = c.cursor(); t0 = time.time(); cur.execute(sql); dt = time.time() - t0
        if cur.description:
            cols = [d[0] for d in cur.description]; rows = cur.fetchall(); rr = []
            for row in rows:
                d = {}
                for i, col in enumerate(cols):
                    v = row[i]
                    if isinstance(v, (datetime, timedelta)): v = str(v)
                    elif isinstance(v, Decimal): v = float(v)
                    d[col] = v
                rr.append(d)
            cur.close(); c.close()
            return jsonify({'ok': True, 'data': {'columns': cols, 'rows': rr, 'row_count': len(rr), 'execution_time': round(dt*1000, 2)}})
        cur.close(); c.close()
        return jsonify({'ok': True, 'data': {'message': 'Выполнено', 'execution_time': round(dt*1000, 2)}})
    except Exception as e:
        c.close(); return jsonify({'ok': False, 'error': str(e)})

bp_dba = Blueprint('dba', __name__)
@bp_dba.route('/api/dba/summary')
@ttl_cache('dba.summary', 20)
def api_dba_summary():
    try:
        ln = leader_node()
        if not ln: return {'ok': False, 'error': 'Лидер не найден'}
        c = pg_conn(ln['host'])
        if not c: return {'ok': False, 'error': 'Не удалось подключиться'}
        try:
            s = {'idle':[],'long':[],'bloat':[],'vacuum':[],'unused':[],'deadlocks':[],'alerts':[]}
            try:
                s['idle'] = q(c, "SELECT pid, usename, application_name, client_addr, state, query, extract(epoch from (now()-state_change))::int AS idle_s, extract(epoch from (now()-xact_start))::int AS xact_s FROM pg_stat_activity WHERE state='idle in transaction' AND extract(epoch from (now()-state_change))>300 ORDER BY xact_start")
                if s['idle']: s['alerts'].append({'type':'critical','msg':f"{len(s['idle'])} idle in transaction > 5 мин"})
            except Exception: pass
            try:
                s['long'] = q(c, "SELECT pid, usename, application_name, query, extract(epoch from (now()-query_start))::int AS dur_s FROM pg_stat_activity WHERE state='active' AND now()-query_start>interval '1 hour'")
                if s['long']: s['alerts'].append({'type':'warning','msg':f"{len(s['long'])} запросов > 1 часа"})
            except Exception: pass
            try:
                s['bloat'] = q(c, "SELECT u.schemaname, u.relname AS t, u.n_dead_tup, u.n_live_tup, CASE WHEN u.n_live_tup+u.n_dead_tup>0 THEN round(100.0*u.n_dead_tup/(u.n_live_tup+u.n_dead_tup),2) ELSE 0 END AS pct, pg_total_relation_size(u.relid) AS sz FROM pg_stat_user_tables u WHERE u.n_dead_tup>1000 ORDER BY u.n_dead_tup DESC LIMIT 20")
                hb = [r for r in s['bloat'] if r.get('pct',0) > 30]
                if hb: s['alerts'].append({'type':'warning','msg':f'{len(hb)} таблиц с bloat > 30%'})
            except Exception: pass
            try:
                s['vacuum'] = q(c, "SELECT schemaname, relname, n_dead_tup, n_live_tup, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables WHERE (n_dead_tup>0.2*n_live_tup AND n_live_tup>1000) OR (last_autovacuum IS NULL AND n_live_tup>10000) ORDER BY n_dead_tup DESC LIMIT 20")
                if s['vacuum']: s['alerts'].append({'type':'info','msg':f"{len(s['vacuum'])} таблиц ждут vacuum/analyze"})
            except Exception: pass
            try:
                s['unused'] = q(c, "SELECT s.schemaname, s.relname AS t, s.indexrelname AS ix, pg_relation_size(s.indexrelid) AS sz FROM pg_stat_user_indexes s JOIN pg_index i ON s.indexrelid=i.indexrelid WHERE s.idx_scan=0 AND i.indisunique=false AND pg_relation_size(s.indexrelid)>1048576 ORDER BY sz DESC LIMIT 20")
                if s['unused']:
                    tw = sum(r.get('sz',0) for r in s['unused']); s['alerts'].append({'type':'info','msg':f"{len(s['unused'])} неиспользуемых индексов ({fb(tw)})"})
            except Exception: pass
            try: s['deadlocks'] = q(c, "SELECT datname, deadlocks, stats_reset FROM pg_stat_database WHERE datname IS NOT NULL AND deadlocks>0 ORDER BY deadlocks DESC LIMIT 10")
            except Exception: pass
            return {'ok': True, 'data': s}
        finally: c.close()
    except Exception as e: return {'ok': False, 'error': str(e)}

@bp_dba.route('/api/dba/bloat')
@ttl_cache('dba.bloat', 20)
def api_bloat():
    try:
        ln = leader_node()
        if not ln: return {'ok': False, 'error': 'Лидер не найден'}
        c = pg_conn(ln['host'])
        if not c: return {'ok': False, 'error': 'Не удалось подключиться'}
        try:
            t = q(c, "SELECT u.schemaname, u.relname AS t, u.n_dead_tup, u.n_live_tup, CASE WHEN u.n_live_tup+u.n_dead_tup>0 THEN round(100.0*u.n_dead_tup/(u.n_live_tup+u.n_dead_tup),2) ELSE 0 END AS pct, pg_total_relation_size(u.relid) AS sz, u.last_autovacuum FROM pg_stat_user_tables u WHERE u.n_dead_tup>100 ORDER BY u.n_dead_tup DESC")
            ix = q(c, "SELECT schemaname, relname AS t, indexrelname AS ix, pg_relation_size(indexrelid) AS sz, idx_scan FROM pg_stat_user_indexes ORDER BY sz DESC LIMIT 50")
            return {'ok': True, 'data': {'tables': t, 'indexes': ix}}
        finally: c.close()
    except Exception as e: return {'ok': False, 'error': str(e)}

@bp_dba.route('/api/dba/locks')
def api_locks():
    locks = []; checked = 0
    for n in NODES:
        c = pg_conn(n['host'])
        if not c:
            locks.append({'error': f"нет связи с {n['name']}"}); continue
        checked += 1
        try:
            for r in q(c, "SELECT l.pid, l.mode, CASE WHEN l.relation IS NOT NULL THEN l.relation::regclass::text ELSE (SELECT datname FROM pg_database WHERE oid=l.database) END AS obj, a.usename, a.state, a.query, l.granted FROM pg_locks l LEFT JOIN pg_stat_activity a ON l.pid=a.pid WHERE l.database=(SELECT oid FROM pg_database WHERE datname=current_database()) AND l.pid<>pg_backend_pid() ORDER BY l.pid"):
                r['node'] = n['name']; r['query'] = (r.get('query') or '')[:100]; locks.append(r)
        except Exception as e: locks.append({'error': f"{n['name']}: {e}"})
        finally: c.close()
    return jsonify({'ok': True, 'data': locks, 'meta': {'checked': checked, 'total': len(NODES), 'ts': time.time()}})

@bp_dba.route('/api/dba/connections')
def api_connections():
    try:
        max_conn = 100; by_node = []; total_all = 0
        for n in NODES:
            c = pg_conn(n['host'])
            if not c:
                by_node.append({'name':n['name'],'ok':False,'total':0,'max':None,'by_user':[]}); continue
            try:
                tot = q(c, "SELECT count(*) AS total FROM pg_stat_activity")
                mx = q(c, "SHOW max_connections")
                by = q(c, "SELECT usename, application_name, state, count(*) AS cnt FROM pg_stat_activity GROUP BY usename, application_name, state ORDER BY cnt DESC")
                t = tot[0]['total'] if tot else 0; m = int(mx[0]['max_connections']) if mx else None
                if m: max_conn = m
                total_all += t
                by_node.append({'name':n['name'],'ok':True,'total':t,'max':m,'by_user':by})
            except Exception as e: by_node.append({'name':n['name'],'ok':False,'total':0,'max':None,'by_user':[],'error':str(e)})
            finally: c.close()
        return jsonify({'ok': True, 'data': {'by_node': by_node, 'total_all': total_all, 'max_connections': max_conn}})
    except Exception as e: return jsonify({'ok': False, 'error': str(e)})

@bp_dba.route('/api/dba/kill', methods=['POST'])
def api_kill():
    b = request.json or {}; nm, pid = b.get('node'), b.get('pid')
    if not nm or not pid: return jsonify({'ok': False, 'error': 'node и pid обязательны'})
    t = next((n for n in NODES if n['name'] == nm), None)
    if not t: return jsonify({'ok': False, 'error': 'Нода не найдена'})
    c = pg_conn(t['host'])
    if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
    try:
        cur = c.cursor(); cur.execute("SELECT pg_terminate_backend(%s)", (pid,)); ok = cur.fetchone()[0]; cur.close(); c.close()
        return jsonify({'ok': ok, 'data': {'terminated': ok}})
    except Exception as e:
        c.close(); return jsonify({'ok': False, 'error': str(e)})

@bp_dba.route('/api/dba/cancel', methods=['POST'])
def api_cancel():
    pid = (request.json or {}).get('pid')
    if not pid: return jsonify({'ok': False, 'error': 'pid обязателен'})
    ln = leader_node()
    if not ln: return jsonify({'ok': False, 'error': 'Лидер не найден'})
    c = pg_conn(ln['host'])
    if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
    try:
        cur = c.cursor(); cur.execute("SELECT pg_cancel_backend(%s)", (pid,)); ok = cur.fetchone()[0]; cur.close(); c.close()
        return jsonify({'ok': ok, 'data': {'cancelled': ok}})
    except Exception as e:
        c.close(); return jsonify({'ok': False, 'error': str(e)})

@bp_dba.route('/api/dba/kill-user-sessions', methods=['POST'])
def api_kill_user_sessions():
    member_names = [n['name'] for n in NODES]; results = []
    for n in NODES:
        c = pg_conn(n['host'])
        if not c:
            results.append({'node':n['name'],'ok':False,'killed':0,'error':'нет связи'}); continue
        try:
            cur = c.cursor()
            cur.execute("""
                SELECT count(*) FROM (
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE pid <> pg_backend_pid()
                      AND backend_type = 'client backend'
                      AND coalesce(application_name,'') !~* 'patroni'
                      AND coalesce(application_name,'') <> ALL(%s)
                ) s
            """, (member_names,))
            killed = cur.fetchone()[0]; cur.close(); c.close()
            results.append({'node':n['name'],'ok':True,'killed':killed})
        except Exception as e:
            try: c.close()
            except Exception: pass
            results.append({'node':n['name'],'ok':False,'killed':0,'error':str(e)})
    total = sum(r['killed'] for r in results)
    return jsonify({'ok': True, 'data': {'results': results, 'total': total}})

@bp_dba.route('/api/dba/vacuum', methods=['POST'])
def api_vacuum():
    b = request.json or {}; t, sch, full, anl = b.get('table'), b.get('schema','public'), b.get('full',False), b.get('analyze',True)
    if not t: return jsonify({'ok': False, 'error': 'table обязателен'})
    try: qi_t, qi_s = _qid(t), _qid(sch)
    except ValueError as e: return jsonify({'ok': False, 'error': str(e)})
    ln = leader_node()
    if not ln: return jsonify({'ok': False, 'error': 'Лидер не найден'})
    c = pg_conn(ln['host'])
    if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
    try:
        cmd = 'VACUUM' + (' FULL' if full else '') + (' ANALYZE' if anl else '') + f' {qi_s}.{qi_t}'
        cur = c.cursor(); t0 = time.time(); cur.execute(cmd); dt = time.time() - t0; cur.close(); c.close()
        return jsonify({'ok': True, 'data': {'message': f'{cmd} за {dt:.2f}с'}})
    except Exception as e:
        c.close(); return jsonify({'ok': False, 'error': str(e)})

@bp_dba.route('/api/dba/drop-index', methods=['POST'])
def api_dropidx():
    b = request.json or {}; ix, sch = b.get('index'), b.get('schema','public')
    if not ix: return jsonify({'ok': False, 'error': 'index обязателен'})
    try: qi_ix, qi_s = _qid(ix), _qid(sch)
    except ValueError as e: return jsonify({'ok': False, 'error': str(e)})
    ln = leader_node()
    if not ln: return jsonify({'ok': False, 'error': 'Лидер не найден'})
    c = pg_conn(ln['host'])
    if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
    try:
        cur = c.cursor(); cur.execute(f"DROP INDEX CONCURRENTLY {qi_s}.{qi_ix}"); cur.close(); c.close()
        return jsonify({'ok': True, 'data': {'message': f'{sch}.{ix} удалён'}})
    except Exception as e:
        c.close(); return jsonify({'ok': False, 'error': str(e)})

@bp_dba.route('/api/dba/delay-replica', methods=['POST'])
def api_delay_replica():
    b = request.json or {}; nm = b.get('node'); reset = bool(b.get('reset'))
    try: hours = max(1, min(24, int(b.get('hours', 1) or 1)))
    except (TypeError, ValueError): return jsonify({'ok': False, 'error': 'hours должно быть числом (1–24)'})
    t = next((n for n in NODES if n['name'] == nm), None)
    if not t: return jsonify({'ok': False, 'error': 'Нода не найдена'})
    info = patroni_request(t, 'GET', '/patroni')
    role = (info['data'].get('role','') if info['ok'] else '').lower()
    if role in ('master','leader','primary'): return jsonify({'ok': False, 'error': 'Нельзя ставить задержку лидеру'})
    if agent_api is not None and hasattr(agent_api, 'agent_online') and agent_api.agent_online(nm):
        cmd_id = agent_api.enqueue_agent(nm, ('remove_recovery_delay' if reset else 'set_recovery_delay'), hours=hours)
        DELAY_CACHE.pop(t['host'], None)
        return jsonify({'ok': True, 'data': {'node': nm, 'reset': reset, 'hours': hours, 'via': 'agent', 'cmd_id': cmd_id}})
    c = pg_conn(t['host'])
    if not c: return jsonify({'ok': False, 'error': 'Не удалось подключиться'})
    try:
        cur = c.cursor()
        if reset: cur.execute("ALTER SYSTEM RESET recovery_min_apply_delay")
        else: cur.execute(f"ALTER SYSTEM SET recovery_min_apply_delay = '{hours}h'")
        cur.execute("SELECT pg_reload_conf()")
        cur.execute("SHOW recovery_min_apply_delay"); value = cur.fetchone()[0]
        cur.close(); c.close()
        DELAY_CACHE[t['host']] = (time.time(), value)
        return jsonify({'ok': True, 'data': {'node': nm, 'reset': reset, 'hours': hours, 'via': 'runtime', 'value': value}})
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({'ok': False, 'error': str(e)})

TUNE_PARAMS = ['shared_buffers','effective_cache_size','work_mem','maintenance_work_mem','wal_buffers','max_connections','random_page_cost','effective_io_concurrency','huge_pages','checkpoint_timeout','checkpoint_completion_target','default_statistics_target','temp_buffers','autovacuum_naptime','autovacuum_vacuum_scale_factor','autovacuum_analyze_scale_factor','autovacuum_vacuum_insert_scale_factor','autovacuum_vacuum_insert_threshold','autovacuum_max_workers','log_min_duration_statement','log_temp_files','track_activity_query_size','max_wal_size','min_wal_size','from_collapse_limit','geqo','geqo_threshold','enable_sorted_merge_join','jit','ssl','max_worker_processes','max_parallel_workers','max_parallel_workers_per_gather','online_analyze.verbose','online_analyze.min_interval','online_analyze.table_type','plantuner.fix_empty_table']

@bp_dba.route('/api/dba/config-recs')
@ttl_cache('dba.config-recs', 30)
def api_cfgrecs():
    try:
        ln = leader_node()
        if not ln: return {'ok': False, 'error': 'Лидер не найден'}
        c = pg_conn(ln['host'])
        if not c: return {'ok': False, 'error': 'Не удалось подключиться'}
        try:
            cur = {}
            for p in TUNE_PARAMS:
                try:
                    r = q(c, f"SHOW {p}")
                    if r: cur[p] = r[0].get(p, '')
                except Exception:
                    # пользовательские GUC (online_analyze.*, plantuner.*) есть только
                    # в Postgres Pro с патчем — пропускаем отсутствующие
                    cur[p] = None
            return {'ok': True, 'data': {'current': cur}}
        finally: c.close()
    except Exception as e: return {'ok': False, 'error': str(e)}

@bp_dba.route('/api/dba/hostinfo')
def api_hostinfo():
    ram = None; cpu = None
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    ram = round(int(line.split()[1]) / 1024 / 1024, 1); break
    except Exception: pass
    try: cpu = os.cpu_count()
    except Exception: pass
    return jsonify({'ok': True, 'data': {'ram_gb': ram, 'cpu': cpu, 'available': ram is not None or cpu is not None, 'note': 'характеристики хоста UI-сервера'}})

bp_ops = Blueprint('ops', __name__)
@bp_ops.route('/api/ops/restart', methods=['POST'])
def api_restart():
    nm = (request.json or {}).get('node')
    t = next((n for n in NODES if n['name'] == nm), None)
    if not t: return jsonify({'ok': False, 'error': 'Нода не найдена'})
    info = patroni_request(t, 'GET', '/patroni')
    if not info['ok']: return jsonify({'ok': False, 'error': 'нет информации о ноде'})
    pend = info['data'].get('pending_restart', False)
    return jsonify(patroni_request(t, 'POST', '/restart', json={'restart_pending': True} if pend else {}))

@bp_ops.route('/api/ops/reload', methods=['POST'])
def api_reload():
    nm = (request.json or {}).get('node')
    t = next((n for n in NODES if n['name'] == nm), None)
    if not t: return jsonify({'ok': False, 'error': 'Нода не найдена'})
    return jsonify(patroni_request(t, 'POST', '/reload'))

@bp_ops.route('/api/ops/restart-cluster', methods=['POST'])
def api_restart_cluster():
    info = []
    for n in NODES:
        r = patroni_request(n, 'GET', '/patroni')
        info.append((n, r['data'].get('role','unknown') if r['ok'] else 'unreachable', r['data'].get('pending_restart',False) if r['ok'] else False))
    ordered = [(n,r,p) for n,r,p in info if r not in ('master','leader','primary')] + [(n,r,p) for n,r,p in info if r in ('master','leader','primary')]
    res = []
    for n, role, pend in ordered:
        if role == 'unreachable':
            res.append({'node':n['name'],'role':'unreachable','ok':False,'status_code':0,'error':'недоступна'}); continue
        r = patroni_request(n, 'POST', '/restart', json={'restart_pending': True} if pend else {})
        res.append({'node':n['name'],'role':role,'ok':r['ok'],'status_code':r['status_code'],'error':r.get('error') or r.get('data',{}).get('_raw_response','')})
    ok = sum(1 for x in res if x['ok'])
    return jsonify({'ok': True, 'data': {'results': res, 'summary': f'Перезапущено {ok}/{len(res)}'}})

@bp_ops.route('/api/ops/patroni-config', methods=['GET'])
def api_pcfg_get():
    for n in NODES:
        r = patroni_request(n, 'GET', '/config')
        if r['ok']: return jsonify({'ok': True, 'data': {'config': r['data']}})
    return jsonify({'ok': False, 'error': 'не удалось подключиться'})

@bp_ops.route('/api/ops/patroni-config', methods=['POST'])
def api_pcfg_set():
    cd = (request.json or {}).get('config')
    if not cd: return jsonify({'ok': False, 'error': 'конфиг пуст'})
    if isinstance(cd, str):
        try: cd = yaml.safe_load(cd)
        except yaml.YAMLError as e: return jsonify({'ok': False, 'error': f'YAML: {e}'})
    for n in NODES:
        r = patroni_request(n, 'PATCH', '/config', json=cd)
        if r['ok']: return jsonify({'ok': True, 'data': {'message': 'применено'}})
    return jsonify({'ok': False, 'error': 'не удалось применить'})

@bp_ops.route('/api/ops/history')
def api_history():
    return jsonify(_first_ok('GET', '/history'))

@bp_ops.route('/api/backups', methods=['POST'])
def api_backups():
    b = request.json or {}; node = b.get('node'); action = b.get('action')
    allowed = {'backup_full','backup_delta','list_backups','rotate','delete_backup',
               'verify_backup','set_wal_archiving','restore','pitr','clone_restore','clone_inspect'}
    if action not in allowed: return jsonify({'ok': False, 'error': 'недопустимое действие'})
    if agent_api is None or not agent_api.agent_online(node):
        return jsonify({'ok': False, 'error': 'агент офлайн на этой ноде'})
    cid = agent_api.enqueue_agent(node, action, keep=b.get('keep', 3), name=b.get('name'),
                                  on=b.get('on'), target_time=b.get('target_time'),
                                  label=b.get('label'), path=b.get('path'))
    return jsonify({'ok': True, 'data': {'cmd_id': cid}})

# ==================== ЛОГИЧЕСКИЕ ДАМПЫ (разд. 3 спеки) ====================
_DUMP_ACTIONS = {
    'create': 'dump_create', 'list': 'dump_list', 'rotate': 'dump_rotate', 'delete': 'dump_delete',
    'nfs_mount': 'nfs_mount', 'nfs_umount': 'nfs_umount', 'report': 'send_report',
}
_DUMP_ADMIN_ACTIONS = {'nfs_mount', 'nfs_umount'}  # управление монтированием — только admin
_RE_BNAME = re.compile(r'^[A-Za-z0-9_.+-]+$')  # имя файла дампа (агент тоже валидирует)

@bp_ops.route('/api/dumps', methods=['POST'])
def api_dumps():
    b = request.json or {}
    node = b.get('node'); action = b.get('action')
    agent_action = _DUMP_ACTIONS.get(action)
    if agent_action is None: return jsonify({'ok': False, 'error': 'недопустимое действие'})
    if action in _DUMP_ADMIN_ACTIONS and session.get('role') != 'admin':
        return jsonify({'ok': False, 'error': 'требуется роль admin'}), 403
    params = {}
    if action == 'create':
        db = (b.get('db') or '').strip()
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$', db):
            return jsonify({'ok': False, 'error': 'некорректное имя БД'})
        if b.get('mode') not in (None, 'custom', 'dir', 'sql-zstd'):
            return jsonify({'ok': False, 'error': 'неизвестный режим дампа'})
        params = {'db': db, 'mode': b.get('mode') or 'custom',
                  'jobs': _arg_int(b.get('jobs'), 2, 1, 8), 'compress': _arg_int(b.get('compress'), 3, 0, 19)}
    elif action == 'rotate':
        params = {'days': _arg_int(b.get('days'), 7, 1, 365)}
    elif action == 'delete':
        name = b.get('name') or ''
        if not _RE_BNAME.match(name) or '..' in name:
            return jsonify({'ok': False, 'error': 'некорректное имя файла'})
        params = {'name': name}
    elif action == 'nfs_mount':
        share = b.get('share') or ''
        if not re.match(r'^[\w.-]+:/[\w./-]*$', share):
            return jsonify({'ok': False, 'error': 'share в формате host:/path'})
        params = {'share': share, 'mountpoint': b.get('mountpoint') or '/backup', 'options': b.get('options') or ''}
    elif action == 'nfs_umount':
        params = {'mountpoint': b.get('mountpoint') or '/backup'}
    elif action == 'report':
        to = b.get('to') or ''
        if not re.match(r'^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$', to):
            return jsonify({'ok': False, 'error': 'некорректный e-mail'})
        params = {'to': to, 'subject': (b.get('subject') or 'отчёт бэкапа')[:200], 'body': (b.get('body') or '')[:8000]}
    if agent_api is None or not agent_api.agent_online(node):
        return jsonify({'ok': False, 'error': 'агент офлайн на этой ноде'})
    cid = agent_api.enqueue_agent(node, agent_action, **params)
    return jsonify({'ok': True, 'data': {'cmd_id': cid}})

@bp_ops.route('/api/ops/cmd-result')
def api_cmd_result():
    """Результат конкретной команды агента (для поллинга длинных задач: дамп, ротация…)."""
    node = request.args.get('node') or ''; cid = request.args.get('id') or ''
    if agent_api is None or not re.match(r'^[\w.-]+-\d+$', cid) or not cid.startswith(node + '-'):
        return jsonify({'ok': False, 'error': 'некорректный запрос'}), 400
    with agent_api._LOCK:
        ag = agent_api.AGENTS.get(node)
        res = (ag or {}).get('results', {}).get(cid)
    return jsonify({'ok': True, 'data': res})
# ==================== /ЛОГИЧЕСКИЕ ДАМПЫ ====================

# ==================== ОБСЛУЖИВАНИЕ: REPACK + ПЛАНИРОВЩИК (разд. 12 спеки) ====================
@bp_ops.route('/api/ops/repack', methods=['POST'])
def api_repack():
    """Дефрагментация таблицы через агента: pg_repack (онлайн) или VACUUM FULL."""
    b = request.json or {}
    db = (b.get('db') or '').strip()
    schema = (b.get('schema') or 'public').strip()
    table = (b.get('table') or '').strip()
    method = b.get('method') or 'auto'
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$', db):
        return jsonify({'ok': False, 'error': 'некорректное имя БД'})
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$', schema):
        return jsonify({'ok': False, 'error': 'некорректная схема'})
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$', table):
        return jsonify({'ok': False, 'error': 'некорректная таблица'})
    if method not in ('auto', 'repack', 'vacuum'):
        return jsonify({'ok': False, 'error': 'метод должен быть auto/repack/vacuum'})
    node = b.get('node') or (leader_node() or {}).get('name')
    if agent_api is None or not agent_api.agent_online(node):
        return jsonify({'ok': False, 'error': 'агент офлайн на этой ноде'})
    cid = agent_api.enqueue_agent(node, 'repack_table', db=db, schema=schema, table=table, method=method)
    return jsonify({'ok': True, 'data': {'cmd_id': cid, 'node': node}})

MAINT_PATH = data_path('maint_schedule.json', os.path.dirname(os.path.abspath(CONFIG_PATH)))
MAINT = {'enabled': False, 'time': '03:30', 'days': [1, 4], 'node': '',
         'min_bloat_pct': 40, 'max_tables': 5, 'method': 'auto', 'db': ''}
try:
    with open(MAINT_PATH, encoding='utf-8', errors='replace') as f: MAINT.update(_json.load(f))
except Exception: pass

def _maint_save():
    try:
        with open(MAINT_PATH, 'w', encoding='utf-8') as f: _json.dump(MAINT, f)
    except Exception: pass

_maint_last = {'date': None}
def _maint_loop():
    """Планировщик обслуживания: в окно (время + дни нед.) чистит самые раздутые таблицы."""
    while True:
        try:
            now = time.localtime()
            hm = time.strftime('%H:%M', now); today = time.strftime('%Y-%m-%d', now)
            if MAINT.get('enabled') and hm >= MAINT.get('time', '03:30') and _maint_last['date'] != today \
               and (now.tm_wday in (MAINT.get('days') or [])):
                ln = leader_node()
                if ln and agent_api:
                    node = MAINT.get('node') or ln['name']
                    if agent_api.agent_online(node):
                        try:
                            c = pg_conn(ln['host'])
                            db = MAINT.get('db') or PG_DB
                            if c:
                                try:
                                    rows = q(c, "SELECT schemaname, relname AS t, n_dead_tup, n_live_tup, "
                                                "CASE WHEN n_live_tup+n_dead_tup>0 THEN round(100.0*n_dead_tup/(n_live_tup+n_dead_tup),2) ELSE 0 END AS pct "
                                                "FROM pg_stat_user_tables WHERE n_dead_tup>1000 "
                                                "ORDER BY pct DESC LIMIT %s" % int(MAINT.get('max_tables', 5)))
                                    for r in rows:
                                        if float(r.get('pct', 0)) >= float(MAINT.get('min_bloat_pct', 40)):
                                            agent_api.enqueue_agent(node, 'repack_table', db=db,
                                                                     schema=r['schemaname'], table=r['t'],
                                                                     method=MAINT.get('method', 'auto'))
                                finally: c.close()
                        except Exception: pass
                        _maint_last['date'] = today
        except Exception: pass
        time.sleep(30)
if not getattr(_maint_loop, '_started', False):
    _maint_loop._started = True
    threading.Thread(target=_maint_loop, daemon=True, name='maint-scheduler').start()

@bp_ops.route('/api/maint-schedule', methods=['GET'])
def api_maint_get():
    return jsonify({'ok': True, 'data': MAINT})

@bp_ops.route('/api/maint-schedule', methods=['POST'])
def api_maint_set():
    b = request.json or {}
    for k in ('enabled', 'time', 'days', 'node', 'min_bloat_pct', 'max_tables', 'method', 'db'):
        if k in b: MAINT[k] = b[k]
    _maint_save()
    return jsonify({'ok': True, 'data': MAINT})
# ==================== /ОБСЛУЖИВАНИЕ ====================

# ==================== ШЕДУЛЕР БЭКАПОВ ====================
SCHED_PATH = data_path('backup_schedule.json', os.path.dirname(os.path.abspath(CONFIG_PATH)))
SCHED = {'enabled': False, 'time': '02:00', 'node': '', 'type': 'delta', 'keep': 3}
try:
    with open(SCHED_PATH, encoding='utf-8', errors='replace') as f: SCHED.update(_json.load(f))
except Exception: pass

def _sched_save():
    try:
        with open(SCHED_PATH, 'w', encoding='utf-8') as f: _json.dump(SCHED, f)
    except Exception: pass

def _pick_backup_node():
    for n in NODES:
        r = patroni_request(n, 'GET', '/patroni')
        role = (r['data'].get('role','') if r['ok'] else '').lower()
        if role not in ('master','leader','primary') and agent_api and agent_api.agent_online(n['name']):
            return n['name']
    for n in NODES:
        if agent_api and agent_api.agent_online(n['name']): return n['name']
    return NODES[0]['name']

_sched_last = {'date': None}
def _scheduler_loop():
    while True:
        try:
            now = time.localtime()
            hm = time.strftime('%H:%M', now); today = time.strftime('%Y-%m-%d', now)
            if SCHED.get('enabled') and hm >= SCHED.get('time','02:00') and _sched_last['date'] != today:
                node = SCHED.get('node') or _pick_backup_node()
                if agent_api and agent_api.agent_online(node):
                    act = 'backup_full' if SCHED.get('type') == 'full' else 'backup_delta'
                    agent_api.enqueue_agent(node, act, keep=SCHED.get('keep', 3))
                    _sched_last['date'] = today
        except Exception: pass
        time.sleep(30)
if not getattr(_scheduler_loop, '_started', False):
    _scheduler_loop._started = True
    threading.Thread(target=_scheduler_loop, daemon=True, name='backup-scheduler').start()

@bp_ops.route('/api/backup-schedule', methods=['GET'])
def api_sched_get():
    return jsonify({'ok': True, 'data': SCHED})

@bp_ops.route('/api/backup-schedule', methods=['POST'])
def api_sched_set():
    b = request.json or {}
    for k in ('enabled', 'time', 'node', 'type', 'keep'):
        if k in b: SCHED[k] = b[k]
    _sched_save()
    return jsonify({'ok': True, 'data': SCHED})

# ---- планировщик ЛОГИЧЕСКИХ дампов (pg_dump) ----
DUMP_SCHED_PATH = data_path('dump_schedule.json', os.path.dirname(os.path.abspath(CONFIG_PATH)))
DUMP_SCHED = {'enabled': False, 'time': '03:00', 'node': '', 'db': '', 'mode': 'custom',
              'jobs': 2, 'compress': 3, 'rotate_days': 7}
try:
    with open(DUMP_SCHED_PATH, encoding='utf-8', errors='replace') as f: DUMP_SCHED.update(_json.load(f))
except Exception: pass

def _dump_sched_save():
    try:
        with open(DUMP_SCHED_PATH, 'w', encoding='utf-8') as f: _json.dump(DUMP_SCHED, f)
    except Exception: pass

_dump_sched_last = {'date': None}
def _dump_scheduler_loop():
    while True:
        try:
            now = time.localtime()
            hm = time.strftime('%H:%M', now); today = time.strftime('%Y-%m-%d', now)
            if DUMP_SCHED.get('enabled') and hm >= DUMP_SCHED.get('time', '03:00') and _dump_sched_last['date'] != today:
                node = DUMP_SCHED.get('node') or (_pick_backup_node() if NODES else '')
                db = (DUMP_SCHED.get('db') or '').strip()
                if node and db and agent_api and agent_api.agent_online(node) \
                   and re.match(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$', db):
                    agent_api.enqueue_agent(node, 'dump_create', db=db,
                                            mode=DUMP_SCHED.get('mode') or 'custom',
                                            jobs=int(DUMP_SCHED.get('jobs') or 2),
                                            compress=int(DUMP_SCHED.get('compress') or 3))
                    days = int(DUMP_SCHED.get('rotate_days') or 0)
                    if days > 0:
                        agent_api.enqueue_agent(node, 'dump_rotate', days=days)
                    _dump_sched_last['date'] = today
        except Exception: pass
        time.sleep(30)
if not getattr(_dump_scheduler_loop, '_started', False):
    _dump_scheduler_loop._started = True
    threading.Thread(target=_dump_scheduler_loop, daemon=True, name='dump-scheduler').start()

@bp_ops.route('/api/dump-schedule', methods=['GET'])
def api_dump_sched_get():
    return jsonify({'ok': True, 'data': DUMP_SCHED})

@bp_ops.route('/api/dump-schedule', methods=['POST'])
def api_dump_sched_set():
    b = request.json or {}
    for k in ('enabled', 'time', 'node', 'db', 'mode', 'jobs', 'compress', 'rotate_days'):
        if k in b: DUMP_SCHED[k] = b[k]
    _dump_sched_save()
    return jsonify({'ok': True, 'data': DUMP_SCHED})
# ==================== /ШЕДУЛЕР ====================

# ==================== МЕТРИКИ ====================
METRICS_HISTORY = {}
MAX_METRICS_POINTS = 1200

def _metrics_collector():
    while True:
        try:
            snap = {}
            with agent_api._LOCK:
                agents = dict(agent_api.AGENTS)
            for name, ag in agents.items():
                m = ag.get('metrics')
                if m and isinstance(m, dict) and 'error' not in m:
                    snap[name] = {'ts': time.time(), 'metrics': m}
            for name, point in snap.items():
                if name not in METRICS_HISTORY:
                    METRICS_HISTORY[name] = deque(maxlen=MAX_METRICS_POINTS)
                METRICS_HISTORY[name].append(point)
        except Exception: pass
        time.sleep(3)

if not getattr(_metrics_collector, '_started', False):
    _metrics_collector._started = True
    threading.Thread(target=_metrics_collector, daemon=True, name='metrics-collector').start()

bp_metrics = Blueprint('metrics', __name__)

@bp_metrics.route('/api/metrics/snapshot')
def api_metrics_snapshot():
    with agent_api._LOCK:
        agents = dict(agent_api.AGENTS)
    out = {}
    for name, ag in agents.items():
        m = ag.get('metrics')
        online = bool(ag and time.time() - ag['last_seen'] < 30)
        out[name] = {'online': online, 'metrics': m}
    return jsonify({'ok': True, 'data': out})

def _arg_int(s, default, lo=None, hi=None):
    try: v = int(s)
    except (TypeError, ValueError): return default
    if lo is not None: v = max(lo, v)
    if hi is not None: v = min(hi, v)
    return v

@bp_metrics.route('/api/metrics/history')
def api_metrics_history():
    node = request.args.get('node')
    points = _arg_int(request.args.get('points', 200), 200, 1, 1200)
    if node and node in METRICS_HISTORY:
        data = list(METRICS_HISTORY[node])[-points:]
    else:
        data = {name: list(h)[-points:] for name, h in METRICS_HISTORY.items()}
    return jsonify({'ok': True, 'data': data})

@bp_metrics.route('/api/metrics/nodes')
def api_metrics_nodes():
    return jsonify({'ok': True, 'data': list(METRICS_HISTORY.keys())})
# ==================== /МЕТРИКИ ====================

# ==================== ЦЕНТР (мульти-кластер) ====================
bp_center = Blueprint('center', __name__)

@bp_center.route('/api/center')
def api_center():
    out = []

    def _cluster_info(c):
        members = []; leader = None; max_lag = 0; cd = None
        futs = {EXECUTOR.submit(patroni_request, n, 'GET', '/cluster'): n for n in c['nodes']}
        try:
            for f in as_completed(futs, timeout=6):
                r = f.result()
                if r['ok']: cd = r['data']; break
        except Exception: pass
        if cd and 'members' in cd:
            for m in cd['members']:
                role = m.get('role','?'); state = m.get('state','?'); lag = m.get('lag',0) or 0
                if role in ('leader','master','primary'): leader = m.get('name')
                if lag > max_lag: max_lag = lag
                members.append({'name':m.get('name'),'role':role,'state':state,'lag':lag})
        ag_on = 0
        if agent_api is not None:
            names = {n['name'] for n in c['nodes']}
            with agent_api._LOCK:
                for nm, ag in agent_api.AGENTS.items():
                    if nm in names and time.time() - ag['last_seen'] < 30: ag_on += 1
        return {'name':c['name'],'nodes':len(c['nodes']),'members':members,
                'leader':leader,'max_lag':max_lag,'agents_online':ag_on,
                'active': c['name'] == CLUSTER_NAME,
                'up': cd is not None}

    futs = [EXECUTOR.submit(_cluster_info, c) for c in CLUSTERS]
    for f in as_completed(futs, timeout=8):
        try: out.append(f.result())
        except Exception: pass
    return jsonify({'ok': True, 'data': out})

@bp_center.route('/api/center/clusters')
def api_center_clusters():
    return jsonify({'ok': True, 'data': [{'name':c['name'],'active':c['name']==CLUSTER_NAME} for c in CLUSTERS]})

@bp_center.route('/api/center/select', methods=['POST'])
def api_center_select():
    global NODES, CLUSTER_NAME
    name = (request.json or {}).get('name')
    c = next((x for x in CLUSTERS if x['name'] == name), None)
    if not c: return jsonify({'ok': False, 'error': 'кластер не найден'})
    NODES = c['nodes']; CLUSTER_NAME = c['name']
    return jsonify({'ok': True, 'data': {'name': CLUSTER_NAME}})
# ==================== PROMETHEUS /metrics ====================
@app.route('/metrics')
def prometheus_metrics():
    """Классический текстовый формат Prometheus: метрики агентов + кластер (публично)."""
    lines = []
    lines.append('# HELP patroni_web_cluster_up Доступность кластера (1/0)')
    lines.append('# TYPE patroni_web_cluster_up gauge')
    lines.append(f'patroni_web_cluster_up 1')
    lag = 0; role_lines = []
    if agent_api is not None:
        now = time.time()
        with agent_api._LOCK: agents = dict(agent_api.AGENTS)
        for name, a in agents.items():
            online = 1 if (now - (a.get('last_seen') or 0) < 30) else 0
            lines.append(f'patroni_console_agent_online{{node="{name}"}} {online}')
            m = (a.get('metrics') or {})
            r = m.get('rates') or {}
            conn = m.get('conn') or {}
            db = m.get('db') or {}
            if r.get('tps') is not None: lines.append(f'patroni_console_tps{{node="{name}"}} {r["tps"]}')
            if r.get('cache_hit_ratio') is not None: lines.append(f'patroni_console_cache_hit_ratio{{node="{name}"}} {r["cache_hit_ratio"]}')
            if conn.get('total') is not None: lines.append(f'patroni_console_connections{{node="{name}"}} {conn["total"]}')
            if conn.get('idle_in_tx') is not None: lines.append(f'patroni_console_idle_in_tx{{node="{name}"}} {conn["idle_in_tx"]}')
            if db.get('deadlocks') is not None: lines.append(f'patroni_console_deadlocks{{node="{name}"}} {db["deadlocks"]}')
            if m.get('in_recovery') is not None: lines.append(f'patroni_console_in_recovery{{node="{name}"}} {1 if m["in_recovery"] else 0}')
            d = a.get('delay')
            if d not in (None, '', '?'):
                try: lines.append(f'patroni_console_recovery_delay{{node="{name}"}} {float(d)}')
                except (TypeError, ValueError): pass
            if m.get('replication'):
                for rep in m['replication']:
                    lag = max(lag, int(rep.get('lag_bytes') or 0))
            # ОС-метрики с агентов (гостевые данные)
            osd = a.get('os') or {}
            if osd.get('loadavg') and len(osd['loadavg']) == 3:
                lines.append(f'patroni_console_load1{{node="{name}"}} {osd["loadavg"][0]}')
                lines.append(f'patroni_console_load5{{node="{name}"}} {osd["loadavg"][1]}')
                lines.append(f'patroni_console_load15{{node="{name}"}} {osd["loadavg"][2]}')
            if osd.get('cpu_used_pct') is not None:
                lines.append(f'patroni_console_cpu_used_pct{{node="{name}"}} {osd["cpu_used_pct"]}')
            mem = osd.get('mem') or {}
            if mem.get('available') is not None and mem.get('total'):
                mempct = round(100.0 * (mem['total'] - mem['available']) / mem['total'], 2)
                lines.append(f'patroni_console_mem_used_pct{{node="{name}"}} {mempct}')
            for d in (osd.get('disks') or []):
                lp = d.get('path') or '?'
                if d.get('used_pct') is not None:
                    lines.append(f'patroni_console_disk_used_pct{{node="{name}",mount="{lp}"}} {d["used_pct"]}')
                if d.get('free') is not None:
                    lines.append(f'patroni_console_disk_free_bytes{{node="{name}",mount="{lp}"}} {d["free"]}')
        try:
            lsn, _ = collect_lsn_lag()
            for nm, v in lsn.items():
                if v.get('role') not in ('master','leader','primary'):
                    role_lines.append(f'patroni_console_repl_lag_bytes{{node="{nm}"}} {int(v.get("lag") or 0)}')
        except Exception: pass
    lines.append('# HELP patroni_console_repl_lag_bytes Лаг реплики в байтах')
    lines.append('# TYPE patroni_console_repl_lag_bytes gauge')
    lines.extend(role_lines)
    return Response('\n'.join(lines) + '\n', mimetype='text/plain; version=0.0.4; charset=utf-8')

@app.route('/api/metrics/export')
def metrics_export():
    """Экспорт метрик для Zabbix sender (zabbix_sender -i) / JSON."""
    fmt = request.args.get('format', 'json')
    items = []
    if agent_api is not None:
        now = time.time()
        with agent_api._LOCK: agents = dict(agent_api.AGENTS)
        for name, a in agents.items():
            online = 1 if (now - (a.get('last_seen') or 0) < 30) else 0
            m = (a.get('metrics') or {})
            r = m.get('rates') or {}; conn = m.get('conn') or {}; db = m.get('db') or {}
            rows = [
                (name, 'agent.online', online),
                (name, 'tps', r.get('tps')),
                (name, 'cache_hit_ratio', r.get('cache_hit_ratio')),
                (name, 'connections', conn.get('total')),
                (name, 'idle_in_tx', conn.get('idle_in_tx')),
                (name, 'deadlocks', db.get('deadlocks')),
            ]
            d = a.get('delay')
            if d not in (None, '', '?'):
                try: rows.append((name, 'recovery_delay', float(d)))
                except (TypeError, ValueError): pass
            osd = a.get('os') or {}
            if osd.get('loadavg') and len(osd['loadavg']) == 3:
                rows.append((name, 'load1', osd['loadavg'][0]))
                rows.append((name, 'load5', osd['loadavg'][1]))
            if osd.get('cpu_used_pct') is not None:
                rows.append((name, 'cpu_used_pct', osd['cpu_used_pct']))
            mem = osd.get('mem') or {}
            if mem.get('available') is not None and mem.get('total'):
                rows.append((name, 'mem_used_pct', round(100.0 * (mem['total'] - mem['available']) / mem['total'], 2)))
            for nm2, k, v in rows:
                if v is not None:
                    items.append({'key': f'patroni_console[{nm2},{k}]', 'value': v})
    if fmt == 'zabbix':
        # формат zabbix_sender: "<host> <key> <value>" (host = имя ноды)
        txt = '\n'.join(f'{it["key"].split(",")[0].split("[")[0]} {it["key"]} {it["value"]}' for it in items)
        return Response(txt + '\n', mimetype='text/plain; charset=utf-8')
    return jsonify({'ok': True, 'data': items})

# ==================== /PROMETHEUS ====================

for bp in (bp_cluster, bp_repl, bp_perf, bp_schema, bp_ql, bp_dba, bp_ops, bp_metrics, bp_center):
    app.register_blueprint(bp)
app.register_blueprint(auth.bp)
app.register_blueprint(admin_api.bp)
app.register_blueprint(alerts.bp)
app.register_blueprint(reports.bp)
app.register_blueprint(slots.bp)
app.register_blueprint(hosts.bp)
app.register_blueprint(templates.bp)
app.register_blueprint(notify.bp)
app.register_blueprint(dcs.bp)
app.register_blueprint(assistant.bp)
app.register_blueprint(tuning.bp)
if agent_api is not None and hasattr(agent_api, 'bp'):
    app.register_blueprint(agent_api.bp)

import risk, perf, logscan, secaudit, weekly
for _bp in (risk.bp, perf.bp, logscan.bp, secaudit.bp, weekly.bp):
    app.register_blueprint(_bp)

import demo
demo.init_app(app)
if not demo.ENABLED:
    perf.start()       # ASH, история pg_stat_statements, checkpoints, размеры баз
    weekly.start()     # рассылка еженедельного отчёта по расписанию
if demo.ENABLED:
    CLUSTER_NAME = demo.CLUSTER

APP_VERSION = '2026.10.17'
_STARTED = time.time()

def _asset_version():
    """Версия статики = хеш содержимого JS/CSS: браузер кэширует файлы навсегда
    и сам подтягивает новые после обновления консоли."""
    import hashlib
    h = hashlib.sha1(APP_VERSION.encode())
    sdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    for name in ('style.css', 'ui-kit.js', 'app-core.js', 'app-modules.js', 'app-dba.js'):
        try:
            with open(os.path.join(sdir, name), 'rb') as f: h.update(f.read())
        except OSError: pass
    return h.hexdigest()[:10]
ASSET_V = _asset_version()

@app.route('/')
def index():
    v = _asset_version() if app.debug else ASSET_V
    return render_template('index.html', cluster_name=CLUSTER_NAME, asset_v=v)

@app.route('/healthz')
def healthz():
    """Liveness: процесс консоли жив (для Docker/systemd/балансировщика). Без авторизации."""
    return jsonify({'ok': True, 'version': APP_VERSION, 'uptime_s': int(time.time() - _STARTED)})

@app.route('/readyz')
def readyz():
    """Readiness: хотя бы один узел Patroni отвечает по REST API."""
    if demo.ENABLED:
        return jsonify({'ok': True, 'demo': True})
    futs = [EXECUTOR.submit(patroni_request, n, 'GET', '/liveness') for n in NODES]
    up = sum(1 for f in futs if f.result().get('status_code'))
    code = 200 if up else 503
    return jsonify({'ok': bool(up), 'patroni_reachable': up, 'patroni_total': len(NODES)}), code

# Заголовки безопасности. 'unsafe-inline' для скриптов нужен из-за inline-обработчиков
# в шаблоне; внешние источники запрещены — консоль самодостаточна (закрытая сеть).
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'")

@app.after_request
def _security_headers(resp):
    h = resp.headers
    h.setdefault('X-Content-Type-Options', 'nosniff')
    h.setdefault('X-Frame-Options', 'DENY')
    h.setdefault('Referrer-Policy', 'same-origin')
    h.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if resp.mimetype == 'text/html':
        h.setdefault('Content-Security-Policy', _CSP)
    p = request.path
    if p.startswith('/api/') or p == '/':
        h['Cache-Control'] = 'no-store'
    elif p.startswith('/static/'):
        # файлы подключаются с ?v=<версия>: можно кэшировать надолго
        h['Cache-Control'] = 'public, max-age=31536000, immutable' if request.args.get('v') else 'public, max-age=3600'
    if CONFIG.get('web', {}).get('cookie_secure'):
        h.setdefault('Strict-Transport-Security', 'max-age=31536000')
    return resp

if __name__ == '__main__':
    w = CONFIG.get('web', {})
    app.run(host=w.get('host', '0.0.0.0'), port=w.get('port', 5000), debug=bool(w.get('debug', False)))

