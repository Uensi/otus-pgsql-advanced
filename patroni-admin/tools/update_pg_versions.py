#!/usr/bin/env python3
"""Обновляет pg_versions.json из официального списка https://www.postgresql.org/versions.json.

Консоль работает в закрытой сети и сама в интернет не ходит. Запускайте этот скрипт
на машине с доступом в интернет (раз в квартал, после очередного релиза PostgreSQL)
и скопируйте pg_versions.json на сервер консоли:
    python3 tools/update_pg_versions.py [путь/к/pg_versions.json]"""
import json
import os
import sys
import urllib.request

URL = 'https://www.postgresql.org/versions.json'
dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'pg_versions.json')
data = json.load(urllib.request.urlopen(URL, timeout=30))
latest, eol, rel = {}, {}, []
for v in data:
    try:
        major = int(float(v['major']))
    except (TypeError, ValueError):
        continue
    if major < 10 or str(v['major']).count('.'):
        continue
    latest[str(major)] = f"{major}.{v['latestMinor']}"
    eol[str(major)] = v.get('eolDate')
    if v.get('supported'):
        rel.append(v.get('relDate') or '')
out = {'_comment': 'Последние минорные версии и сроки поддержки PostgreSQL. Источник: ' + URL +
                   '. Обновление: python3 tools/update_pg_versions.py',
       'updated': max(rel) if rel else None, 'latest': latest, 'eol': eol}
with open(dst, 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"{dst}: данные от {out['updated']}, " + ', '.join(f'{k}={v}' for k, v in sorted(latest.items(), key=lambda kv: int(kv[0]))))
