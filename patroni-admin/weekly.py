#!/usr/bin/env python3
"""Еженедельный отчёт для руководства и команды.

/report/weekly — печатная HTML-страница (в браузере «Печать → Сохранить как PDF»):
состояние кластера, риски, бэкапы и проверки восстановления, сработавшие алерты,
рост баз и прогноз на 30 дней, прогноз заполнения дисков, тяжёлые запросы и
регрессии, оценка безопасности, версии PostgreSQL.
По расписанию краткая текстовая версия уходит по e-mail/Telegram (notify.py)."""
import json
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, redirect, render_template, request, session

import pgx
from paths import data_path

bp = Blueprint('weekly', __name__)
SCHED_PATH = data_path('weekly_report.json')
DEFAULT_SCHED = {'enabled': False, 'weekday': 0, 'time': '09:00', 'last_sent': None}
WEEKDAYS = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье']


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return default if default is not None else {'ok': False, 'error': str(e)}


def build_data(days=7):
    import demo
    if demo.ENABLED:
        return demo.weekly_data(days)
    import alerts, perf, risk, secaudit, hosts
    A = pgx.app()
    now = time.time(); start = now - days * 86400
    members = _safe(lambda: A.cluster_state().get('members', []), [])
    leader = next((m.get('name') for m in members if (m.get('role') or '') in pgx.LEADER_ROLES), None)
    hist = [h for h in list(alerts.HISTORY) if h.get('ts', 0) >= start and h.get('type') == 'firing']
    by_rule = {}
    for h in hist:
        k = (h.get('rule'), h.get('severity'))
        by_rule[k] = by_rule.get(k, 0) + 1
    disks = []
    for name, ag in _safe(hosts._agents, {}).items():
        os_info = ag.get('os') or ag.get('host') or {}
        for d in os_info.get('disks') or []:
            h = hosts.DISK_HISTORY.get(f"{name}:{d.get('path')}")
            fc = hosts._disk_forecast(list(h)) if h else None
            disks.append({'host': name, 'path': d.get('path'), 'used_pct': d.get('used_pct'),
                          'free': d.get('free'), 'days': round(fc['hours'] / 24, 1) if fc and fc.get('hours') is not None else None})
    disks.sort(key=lambda d: (d['days'] is None, d['days'] or 0))
    sec = _safe(secaudit.security_audit, {})
    upg = _safe(secaudit.upgrade_check, {})
    stm = _safe(lambda: perf.statements_report(start, now), {})
    return {
        'generated': datetime.now().strftime('%Y-%m-%d %H:%M'), 'days': days,
        'period': (datetime.fromtimestamp(start).strftime('%d.%m.%Y'), datetime.fromtimestamp(now).strftime('%d.%m.%Y')),
        'cluster': A.CLUSTER_NAME, 'members': members, 'leader': leader,
        'risk': _safe(risk.summary, {}),
        'alerts': [{'rule': k[0], 'severity': k[1], 'count': v} for k, v in sorted(by_rule.items(), key=lambda kv: -kv[1])],
        'alerts_total': len(hist),
        'growth': _safe(lambda: perf.size_growth(days), []),
        'disks': disks[:12],
        'statements': stm if stm.get('ok') else None,
        'security': {'score': sec.get('score'), 'bad': sum(1 for f in sec.get('findings', []) if f['level'] == 'bad'),
                     'warn': sum(1 for f in sec.get('findings', []) if f['level'] == 'warn'),
                     'top': [f['title'] for f in sec.get('findings', []) if f['level'] in ('bad', 'warn')][:5]} if sec.get('ok') else None,
        'versions': [{'node': n['node'], **(n.get('data') or {})} for n in upg.get('nodes', []) if n.get('ok')],
    }


