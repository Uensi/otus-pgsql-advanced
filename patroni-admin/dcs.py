#!/usr/bin/env python3
"""Здоровье DCS (etcd/Consul) и синхронная репликация (разд. 11 спеки).

Адреса DCS не храним в config.yaml (он неприкосновенен) — настройка живёт в
dcs_config.json: {"etcd": ["http://10.0.0.1:2379", ...], "consul": ["http://10.0.0.2:8500", ...]}.
synchronous_standby_names читается с лидера через PG.
"""
import os
import json
import time
import threading
from flask import Blueprint, jsonify, request

bp = Blueprint('dcs', __name__)
BASE = os.path.dirname(os.path.abspath(__file__))
from paths import data_path
CFG_PATH = data_path('dcs_config.json')
CFG = {'etcd': [], 'consul': []}
_CACHE = {'ts': 0, 'data': None}
_CACHE_TTL = 15

def _app():
    import app as A
    return A

def _load():
    global CFG
    try:
        with open(CFG_PATH, encoding='utf-8', errors='replace') as f:
            d = json.load(f)
            CFG = {'etcd': d.get('etcd') or [], 'consul': d.get('consul') or []}
    except Exception:
        CFG = {'etcd': [], 'consul': []}

def _save():
    try:
        with open(CFG_PATH, 'w', encoding='utf-8') as f:
            json.dump(CFG, f, ensure_ascii=False, indent=1)
    except Exception:
        pass

_load()


def _etcd_health(url):
    """etcd: GET /health (здоровье) + POST /v3/cluster/member/list (члены и лидер).
    Пути = реальные endpoints etcd v3.6 (HTTP-совместимые с gRPC-gateway)."""
    import requests as _req
    base = url.rstrip('/')
    try:
        r = _req.get(base + '/health', timeout=5)
        ok = r.status_code == 200 and r.json().get('health') == 'true'
    except Exception as e:
        return {'url': url, 'ok': False, 'error': str(e)[:150]}
    members = []
    try:
        mr = _req.post(base + '/v3/cluster/member/list', json={}, timeout=5)
        if mr.status_code == 200:
            for m in (mr.json().get('members') or []):
                members.append({'id': m.get('ID', ''), 'name': m.get('name', '?'),
                                'peer_urls': m.get('peerURLs', [])})
    except Exception:
        pass
    return {'url': url, 'ok': ok, 'members': members, 'error': None}


def _etcd_kv_get(base_url, key):
    """Прочитать значение ключа etcd v3 (KV API). key БЕЗ слэша в начале."""
    import requests as _req
    import base64
    full = ('/' + key.lstrip('/')) if not key.startswith('/') else key
    bkey = base64.b64encode(full.encode('utf-8')).decode('ascii')
    try:
        r = _req.post(base_url.rstrip('/') + '/v3/kv/range',
                      json={'key': bkey}, timeout=5)
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}'
        j = r.json()
        kvs = j.get('kvs') or []
        if not kvs:
            return None, None
        v = kvs[0].get('value', '')
        raw = base64.b64decode(v).decode('utf-8', 'replace')
        return raw, None
    except Exception as e:
        return None, str(e)[:150]


def _etcd_kv_put(base_url, key, value):
    """Записать значение ключа etcd v3 (KV API). key БЕЗ слэша в начале."""
    import requests as _req
    import base64
    full = ('/' + key.lstrip('/')) if not key.startswith('/') else key
    bkey = base64.b64encode(full.encode('utf-8')).decode('ascii')
    bval = base64.b64encode(value.encode('utf-8')).decode('ascii')
    try:
        r = _req.post(base_url.rstrip('/') + '/v3/kv/put',
                      json={'key': bkey, 'value': bval}, timeout=5)
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}: {r.text[:150]}'
        return 'ok', None
    except Exception as e:
        return None, str(e)[:150]


def _consul_kv_get(base_url, key):
    """Прочитать значение ключа Consul KV (v1/kv/)."""
    import requests as _req
    try:
        r = _req.get(base_url.rstrip('/') + '/v1/kv/' + key.lstrip('/'), timeout=5)
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}'
        j = r.json()
        if not j:
            return None, None
        raw = j[0].get('Value') or ''
        import base64
        return base64.b64decode(raw).decode('utf-8', 'replace'), None
    except Exception as e:
        return None, str(e)[:150]


def _consul_kv_put(base_url, key, value):
    """Записать значение ключа Consul KV (v1/kv/)."""
    import requests as _req
    import base64
    try:
        r = _req.put(base_url.rstrip('/') + '/v1/kv/' + key.lstrip('/'),
                     data=base64.b64encode(value.encode('utf-8')).decode('ascii'), timeout=5)
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}: {r.text[:150]}'
        return 'ok', None
    except Exception as e:
        return None, str(e)[:150]


def _dcs_urls():
    etcd = [u for u in (CFG.get('etcd') or [])]
    consul = [u for u in (CFG.get('consul') or [])]
    return etcd, consul


def dcs_read_config():
    """Прочитать глобальный конфиг Patroni из DCS (ключ /service/<scope>/config).
    Пробует все ноды etcd/consul и все варианты scope."""
    A = _app()
    scope = None
    # scope берём из первого живого /patroni ноды:
    #  - Patroni ≥3.x кладёт в patroni.scope (напр. {"patroni": {"scope": "test_db_cluster"}})
    #  - старые версии могли отдавать scope на верхнем уровне
    for n in (A.NODES or []):
        r = A.patroni_request(n, 'GET', '/patroni')
        if r.get('ok'):
            d = r.get('data') or {}
            scope = (d.get('patroni') or {}).get('scope') or d.get('scope')
            if scope:
                break
    candidates = []
    if scope:
        # Patroni 4.x: /db/<scope>/config; старые: /service/<scope>/config
        candidates.append(f'db/{scope}/config')
        candidates.append(f'service/{scope}/config')
        # вариант с дефисом вместо подчёркивания (некоторые кластеры)
        candidates.append(f'db/{scope.replace("_", "-")}/config')
        candidates.append(f'service/{scope.replace("_", "-")}/config')
    # запасные варианты
    for n in (A.NODES or []):
        nm = n.get('name') or ''
        if nm:
            candidates.append(f'service/{nm}/config')
    if A.CLUSTER_NAME:
        candidates.append(f'service/{A.CLUSTER_NAME}/config')
    # уникальные
    seen = set(); uniq = []
    for c in candidates:
        if c not in seen:
            seen.add(c); uniq.append(c)

    etcd, consul = _dcs_urls()
    errors = []
    for key in uniq:
        for u in etcd:
            raw, err = _etcd_kv_get(u, key)
            if err:
                errors.append(f'{u}: {err}'); continue
            if raw is not None:
                return {'ok': True, 'data': {'key': '/' + key.lstrip('/'), 'config': raw,
                                             'backend': 'etcd', 'url': u, 'candidates': uniq}}
        for u in consul:
            raw, err = _consul_kv_get(u, key)
            if err:
                errors.append(f'{u}: {err}'); continue
            if raw is not None:
                return {'ok': True, 'data': {'key': '/' + key.lstrip('/'), 'config': raw,
                                             'backend': 'consul', 'url': u, 'candidates': uniq}}
    # не нашли ключ — вернём список попыток
    return {'ok': False, 'error': 'ключ config не найден в DCS',
            'tried': uniq, 'errors': errors[:5]}


def dcs_write_config(config_text, key=None):
    """Записать глобальный конфиг Patroni в DCS (полная замена значения ключа).
    key — '/service/<scope>/config'; если не указан, ищем как при чтении."""
    A = _app()
    if key:
        k = key.strip().lstrip('/')
    else:
        # ищем существующий ключ, либо строим из scope
        r = dcs_read_config()
        if r.get('ok') and r['data'].get('key'):
            k = r['data']['key'].lstrip('/')
        else:
            scope = None
            for n in (A.NODES or []):
                rr = A.patroni_request(n, 'GET', '/patroni')
                if rr.get('ok'):
                    d = rr.get('data') or {}
                    scope = (d.get('patroni') or {}).get('scope') or d.get('scope')
                    if scope:
                        break
            k = f"db/{scope or A.CLUSTER_NAME}/config"

    etcd, consul = _dcs_urls()
    errs = []
    for u in etcd:
        res, err = _etcd_kv_put(u, k, config_text)
        if err:
            errs.append(f'etcd {u}: {err}')
        else:
            return {'ok': True, 'data': {'key': '/' + k.lstrip('/'), 'backend': 'etcd',
                                         'url': u, 'written': True}}
    for u in consul:
        res, err = _consul_kv_put(u, k, config_text)
        if err:
            errs.append(f'consul {u}: {err}')
        else:
            return {'ok': True, 'data': {'key': '/' + k.lstrip('/'), 'backend': 'consul',
                                         'url': u, 'written': True}}
    return {'ok': False, 'error': 'не удалось записать в DCS', 'errors': errs[:5]}


def _consul_health(url):
    """Consul: /v1/status/leader + /v1/agent/members + /v1/health/state/any."""
    import requests as _req
    base = url.rstrip('/')
    try:
        lr = _req.get(base + '/v1/status/leader', timeout=5)
        mr = _req.get(base + '/v1/agent/members', timeout=5)
        led = lr.text.strip('"') if lr.status_code == 200 else None
        members = []
        if mr.status_code == 200:
            for m in mr.json():
                members.append({'name': m.get('Name', '?'), 'addr': m.get('Addr', ''),
                                'status': m.get('Status', ''), 'tags': m.get('Tags', {})})
        return {'url': url, 'ok': lr.status_code == 200, 'leader': led,
                'members': members, 'error': None}
    except Exception as e:
        return {'url': url, 'ok': False, 'error': str(e)[:150]}


def sync_standby():
    """synchronous_standby_names и синхронность реплик с лидера."""
    A = _app()
    ln = A.leader_node()
    if not ln:
        return {'ok': False, 'error': 'лидер не найден'}
    c = A.pg_conn(ln['host'])
    if not c:
        why = A.pg_error(ln['host']) or 'причина неизвестна'
        return {'ok': False, 'error': f"консоль не подключилась к PostgreSQL лидера {ln['host']}:{A.PG_PORT} — {why}"}
    try:
        ssn = A.q(c, "SHOW synchronous_standby_names")
        ssn = ssn[0]['synchronous_standby_names'] if ssn else ''
        synced = A.q(c, """SELECT application_name AS app, state, sync_state,
                                  pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_bytes
                           FROM pg_stat_replication ORDER BY sync_priority NULLS LAST""")
        return {'ok': True, 'synchronous_standby_names': ssn, 'replicas': synced}
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}
    finally:
        c.close()


def collect():
    """Полный снимок здоровья DCS (с кэшем, чтобы не долбить etcd/consul каждые 5 сек)."""
    now = time.time()
    if _CACHE['data'] and now - _CACHE['ts'] < _CACHE_TTL:
        return _CACHE['data']
    out = {'ts': now, 'etcd': [], 'consul': [], 'sync': None, 'configured': False}
    for url in CFG.get('etcd') or []:
        out['etcd'].append(_etcd_health(url))
    for url in CFG.get('consul') or []:
        out['consul'].append(_consul_health(url))
    out['configured'] = bool(out['etcd'] or out['consul'])
    out['sync'] = sync_standby()
    _CACHE['data'] = out
    _CACHE['ts'] = now
    return out


@bp.route('/api/dcs/health')
def dcs_health():
    return jsonify({'ok': True, 'data': collect()})


@bp.route('/api/dcs/config', methods=['GET'])
def dcs_cfg_get():
    return jsonify({'ok': True, 'data': CFG})


@bp.route('/api/dcs/config', methods=['POST'])
def dcs_cfg_set():
    b = request.json or {}
    if 'etcd' in b:
        if not isinstance(b['etcd'], list) or any(not str(x).startswith('http') for x in b['etcd']):
            return jsonify({'ok': False, 'error': 'etcd — список URL вида http://host:2379'})
        CFG['etcd'] = [str(x).rstrip('/') for x in b['etcd']]
    if 'consul' in b:
        if not isinstance(b['consul'], list) or any(not str(x).startswith('http') for x in b['consul']):
            return jsonify({'ok': False, 'error': 'consul — список URL вида http://host:8500'})
        CFG['consul'] = [str(x).rstrip('/') for x in b['consul']]
    _save()
    _CACHE['data'] = None
    return jsonify({'ok': True, 'data': CFG})


@bp.route('/api/dcs/discover', methods=['POST'])
def dcs_discover():
    """Авто-обнаружение DCS: опрашивает агенты (dcs_discover), собирает адреса из patroni.yml.
    Результат предлагается сохранить в конфиг (кнопка «применить» на фронте)."""
    A = _app()
    if A.agent_api is None:
        return jsonify({'ok': False, 'error': 'агенты не подключены'})
    seen = {'etcd': [], 'consul': []}
    per_node = {}
    with A.agent_api._LOCK:
        agents = dict(A.agent_api.AGENTS)
    now = time.time()
    for name, ag in agents.items():
        if now - (ag.get('last_seen') or 0) > 30:
            continue  # только онлайн агенты
        cid = A.agent_api.enqueue_agent(name, 'dcs_discover')
        res = None
        try:
            import time as _t
            for _ in range(6):  # ≤3 сек ждём результат
                _t.sleep(0.5)
                with A.agent_api._LOCK:
                    res = (A.agent_api.AGENTS.get(name) or {}).get('results', {}).get(cid)
                if res is not None: break
            if res and res.get('ok'):
                d = (res.get('out') or {}).get('data') or {}
                per_node[name] = d
                for u in (d.get('etcd') or []):
                    if u not in seen['etcd']: seen['etcd'].append(u)
                for u in (d.get('consul') or []):
                    if u not in seen['consul']: seen['consul'].append(u)
        except Exception:
            continue
    return jsonify({'ok': True, 'data': {'found': seen, 'per_node': per_node, 'current': CFG}})


@bp.route('/api/dcs/patroni-config', methods=['GET'])
def dcs_pcfg_get():
    """Прочитать глобальный конфиг Patroni напрямую из DCS (ключ /service/<scope>/config)."""
    r = dcs_read_config()
    return jsonify(r)


@bp.route('/api/dcs/patroni-config', methods=['POST'])
def dcs_pcfg_set():
    """Записать глобальный конфиг Patroni в DCS (полная замена значения ключа).
    Body: {"config": "<YAML/JSON>", "key": "/service/.../config" (необязательно)}"""
    b = request.json or {}
    text = b.get('config')
    if not text:
        return jsonify({'ok': False, 'error': 'поле config обязательно'})
    text = str(text)
    key = (b.get('key') or '').strip()
    # базовая валидация: должен быть YAML/JSON-объект
    try:
        import yaml as _yaml
        obj = _yaml.safe_load(text)
        if not isinstance(obj, dict):
            return jsonify({'ok': False, 'error': 'конфиг должен быть объектом (YAML/JSON)'})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'некорректный YAML/JSON: {e}'})
    r = dcs_write_config(text, key=key or None)
    return jsonify(r)