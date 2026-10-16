"""Глобальная (динамическая) конфигурация Patroni: проверка изменений, diff, откат.

Изменение задаётся как JSON merge patch, как его понимает PATCH /config Patroni:
вложенные объекты сливаются, значение null удаляет ключ.

Ключи проверяются по белому списку: опечатка в имени ключа в DCS молча
игнорируется Patroni, и DBA думает, что параметр применён. Поэтому
неизвестный ключ — это отказ, а не предупреждение.
"""
import hashlib
import json
import re

# ключ верхнего уровня -> валидатор значения
TOP = {
    'ttl': ('int', 20, None),
    'loop_wait': ('int', 1, None),
    'retry_timeout': ('int', 3, None),
    'maximum_lag_on_failover': ('int', 0, None),
    'maximum_lag_on_syncnode': ('int', -1, None),
    'max_timelines_history': ('int', 0, None),
    'primary_start_timeout': ('int', 0, None),
    'primary_stop_timeout': ('int', 0, None),
    'synchronous_node_count': ('int', 1, None),
    'synchronous_mode': ('enum', (True, False, 'on', 'off', 'quorum')),
    'synchronous_mode_strict': ('bool',),
    'failsafe_mode': ('bool',),
    'check_timeline': ('bool',),
    'member_slots_ttl': ('str',),
    'standby_cluster': ('dict',),
    'slots': ('dict',),
    'ignore_slots': ('list',),
    'postgresql': ('dict',),
}
PG = {
    'use_pg_rewind': ('bool',),
    'use_slots': ('bool',),
    'parameters': ('dict',),
    'pg_hba': ('list',),
    'pg_ident': ('list',),
    'recovery_conf': ('dict',),
}
# управляются самим Patroni (из локального конфига ноды) — менять через /config бессмысленно
PATRONI_OWNED = {'listen_addresses', 'port', 'cluster_name', 'hba_file', 'ident_file', 'config_file',
                 'data_directory', 'hot_standby', 'primary_conninfo', 'primary_slot_name', 'restore_command',
                 'recovery_target', 'recovery_target_timeline', 'synchronous_standby_names'}
# требуют рестарта PostgreSQL (postmaster context)
RESTART = {'shared_buffers', 'max_connections', 'max_wal_senders', 'max_replication_slots', 'max_worker_processes',
           'max_prepared_transactions', 'max_locks_per_transaction', 'max_pred_locks_per_transaction',
           'wal_level', 'wal_log_hints', 'shared_preload_libraries', 'track_commit_timestamp', 'huge_pages',
           'wal_buffers', 'archive_mode', 'autovacuum_max_workers', 'max_files_per_process', 'superuser_reserved_connections',
           'unix_socket_directories', 'max_logical_replication_workers', 'old_snapshot_threshold',
           'logging_collector', 'jit_provider', 'dynamic_shared_memory_type', 'shared_memory_type', 'io_method'}
# на репликах должны быть не меньше, чем на лидере: уменьшать — с пониманием порядка рестартов
HOT_STANDBY_MIN = {'max_connections', 'max_worker_processes', 'max_wal_senders', 'max_prepared_transactions',
                   'max_locks_per_transaction'}
_PARAM_RE = re.compile(r'^[a-z][a-z0-9_.]{0,62}$')


def config_hash(cfg):
    return hashlib.sha256(json.dumps(cfg or {}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _check_value(path, spec, v):
    if v is None:
        return  # удаление ключа
    kind = spec[0]
    if kind == 'int':
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f'{path}: ожидается целое число')
        lo, hi = spec[1], spec[2]
        if lo is not None and v < lo:
            raise ValueError(f'{path}: не меньше {lo}')
    elif kind == 'bool' and not isinstance(v, bool):
        raise ValueError(f'{path}: ожидается true или false')
    elif kind == 'enum' and v not in spec[1]:
        raise ValueError(f'{path}: допустимы {", ".join(map(str, spec[1]))}')
    elif kind == 'dict' and not isinstance(v, dict):
        raise ValueError(f'{path}: ожидается объект')
    elif kind == 'list' and not isinstance(v, list):
        raise ValueError(f'{path}: ожидается список')
    elif kind == 'str' and not isinstance(v, str):
        raise ValueError(f'{path}: ожидается строка')


def validate(changes):
    """Проверяет форму изменения. Бросает ValueError с понятным текстом."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError('changes: нужен непустой объект')
    for k, v in changes.items():
        if k == 'pause':
            raise ValueError('pause меняется операциями «Пауза» / «Снятие паузы»')
        if k not in TOP:
            raise ValueError(f'неизвестный ключ {k!r}: Patroni его проигнорирует; проверьте имя')
        _check_value(k, TOP[k], v)
    pg = changes.get('postgresql')
    if isinstance(pg, dict):
        for k, v in pg.items():
            if k not in PG:
                raise ValueError(f'неизвестный ключ postgresql.{k}')
            _check_value(f'postgresql.{k}', PG[k], v)
        for name, v in (pg.get('parameters') or {}).items():
            if not _PARAM_RE.match(name):
                raise ValueError(f'недопустимое имя параметра {name!r}')
            if isinstance(v, (dict, list)):
                raise ValueError(f'postgresql.parameters.{name}: ожидается скалярное значение')
    return changes


def merge(base, patch):
    """JSON merge patch (RFC 7386), как в Patroni."""
    if not isinstance(patch, dict):
        return patch
    out = dict(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge(out.get(k), v)
    return out


def patch_to(current, target):
    """Минимальный patch, превращающий current в target (для отката)."""
    patch = {}
    for k in set(current) | set(target):
        if k == 'pause':
            continue
        if k not in target:
            patch[k] = None
        elif k not in current:
            patch[k] = target[k]
        elif isinstance(current[k], dict) and isinstance(target[k], dict):
            sub = patch_to(current[k], target[k])
            if sub:
                patch[k] = sub
        elif current[k] != target[k]:
            patch[k] = target[k]
    return patch


def diff(old, new, prefix=''):
    """Плоский список изменений [{path, old, new}]; отсутствие ключа — None."""
    rows = []
    old, new = old or {}, new or {}
    for k in sorted(set(old) | set(new)):
        if k == 'pause':
            continue
        p = f'{prefix}{k}'
        a, b = old.get(k), new.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            rows += diff(a, b, p + '.')
        elif a != b:
            rows.append({'path': p, 'old': a, 'new': b})
    return rows


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def checks(current, changes, base_hash=None):
    """Проверки изменения глобального конфига. current — ответ GET /config."""
    out = []

    def c(level, code, text):
        out.append({'level': level, 'code': code, 'text': text})

    if current is None:
        return out
    if base_hash and base_hash != config_hash(current):
        c('block', 'config_changed', 'конфиг кластера изменился после проверки: обновите предпросмотр')
    new = merge(current, changes)
    rows = diff(current, new)
    if not rows:
        c('block', 'no_changes', 'изменение ничего не меняет')
        return out

    ttl = new.get('ttl', 30)
    lw = new.get('loop_wait', 10)
    rt = new.get('retry_timeout', 10)
    if lw + 2 * rt > ttl:
        c('block', 'timing', f'нужно loop_wait + 2×retry_timeout ≤ ttl, а получается {lw} + 2×{rt} > {ttl}: '
                             'лидер будет терять ключ в DCS')

    cur_params = (current.get('postgresql') or {}).get('parameters') or {}
    new_params = (new.get('postgresql') or {}).get('parameters') or {}
    changed_params = {r['path'].split('.', 2)[2] for r in rows if r['path'].startswith('postgresql.parameters.')}
    for name in sorted(changed_params & PATRONI_OWNED):
        c('block', 'patroni_owned', f'{name} задаёт сам Patroni из конфига ноды; через /config он не применится')
    wl = new_params.get('wal_level')
    if 'wal_level' in changed_params and str(wl) == 'minimal':
        c('block', 'wal_level', 'wal_level=minimal несовместим с репликацией')
    restart = sorted(changed_params & RESTART)
    if restart:
        c('warn', 'needs_restart', f'потребуется рестарт PostgreSQL ({", ".join(restart)}): '
                                   'после применения запустите rolling restart')
    for name in sorted(changed_params & HOT_STANDBY_MIN):
        a, b = _num(cur_params.get(name)), _num(new_params.get(name))
        if a is not None and b is not None and b < a:
            c('warn', 'decrease_' + name, f'{name} уменьшается ({a:g} → {b:g}): на репликах значение не может быть '
                                          'меньше, чем на лидере; рестарт реплик раньше лидера завершится ошибкой')

    if 'synchronous_mode' in changes and new.get('synchronous_mode') not in (None, False, 'off') \
            and current.get('synchronous_mode') in (None, False, 'off'):
        c('warn', 'sync_on', 'синхронная репликация: каждая транзакция будет ждать подтверждения реплики')
    if changes.get('synchronous_mode_strict') is True:
        c('warn', 'sync_strict', 'synchronous_mode_strict: без синхронной реплики запись на лидере остановится')
    if 'maximum_lag_on_failover' in changes and (new.get('maximum_lag_on_failover') or 0) > 64 * 1024 * 1024:
        c('warn', 'big_lag', 'большой maximum_lag_on_failover: при failover можно потерять много данных')
    if 'failsafe_mode' in changes and new.get('failsafe_mode') is False and current.get('failsafe_mode'):
        c('warn', 'failsafe_off', 'без failsafe_mode при недоступности DCS лидер перестанет принимать запись')
    pg_changes = changes.get('postgresql') or {}
    if 'pg_hba' in pg_changes:
        c('warn', 'pg_hba', 'pg_hba меняется для всех нод: ошибка может закрыть доступ, в том числе для репликации')
    if 'standby_cluster' in changes:
        c('force', 'standby_cluster', 'изменение standby_cluster превращает кластер в standby или обратно')
    if 'slots' in changes or 'ignore_slots' in changes:
        c('warn', 'slots', 'постоянные слоты удерживают WAL: следите за местом на диске')
    return out
