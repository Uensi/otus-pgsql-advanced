#!/usr/bin/env bash
# Поднимает локальный стенд: 3 члена etcd 3.6 + 3 ноды Patroni 4 / PostgreSQL на 127.0.0.1.
# Только для интеграционных тестов. Пароли тестовые и известны всем, в прод не годится.
# Требования: бинарники etcd (ETCD_BIN), patroni (PATRONI_BIN), PostgreSQL (PG_BIN), пользователь PG_USER (не root).
set -euo pipefail
ROOT=${ITEST_ROOT:-/opt/itest/run}
ETCD_BIN=${ETCD_BIN:-/opt/itest/etcd-v3.6.4-linux-amd64/etcd}
PATRONI_BIN=${PATRONI_BIN:-/opt/itest/venv/bin/patroni}
PG_BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}
PG_USER=${PG_USER:-pgtest}
mkdir -p "$ROOT"
chmod 755 "$ROOT"

cluster="e1=http://127.0.0.1:2380,e2=http://127.0.0.1:2382,e3=http://127.0.0.1:2384"
for i in 1 2 3; do
  c=$((2377 + 2*i)); p=$((c + 1))
  mkdir -p "$ROOT/etcd$i"
  nohup "$ETCD_BIN" --name "e$i" --data-dir "$ROOT/etcd$i" \
    --listen-client-urls "http://127.0.0.1:$c" --advertise-client-urls "http://127.0.0.1:$c" \
    --listen-peer-urls "http://127.0.0.1:$p" --initial-advertise-peer-urls "http://127.0.0.1:$p" \
    --initial-cluster "$cluster" --initial-cluster-state new --initial-cluster-token itest \
    > "$ROOT/etcd$i.log" 2>&1 &
  echo $! > "$ROOT/etcd$i.pid"
done
sleep 3

for i in 1 2 3; do
  rest=$((8007 + i)); pg=$((5431 + i))
  d="$ROOT/n$i"; mkdir -p "$d"; chown "$PG_USER" "$d"; chmod 700 "$d"
  cat > "$ROOT/n$i.yml" <<YML
scope: itest
name: n$i
restapi:
  listen: 127.0.0.1:$rest
  connect_address: 127.0.0.1:$rest
  authentication: {username: patroni, password: itest-rest-pass}
etcd3:
  hosts: [127.0.0.1:2379, 127.0.0.1:2381, 127.0.0.1:2383]
bootstrap:
  dcs:
    ttl: 20
    loop_wait: 5
    retry_timeout: 5
    maximum_lag_on_failover: 1048576
    postgresql:
      use_pg_rewind: true
      parameters: {wal_level: replica, hot_standby: 'on', max_wal_senders: 10, max_replication_slots: 10, wal_log_hints: 'on'}
  initdb: [{encoding: UTF8}, data-checksums]
  pg_hba:
    - local all all trust
    - host replication replicator 127.0.0.1/32 md5
    - host all all 127.0.0.1/32 md5
postgresql:
  listen: 127.0.0.1:$pg
  connect_address: 127.0.0.1:$pg
  data_dir: $d/data
  bin_dir: $PG_BIN
  authentication:
    replication: {username: replicator, password: itest-rep-pass}
    superuser: {username: postgres, password: itest-su-pass}
  parameters: {unix_socket_directories: '$d'}
YML
  chown "$PG_USER" "$ROOT/n$i.yml"
  su "$PG_USER" -c "nohup $PATRONI_BIN $ROOT/n$i.yml > $d/patroni.log 2>&1 & echo \$! > $d/patroni.pid"
  [ "$i" = 1 ] && sleep 10
done
echo "стенд запускается: REST 127.0.0.1:8008-8010, etcd 127.0.0.1:2379/2381/2383"
