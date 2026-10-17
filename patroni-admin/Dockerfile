# Patroni Console — образ для продакшена.
# Сборка:   docker build -t patroni-console .
# Запуск:   docker run -d -p 5000:5000 -v $PWD/config.yaml:/app/config.yaml:ro \
#             -v patroni-console-data:/app/data patroni-console
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATRONI_WEB_CONFIG=/app/config.yaml PATRONI_CONSOLE_DATA=/app/data

RUN useradd --system --uid 10001 --home /app console
WORKDIR /app
COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-prod.txt
COPY --chown=console:console . .
RUN rm -f config.yaml config.yaml_test *.jsonl && mkdir -p /app/data && chown console:console /app/data

USER console
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz',timeout=3).status==200 else 1)"
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "app:app"]
