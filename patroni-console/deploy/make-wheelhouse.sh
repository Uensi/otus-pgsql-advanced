#!/usr/bin/env bash
# Готовит каталог пакетов для установки на сервер без интернета.
# Запускать на любой машине с интернетом и Python 3.10+ (Linux, macOS, WSL):
#   deploy/make-wheelhouse.sh [каталог] [версия Python сервера, по умолчанию 3.12]
# Затем перенести каталог проекта вместе с wheelhouse на сервер и выполнить
#   sudo WHEELS=./wheelhouse ./deploy/install.sh
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$SRC/wheelhouse}
PYVER=${2:-3.12}
PY=${PYTHON:-python3}
mkdir -p "$OUT"
# пакеты под Linux x86_64 и нужную версию Python, независимо от того, где запущен скрипт
"$PY" -m pip download -q -d "$OUT" --only-binary=:all: \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --python-version "$PYVER" \
  -r "$SRC/requirements.txt" pip setuptools wheel
# сам пакет консоли: собирается из исходников без сети
"$PY" -m pip wheel -q --no-deps --no-build-isolation -w "$OUT" "$SRC" 2>/dev/null \
  || "$PY" -m pip wheel -q --no-deps -w "$OUT" "$SRC"
echo "готово: $(ls "$OUT" | wc -l) пакетов в $OUT"
