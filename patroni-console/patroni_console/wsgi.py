"""Точка входа WSGI. Запускать ОДНИМ процессом:

    gunicorn -c deploy/gunicorn.conf.py patroni_console.wsgi:app

HTTPS обслуживает сам gunicorn (сертификат и ключ — в deploy/gunicorn.conf.py через переменные окружения).

Несколько воркеров запустят несколько наблюдателей и продублируют уведомления.
"""
import logging
import os
import sys

from .config import load
from .web import JsonFormatter, create_app, start_background

_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(JsonFormatter())
logging.basicConfig(level=os.environ.get('PATRONI_CONSOLE_LOG_LEVEL', 'INFO'), handlers=[_h])

app = create_app(load(os.environ.get('PATRONI_CONSOLE_CONFIG', 'config.yaml')))
start_background(app)
