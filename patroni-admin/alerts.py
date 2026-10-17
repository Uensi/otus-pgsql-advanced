#!/usr/bin/env python3
"""Система алертов: пороговые правила по метрикам, история срабатываний."""
import os, json, time, threading, operator as opmod
from collections import deque
from flask import Blueprint, jsonify, request

bp = Blueprint('alerts', __name__)
from paths import data_path
RULES_PATH = data_path('alerts_rules.json')
HISTORY_PATH = data_path('alerts_history.jsonl')
OPS = {'>': opmod.gt, '<': opmod.lt, '>=': opmod.ge, '<=': opmod.le, '==': opmod.eq}

DEFAULT_RULES = [
  {'id':'r1','name':'Высокий лаг репликации','metric':'max_lag','operator':'>','threshold':10,'severity':'critical','enabled':True},
  {'id':'r2','name':'Много idle-in-transaction','metric':'idle_in_tx','operator':'>','threshold':10,'severity':'warning','enabled':True},
  {'id':'r3','name':'Низкий cache hit','metric':'cache_hit_ratio','operator':'<','threshold':90,'severity':'warning','enabled':True},
]
# правила рисков добавляются и в существующие файлы правил (по id)
RISK_RULES = [
  {'id':'risk_xid','name':'Wraparound: возраст XID','metric':'xid_age_pct','operator':'>=','threshold':50,'severity':'critical','enabled':True},
  {'id':'risk_backup','name':'Нет свежего бэкапа','metric':'backup_age_h','operator':'>','threshold':26,'severity':'critical','enabled':True},
  {'id':'risk_archive','name':'Архивация WAL падает','metric':'archive_failing','operator':'>=','threshold':1,'severity':'critical','enabled':True},
  {'id':'risk_slots','name':'Слоты удерживают/теряют WAL','metric':'slots_at_risk','operator':'>=','threshold':1,'severity':'warning','enabled':True},
  {'id':'risk_replay','name':'Лаг реплики во времени','metric':'replay_lag_s','operator':'>','threshold':60,'severity':'warning','enabled':True},
]
RULES = []; ACTIVE = {}; HISTORY = deque(maxlen=1000)
_LAG_CACHE = {'ts': 0, 'val': 0}

def _save():
    try:
        with open(RULES_PATH, 'w', encoding='utf-8') as f: json.dump(RULES, f, indent=1)
    except Exception: pass

def _load():
    global RULES
    try:
        with open(RULES_PATH, encoding='utf-8', errors='replace') as f: RULES = json.load(f)
    except Exception:
        RULES = [dict(r) for r in DEFAULT_RULES]; _save()

def _hist(rec):
    HISTORY.append(rec)
    try:
        with open(HISTORY_PATH, 'a', encoding='utf-8') as f: f.write(json.dumps(rec)+'\n')
    except Exception: pass

_load()
_ids = {r.get('id') for r in RULES}
_new = [dict(r) for r in RISK_RULES if r['id'] not in _ids]
if _new:
    RULES.extend(_new); _save()
try:
    with open(HISTORY_PATH, encoding='utf-8', errors='replace') as f:
        for line in f:
            try: HISTORY.append(json.loads(line))
            except Exception: pass
except Exception: pass

def _app():
    import app as A; return A

def current_metrics():
    A = _app(); vals = {}
    now = time.time()
    # max_lag в секундах с кэшем 10 сек
    if now - _LAG_CACHE['ts'] > 10:
        try:
            lsn, _ = A.collect_lsn_lag()
            lags = [v.get('lag', 0) or 0 for v in lsn.values() if v.get('role') not in ('master','leader','primary')]
            _LAG_CACHE['val'] = max(lags) if lags else 0
            _LAG_CACHE['ts'] = now
        except Exception: pass
    vals['max_lag'] = _LAG_CACHE['val']
    idle=0; locks=0; longq=0; dead=0; tps=0; cache=None
    ag = A.agent_api
    if ag is not None:
        with ag._LOCK: agents = dict(ag.AGENTS)
        for name, a in agents.items():
            m = a.get('metrics') or {}
            conn = m.get('conn') or {}
            idle += conn.get('idle_in_tx',0) or 0
            locks += m.get('locks_waiting',0) or 0
            longq += m.get('long_queries',0) or 0
            db = m.get('db') or {}
            dead += db.get('deadlocks',0) or 0
            r = m.get('rates') or {}
            tps += r.get('tps',0) or 0
            ch = r.get('cache_hit_ratio')
            if ch is not None:
                cache = ch if cache is None else min(cache, ch)
    vals.update({'idle_in_tx':idle,'locks_waiting':locks,'long_queries':longq,'deadlocks':dead,'tps':round(tps,1)})
    if cache is not None: vals['cache_hit_ratio'] = cache
    try:
        import risk
        vals.update(risk.metrics())   # xid_age_pct, backup_age_h, archive_failing, slots_at_risk, replay_lag_s
    except Exception: pass
    return vals

