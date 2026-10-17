# Скачивает на Windows всё, что нужно для установки в WSL без интернета
# (например, когда VPN на Windows не пропускает трафик WSL).
#   powershell -ExecutionPolicy Bypass -File fetch-wheels.ps1 -Out D:\patroni\offline
# Результат: <Out>\console-wheels, <Out>\itest-wheels, <Out>\etcd-<ver>-linux-amd64.tar.gz
param(
  [string]$Out = (Join-Path $PSScriptRoot 'offline'),
  [string]$PyVer = '3.12',          # версия python3 в WSL: python3 --version
  [string]$EtcdVer = 'v3.6.15'
)
$ErrorActionPreference = 'Stop'
$plat = @('--platform', 'manylinux2014_x86_64', '--platform', 'manylinux_2_17_x86_64',
          '--platform', 'manylinux_2_28_x86_64', '--python-version', $PyVer,
          '--implementation', 'cp', '--only-binary=:all:')
New-Item -ItemType Directory -Force "$Out\console-wheels", "$Out\itest-wheels" | Out-Null

# консоль
python -m pip download -q -d "$Out\console-wheels" @plat `
  'Flask>=3.1,<4' 'requests>=2.32.4,<3' 'PyYAML>=6.0.2,<7' 'gunicorn>=23,<24' 'setuptools>=68' wheel pip

# Patroni 4 + etcd3. ydiff<1.5 и python-etcd есть на PyPI только исходниками:
# качаем их tar.gz напрямую со сверкой sha256 (без запуска setup.py на Windows).
python -m pip download -q -d "$Out\itest-wheels" @plat `
  'patroni>=4,<5' psycopg2-binary dnspython 'py-consul>=1.1.1' 'setuptools>=68' wheel pip
Remove-Item "$Out\itest-wheels\ydiff-1.5*.whl" -ErrorAction SilentlyContinue
foreach ($p in @(@('ydiff', '1.4.2'), @('python-etcd', '0.4.5'))) {
  $j = Invoke-RestMethod -TimeoutSec 30 "https://pypi.org/pypi/$($p[0])/$($p[1])/json"
  $u = ($j.urls | Where-Object packagetype -eq 'sdist')[0]
  $dst = Join-Path "$Out\itest-wheels" $u.filename
  Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 $u.url -OutFile $dst
  if ((Get-FileHash $dst -Algorithm SHA256).Hash.ToLower() -ne $u.digests.sha256) { throw "sha256 mismatch: $dst" }
}
# лишние версии Patroni, которые резолвер мог скачать при переборе
Get-ChildItem "$Out\itest-wheels\patroni-*.whl" | Sort-Object { [version]($_.Name -split '-')[1] } |
  Select-Object -SkipLast 1 | Remove-Item

# etcd
$tgz = Join-Path $Out "etcd-$EtcdVer-linux-amd64.tar.gz"
if (-not (Test-Path $tgz)) {
  Invoke-WebRequest -UseBasicParsing -TimeoutSec 600 `
    "https://github.com/etcd-io/etcd/releases/download/$EtcdVer/etcd-$EtcdVer-linux-amd64.tar.gz" -OutFile $tgz
}
Get-ChildItem -Recurse $Out | Select-Object FullName, Length
