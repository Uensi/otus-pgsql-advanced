#!/usr/bin/env bash
# Останавливает стенд и удаляет его данные.
ROOT=${ITEST_ROOT:-/opt/itest/run}
PG_USER=${PG_USER:-pgtest}
for f in "$ROOT"/n*/patroni.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; done
for p in $(pgrep -x etcd); do kill -CONT "$p" 2>/dev/null; kill "$p" 2>/dev/null; done
for _ in $(seq 20); do
  pgrep -u "$PG_USER" >/dev/null || pgrep -x etcd >/dev/null || break
  sleep 1
done
pkill -9 -u "$PG_USER" 2>/dev/null
for p in $(pgrep -x etcd); do kill -9 "$p" 2>/dev/null; done
sleep 1
rm -rf "$ROOT"
