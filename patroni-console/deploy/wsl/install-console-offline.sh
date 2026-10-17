#!/usr/bin/env bash
# Установка Patroni Console в WSL без интернета и без python3-venv (ensurepip).
# Запуск от root: ./install-console-offline.sh <распакованный patroni-console> <каталог с колёсами>
# Колёса: Flask, requests, PyYAML, gunicorn, setuptools, wheel, pip под Linux/py3 (см. fetch-wheels.ps1).
# Дальше работает штатный deploy/install.sh: venv создан заранее, pip берёт пакеты только из каталога.
# Сертификат: если в /etc/patroni-console/tls его нет, создаётся самоподписанный (localhost, 127.0.0.1).
set -euo pipefail
SRC=$(cd "${1:?каталог patroni-console}" && pwd)
WHEELS=$(cd "${2:?каталог с колёсами}" && pwd)
PREFIX=/opt/patroni-console
ETC=/etc/patroni-console

install -d -m 0755 "$PREFIX"
if [ ! -x "$PREFIX/venv/bin/pip" ]; then
  rm -rf "$PREFIX/venv"
  python3 -m venv --without-pip "$PREFIX/venv"
  W=$(ls "$WHEELS"/pip-*.whl)
  "$PREFIX/venv/bin/python" "$W/pip" install -q --no-index "$W"
fi
sed -i 's/\r$//' "$SRC"/deploy/*.sh
PIP_NO_INDEX=1 PIP_FIND_LINKS="$WHEELS" bash "$SRC/deploy/install.sh"

if [ ! -f "$ETC/tls/console.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$ETC/tls/console.key" -out "$ETC/tls/console.crt" \
    -subj '/CN=patroni-console' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' 2>/dev/null
  echo "создан самоподписанный сертификат $ETC/tls/console.crt"
fi
chown root:patroni-console "$ETC"/tls/console.*; chmod 0640 "$ETC"/tls/console.*
