#!/usr/bin/env bash
# Установка Patroni Console на Linux с systemd. Запускать от root из распакованного каталога.
# Повторный запуск обновляет код и зависимости, не трогая конфиг, ключи и базу.
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/patroni-console
ETC=/etc/patroni-console
DATA=/var/lib/patroni-console
PY=${PYTHON:-python3}

"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python 3.10+"'
id patroni-console >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin patroni-console

install -d -m 0755 "$PREFIX"
install -d -m 0750 -o root -g patroni-console "$ETC"
install -d -m 0700 -o patroni-console -g patroni-console "$DATA"

# код: только нужное, без тестов и стенда
rm -rf "$PREFIX/src.new" && mkdir -p "$PREFIX/src.new"
cp -r "$SRC/patroni_console" "$SRC/pyproject.toml" "$SRC/README.md" "$PREFIX/src.new/"
rm -rf "$PREFIX/src" && mv "$PREFIX/src.new" "$PREFIX/src"
cp -r "$SRC/deploy" "$SRC/docs" "$PREFIX/"
[ -d "$PREFIX/venv" ] || "$PY" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q "$PREFIX/src"

if [ ! -f "$ETC/config.yaml" ]; then
  install -m 0640 -o root -g patroni-console "$SRC/config.example.yaml" "$ETC/config.yaml"
  echo "создан $ETC/config.yaml — заполните его"
fi
if [ ! -f "$ETC/env" ]; then
  umask 077
  cat > "$ETC/env" <<ENV
PATRONI_CONSOLE_SECRET=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')
PATRONI_CONSOLE_BIND=0.0.0.0:8443
PATRONI_CONSOLE_CERT=$ETC/tls/console.crt
PATRONI_CONSOLE_KEY=$ETC/tls/console.key
PATRONI_REST_PASSWORD=
ETCD_PASSWORD=
# ALERT_WEBHOOK_TOKEN=
# TELEGRAM_BOT_TOKEN=
ENV
  chown root:patroni-console "$ETC/env"; chmod 0640 "$ETC/env"
  echo "создан $ETC/env (секреты) — впишите пароли"
fi
install -d -m 0750 -o root -g patroni-console "$ETC/tls"

install -m 0644 "$PREFIX/deploy/patroni-console.service" /etc/systemd/system/patroni-console.service
systemctl daemon-reload
echo
echo "Дальше:"
echo "  1. положите сертификат и ключ в $ETC/tls/ (ключ: chown root:patroni-console, chmod 0640)"
echo "  2. заполните $ETC/config.yaml и $ETC/env; хеш пароля: $PREFIX/venv/bin/patroni-console-hashpw"
echo "  3. проверьте конфиг и связь с кластерами:"
echo "     sudo -u patroni-console bash -c 'set -a; . $ETC/env; $PREFIX/venv/bin/patroni-console-check $ETC/config.yaml'"
echo "  4. systemctl enable --now patroni-console && journalctl -u patroni-console -f"
