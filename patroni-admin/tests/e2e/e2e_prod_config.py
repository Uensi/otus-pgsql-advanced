#!/usr/bin/env python3
"""E2E: «прод-конфиг» не совпадает с кластером — консоль должна работать и объяснять.

Сценарий из реального внедрения: в config.yaml узлы названы иначе, чем члены кластера
в Patroni, а консоль не может подключиться к PostgreSQL (неверный пароль). Раньше это
давало пустые LSN, «Нода не найдена» на restart/reload и «нет подключения к лидеру»
без причины. Запускает две дополнительные консоли на стенде (tests/e2e/stand.sh up).

    PYTHONPATH=~/pctest/pylibs python3 tests/e2e/e2e_prod_config.py"""
import os
import subprocess
import sys
import time

import requests

ST = os.environ.get('PCTEST_DIR', os.path.expanduser('~/pctest'))
SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
RESULTS = []

CFG = """web: {{host: 127.0.0.1, port: {port}, secret: "e2e-prod"}}
postgres: {{user: postgres, password: "{pw}", database: postgres, port: 25432}}
agents: {{key: "e2e-agent-key-2"}}
clusters:
  - name: prod-like
    nodes:
      - {{name: db-a, host: 127.0.0.2, port: 8008}}
      - {{name: db-b, host: 127.0.0.3, port: 8008}}
users:
  - {{name: admin, password: "e2e-admin", role: admin}}
"""


def start_console(port, pw):
    path = os.path.join(ST, f'config.prod{port}.yaml')
    with open(path, 'w') as f:
        f.write(CFG.format(port=port, pw=pw))
    env = dict(os.environ, PATRONI_WEB_CONFIG=path, PATRONI_CONSOLE_DATA=os.path.join(ST, f'console-data-{port}'),
               PATRONI_CONSOLE_NO_SAMPLERS='1', PYTHONUNBUFFERED='1')
    p = subprocess.Popen([PY, '-c', f"import app; app.app.run(host='127.0.0.1', port={port}, threaded=True)"],
                         cwd=SRC, env=env, stdout=open(os.path.join(ST, 'run', f'console{port}.log'), 'w'), stderr=subprocess.STDOUT)
    url = f'http://127.0.0.1:{port}'
    for _ in range(60):
        try:
            if requests.get(url + '/healthz', timeout=2).ok:
                break
        except Exception:
            time.sleep(0.5)
    s = requests.Session()
    assert s.post(url + '/api/auth/login', json={'user': 'admin', 'password': 'e2e-admin'}, timeout=10).json()['ok']
    return p, s, url


def check(name, fn):
    t0 = time.time()
    try:
        d = fn()
        RESULTS.append(('PASS', name, d)); print(f'PASS  {name} ({time.time() - t0:.1f} с) {d}', flush=True)
    except Exception as e:
        RESULTS.append(('FAIL', name, str(e))); print(f'FAIL  {name}: {e}', flush=True)


def main():
    p1, s, url = start_console(15011, 'pctest')
    p2, s2, url2 = start_console(15012, 'wrong-password')
    try:
        def lsn_by_member_name():
            s.get(url + '/api/cluster', timeout=30)
            lsn = s.get(url + '/api/lsn', timeout=30).json()['data']
            assert {'pgnode1', 'pgnode2'} <= set(lsn), lsn.keys()
            rep = next(v for v in lsn.values() if v['role'] == 'replica')
            assert rep['received'] and rep['replayed'], rep
            return f"LSN по именам Patroni: {sorted(lsn)}; реплика получила {rep['received']}"
        check('Имена в config.yaml ≠ Patroni: LSN заполнены', lsn_by_member_name)

        def reload_by_member_name():
            j = s.post(url + '/api/ops/reload', json={'node': 'pgnode2'}, timeout=30).json()
            assert j.get('error') != 'Нода не найдена' and j.get('ok') is not False, j
            j2 = s.get(url + '/api/querylab/show?param=archive_mode&node=pgnode1', timeout=30).json()
            assert j2['ok'] and j2['data']['value'] == 'on', j2
            return 'reload по имени из Patroni выполнен, SHOW на pgnode1 работает'
        check('Действия по имени члена Patroni (раньше «Нода не найдена»)', reload_by_member_name)

        def diag_names():
            d = s.get(url + '/api/diag/connectivity', timeout=60).json()['data']
            assert d['level'] == 'info', d
            assert all(n['rest_ok'] and n['pg_ok'] for n in d['nodes']), d['nodes']
            assert any('назван «db-a», а в Patroni — «pgnode1»' in i['text'] for i in d['issues']), d['issues']
            return '; '.join(i['text'] for i in d['issues'])
        check('Диагностика подсказывает переименовать узлы', diag_names)

        def wrong_password_explained():
            d = s2.get(url2 + '/api/diag/connectivity', timeout=60).json()['data']
            bad = [i['text'] for i in d['issues'] if i['level'] == 'bad']
            assert bad and all('неверный пароль' in t for t in bad), bad
            dcs = s2.get(url2 + '/api/dcs/health', timeout=60).json()['data']['sync']
            assert 'password authentication failed' in dcs['error'] or 'пароль' in dcs['error'], dcs
            return bad[0]
        check('Неверный пароль PostgreSQL объясняется явно', wrong_password_explained)

        def stays_responsive():
            t0 = time.time()
            for path in ('/api/risk/summary', '/api/db/overview', '/api/dba/summary', '/api/repl/slots'):
                s2.get(url2 + path, timeout=60)
            slow = time.time() - t0
            t1 = time.time()
            for _ in range(5):
                assert s2.get(url2 + '/api/cluster', timeout=10).json()['ok']
            fast = (time.time() - t1) / 5
            assert fast < 2, f'/api/cluster отвечает {fast:.1f} с'
            return f'экраны с БД ответили за {slow:.1f} с, /api/cluster в среднем {fast * 1000:.0f} мс'
        check('Консоль не зависает без доступа к PostgreSQL', stays_responsive)

        def check_tool():
            r = subprocess.run([PY, os.path.join(SRC, 'tools', 'check_config.py'), os.path.join(ST, 'config.prod15012.yaml')],
                               capture_output=True, text=True, cwd=SRC, timeout=120)
            assert r.returncode == 1 and 'неверный пароль' in r.stdout and 'назван «db-a»' in r.stdout, r.stdout[-800:]
            return 'tools/check_config.py нашёл пароль и имена (код выхода 1)'
        check('tools/check_config.py на прод-конфиге', check_tool)
    finally:
        for p in (p1, p2):
            p.terminate()
    ok = sum(1 for r in RESULTS if r[0] == 'PASS')
    print(f'\nИТОГО: {ok}/{len(RESULTS)}')
    sys.exit(0 if ok == len(RESULTS) else 1)


if __name__ == '__main__':
    main()
