# Patroni Console в WSL2 с тестовым кластером itest

Стенд на одном Windows-компьютере: Patroni Console и тестовый кластер Patroni работают в WSL2
(Ubuntu 24.04, systemd включён), консоль открывается из Windows по `https://localhost:8443/`.
Docker и nginx не нужны.

| Компонент | Где | Порты (127.0.0.1) |
|---|---|---|
| etcd 3.6 ×3 (`itest-etcd@1..3`) | `/opt/itest/etcd`, данные `/var/lib/itest/etcd*` | client 2379/2381/2383, peer 2380/2382/2384 |
| Patroni 4 + PostgreSQL 16 ×3 (`itest-patroni@1..3`, scope `itest`) | `/opt/itest/venv`, конфиги `/etc/itest/n*.yml` | REST 8008–8010, PostgreSQL 5442–5444 |
| Patroni Console (`patroni-console`) | `/opt/patroni-console`, конфиг `/etc/patroni-console` | HTTPS 8443 (0.0.0.0) |

Порт 5432 не занимается: он остаётся системному PostgreSQL, если тот уже стоит.

## Файлы

| Файл | Что делает |
|---|---|
| `fetch-wheels.ps1` | на Windows качает колёса под Linux/py3.12 и etcd, чтобы ставить в WSL без интернета |
| `install-console-offline.sh` | ставит консоль штатным `deploy/install.sh`, но из локальных колёс; venv без `python3-venv`; самоподписанный сертификат |
| `itest-cluster.sh` | ставит etcd и Patroni, генерирует пароли в `/etc/itest/patroni.env`, создаёт юниты и запускает кластер |
| `config.itest.yaml` | шаблон `config.yaml` консоли для itest (секреты только `${VAR}`) |
| `env.example` | что должно быть в `/etc/patroni-console/env` |
| `console-itest.sh` | переносит пароль REST API в env консоли, пишет `config.yaml`, запускает `patroni-console-check`, перезапускает консоль |
| `start.cmd` / `stop.cmd` / `status.cmd` | запуск, остановка и проверка из Windows |

## Установка

Если у WSL есть интернет, колёса можно не качать на Windows (`apt install python3-venv`, `pip` из PyPI).
Ниже — вариант без интернета в WSL (например, VPN на Windows не пропускает трафик WSL).

1. Windows (PowerShell), в каталоге репозитория:
   ```powershell
   powershell -ExecutionPolicy Bypass -File patroni-console\deploy\wsl\fetch-wheels.ps1 -Out D:\patroni\offline
   ```
2. WSL от root (`wsl -u root`); PostgreSQL 16 должен стоять (`/usr/lib/postgresql/16/bin`):
   ```bash
   cd /mnt/d/путь/к/patroni-console
   W=/mnt/d/patroni/offline
   bash deploy/wsl/install-console-offline.sh . $W/console-wheels
   bash deploy/wsl/itest-cluster.sh $W/etcd-v3.6.15-linux-amd64.tar.gz $W/itest-wheels
   /opt/patroni-console/venv/bin/patroni-console-hashpw > /root/console-admin.hash   # пароль admin, ≥12 символов
   bash deploy/wsl/console-itest.sh /root/console-admin.hash                        # check: код 0
   ```
3. Откройте `https://localhost:8443/`, логин `admin`. Сертификат самоподписанный — браузер предупредит.

## Запуск и остановка

WSL выключается сам, когда к нему никто не подключён, и службы останавливаются вместе с ним.

- `start.cmd` — запускает кластер и консоль и держит WSL, пока открыто окно.
- `stop.cmd` — останавливает консоль и кластер (данные сохраняются).
- `status.cmd` — `patronictl list` и `patroni-console-check`.

В WSL напрямую:
```bash
sudo systemctl start itest.target patroni-console      # запуск
sudo systemctl stop patroni-console itest.target 'itest-patroni@*' 'itest-etcd@*'   # остановка
sudo systemctl restart patroni-console                 # перезапуск консоли после правки config.yaml
patronictl -c /etc/itest/n1.yml list                   # состояние кластера
journalctl -u patroni-console -f                       # лог консоли
```
Автозапуск при старте WSL (по желанию):
`sudo systemctl enable itest.target itest-etcd@{1,2,3} itest-patroni@{1,2,3} patroni-console`.

## Замечания

- Секреты: пароли Patroni — `/etc/itest/patroni.env` (0640 root:pgtest), секреты консоли —
  `/etc/patroni-console/env` (0640 root:patroni-console). В YAML и в git их нет.
- `postgresql.pgpass` вынесен в каталог ноды: юнит с `ProtectSystem=strict` пускает запись только туда,
  а по умолчанию Patroni пишет `~/.pgpass`.
- `ydiff<1.5` (нужен Patroni 4.1) и `python-etcd` есть на PyPI только исходниками; `fetch-wheels.ps1`
  качает их tar.gz со сверкой sha256, собираются они уже в WSL.
- Стенд только для проверки консоли: всё слушает 127.0.0.1, etcd без TLS и без аутентификации.
