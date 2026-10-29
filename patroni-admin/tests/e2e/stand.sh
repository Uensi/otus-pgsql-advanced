#!/usr/bin/env bash
# Стенд E2E Patroni Console: etcd + 2 узла Patroni (PostgreSQL) + wal-g (файловое
# хранилище) + 2 агента + консоль. Без root и без Docker — всё в $PCTEST_DIR.
#
#   PCTEST_DL=<каталог: walg.tar.gz, etcd.tar.gz, wheels/>  PCTEST_SRC=<код консоли>
#   bash stand.sh install   # бинарники и python-пакеты в $PCTEST_DIR
#   bash stand.sh up        # запустить всё; консоль: http://127.0.0.1:15000 (admin / e2e-admin)
#   bash stand.sh down      # остановить
#   bash stand.sh clean     # остановить и удалить данные
#
# Узлы — отдельные loopback-адреса (как отдельные серверы): pgnode1 = 127.0.0.2,
# pgnode2 = 127.0.0.3; PostgreSQL :25432, REST API Patroni :8008.
set -euo pipefail
ST=${PCTEST_DIR:-$HOME/pctest}
PCTEST_SRC=${PCTEST_SRC:-$(cd "$(dirname "$0")/../.." && pwd)}
PGBIN=${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}
PY=${PYTHON:-python3}
export PYTHONPATH="$ST/pylibs"
PGPASS_SU=pctest
CONSOLE_PORT=${CONSOLE_PORT:-15000}

log(){ echo "[stand] $*"; }

install(){
  : "${PCTEST_DL:?укажите PCTEST_DL}"
  mkdir -p "$ST/bin" "$ST/pylibs"
  tar -xzf "$PCTEST_DL/walg.tar.gz" -C "$ST/bin"
  mv -f "$ST/bin"/wal-g-pg-* "$ST/bin/wal-g" 2>/dev/null || true
  tar -xzf "$PCTEST_DL/etcd.tar.gz" -C "$ST/bin" --strip-components=1 --wildcards '*/etcd' '*/etcdctl'
  chmod +x "$ST/bin/"*
  local pipwhl; pipwhl=$(ls "$PCTEST_DL"/wheels/pip-*.whl)
  $PY "$pipwhl/pip" install --quiet --no-deps --no-index --target "$ST/pylibs" "$PCTEST_DL"/wheels/*.whl
  if ls "$PCTEST_DL"/wheels/python-etcd-*.tar.gz >/dev/null 2>&1; then
    tar -xzf "$PCTEST_DL"/wheels/python-etcd-*.tar.gz -C "$ST" && cp -r "$ST"/python-etcd-*/src/etcd "$ST/pylibs/"
  fi
  "$ST/bin/wal-g" --version | head -1
  $PY -c "import patroni, psycopg2, flask; print('patroni', patroni.__version__ if hasattr(patroni,'__version__') else 'ok', 'psycopg2', psycopg2.__version__)"
  log "установлено в $ST (PostgreSQL: $PGBIN)"
}

node_yml(){ # $1 = номер узла
  local i=$1 ip="127.0.0.$(( $1 + 1 ))"
  mkdir -p "$ST/n$i"
  cat > "$ST/n$i/patroni.yml" <<EOF
scope: pctest
name: pgnode$i
restapi:
  listen: $ip:8008
  connect_address: $ip:8008
etcd3:
  hosts: 127.0.0.1:12379
bootstrap:
  dcs:
    ttl: 20
    loop_wait: 5
    retry_timeout: 10
    maximum_lag_on_failover: 104857600
    postgresql:
      use_pg_rewind: true
      parameters:
        wal_level: replica
        hot_standby: "on"
        max_wal_senders: 10
        max_replication_slots: 10
        wal_log_hints: "on"
        archive_mode: "on"
        archive_command: "$ST/bin/wal-g --config $ST/walg.json wal-push %p"
        archive_timeout: 30
        shared_preload_libraries: pg_stat_statements
        track_io_timing: "on"
        max_wal_size: 256MB
  initdb:
    - encoding: UTF8
    - data-checksums
postgresql:
  listen: $ip:25432
  connect_address: $ip:25432
  data_dir: $ST/n$i/data
  bin_dir: $PGBIN
  pgpass: $ST/n$i/pgpass
  authentication:
    superuser: {username: postgres, password: $PGPASS_SU}
    replication: {username: replicator, password: replpass}
  parameters:
    unix_socket_directories: $ST/n$i
    logging_collector: "on"
    log_directory: log
    log_filename: postgresql.log
    log_line_prefix: "%m [%p] %q%u@%d "
    log_lock_waits: "on"
    log_temp_files: 0
    log_checkpoints: "on"
    log_min_duration_statement: 200
    log_autovacuum_min_duration: 0
    log_connections: "on"
  pg_hba:
    - local all all trust
    - host all all 127.0.0.0/8 scram-sha-256
    - host replication replicator 127.0.0.0/8 scram-sha-256
    - host replication postgres 127.0.0.0/8 scram-sha-256
  basebackup:
    checkpoint: fast
EOF
}

