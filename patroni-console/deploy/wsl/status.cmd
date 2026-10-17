@echo off
rem Состояние кластера itest и проверка Patroni Console (patroni-console-check, только чтение).
if "%WSL_DISTRO%"=="" set WSL_DISTRO=Ubuntu-24.04
wsl -d %WSL_DISTRO% -u root -- sh -c "patronictl -c /etc/itest/n1.yml list; sudo -u patroni-console bash -c 'set -a; . /etc/patroni-console/env; /opt/patroni-console/venv/bin/patroni-console-check /etc/patroni-console/config.yaml'; systemctl is-active patroni-console"
