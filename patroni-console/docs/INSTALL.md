# Установка Patroni Console

Консоль ставится на отдельный сервер (или ВМ) в сети, откуда видны REST API Patroni (порт 8008)
и клиентский порт etcd (2379) всех кластеров. На ноды кластера ставить её не нужно.

## Требования

- Linux с systemd, Python 3.10+ (`python3 -m venv` должен работать).
- Patroni 4.x, etcd 3.5–3.6 (API v3). PostgreSQL — любая версия, поддерживаемая Patroni 4.
- Сертификат и ключ для HTTPS (внутренний CA или самоподписанный, PEM).
- Доступ к REST API Patroni с логином и паролем (`restapi.authentication` в patroni.yml).

## 1. Установка

```bash
tar xzf patroni-console-1.0.0.tar.gz && cd patroni-console
sudo ./deploy/install.sh
```

Скрипт создаёт системного пользователя `patroni-console`, ставит код в `/opt/patroni-console`
(отдельный venv), создаёт `/etc/patroni-console/{config.yaml,env,tls/}`, базу в
`/var/lib/patroni-console` и unit `patroni-console.service`. Повторный запуск обновляет код,
не трогая конфиг, секреты и базу.

## 2. Сертификат

```bash
sudo cp console.crt console.key /etc/patroni-console/tls/
sudo chown root:patroni-console /etc/patroni-console/tls/*
sudo chmod 0640 /etc/patroni-console/tls/*
```

Консоль сама обслуживает HTTPS (gunicorn, TLS 1.2+). Без сертификата она не стартует.
Порт по умолчанию 8443, меняется в `PATRONI_CONSOLE_BIND` в `/etc/patroni-console/env`.

## 3. Доступ к Patroni

На каждой ноде в `patroni.yml` должна быть включена аутентификация REST API:

```yaml
restapi:
  listen: 0.0.0.0:8008
  authentication:
    username: patroni
    password: <длинный пароль>
```

Без неё любой, кто видит порт 8008, может сделать switchover и рестарт. Пароль впишите в
`PATRONI_REST_PASSWORD` в `/etc/patroni-console/env`. Если REST API работает по HTTPS, укажите
`rest.scheme: https` и `rest.tls.ca_file` в config.yaml.

## 4. Доступ к etcd (только чтение)

Консоль читает у etcd только статус endpoint-ов, alarms и список членов. Ключи Patroni она не читает
и в etcd ничего не пишет. Если в etcd включена аутентификация, заведите отдельного пользователя
без прав на ключи:

```bash
etcdctl user add patroni-console        # пароль -> ETCD_PASSWORD в /etc/patroni-console/env
```

и укажите `etcd.username: patroni-console`, `etcd.password: ${ETCD_PASSWORD}` в config.yaml.

## 5. Пользователи консоли

```bash
/opt/patroni-console/venv/bin/patroni-console-hashpw
```

Полученный хеш (`scrypt:...`) вставьте в `users[].password_hash`. Роли:

| Роль | Что может |
|---|---|
| viewer | смотреть статус, события, конфиг, историю |
| operator | + switchover, пауза, рестарты, rolling restart |
| admin | + failover, reinit, изменение глобального конфига, аудит, тестовое уведомление |

Смена роли в config.yaml после перезапуска сразу завершает сессии этого пользователя.

## 6. Проверка и запуск

```bash
sudo -u patroni-console bash -c 'set -a; . /etc/patroni-console/env; \
  /opt/patroni-console/venv/bin/patroni-console-check /etc/patroni-console/config.yaml'
sudo systemctl enable --now patroni-console
journalctl -u patroni-console -f
```

`patroni-console-check` ничего не меняет: читает конфиг, опрашивает каждую ноду и etcd и печатает
таблицу с проблемами. Код выхода 0 — можно запускать.

Откройте `https://<сервер>:8443/`.

## Обновление

Распакуйте новую версию и снова запустите `sudo ./deploy/install.sh`, затем
`sudo systemctl restart patroni-console`. Схема базы обновляется автоматически при старте.

## Резервная копия

Всё состояние консоли — один файл SQLite `/var/lib/patroni-console/console.db`
(сессии, аудит, операции, события, версии конфига). Копия на лету:

```bash
sqlite3 /var/lib/patroni-console/console.db ".backup /backup/console-$(date +%F).db"
```

## Важно

- Ровно один процесс консоли на базу (`workers = 1` уже зафиксирован в `deploy/gunicorn.conf.py`).
  Второй экземпляр продублирует уведомления и обойдёт защиту «одна операция на кластер».
- Консоль не нуждается в доступе к PostgreSQL (порт 5432) и к SSH нод.
