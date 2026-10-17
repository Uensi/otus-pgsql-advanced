#!/usr/bin/env python3
"""Шаблоны конфигураций PostgreSQL + история изменений (разд. 10 спеки).

Применение идёт через Patroni REST PATCH /config (параметры postgresql.parameters),
история пишется в config_history.jsonl с ротацией.
"""
import os
import time
import json
from flask import Blueprint, jsonify, request

bp = Blueprint('templates', __name__)

BASE = os.path.dirname(os.path.abspath(__file__))
from paths import data_path
HISTORY_PATH = data_path('config_history.jsonl')
HISTORY_MAX_BYTES = 10 * 1024 * 1024
HISTORY = []

# --- пресеты: параметры postgresql.parameters (Patroni рестартует, если нужно) ---
TEMPLATES = {
    '1c-high': {
        'name': '1С — высокая производительность',
        'description': 'Для типовых нагрузок 1С: Бухгалтерия/Управление. Умеренный work_mem, '
                       'проверенные на практике настройки, без риска «просадки» при скачках.',
        'params': {
            'shared_buffers': '1GB',
            'effective_cache_size': '4GB',
            'work_mem': '32MB',
            'maintenance_work_mem': '256MB',
            'max_connections': '400',
            'checkpoint_completion_target': '0.9',
            'wal_buffers': '16MB',
            'random_page_cost': '1.1',
            'effective_io_concurrency': '2',
            'max_worker_processes': '8',
            'max_parallel_workers_per_gather': '2',
            'autovacuum_max_workers': '4',
            'autovacuum_naptime': '20s',
            'autovacuum_vacuum_scale_factor': '0.05',
            'autovacuum_analyze_scale_factor': '0.02',
        },
    },
    '1c-postgrespro': {
        'name': '1С — патч Postgres Pro (online_analyze / plantuner)',
        'description': 'Специфичные для Postgres Pro параметры патча online_analyze и '
                       'plantuner + отключение merge join, JIT и GEQO-порог. Кратно ускоряют '
                       'работу 1С за счёт мгновенной статистики временных таблиц.',
        'params': {
            'online_analyze.verbose': 'off',
            'online_analyze.min_interval': '10000',
            'online_analyze.table_type': 'temporary',
            'plantuner.fix_empty_table': 'on',
            'ssl': 'off',
            'huge_pages': 'try',
            'enable_sorted_merge_join': 'off',
            'geqo': 'on',
            'geqo_threshold': '12',
            'jit': 'off',
            'effective_io_concurrency': '200',
        },
    },
    '1c-audit': {
        'name': '1С — после аудита (сервер ≥512GB RAM)',
        'description': 'Итоговые настройки после аудита типового сервера 1С. Внимание: '
                       'shared_buffers=130GB рассчитан на ≥512GB ОЗУ — на меньшем сервере '
                       'подберите значение через «Оптимизация».',
        'params': {
            'shared_buffers': '130GB',
            'from_collapse_limit': '20',
            'track_activity_query_size': '1MB',
            'max_wal_size': '8GB',
            'autovacuum_max_workers': '16',
            'autovacuum_naptime': '10s',
            'autovacuum_vacuum_insert_scale_factor': '0.01',
            'autovacuum_vacuum_insert_threshold': '50',
            'default_statistics_target': '100',
            'log_temp_files': '256MB',
            'huge_pages': 'try',
            'max_parallel_workers_per_gather': '2',
            'max_parallel_workers': '12',
            'maintenance_work_mem': '4GB',
        },
    },
    'oltp': {
        'name': 'OLTP (много мелких транзакций)',
        'description': 'Баланс записи/чтения: выше checkpoint частота, умеренные буферы, '
                       'больше работников фоновой записи.',
        'params': {
            'shared_buffers': '2GB',
            'effective_cache_size': '8GB',
            'work_mem': '64MB',
            'maintenance_work_mem': '512MB',
            'max_connections': '300',
            'checkpoint_timeout': '5min',
            'checkpoint_completion_target': '0.9',
            'max_wal_size': '4GB',
            'min_wal_size': '2GB',
            'wal_buffers': '32MB',
            'random_page_cost': '1.1',
            'effective_io_concurrency': '4',
            'max_parallel_workers_per_gather': '2',
        },
    },
    'olap': {
        'name': 'OLAP / аналитика',
        'description': 'Акцент на чтение больших объёмов: крупные буферы, широкий параллелизм, '
                       'низкий random_page_cost для SSD.',
        'params': {
            'shared_buffers': '8GB',
            'effective_cache_size': '24GB',
            'work_mem': '256MB',
            'maintenance_work_mem': '2GB',
            'max_connections': '200',
            'checkpoint_completion_target': '0.9',
            'random_page_cost': '1.0',
            'effective_io_concurrency': '200',
            'max_parallel_workers_per_gather': '8',
            'max_parallel_workers': '16',
            'parallel_tuple_cost': '0.05',
            'default_statistics_target': '500',
        },
    },
    'safety': {
        'name': 'Надёжность / консервативный',
        'description': 'Консервативные настройки: синхронный коммит, частые контрольные точки, '
                       'минимум риска потери данных.',
        'params': {
            'synchronous_commit': 'on',
            'checkpoint_timeout': '3min',
            'max_wal_size': '6GB',
            'min_wal_size': '2GB',
            'wal_keep_size': '512MB',
            'full_page_writes': 'on',
            'max_connections': '200',
            'work_mem': '32MB',
        },
    },
}

