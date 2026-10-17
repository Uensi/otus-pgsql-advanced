#!/usr/bin/env python3
"""Оптимизация кластера PostgreSQL (Postgres Pro / vanilla): каталог параметров
с пояснениями, системно-зависимые рекомендации (диск SSD/NVMe/RAID, RAM, CPU)
и расчёт «под систему». Чисто рекомендательный слой — ничего не применяет сам.

Источники: наработки по настройке PostgreSQL под 1С + результаты аудита
(см. экран «Оптимизация» в UI). Применение — через «Шаблоны» (Patroni /config).
"""
import re
from flask import Blueprint, jsonify, request

bp = Blueprint('tuning', __name__)

# --- категории (для группировки в UI) ---
CATEGORIES = [
    {'key': 'memory',   'name': 'Память и буферы', 'icon': '🧠'},
    {'key': 'wal',      'name': 'WAL и контрольные точки', 'icon': '💾'},
    {'key': 'autovacuum', 'name': 'Autovacuum и статистика', 'icon': '🧹'},
    {'key': 'planner',  'name': 'Планировщик', 'icon': '🧭'},
    {'key': 'parallel', 'name': 'Параллелизм', 'icon': '⚙️'},
    {'key': 'io',       'name': 'I/O и диск', 'icon': '💽'},
    {'key': 'c1c',      'name': 'Специфично для 1С (Postgres Pro)', 'icon': '1️⃣'},
    {'key': 'logging',  'name': 'Логирование и диагностика', 'icon': '📜'},
    {'key': 'other',    'name': 'Прочее', 'icon': '🧩'},
]

# Системно-зависимые варианты (диск). Ключи: ssd / nvme / hdd(raid)
DISK_TYPES = {
    'ssd':  {'label': 'SSD (SATA/SAS)', 'effective_io_concurrency': '200', 'random_page_cost': '1.1'},
    'nvme': {'label': 'NVMe',           'effective_io_concurrency': '500', 'random_page_cost': '1.0'},
    'hdd':  {'label': 'HDD / RAID (магнитные)', 'effective_io_concurrency': '2', 'random_page_cost': '4.0'},
}

