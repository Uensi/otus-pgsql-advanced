#!/bin/sh
# Скачивает библиотеки графиков в static/vendor/ для работы консоли без интернета.
set -e
cd "$(dirname "$0")/static/vendor"
curl -fL -o chart.umd.min.js https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js
curl -fL -o luxon.min.js https://cdn.jsdelivr.net/npm/luxon@3.4.4/build/global/luxon.min.js
curl -fL -o chartjs-adapter-luxon.umd.min.js https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.3.1/dist/chartjs-adapter-luxon.umd.min.js
echo "готово: $(ls)"
