"""Точка входа WSGI. Запускать ОДНИМ процессом:

    PATRONI_CONSOLE_CONFIG=/etc/patroni-console/config.yaml \
    gunicorn -w 1 --threads 8 -b 127.0.0.1:8080 'patroni_console.wsgi:app'

Несколько воркеров не нужны и опасны для будущих фоновых задач.
"""
import logging
import os
import sys

from .config import load
from .web import JsonFormatter, create_app

_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(JsonFormatter())
logging.basicConfig(level=os.environ.get('PATRONI_CONSOLE_LOG_LEVEL', 'INFO'), handlers=[_h])

app = create_app(load(os.environ.get('PATRONI_CONSOLE_CONFIG', 'config.yaml')))
