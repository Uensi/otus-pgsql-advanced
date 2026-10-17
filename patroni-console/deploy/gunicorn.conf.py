"""Конфиг gunicorn для Patroni Console. HTTPS обслуживает сам gunicorn, без прокси.

Переменные окружения (задаются в /etc/patroni-console/env):
  PATRONI_CONSOLE_BIND   адрес:порт, по умолчанию 0.0.0.0:8443
  PATRONI_CONSOLE_CERT   путь к сертификату (PEM, с цепочкой)
  PATRONI_CONSOLE_KEY    путь к закрытому ключу
Без сертификата консоль не стартует: пароли и сессии не должны ходить открытым текстом.
Для локальной проверки без TLS: PATRONI_CONSOLE_INSECURE_HTTP=1 (и cookie_secure: false в config.yaml).
"""
import os
import ssl

# Ровно один процесс: наблюдатель, отправщик уведомлений и операции живут внутри него.
workers = 1
threads = 8
worker_class = 'gthread'
bind = os.environ.get('PATRONI_CONSOLE_BIND', '0.0.0.0:8443')
timeout = 120
graceful_timeout = 30
keepalive = 5
# прокси нет: заголовкам X-Forwarded-* не доверяем
forwarded_allow_ips = ''
limit_request_line = 8190
limit_request_fields = 50
accesslog = None          # запросы пишет сама консоль в JSON-лог с request_id
errorlog = '-'

if os.environ.get('PATRONI_CONSOLE_INSECURE_HTTP') != '1':
    certfile = os.environ.get('PATRONI_CONSOLE_CERT')
    keyfile = os.environ.get('PATRONI_CONSOLE_KEY')
    if not certfile or not keyfile:
        raise SystemExit('PATRONI_CONSOLE_CERT и PATRONI_CONSOLE_KEY обязательны (HTTPS)')

    def ssl_context(conf, default_ssl_context_factory):
        ctx = default_ssl_context_factory()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx
