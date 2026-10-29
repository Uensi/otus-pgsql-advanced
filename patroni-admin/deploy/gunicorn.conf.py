"""Конфигурация gunicorn для Patroni Console.

ВАЖНО: ровно один воркер. Реестр агентов, история метрик и планировщики
(бэкапы, дампы, обслуживание) живут в памяти процесса. При нескольких воркерах
агент регистрируется в одном, а браузер спрашивает другой, и задания по
расписанию запускаются по разу в каждом воркере. Параллелизм — через потоки."""
import os

bind = os.environ.get('PATRONI_CONSOLE_BIND', '0.0.0.0:5000')
workers = 1
threads = int(os.environ.get('PATRONI_CONSOLE_THREADS', '16'))
worker_class = 'gthread'
timeout = 120            # restore/PITR и анализ запросов могут идти долго
graceful_timeout = 30
keepalive = 5
accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('PATRONI_CONSOLE_LOGLEVEL', 'info')
# за reverse-proxy (nginx) — передавайте X-Forwarded-* и включите web.trust_proxy
forwarded_allow_ips = os.environ.get('FORWARDED_ALLOW_IPS', '127.0.0.1')
