"""Проверка перед запуском: конфиг читается, секреты на месте, ноды Patroni и etcd отвечают.

    patroni-console-check /etc/patroni-console/config.yaml

Только чтение: ничего не меняет ни в кластерах, ни в базе консоли. Код выхода 0 — всё в порядке,
1 — есть проблемы (они перечислены), 2 — конфиг не читается.
"""
import os
import sys

from . import status as status_mod
from .config import ConfigError, load
from .etcd import EtcdClient
from .patroni import PatroniClient, PatroniError


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else os.environ.get('PATRONI_CONSOLE_CONFIG', 'config.yaml')
    try:
        cfg = load(path)
    except (OSError, ConfigError) as e:
        print(f'конфиг {path}: {e}')
        return 2
    problems = 0
    print(f'конфиг {path}: прочитан; кластеров {len(cfg.clusters)}, пользователей {len(cfg.users)}, '
          f'каналов уведомлений {len(cfg.channels)}')
    db_dir = os.path.dirname(os.path.abspath(cfg.db_path))
    if not os.access(db_dir, os.W_OK):
        print(f'  база {cfg.db_path}: нет прав на запись в {db_dir}')
        problems += 1
    for c in cfg.clusters.values():
        print(f'\nкластер {c.name}')
        client = PatroniClient(c, timeout=cfg.timeout)
        for n in c.nodes:
            try:
                d = client.node_status(n)
                print(f"  {n.name:<20} {n.url:<32} ok  {d.get('role', '?'):<8} {d.get('state', '?'):<10} "
                      f"Patroni {(d.get('patroni') or {}).get('version', '?')}, scope {(d.get('patroni') or {}).get('scope', '?')}")
            except PatroniError as e:
                print(f'  {n.name:<20} {n.url:<32} ОШИБКА {e}')
                problems += 1
        snap = status_mod.collect(c, client)
        for w in snap['warnings']:
            if w['level'] != 'info':
                print(f"  ! {w.get('node') or ''} {w['text']}")
        versions = {m.get('patroni_version') for m in snap['members'] if m.get('patroni_version')}
        if len(versions) > 1:
            print('  ! на нодах разные версии Patroni: ' + ', '.join(sorted(versions)))
        try:
            client.dynamic_config(next(n for n in c.nodes if n.name == (snap['leader'] or c.nodes[0].name)))
            print('  /config читается')
        except PatroniError as e:
            print(f'  /config: {e}')
            problems += 1
        if c.etcd.endpoints:
            h = EtcdClient(c.etcd, timeout=cfg.timeout).health()
            for ep in h['endpoints']:
                state = 'ok' if not ep['error'] else f"ОШИБКА {ep['error']}"
                print(f"  etcd {ep['endpoint']:<36} {state} {ep.get('version') or ''}")
                problems += bool(ep['error'])
            print(f"  etcd: кворум {'есть' if h['quorum'] else 'НЕТ'}, членов {len(h['members'])}")
            problems += not h['quorum']
        else:
            print('  etcd: адреса не заданы, диагностика etcd будет недоступна')
    print('\nитог: ' + ('всё в порядке' if not problems else f'проблем: {problems}'))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