def text_summary(d):
    r = d.get('risk') or {}
    lines = [f"Patroni Console — отчёт за {d['period'][0]}–{d['period'][1]} · кластер {d['cluster']}", '']
    lines.append(f"Лидер: {d.get('leader') or 'нет'}; узлов: {len(d.get('members') or [])}")
    lv = {'ok': 'рисков нет', 'warn': 'есть риски, требующие внимания', 'bad': 'КРИТИЧЕСКИЕ РИСКИ'}.get(r.get('level'), 'нет данных')
    lines.append(f"Риски: {lv}")
    for i in (r.get('items') or [])[:8]:
        lines.append(f"  • {i['text']}")
    b = r.get('backups') or {}
    if b.get('last_backup_age_h') is not None:
        lines.append(f"Последний бэкап: {b['last_backup_age_h']} ч назад; проверок восстановления: {len(b.get('restore_tests') or [])}")
    lines.append(f"Алертов за период: {d.get('alerts_total', 0)}")
    for g in (d.get('growth') or [])[:5]:
        lines.append(f"  рост {g['name']}: {pgx.fmt_bytes(g['delta'])} (≈{pgx.fmt_bytes(g['per_day'])}/сут)")
    for k in (d.get('disks') or [])[:3]:
        if k.get('days') is not None:
            lines.append(f"  диск {k['host']}:{k['path']} заполнится через ~{k['days']} дн.")
    s = d.get('security')
    if s:
        lines.append(f"Безопасность: {s['score']}/100 (критичных {s['bad']}, предупреждений {s['warn']})")
    lines += ['', 'Полный отчёт: экран «Отчёты» → «Еженедельный отчёт».']
    return '\n'.join(lines)


@bp.route('/report/weekly')
def page():
    if not session.get('user'):
        return redirect('/#reports')
    try:
        days = max(1, min(int(request.args.get('days', 7)), 90))
    except (TypeError, ValueError):
        days = 7
    return render_template('weekly.html', d=build_data(days), fmt=pgx.fmt_bytes)


@bp.route('/api/report/weekly/data')
def data():
    return jsonify({'ok': True, 'data': build_data()})


def _sched():
    s = dict(DEFAULT_SCHED)
    try:
        with open(SCHED_PATH, encoding='utf-8') as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def _save(s):
    with open(SCHED_PATH, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def send_now():
    import notify
    d = build_data()
    res = notify.notify_direct(f"Еженедельный отчёт PostgreSQL · {d['cluster']}", text_summary(d), severity='info')
    s = _sched(); s['last_sent'] = time.strftime('%Y-%m-%d %H:%M'); _save(s)
    return res


@bp.route('/api/report/weekly/schedule', methods=['GET', 'POST'])
def schedule():
    if request.method == 'GET':
        return jsonify({'ok': True, 'data': _sched()})
    b = request.json or {}
    s = _sched()
    try:
        s['enabled'] = bool(b.get('enabled', s['enabled']))
        s['weekday'] = int(b.get('weekday', s['weekday'])) % 7
        t = str(b.get('time', s['time']))
        datetime.strptime(t, '%H:%M')
        s['time'] = t
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'время в формате ЧЧ:ММ, день недели 0–6'})
    _save(s)
    return jsonify({'ok': True, 'data': s})


@bp.route('/api/report/weekly/send', methods=['POST'])
def send():
    res = send_now()
    if not res:
        return jsonify({'ok': False, 'error': 'ни один канал уведомлений не включён (экран «Алерты» → «Уведомления»)'})
    return jsonify({'ok': any(r['ok'] for r in res), 'data': res,
                    'error': None if any(r['ok'] for r in res) else '; '.join(r['error'] or '' for r in res)})


def _loop():
    while True:
        try:
            s = _sched()
            now = datetime.now()
            if s.get('enabled') and now.weekday() == s.get('weekday') and now.strftime('%H:%M') == s.get('time') \
                    and (s.get('last_sent') or '')[:10] != now.strftime('%Y-%m-%d'):
                send_now()
        except Exception:
            pass
        time.sleep(30)


_started = [False]


def start():
    if not _started[0]:
        _started[0] = True
        threading.Thread(target=_loop, daemon=True, name='weekly-report').start()