def _app():
    import app as A
    return A


def _load_history():
    try:
        with open(HISTORY_PATH, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    HISTORY.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass


def _rotate_history():
    try:
        if os.path.exists(HISTORY_PATH) and os.path.getsize(HISTORY_PATH) > HISTORY_MAX_BYTES:
            tail = HISTORY[-max(1, len(HISTORY) // 2):]
            tmp = HISTORY_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                for rec in tail:
                    f.write(json.dumps(rec) + '\n')
            os.replace(tmp, HISTORY_PATH)
    except Exception:
        pass


def _append_history(rec):
    HISTORY.append(rec)
    try:
        with open(HISTORY_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
        _rotate_history()
    except Exception:
        pass


_load_history()


@bp.route('/api/templates')
def templates_list():
    """Список пресетов + текущие ключи из Patroni /config (для сравнения «до/после»)."""
    A = _app()
    cur = None
    try:
        for n in A.NODES:
            r = A.patroni_request(n, 'GET', '/config')
            if r['ok']:
                cur = r['data']
                break
    except Exception:
        pass
    out = []
    for tkey, t in TEMPLATES.items():
        out.append({'key': tkey, 'name': t['name'], 'description': t['description'], 'params': t['params']})
    return jsonify({'ok': True, 'data': {'templates': out, 'current': cur}})


@bp.route('/api/templates/apply', methods=['POST'])
def templates_apply():
    """Применить пресет: слить params в postgresql.parameters и PATCH в /config лидера."""
    A = _app()
    b = request.json or {}
    tkey = b.get('name') or b.get('key')
    t = TEMPLATES.get(tkey)
    if not t:
        return jsonify({'ok': False, 'error': 'неизвестный шаблон'})
    if not b.get('confirm'):
        return jsonify({'ok': False, 'error': 'подтвердите применение (confirm: true)'})
    ln = A.leader_node()
    if not ln:
        return jsonify({'ok': False, 'error': 'лидер не найден'})
    cd = {'postgresql': {'parameters': dict(t['params'])}}
    r = A.patroni_request(ln, 'PATCH', '/config', json=cd)
    if not r['ok']:
        return jsonify({'ok': False, 'error': r.get('error') or 'ошибка Patroni'})
    rec = {'ts': time.time(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
           'template': tkey, 'node': ln['name'], 'params': dict(t['params'])}
    try:
        from flask import session as _s
        rec['user'] = _s.get('user') or '?'
    except Exception:
        rec['user'] = '?'
    _append_history(rec)
    return jsonify({'ok': True, 'data': {'node': ln['name'], 'params': list(t['params'].keys())}})


@bp.route('/api/templates/history')
def templates_history():
    return jsonify({'ok': True, 'data': list(reversed(HISTORY[-100:]))})