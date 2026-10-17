@echo off
rem Запускает кластер itest и Patroni Console в WSL и держит WSL включённым, пока открыто это окно.
rem (WSL сам выключается, когда к нему никто не подключён, и службы останавливаются вместе с ним.)
rem Консоль: https://localhost:8443/  ·  остановка: закрыть окно или stop.cmd
if "%WSL_DISTRO%"=="" set WSL_DISTRO=Ubuntu-24.04
wsl -d %WSL_DISTRO% -u root -- sh -c "systemctl start itest.target patroni-console && echo 'itest + patroni-console started: https://localhost:8443/ (keep this window open)' && exec sleep infinity"
