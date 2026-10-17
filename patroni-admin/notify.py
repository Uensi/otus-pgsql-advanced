#!/usr/bin/env python3
"""Уведомления об алертах (разд. 13 спеки): e-mail + Telegram, маршрутизация по severity.

Настройки лежат в notify_config.json (config.yaml не трогаем), формат:
{
  "email":  {"enabled": false, "to": "dba@example.com", "method": "msmtp",
             "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_pass": "", "from": "patroni-console@local"},
  "telegram": {"enabled": false, "bot_token": "", "chat_id": ""},
  "routes": {"critical": ["email", "telegram"], "warning": ["email"], "info": []}
}
"""
import os
import re
import json
import time
from collections import deque
from flask import Blueprint, jsonify, request

bp = Blueprint('notify', __name__)

BASE = os.path.dirname(os.path.abspath(__file__))
from paths import data_path
CFG_PATH = data_path('notify_config.json')
LOG_PATH = data_path('notify_log.jsonl')
DEFAULT = {
    'email': {'enabled': False, 'to': '', 'method': 'msmtp',
              'smtp_host': '', 'smtp_port': 587, 'smtp_user': '', 'smtp_pass': '',
              'from': 'patroni-console@localhost'},
    'telegram': {'enabled': False, 'bot_token': '', 'chat_id': ''},
    'routes': {'critical': ['email', 'telegram'], 'warning': ['email'], 'info': []},
}
CFG = {}
LOG = deque(maxlen=300)
_RE_MAIL = re.compile(r'^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$')


def _load():
    global CFG
    try:
        with open(CFG_PATH, encoding='utf-8', errors='replace') as f: CFG = json.load(f)
    except Exception:
        CFG = json.loads(json.dumps(DEFAULT))
        _save()