# Каталог параметров. Поля:
#   name, category, description, default
#   recommend: строка | {'formula': '<expr>'} | {'disk': {...}} — рекомендация
#   note: доп. пояснение / источник
#   restart: требует ли перезапуск (true) или достаточно reload (false)
PARAMS = [
    # ---------------- Память ----------------
    {
        'name': 'shared_buffers', 'category': 'memory', 'default': '128MB',
        'description': 'Разделяемый кэш страниц PostgreSQL. Главный буфер данных.',
        'recommend': {'formula': 'shared_buffers'},
        'note': 'Обычно ≈25% ОЗУ, но не более 8–40GB для смешанной нагрузки. '
                'На выделенном сервере 1С с 512GB+ RAM ставят и 130GB (см. аудит).',
        'restart': True,
    },
    {
        'name': 'effective_cache_size', 'category': 'memory', 'default': '4GB',
        'description': 'Оценка ОС-кэша + shared_buffers для планировщика (не выделяет память).',
        'recommend': {'formula': 'effective_cache_size'},
        'note': '≈75% ОЗУ. Завышать вредно — планировщик начнёт выбирать дорогие планы.',
        'restart': True,
    },
    {
        'name': 'work_mem', 'category': 'memory', 'default': '4MB',
        'description': 'Память на сортировку/hash на одну операцию (не на сессию целиком).',
        'recommend': {'formula': 'work_mem'},
        'note': 'Для OLTP 32–64MB, для OLAP 256MB+. Избыток при большом числе сортировок '
                'быстро съедает RAM (work_mem × соединений × операций).',
        'restart': False,
    },
    {
        'name': 'maintenance_work_mem', 'category': 'memory', 'default': '64MB',
        'description': 'Память на обслуживание: VACUUM, CREATE INDEX, REINDEX, pg_repack.',
        'recommend': {'formula': 'maintenance_work_mem'},
        'note': 'По аудиту 1С — 4GB: ускоряет пересборку индексов и вакуум.',
        'restart': False,
    },
    {
        'name': 'temp_buffers', 'category': 'memory', 'default': '8MB',
        'description': 'Кэш для временных таблиц в рамках сессии.',
        'recommend': '64MB',
        'note': 'Для 1С (масса temp-таблиц) полезно поднять до 64–256MB.',
        'restart': False,
    },
    {
        'name': 'wal_buffers', 'category': 'wal', 'default': '-1',
        'description': 'Буфер WAL перед записью на диск.',
        'recommend': {'formula': 'wal_buffers'},
        'note': 'Обычно auto (-1, ≈1/32 shared_buffers), потолок 64MB.',
        'restart': True,
    },
    {
        'name': 'huge_pages', 'category': 'memory', 'default': 'try',
        'description': 'Использование Huge Pages (больших страниц памяти).',
        'recommend': 'try',
        'note': 'Снижает накладные расходы TLB на больших объёмах shared_buffers. '
                '«try» безопасен: если huge-страниц нет — молча откатится на обычные.',
        'restart': True,
    },

    # ---------------- WAL и контрольные точки ----------------
    {
        'name': 'max_wal_size', 'category': 'wal', 'default': '1GB',
        'description': 'Целевой потолок WAL между контрольными точками.',
        'recommend': {'formula': 'max_wal_size'},
        'note': 'По аудиту 1С — 8GB: реже чекпоинты, меньше пиковой записи.',
        'restart': False,
    },
    {
        'name': 'checkpoint_timeout', 'category': 'wal', 'default': '5min',
        'description': 'Максимальный интервал между контрольными точками.',
        'recommend': '10min',
        'note': 'Увеличение снижает частоту чекпоинтов, но растёт время восстановления.',
        'restart': False,
    },
    {
        'name': 'checkpoint_completion_target', 'category': 'wal', 'default': '0.5',
        'description': 'Доля времени на «размазывание» чекпоинта (сглаживание записи).',
        'recommend': '0.9',
        'note': '0.9 сглаживает пики записи WAL, важна для стабильного latency.',
        'restart': False,
    },

    # ---------------- Autovacuum и статистика ----------------
    {
        'name': 'autovacuum_max_workers', 'category': 'autovacuum', 'default': '3',
        'description': 'Число параллельных воркеров autovacuum.',
        'recommend': {'formula': 'autovacuum_max_workers'},
        'note': 'По аудиту 1С — 16. Больше воркеров — быстрее очистка, но выше нагрузка.',
        'restart': True,
    },
    {
        'name': 'autovacuum_naptime', 'category': 'autovacuum', 'default': '1min',
        'description': 'Пауза между проверками таблиц на необходимость vacuum.',
        'recommend': '10s',
        'note': '10s (аудит 1С) — вакуум успевает за активной записью.',
        'restart': False,
    },
    {
        'name': 'autovacuum_vacuum_scale_factor', 'category': 'autovacuum', 'default': '0.2',
        'description': 'Доля изменённых строк, после которой срабатывает VACUUM.',
        'recommend': '0.05',
        'note': 'Ниже значение — раньше вакуум, меньше bloat.',
        'restart': False,
    },
    {
        'name': 'autovacuum_analyze_scale_factor', 'category': 'autovacuum', 'default': '0.1',
        'description': 'Доля изменённых строк, после которой срабатывает ANALYZE.',
        'recommend': '0.02',
        'note': 'Чаще обновляется статистика — точнее планы.',
        'restart': False,
    },
    {
        'name': 'autovacuum_vacuum_insert_scale_factor', 'category': 'autovacuum', 'default': '0.2',
        'description': 'Доля вставленных строк (insert-only таблицы), после которой срабатывает VACUUM.',
        'recommend': '0.01',
        'note': 'По аудиту 1С — 0.01: критично для таблиц, где только INSERT.',
        'restart': False,
    },
    {
        'name': 'autovacuum_vacuum_insert_threshold', 'category': 'autovacuum', 'default': '1000',
        'description': 'Минимальное число вставленных строк для VACUUM (вместе со scale_factor).',
        'recommend': '50',
        'note': 'По аудиту 1С — 50.',
        'restart': False,
    },
    {
        'name': 'default_statistics_target', 'category': 'autovacuum', 'default': '100',
        'description': 'Глубина статистики по умолчанию (сколько MCV/гистограммных корзин).',
        'recommend': '100',
        'note': 'По аудиту 1С — 100. Для аналитики поднимают до 300–500 (на конкретные колонки).',
        'restart': False,
    },

    # ---------------- Планировщик ----------------
    {
        'name': 'from_collapse_limit', 'category': 'planner', 'default': '8',
        'description': 'Глубина свёртки подзапросов в FROM (объединение в один запрос).',
        'recommend': '20',
        'note': 'По аудиту 1С — 20: 1С генерирует глубоко вложенные подзапросы.',
        'restart': False,
    },
    {
        'name': 'geqo', 'category': 'planner', 'default': 'on',
        'description': 'Генетический оптимизатор запросов для сложных JOIN.',
        'recommend': 'on',
        'note': 'Сокращает время планирования на запросах с большим числом отношений.',
        'restart': False,
    },
    {
        'name': 'geqo_threshold', 'category': 'planner', 'default': '12',
        'description': 'С какого числа отношений планировщик переключается на GEQO.',
        'recommend': '12',
        'note': '12 — разумный баланс качества плана и скорости планирования.',
        'restart': False,
    },
    {
        'name': 'enable_sorted_merge_join', 'category': 'planner', 'default': 'on',
        'description': 'Разрешить планировщику merge join (соединение слиянием).',
        'recommend': 'off',
        'note': 'Для 1С отключают: 1С не использует merge join, а планировщик '
                'зря тратит время на их перебор.',
        'restart': False,
    },
    {
        'name': 'random_page_cost', 'category': 'io', 'default': '4.0',
        'description': 'Относительная стоимость случайного чтения страницы.',
        'recommend': {'disk': {'ssd': '1.1', 'nvme': '1.0', 'hdd': '4.0'}},
        'note': 'Для SSD/NVMe снижать до 1.0–1.1 — иначе планировщик избегает индексов.',
        'restart': False,
    },
    {
        'name': 'effective_io_concurrency', 'category': 'io', 'default': '1',
        'description': 'Число одновременных I/O, которое сессия пытается выполнять '
                       '(влияет на bitmap heap scan).',
        'recommend': {'disk': {'ssd': '200', 'nvme': '500', 'hdd': '2'}},
        'note': '200 для SSD; 500 для NVMe; 2 для RAID HDD. Слишком большое значение '
                'перегружает дисковый массив и CPU.',
        'restart': False,
    },

    # ---------------- Параллелизм ----------------
    {
        'name': 'max_worker_processes', 'category': 'parallel', 'default': '8',
        'description': 'Общее число фоновых воркеров (параллелизм + логическая репликация и т.п.).',
        'recommend': {'formula': 'max_worker_processes'},
        'note': 'Обычно ≥ cpu × 1.5, не меньше max_parallel_workers.',
        'restart': True,
    },
    {
        'name': 'max_parallel_workers', 'category': 'parallel', 'default': '8',
        'description': 'Максимум параллельных воркеров по всему серверу.',
        'recommend': {'formula': 'max_parallel_workers'},
        'note': 'По аудиту 1С — 12. Ориентир: 2×число_ядер, но умеренно для OLTP.',
        'restart': False,
    },
    {
        'name': 'max_parallel_workers_per_gather', 'category': 'parallel', 'default': '2',
        'description': 'Параллельные воркеры на один узел Gather (на запрос).',
        'recommend': {'formula': 'max_parallel_workers_per_gather'},
        'note': 'По аудиту 1С — 2. Для OLAP поднимают до половины ядер.',
        'restart': False,
    },
    {
        'name': 'jit', 'category': 'parallel', 'default': 'on',
        'description': 'JIT-компиляция выражений (LLVM).',
        'recommend': 'off',
        'note': 'Для 1С отключают: накладные расходы JIT выше выгоды на коротких '
                'OLTP-запросах. Полезен только для длительных аналитических.',
        'restart': False,
    },

    # ---------------- 1С (Postgres Pro) ----------------
    {
        'name': 'online_analyze.verbose', 'category': 'c1c', 'default': 'off',
        'description': 'Подробность логирования online analyze (для партиций).',
        'recommend': 'off',
        'note': 'off — без лишнего шума в логах.',
        'restart': False,
    },
    {
        'name': 'online_analyze.min_interval', 'category': 'c1c', 'default': '10000',
        'description': 'Минимальный интервал (мс) между online analyze одной партиции.',
        'recommend': '10000',
        'note': '10000 мс (10 с) — баланс свежести статистики и нагрузки.',
        'restart': False,
    },
    {
        'name': 'online_analyze.table_type', 'category': 'c1c', 'default': 'temporary',
        'description': 'Для каких таблиц online analyze обновляет статистику сразу '
                       'после INSERT/UPDATE/DELETE/SELECT INTO.',
        'recommend': "'temporary'",
        'note': ("'temporary' — мгновенно обновляет статистику временных таблиц, что "
                 "кратно ускоряет 1С (почти все запросы 1С используют temp-таблицы)."),
        'restart': False,
    },
    {
        'name': 'plantuner.fix_empty_table', 'category': 'c1c', 'default': 'off',
        'description': 'Снимает излишнюю пессимистичность планировщика для только что '
                       'созданных (пустых) таблиц.',
        'recommend': 'on',
        'note': 'on — планировщик не ждёт авто-анализа пустой таблицы, что ускоряет '
                'первый запрос после её создания.',
        'restart': False,
    },

    # ---------------- Логирование ----------------
    {
        'name': 'log_temp_files', 'category': 'logging', 'default': '-1',
        'description': 'Порог (в kB) логирования временных файлов (сортировка на диск).',
        'recommend': '256MB',
        'note': 'По аудиту 1С — 256MB: ловит запросы, уходящие в сортировку/хэш на диск.',
        'restart': False,
    },
    {
        'name': 'track_activity_query_size', 'category': 'logging', 'default': '1024',
        'description': 'Сколько байт текста запроса хранится в pg_stat_activity.',
        'recommend': '1MB',
        'note': 'По аудиту 1С — 1MB: полный текст больших запросов в «Текущих».',
        'restart': True,
    },
    {
        'name': 'log_min_duration_statement', 'category': 'logging', 'default': '-1',
        'description': 'Логировать запросы, выполняющиеся дольше N (мс). -1 — выключено.',
        'recommend': '1000',
        'note': '1000 мс — логировать всё дольше 1 с для разбора медленных запросов.',
        'restart': False,
    },

    # ---------------- Прочее ----------------
    {
        'name': 'ssl', 'category': 'other', 'default': 'off',
        'description': 'TLS для подключений к серверу.',
        'recommend': 'off',
        'note': 'В изолированном контуре 1С не используется; внутри сети — off.',
        'restart': True,
    },
]