up(){
  mkdir -p "$ST/run" "$ST/walg-store" "$ST/console-data"
  cat > "$ST/walg.json" <<EOF
{"WALG_FILE_PREFIX": "$ST/walg-store", "WALG_DELTA_MAX_STEPS": "6", "WALG_COMPRESSION_METHOD": "lz4"}
EOF
  if ! pgrep -f "$ST/bin/etcd " >/dev/null; then
    setsid nohup "$ST/bin/etcd" --name e2e --data-dir "$ST/etcd" --listen-client-urls http://127.0.0.1:12379 \
      --advertise-client-urls http://127.0.0.1:12379 --listen-peer-urls http://127.0.0.1:12380 \
      > "$ST/run/etcd.log" 2>&1 & echo $! > "$ST/run/etcd.pid"
    sleep 2
  fi
  for i in 1 2; do
    node_yml $i
    if ! pgrep -f "patroni $ST/n$i/patroni.yml" >/dev/null; then
      setsid $PY -m patroni "$ST/n$i/patroni.yml" < /dev/null > "$ST/run/patroni$i.log" 2>&1 &
    fi
    [ "$i" = 1 ] && wait_leader
  done
  wait_replica
  cat > "$ST/config.e2e.yaml" <<EOF
web: {host: 127.0.0.1, port: $CONSOLE_PORT, secret: "e2e-secret-$(date +%s)"}
postgres: {user: postgres, password: "$PGPASS_SU", database: postgres, port: 25432}
agents: {key: "e2e-agent-key"}
clusters:
  - name: pctest
    nodes:
      - {name: pgnode1, host: 127.0.0.2, port: 8008}
      - {name: pgnode2, host: 127.0.0.3, port: 8008}
users:
  - {name: admin, password: "e2e-admin", role: admin}
  - {name: dba, password: "e2e-dba", role: dba}
  - {name: viewer, password: "e2e-viewer", role: viewer}
patroni: {config_path: $ST/n1/patroni.yml}
EOF
  if ! pgrep -f "console-e2e" >/dev/null; then
    ( cd "$PCTEST_SRC" && exec env PATRONI_WEB_CONFIG="$ST/config.e2e.yaml" PATRONI_CONSOLE_DATA="$ST/console-data"       PATRONI_CONSOLE_ASH_INTERVAL=1 PYTHONUNBUFFERED=1 setsid $PY -c "import sys; sys.argv=['console-e2e']; import app; app.app.run(host='127.0.0.1', port=$CONSOLE_PORT, threaded=True)"     ) < /dev/null > "$ST/run/console.log" 2>&1 &
    sleep 3
  fi
  for i in 1 2; do
    if ! pgrep -f "agent-e2e-n$i" >/dev/null; then
      mkdir -p "$ST/restore-n$i" "$ST/dumps-n$i"
      ( cd "$ST" && exec env PATRONI_CONSOLE_URL="http://127.0.0.1:$CONSOLE_PORT" PATRONI_CONSOLE_KEY=e2e-agent-key PG_PASSWORD=$PGPASS_SU         PATRONI_AGENT_NONROOT=1 PATRONI_AGENT_PATRONI_YML="$ST/n$i/patroni.yml" PATRONI_AGENT_WALG_CONFIG="$ST/walg.json"         PATRONI_AGENT_RESTORE_BASE="$ST/restore-n$i" PATRONI_AGENT_DUMP_BASE="$ST/dumps-n$i" PATH="$ST/bin:$PATH" PYTHONUNBUFFERED=1         setsid $PY -c "import sys, runpy; sys.argv=['agent-e2e-n$i']; runpy.run_path('$PCTEST_SRC/agent.py', run_name='__main__')"       ) < /dev/null > "$ST/run/agent$i.log" 2>&1 &
    fi
  done
  log "запущено: консоль http://127.0.0.1:$CONSOLE_PORT, узлы 127.0.0.2/127.0.0.3:25432"
}

wait_leader(){
  for _ in $(seq 1 90); do
    if curl -sf http://127.0.0.2:8008/primary >/dev/null 2>&1; then log "pgnode1 — лидер"; return; fi
    sleep 2
  done
  log "лидер не поднялся"; tail -30 "$ST/run/patroni1.log"; exit 1
}
wait_replica(){
  for _ in $(seq 1 120); do
    if curl -sf http://127.0.0.3:8008/replica >/dev/null 2>&1; then log "pgnode2 — реплика"; return; fi
    sleep 2
  done
  log "реплика не поднялась"; tail -30 "$ST/run/patroni2.log"; exit 1
}

down(){
  pkill -f "agent-e2e-n" 2>/dev/null || true
  pkill -f "console-e2e" 2>/dev/null || true
  pkill -f "patroni $ST/n2/patroni.yml" 2>/dev/null || true
  pkill -f "patroni $ST/n1/patroni.yml" 2>/dev/null || true
  sleep 5
  for i in 1 2; do "$PGBIN/pg_ctl" -D "$ST/n$i/data" -m fast stop >/dev/null 2>&1 || true; done
  pkill -f "$ST/bin/etcd " 2>/dev/null || true
  rm -f "$ST/run/"*.pid
  log "остановлено"
}

agents(){ # перезапуск агентов (после изменения agent.py)
  pkill -f "agent-e2e-n" 2>/dev/null || true
  sleep 1; up
}

restart_console(){
  pkill -f "console-e2e" 2>/dev/null || true
  sleep 1; up
}

status(){
  declare -A pat=([etcd]="$ST/bin/etcd " [patroni1]="patroni $ST/n1/patroni.yml" [patroni2]="patroni $ST/n2/patroni.yml"
                  [console]="console-e2e" [agent1]="agent-e2e-n1" [agent2]="agent-e2e-n2")
  for f in etcd patroni1 patroni2 console agent1 agent2; do
    if pgrep -f "${pat[$f]}" >/dev/null; then echo "$f: работает"; else echo "$f: остановлен"; fi
  done
  curl -s http://127.0.0.2:8008/cluster | head -c 600; echo
}

clean(){ down; rm -rf "$ST/n1" "$ST/n2" "$ST/etcd" "$ST/walg-store" "$ST/restore-n"* "$ST/dumps-n"* "$ST/console-data" "$ST/run"; log "данные удалены"; }

"${1:-up}"
