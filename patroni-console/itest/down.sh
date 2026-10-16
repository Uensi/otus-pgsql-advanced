#!/usr/bin/env bash
# Останавливает стенд и удаляет его данные.
ROOT=${ITEST_ROOT:-/opt/itest/run}
for f in "$ROOT"/n*/patroni.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; done
sleep 5
pkill -u "${PG_USER:-pgtest}" 2>/dev/null
for p in $(pgrep -x etcd); do kill "$p"; done
sleep 2
rm -rf "$ROOT"