def _check_loop():
    while True:
        try:
            vals = current_metrics(); now = time.time()
            for rule in RULES:
                rid = rule.get('id')
                if not rule.get('enabled'):
                    ACTIVE.pop(rid, None); continue
                v = vals.get(rule['metric']); fn = OPS.get(rule['operator'])
                breach = v is not None and fn is not None and fn(v, rule['threshold'])
                if breach and rid not in ACTIVE:
                    ACTIVE[rid] = {'since': now, 'value': v}
                    h = {'ts':now,'time':time.strftime('%Y-%m-%d %H:%M:%S'),'rule':rule['name'],'severity':rule['severity'],'type':'firing','value':v,'threshold':rule['threshold']}
                    _hist(h)
                    _notify_alert(h)
                elif not breach and rid in ACTIVE:
                    h = {'ts':now,'time':time.strftime('%Y-%m-%d %H:%M:%S'),'rule':rule['name'],'severity':rule['severity'],'type':'resolved','value':v,'threshold':rule['threshold']}
                    _hist(h)
                    ACTIVE.pop(rid, None)
                    _notify_alert(h)
        except Exception: pass
        time.sleep(10)

def _notify_alert(h):
    """Отправить уведомление через notify.py (email/telegram) — тихо при ошибках."""
    try:
        import notify
        sev = h.get('severity', 'warning')
        label = '🔴' if sev == 'critical' else ('🟡' if sev == 'warning' else '🔵')
        kind = h.get('type', 'firing')
        title = f"{label} {kind} · {h.get('rule')} = {h.get('value')} (порог {h.get('threshold')})"
        body = f"Правило: {h.get('rule')}\nМетрика: {h.get('value')} / порог {h.get('threshold')}\nВремя: {h.get('time')}"
        notify.notify(sev, title, body)
    except Exception: pass

if not getattr(_check_loop, '_started', False):
    _check_loop._started = True
    threading.Thread(target=_check_loop, daemon=True, name='alerts-check').start()

@bp.route('/api/alerts/rules')
def rules_get(): return jsonify({'ok': True, 'data': RULES})

@bp.route('/api/alerts/rules', methods=['POST'])
def rules_set():
    b = request.json or {}; rid = b.get('id')
    if rid:
        r = next((x for x in RULES if x['id'] == rid), None)
        if r: r.update({k: b[k] for k in ('name','metric','operator','threshold','severity','enabled') if k in b})
    else:
        RULES.append({'id':'r'+str(int(time.time()*1000)),
                      'name': b.get('name','правило'), 'metric': b.get('metric','max_lag'),
                      'operator': b.get('operator','>'), 'threshold': b.get('threshold',0),
                      'severity': b.get('severity','warning'), 'enabled': bool(b.get('enabled',True))})
    _save(); return jsonify({'ok': True, 'data': RULES})

@bp.route('/api/alerts/rules/delete', methods=['POST'])
def rules_del():
    global RULES
    rid = (request.json or {}).get('id')
    RULES = [r for r in RULES if r['id'] != rid]
    _save(); return jsonify({'ok': True, 'data': RULES})

@bp.route('/api/alerts/history')
def hist_get(): return jsonify({'ok': True, 'data': list(HISTORY)[-300:]})

@bp.route('/api/alerts/active')
def active_get():
    out = []
    for rid, a in ACTIVE.items():
        r = next((x for x in RULES if x['id'] == rid), {})
        out.append({'id':rid,'rule':r.get('name'),'severity':r.get('severity'),'since':a['since'],'value':a['value']})
    return jsonify({'ok': True, 'data': out})

@bp.route('/api/alerts/test', methods=['POST'])
def test_alert():
    b = request.json or {}
    _hist({'ts':time.time(),'time':time.strftime('%Y-%m-%d %H:%M:%S'),'rule':b.get('name','тест'),'severity':b.get('severity','info'),'type':'test','value':None,'threshold':None})
    return jsonify({'ok': True})