def _save():
    try:
        with open(CFG_PATH, 'w', encoding='utf-8') as f: json.dump(CFG, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _log(channel, ok, severity, subject, error=''):
    rec = {'ts': time.time(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'),
           'channel': channel, 'ok': bool(ok), 'severity': severity,
           'subject': (subject or '')[:200], 'error': (error or '')[:300]}
    LOG.append(rec)
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f: f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass


_load()
try:
    with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
        for line in f:
            try: LOG.append(json.loads(line))
            except Exception: pass
except Exception:
    pass


def _find_msmtp():
    """Найти бинарник msmtp: сначала PATH, затем типовые пути.
    systemd-сервисы часто запускают приложение с урезанным PATH,
    поэтому одного shutil.which() недостаточно."""
    import shutil
    p = shutil.which('msmtp')
    if p: return p
    for path in ('/usr/bin/msmtp', '/usr/local/bin/msmtp', '/bin/msmtp',
                 '/usr/sbin/msmtp', '/usr/local/sbin/msmtp'):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _msmtp_diag():
    """Диагностика msmtp для панели уведомлений."""
    msmtp = _find_msmtp()
    conf = None
    if os.path.isfile(os.path.expanduser('~/.msmtprc')): conf = os.path.expanduser('~/.msmtprc')
    elif os.path.isfile('/etc/msmtprc'): conf = '/etc/msmtprc'
    elif os.path.isfile('/etc/msmtp/msmtprc'): conf = '/etc/msmtp/msmtprc'
    return {'installed': bool(msmtp), 'path': msmtp, 'config': conf}


def _send_email(subject, body):
    e = CFG.get('email') or {}
    if not e.get('enabled') or not e.get('to'):
        return None, 'email выключен или не указан получатель'
    to = e['to']
    if not _RE_MAIL.match(to):
        return None, 'некорректный e-mail получателя'
    method = e.get('method', 'msmtp')
    if method == 'msmtp':
        import subprocess
        msmtp = _find_msmtp()
        if not msmtp:
            return None, 'msmtp не установлен (или не найден в PATH процесса)'
        if not _msmtp_diag()['config']:
            return None, 'msmtp установлен, но не настроен: создайте /etc/msmtprc'
        msg = (f"From: {e.get('from', 'patroni-console@localhost')}\nTo: {to}\n"
               f"Subject: [Patroni Console] {subject}\n"
               f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S')}\n"
               f"Content-Type: text/plain; charset=utf-8\n\n{body}\n")
        r = subprocess.run([msmtp, '-t'], input=msg.encode('utf-8', 'replace'),
                           capture_output=True, timeout=90)
        if r.returncode != 0:
            return None, (r.stderr or r.stdout or b'').decode('utf-8', 'replace').strip()[:200]
        return 'ok', None
    # SMTP через smtplib (стандартная библиотека)
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = f'[Patroni Console] {subject}'
    msg['From'] = e.get('from', 'patroni-console@localhost')
    msg['To'] = to
    try:
        s = smtplib.SMTP(e.get('smtp_host') or 'localhost', int(e.get('smtp_port') or 587), timeout=30)
        try:
            s.starttls()
        except Exception:
            pass
        if e.get('smtp_user'):
            s.login(e['smtp_user'], e.get('smtp_pass') or '')
        s.send_message(msg)
        s.quit()
        return 'ok', None
    except Exception as ex:
        return None, str(ex)[:200]


def _send_telegram(subject, body):
    t = CFG.get('telegram') or {}
    if not t.get('enabled') or not t.get('bot_token') or not t.get('chat_id'):
        return None, 'telegram выключен или не настроен'
    import requests as _req
    text = f"[Patroni Console]\n{subject}\n\n{body}"[:4000]
    r = _req.post(f"https://api.telegram.org/bot{t['bot_token']}/sendMessage",
                  json={'chat_id': t['chat_id'], 'text': text}, timeout=30)
    if r.status_code != 200:
        return None, (r.text or '')[:200]
    return 'ok', None


def notify(severity, subject, body=''):
    """Отправить уведомление по маршрутам для severity. Возвращает список результатов."""
    routes = (CFG.get('routes') or {}).get(severity) or []
    results = []
    if 'email' in routes:
        ok, err = _send_email(subject, body)
        _log('email', ok == 'ok', severity, subject, err or '')
        results.append({'channel': 'email', 'ok': ok == 'ok', 'error': err})
    if 'telegram' in routes:
        ok, err = _send_telegram(subject, body)
        _log('telegram', ok == 'ok', severity, subject, err or '')
        results.append({'channel': 'telegram', 'ok': ok == 'ok', 'error': err})
    return results


def notify_direct(subject, body='', severity='info'):
    """Прямое уведомление о СЛУЖЕБНЫХ событиях (бэкап, failover/switchover) —
    шлёт по всем включённым каналам независимо от severity-маршрутов.
    Возвращает список результатов; ошибки не пробрасывает."""
    results = []
    ok, err = _send_email(subject, body)
    if ok == 'ok':
        _log('email', True, severity, subject, '')
        results.append({'channel': 'email', 'ok': True, 'error': None})
    elif err and 'выключен' not in err:
        _log('email', False, severity, subject, err or '')
        results.append({'channel': 'email', 'ok': False, 'error': err})
    ok, err = _send_telegram(subject, body)
    if ok == 'ok':
        _log('telegram', True, severity, subject, '')
        results.append({'channel': 'telegram', 'ok': True, 'error': None})
    elif err and 'выключен' not in err:
        _log('telegram', False, severity, subject, err or '')
        results.append({'channel': 'telegram', 'ok': False, 'error': err})
    return results


@bp.route('/api/notify/config', methods=['GET'])
def cfg_get():
    data = json.loads(json.dumps(CFG))
    data['diag'] = _msmtp_diag()
    return jsonify({'ok': True, 'data': data})


@bp.route('/api/notify/config', methods=['POST'])
def cfg_set():
    b = request.json or {}
    for sec in ('email', 'telegram', 'routes'):
        if sec in b:
            if not isinstance(b[sec], dict):
                return jsonify({'ok': False, 'error': f'{sec} должен быть объектом'})
            CFG[sec] = b[sec]
    _save()
    return jsonify({'ok': True, 'data': CFG})


@bp.route('/api/notify/test', methods=['POST'])
def cfg_test():
    b = request.json or {}
    severity = b.get('severity') or 'warning'
    results = notify(severity, 'Тестовое уведомление', 'Проверка каналов оповещения (severity=' + severity + ')')
    return jsonify({'ok': True, 'data': results})


@bp.route('/api/notify/log')
def log_get():
    return jsonify({'ok': True, 'data': list(LOG)[-200:]})