#!/usr/bin/env bash
# Записывает в ~/dash/.env сведения об операторе панели: они подставляются в политику конфиденциальности (/privacy) и в
# условия использования (/terms), которые видят все, и в письмо об удалении записи. Запускается на VPS:
#
#   ssh -t kicad-vps 'bash ~/dash/deploy/configure-site.sh'
#
# Что нужно решить заранее: под каким именем или названием вы выступаете оператором сервиса и на какой адрес почты люди
# смогут писать по своим данным (удалить, исправить, пожаловаться). Оба значения публичные: пароли и ключи сюда не вводятся.
# Ввод только латиницей и обычными знаками, как в остальных скриптах (невидимый знак при вставке испортил бы значение);
# имя кириллицей можно вписать в .env вручную, сервер его принимает.
# Что делает: спрашивает два значения (Enter оставляет текущее, «-» очищает поле), проверяет их теми же правилами, что и сервер,
# делает копию .env (.env.bak-ДАТА, права 600), правит только DASH_OPERATOR_NAME и DASH_CONTACT_EMAIL, предлагает перезапустить
# панель и проверяет, что на странице /privacy не осталось красных пометок «не указан».
# Прав root не нужно. Проверка без перезапуска: DASH_ENV_FILE=/путь/к/пробному.env bash configure-site.sh
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env-lib.sh"

echo "Оператор панели: значения пишутся в $ENV_FILE и будут видны всем на страницах /privacy и /terms."
echo "Enter оставляет значение в скобках, «-» очищает поле."
ask DASH_OPERATOR_NAME "Имя или название оператора (латиницей, до 100 символов)" ""
ask DASH_CONTACT_EMAIL "Почта для обращений по данным (name@example.com)" ""

# Проверка теми же правилами, что при запуске сервера (server.py: Config.site_problems): одно место для правил
problems="$(cd "$DASH_DIR" && PYTHONIOENCODING=utf-8 DASH_OPERATOR_NAME="$NEW_DASH_OPERATOR_NAME" DASH_CONTACT_EMAIL="$NEW_DASH_CONTACT_EMAIL" \
  "$PY" -c 'import os, sys; sys.path.insert(0, "."); import server; print("\n".join(server.Config(os.environ).site_problems()))')" \
  || { echo "Не удалось проверить значения: server.py не запускается. Ничего не записано." >&2; exit 1; }
if [ -n "$problems" ]; then
  echo "$problems" >&2
  echo "Ничего не записано." >&2
  exit 1
fi

write_env "# Оператор для политики конфиденциальности и условий (записано configure-site.sh)"
restart_panel

if [ -z "${DASH_ENV_FILE:-}" ]; then
  port="$(current DASH_PORT)"
  page="$(curl -fsS --max-time 5 "http://127.0.0.1:${port:-8790}/privacy" 2>/dev/null || true)"
  case "$page" in
    '') echo "Панель не ответила: проверьте pm2 logs dash --lines 30";;
    *'class="todo"'*) echo "На странице /privacy ещё есть пометки «не указан»: задайте оба значения и перезапустите панель.";;
    *) echo "Страница /privacy заполнена: откройте её и прочитайте текст целиком, прежде чем на него ссылаться.";;
  esac
fi
