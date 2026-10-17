#!/usr/bin/env bash
# Тестовый кластер "itest" для Patroni Console: 3 члена etcd 3.6 + 3 ноды Patroni 4 / PostgreSQL 16
# на 127.0.0.1, оформленные как systemd-юниты (itest-etcd@1..3, itest-patroni@1..3, itest.target).
# Только для стенда: всё слушает 127.0.0.1, etcd без TLS.
#
# Запуск от root в WSL (или любом Linux с systemd), сеть не нужна:
#   ./itest-cluster.sh /path/etcd-v3.6.15-linux-amd64.tar.gz /path/wheels
# wheels — каталог с колёсами patroni[etcd3] и psycopg2-binary под Linux/py3 (см. fetch-wheels.ps1).
# Повторный запуск безопасен: пароли и данные сохраняются.
#
# Порты: etcd client 2379/2381/2383 (peer 2380/2382/2384), REST API 8008-8010,
# PostgreSQL 5442-5444 (5432 оставлен системному PostgreSQL).
# Пароли генерируются один раз в /etc/itest/patroni.env (0640 root:pgtest), в YAML их нет.
set -euo pipefail
ETCD_TARBALL=${1:?путь к etcd-vX.Y.Z-linux-amd64.tar.gz}
WHEELS=${2:?каталог с колёсами Patroni}
ETCD_VER=$(basename "$ETCD_TARBALL" | sed -n 's/^etcd-\(v[0-9.]*\)-linux-amd64\.tar\.gz$/\1/p')
[ -n "$ETCD_VER" ] || { echo "не похоже на etcd-vX.Y.Z-linux-amd64.tar.gz: $ETCD_TARBALL"; exit 1; }
OPT=/opt/itest
DATA=/var/lib/itest
ETC=/etc/itest
PG_BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}
[ -x "$PG_BIN/postgres" ] || { echo "нет $PG_BIN/postgres (apt install postgresql-16)"; exit 1; }

id itest-etcd >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin itest-etcd
id pgtest    >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /bin/bash pgtest
install -d -m 0755 "$OPT" "$OPT/bin" "$DATA"
install -d -m 0750 -o root -g pgtest "$ETC"

# etcd
if [ ! -x "$OPT/etcd-$ETCD_VER-linux-amd64/etcd" ]; then
  tar xzf "$ETCD_TARBALL" -C "$OPT"
  chown -R root:root "$OPT/etcd-$ETCD_VER-linux-amd64"
fi
ln -sfn "$OPT/etcd-$ETCD_VER-linux-amd64" "$OPT/etcd"
ln -sf "$OPT/etcd/etcdctl" /usr/local/bin/etcdctl

# Patroni в отдельном venv, без интернета. venv --without-pip: python3-venv (ensurepip) может не стоять.
rm -rf "$OPT/wheels" && cp -r "$WHEELS" "$OPT/wheels"
if [ ! -x "$OPT/venv/bin/pip" ]; then
  rm -rf "$OPT/venv"
  python3 -m venv --without-pip "$OPT/venv"
  W=$(ls "$OPT"/wheels/pip-*.whl)
  "$OPT/venv/bin/python" "$W/pip" install -q --no-index "$W"
fi
PIP_NO_INDEX=1 PIP_FIND_LINKS="$OPT/wheels" "$OPT/venv/bin/pip" install -q "patroni[etcd3]>=4,<5" psycopg2-binary
ln -sf "$OPT/venv/bin/patronictl" /usr/local/bin/patronictl