# Формулы рекомендаций, зависящие от RAM/CPU/соединений.
def _apply_formula(name, ram_gb, cpu, maxconn):
    r = max(1, float(ram_gb or 0))
    c = max(1, int(cpu or 0))
    mc = max(10, int(maxconn or 300))
    if name == 'shared_buffers':
        return f'{max(1, round(r * 0.25))}GB'
    if name == 'effective_cache_size':
        return f'{max(1, round(r * 0.75))}GB'
    if name == 'work_mem':
        mb = max(4, round(r * 1024 / (mc * 3)))
        return f'{mb}MB'
    if name == 'maintenance_work_mem':
        return f'{max(1, min(8, round(r * 0.05)))}GB'
    if name == 'wal_buffers':
        return f'{max(4, min(64, round(r * 1024 * 0.25 / 32)))}MB'
    if name == 'max_wal_size':
        return f'{max(1, min(32, round(r * 0.02)))}GB'
    if name == 'autovacuum_max_workers':
        return f'{max(3, min(16, round(c * 0.5)))}'
    if name == 'max_worker_processes':
        return f'{max(8, round(c * 1.5))}'
    if name == 'max_parallel_workers':
        return f'{max(4, min(32, round(c * 1.0)))}'
    if name == 'max_parallel_workers_per_gather':
        return f'{max(2, min(8, round(c * 0.25)))}'
    return None


