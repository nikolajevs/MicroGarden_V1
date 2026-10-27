#!/usr/bin/env bash
# Выкладывает сервер телеметрии на VPS и (пере)запускает его под pm2. Запускается на
# рабочем компьютере из корня репозитория:
#
#   bash dashboard/deploy/deploy.sh                 # по умолчанию через ssh-алиас kicad-vps
#   DASH_SSH=administrator@dvinsk.lat bash dashboard/deploy/deploy.sh
#
# Права root не нужны. Что делает:
#   1. копирует server.py, static/ и deploy/ в ~/dash (база data/ и секреты .env не трогаются);
#   2. при первом запуске создаёт ~/dash/.env со случайными токеном приёма и паролем просмотра;
#   3. запускает или перезапускает приложение под pm2 и проверяет /healthz.
# Секреты в вывод не попадают: прочитать их можно на сервере, см. dashboard/README.md.
set -euo pipefail

HOST="${DASH_SSH:-kicad-vps}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST")

echo "→ копирую файлы на $HOST:~/dash"
tar -C "$SRC" -czf - --exclude=data --exclude=__pycache__ --exclude=.env --exclude=tests --exclude=tools \
  server.py static deploy README.md \
  | "${SSH[@]}" 'mkdir -p ~/dash && tar -xzf - -C ~/dash && chmod 755 ~/dash && chmod 700 ~/dash/deploy/*.sh 2>/dev/null; true'

echo "→ настраиваю и запускаю"
"${SSH[@]}" bash -s <<'REMOTE'
set -euo pipefail
cd ~/dash
mkdir -p data
chmod 700 data

if [ ! -f .env ]; then
  umask 077
  python3 - <<'PY'
import secrets
with open(".env", "w", encoding="utf-8") as f:
    f.write("# Секреты сервера телеметрии. Файл читает только server.py; права 600.\n")
    f.write("DASH_INGEST_TOKEN=%s\n" % secrets.token_urlsafe(32))
    f.write("DASH_VIEW_USER=garden\n")
    f.write("DASH_VIEW_PASSWORD=%s\n" % secrets.token_urlsafe(18))
    f.write("DASH_PORT=8790\n")
PY
  echo "  создан .env с новыми секретами"
fi
chmod 600 .env

PM2="$(ls -d "$HOME"/.nvm/versions/node/*/bin/pm2 2>/dev/null | tail -n 1 || true)"
[ -n "$PM2" ] || PM2="$(command -v pm2 || true)"
if [ -z "$PM2" ]; then
  echo "pm2 не найден" >&2
  exit 1
fi
export PATH="$(dirname "$PM2"):$PATH"
pm2 startOrReload deploy/ecosystem.config.cjs --update-env >/dev/null
pm2 save >/dev/null

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 3 http://127.0.0.1:8790/healthz >/dev/null 2>&1; then
    echo "  сервер отвечает на 127.0.0.1:8790"
    exit 0
  fi
  sleep 1
done
echo "сервер не ответил за 10 с; смотрите: pm2 logs dash --lines 30" >&2
exit 1
REMOTE
echo "✓ приложение запущено на VPS. Чтобы открыть его по адресу, нужны ещё:"
echo "  1. sudo bash ~/dash/deploy/enable-nginx.sh на VPS (виртуальный хост nginx);"
echo "  2. запись A для dash.dvinsk.lat -> 93.127.143.133 в DNS;"
echo "  3. маршрут Traefik на хосте 93.127.143.133 (пример: deploy/traefik-dash.example.yml)."
