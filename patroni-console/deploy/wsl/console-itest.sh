#!/usr/bin/env bash
# Подключает установленную Patroni Console к тестовому кластеру itest (itest-cluster.sh).
# Запуск от root: ./console-itest.sh <файл с хешем пароля admin>
# Хеш: /opt/patroni-console/venv/bin/patroni-console-hashpw > /root/console-admin.hash
# Пароль REST API берётся из /etc/itest/patroni.env и кладётся в /etc/patroni-console/env
# (PATRONI_REST_PASSWORD); в config.yaml только ссылка ${PATRONI_REST_PASSWORD}.
set -euo pipefail
ETC=/etc/patroni-console
HASH_FILE=${1:?файл с хешем пароля admin (scrypt:...)}
HERE=$(cd "$(dirname "$0")" && pwd)

rest_pw=$(sed -n 's/^PATRONI_RESTAPI_PASSWORD=//p' /etc/itest/patroni.env)
[ -n "$rest_pw" ] || { echo "нет PATRONI_RESTAPI_PASSWORD в /etc/itest/patroni.env"; exit 1; }
tmp=$(mktemp)
grep -v '^PATRONI_REST_PASSWORD=' "$ETC/env" > "$tmp" || true
echo "PATRONI_REST_PASSWORD=$rest_pw" >> "$tmp"
install -m 0640 -o root -g patroni-console "$tmp" "$ETC/env"; rm -f "$tmp"

hash=$(tr -d '\r\n' < "$HASH_FILE")
case "$hash" in scrypt:*|pbkdf2:*) ;; *) echo "в $HASH_FILE не хеш scrypt:/pbkdf2:"; exit 1;; esac
[ -f "$ETC/config.yaml.orig" ] || cp -p "$ETC/config.yaml" "$ETC/config.yaml.orig"
HASH="$hash" python3 - "$HERE/config.itest.yaml" "$ETC/config.yaml" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding='utf-8').read().replace('scrypt:REPLACE_ME', os.environ['HASH'])
with open(dst, 'w', encoding='utf-8') as f:
    f.write(text)
PY
chown root:patroni-console "$ETC/config.yaml"; chmod 0640 "$ETC/config.yaml"

rc=0
sudo -u patroni-console bash -c "set -a; . $ETC/env; /opt/patroni-console/venv/bin/patroni-console-check $ETC/config.yaml" || rc=$?
echo "patroni-console-check: код $rc"
[ "$rc" = 0 ] && systemctl restart patroni-console && systemctl is-active patroni-console
exit "$rc"
