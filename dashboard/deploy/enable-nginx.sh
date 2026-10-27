#!/usr/bin/env bash
# Подключает dash.dvinsk.lat к nginx на VPS. Нужны права root, поэтому запускается вручную:
#
#   sudo bash ~/dash/deploy/enable-nginx.sh
#
# Добавляет один файл конфигурации и перезагружает nginx; чужие сайты (todo.dvinsk.lat и
# остальные) не меняются. Сертификат здесь не выпускается: HTTPS обслуживает Traefik на
# хосте 93.127.143.133. Чтобы адрес заработал снаружи, нужны ещё запись A в DNS и маршрут в
# Traefik, см. dashboard/README.md.
set -euo pipefail

DOMAIN="${DASH_DOMAIN:-dash.dvinsk.lat}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE="/etc/nginx/sites-available/${DOMAIN}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash $0" >&2
  exit 1
fi

# Приложение должно уже отвечать: иначе nginx будет отдавать 502.
if ! curl -fsS --max-time 5 http://127.0.0.1:8790/healthz >/dev/null; then
  echo "Сервер телеметрии не отвечает на 127.0.0.1:8790. Сначала запустите его (deploy.sh)." >&2
  exit 1
fi

if [ -e "$SITE" ] && ! cmp -s <(sed "s/dash\.dvinsk\.lat/${DOMAIN}/g" "$HERE/nginx-dash.dvinsk.lat.conf") "$SITE"; then
  cp -a "$SITE" "${SITE}.bak.$(date +%Y%m%d-%H%M%S)"
  echo "Прежняя конфигурация сохранена рядом с .bak.*"
fi
sed "s/dash\.dvinsk\.lat/${DOMAIN}/g" "$HERE/nginx-dash.dvinsk.lat.conf" > "$SITE"
chmod 644 "$SITE"
ln -sfn "$SITE" "/etc/nginx/sites-enabled/${DOMAIN}"

if ! nginx -t; then
  rm -f "/etc/nginx/sites-enabled/${DOMAIN}"
  echo "nginx отклонил конфигурацию; хост отключён, остальные сайты не затронуты." >&2
  exit 1
fi
systemctl reload nginx

# Проверка через nginx локально: нужный Host попадает именно на этот хост.
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H "Host: ${DOMAIN}" http://127.0.0.1/healthz || true)"
if [ "$code" = "200" ]; then
  echo "✓ nginx проксирует ${DOMAIN} на сервер телеметрии (/healthz = 200)"
else
  echo "nginx ответил ${code:-ничего}, ожидалось 200" >&2
  exit 1
fi