def recommend_value(param, ram_gb, cpu, disk_type, maxconn):
    """Возвращает рекомендуемое значение параметра под конкретную систему."""
    rec = param.get('recommend')
    if isinstance(rec, str):
        return rec
    if isinstance(rec, dict):
        if 'formula' in rec:
            v = _apply_formula(rec['formula'], ram_gb, cpu, maxconn)
            return v if v is not None else param.get('default', '—')
        if 'disk' in rec:
            return rec['disk'].get(disk_type, param.get('default', '—'))
    return param.get('default', '—')


def build_catalog(ram_gb=None, cpu=None, disk_type='ssd', maxconn=None):
    """Полный каталог + рассчитанные рекомендации под выбранную систему."""
    out = []
    for p in PARAMS:
        out.append({
            'name': p['name'],
            'category': p['category'],
            'description': p['description'],
            'default': p['default'],
            'recommended': recommend_value(p, ram_gb, cpu, disk_type, maxconn),
            'note': p.get('note', ''),
            'restart': bool(p.get('restart', False)),
            'disk_sensitive': isinstance(p.get('recommend'), dict) and 'disk' in p.get('recommend', {}),
        })
    return out


@bp.route('/api/tuning/catalog')
def tuning_catalog():
    """Каталог параметров + рекомендации. Query-параметры: ram_gb, cpu, disk, maxconn."""
    def _num(name, default):
        try:
            return float(request.args.get(name, default))
        except (TypeError, ValueError):
            return default
    ram = _num('ram_gb', 32)
    cpu = int(_num('cpu', 8))
    disk = request.args.get('disk', 'ssd')
    if disk not in DISK_TYPES:
        disk = 'ssd'
    maxconn = int(_num('maxconn', 300))
    return jsonify({'ok': True, 'data': {
        'categories': CATEGORIES,
        'disk_types': DISK_TYPES,
        'params': build_catalog(ram, cpu, disk, maxconn),
        'input': {'ram_gb': ram, 'cpu': cpu, 'disk': disk, 'maxconn': maxconn},
    }})


@bp.route('/api/tuning/recommend', methods=['POST'])
def tuning_recommend():
    """Серверный расчёт рекомендаций под систему (ram_gb, cpu, disk, maxconn)."""
    b = request.json or {}

    def _num(key, default):
        try:
            return float(b.get(key, default))
        except (TypeError, ValueError):
            return default
    ram = _num('ram_gb', 32)
    cpu = int(_num('cpu', 8))
    disk = b.get('disk', 'ssd')
    if disk not in DISK_TYPES:
        disk = 'ssd'
    maxconn = int(_num('maxconn', 300))
    recs = {}
    for p in PARAMS:
        recs[p['name']] = recommend_value(p, ram, cpu, disk, maxconn)
    return jsonify({'ok': True, 'data': {'input': {'ram_gb': ram, 'cpu': cpu, 'disk': disk, 'maxconn': maxconn},
                                         'recommendations': recs}})