# пароли (один раз)
gen() { "$OPT/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))'; }
if [ ! -f "$ETC/patroni.env" ]; then
  umask 077
  cat > "$ETC/patroni.env" <<ENV
PATRONI_RESTAPI_USERNAME=patroni
PATRONI_RESTAPI_PASSWORD=$(gen)
PATRONI_REPLICATION_USERNAME=replicator
PATRONI_REPLICATION_PASSWORD=$(gen)
PATRONI_SUPERUSER_USERNAME=postgres
PATRONI_SUPERUSER_PASSWORD=$(gen)
ENV
  umask 022
fi
chown root:pgtest "$ETC/patroni.env"; chmod 0640 "$ETC/patroni.env"

# запуск члена etcd: экземпляр i -> client 2377+2i, peer 2378+2i
cat > "$OPT/bin/etcd-node" <<'SH'
#!/bin/sh
i="$1"; c=$((2377 + 2*i)); p=$((c + 1))
exec /opt/itest/etcd/etcd --name "e$i" --data-dir "/var/lib/itest/etcd$i" \
  --listen-client-urls "http://127.0.0.1:$c" --advertise-client-urls "http://127.0.0.1:$c" \
  --listen-peer-urls "http://127.0.0.1:$p" --initial-advertise-peer-urls "http://127.0.0.1:$p" \
  --initial-cluster "e1=http://127.0.0.1:2380,e2=http://127.0.0.1:2382,e3=http://127.0.0.1:2384" \
  --initial-cluster-state new --initial-cluster-token itest
SH
chmod 0755 "$OPT/bin/etcd-node"

for i in 1 2 3; do
  install -d -m 0700 -o itest-etcd -g itest-etcd "$DATA/etcd$i"
  install -d -m 0700 -o pgtest -g pgtest "$DATA/n$i"
  rest=$((8007 + i)); pg=$((5441 + i))
  cat > "$ETC/n$i.yml" <<YML
# Patroni: нода n$i тестового кластера itest. Пароли — из $ETC/patroni.env (PATRONI_*).
scope: itest
name: n$i
restapi:
  listen: 127.0.0.1:$rest
  connect_address: 127.0.0.1:$rest
etcd3:
  hosts: [127.0.0.1:2379, 127.0.0.1:2381, 127.0.0.1:2383]
bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576
    postgresql:
      use_pg_rewind: true
      parameters: {wal_level: replica, hot_standby: 'on', max_wal_senders: 10, max_replication_slots: 10, wal_log_hints: 'on', password_encryption: scram-sha-256}
  initdb: [{encoding: UTF8}, {locale: C.UTF-8}, data-checksums]
  pg_hba:
    - local all all peer map=itest
    - host replication replicator 127.0.0.1/32 scram-sha-256
    - host all all 127.0.0.1/32 scram-sha-256
postgresql:
  listen: 127.0.0.1:$pg
  connect_address: 127.0.0.1:$pg
  data_dir: $DATA/n$i/data
  pgpass: $DATA/n$i/pgpass          # не в \$HOME: юнит пускает запись только в каталог ноды
  bin_dir: $PG_BIN
  pg_ident:
    - itest pgtest postgres
    - itest pgtest pgtest
  parameters: {unix_socket_directories: '$DATA/n$i'}
YML
  chown root:pgtest "$ETC/n$i.yml"; chmod 0640 "$ETC/n$i.yml"
done

cat > /etc/systemd/system/itest-etcd@.service <<'UNIT'
[Unit]
Description=itest etcd member e%i
PartOf=itest.target
After=network.target

[Service]
User=itest-etcd
Group=itest-etcd
ExecStart=/opt/itest/bin/etcd-node %i
Restart=on-failure
RestartSec=3
LimitNOFILE=65536
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/itest/etcd%i
PrivateTmp=yes

[Install]
WantedBy=itest.target
UNIT

cat > /etc/systemd/system/itest-patroni@.service <<'UNIT'
[Unit]
Description=itest Patroni node n%i (PostgreSQL 16)
PartOf=itest.target
After=itest-etcd@1.service itest-etcd@2.service itest-etcd@3.service
Wants=itest-etcd@1.service itest-etcd@2.service itest-etcd@3.service

[Service]
User=pgtest
Group=pgtest
EnvironmentFile=/etc/itest/patroni.env
ExecStart=/opt/itest/venv/bin/patroni /etc/itest/n%i.yml
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process
TimeoutStopSec=60
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/itest/n%i
PrivateTmp=yes

[Install]
WantedBy=itest.target
UNIT

cat > /etc/systemd/system/itest.target <<'UNIT'
[Unit]
Description=itest test cluster (3x etcd + 3x Patroni)
Wants=itest-etcd@1.service itest-etcd@2.service itest-etcd@3.service itest-patroni@1.service itest-patroni@2.service itest-patroni@3.service

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl start itest-etcd@1 itest-etcd@2 itest-etcd@3
sleep 3
systemctl start itest-patroni@1            # первая нода инициализирует кластер и станет лидером
for _ in $(seq 60); do
  curl -sf -o /dev/null http://127.0.0.1:8008/primary && break; sleep 2
done
systemctl start itest.target
echo "кластер itest запущен; состояние: patronictl -c /etc/itest/n1.yml list"
echo "автозапуск при старте системы (по желанию): systemctl enable itest.target itest-etcd@{1,2,3} itest-patroni@{1,2,3}"
