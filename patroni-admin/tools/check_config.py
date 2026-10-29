#!/usr/bin/env python3
"""Проверка config.yaml на сервере консоли — до запуска или когда «ничего не работает».

    python3 tools/check_config.py [config.yaml]

Для каждого кластера и узла: доступен ли REST API Patroni, совпадает ли имя узла
с именем члена кластера в Patroni, подключается ли консоль к PostgreSQL (с текстом
ошибки: пароль, pg_hba, таймаут), на связи ли агент. Код выхода 1 — есть ошибки."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['PATRONI_WEB_CONFIG'] = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'config.yaml'))
os.environ.setdefault('PATRONI_CONSOLE_NO_SAMPLERS', '1')
os.environ.setdefault('PATRONI_CONSOLE_DATA', os.path.join(ROOT, '.check-data'))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import app as A  # noqa: E402

MARK = {'bad': 'ОШИБКА', 'info': 'совет ', 'ok': 'ок    '}


def main():
    print(f"Конфиг: {os.environ['PATRONI_WEB_CONFIG']}")
    if not (A.CONFIG.get('web') or {}).get('secret') and not os.environ.get('PATRONI_CONSOLE_SECRET'):
        print('ОШИБКА  web.secret не задан: сессии будут сбрасываться при каждом перезапуске консоли')
    bad = 0
    for c in A.CLUSTERS:
        A._switch_cluster(c)
        d = A.connectivity()
        print(f"\n=== Кластер {c['name']} (PostgreSQL: пользователь {d['pg_user']}, порт {d['pg_port']}, база {d['pg_db']})")
        for n in d['nodes']:
            print(f"  {n['config_name']:<22} {n['host']:<16} REST {'ок' if n['rest_ok'] else 'НЕТ':<3}  "
                  f"Patroni: {n.get('member_name') or '—':<20} роль {n.get('role') or '—':<8} "
                  f"PostgreSQL {'ок ' + (n.get('pg_version') or '') if n['pg_ok'] else 'НЕТ'}  "
                  f"агент {'на связи' if n['agent_online'] else '—'}")
        for i in d['issues']:
            print(f"  {MARK.get(i['level'], i['level'])}  {i['text']}")
            print(f"          → {i['fix']}")
            bad += i['level'] == 'bad'
        if not d['issues']:
            print('  ок      все узлы доступны, имена совпадают, PostgreSQL отвечает')
    print(f"\nИтог: {'ошибок нет' if not bad else f'ошибок: {bad}'}")
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
