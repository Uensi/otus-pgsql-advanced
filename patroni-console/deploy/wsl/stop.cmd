@echo off
rem Останавливает Patroni Console и кластер itest в WSL (данные сохраняются).
if "%WSL_DISTRO%"=="" set WSL_DISTRO=Ubuntu-24.04
wsl -d %WSL_DISTRO% -u root -- systemctl stop patroni-console itest.target itest-patroni@1 itest-patroni@2 itest-patroni@3 itest-etcd@1 itest-etcd@2 itest-etcd@3
