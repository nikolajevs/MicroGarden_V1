#!/usr/bin/env bash
# Записывает настройки входа через Google в ~/dash/.env на VPS. Запускается на VPS, нужен терминал, чтобы секрет вводился
# без показа на экране:
#
#   ssh -t kicad-vps 'bash ~/dash/deploy/configure-google.sh'
#
# Что нужно заранее: клиент «Web application» в консоли Google (инструкция в dashboard/README.md, раздел «Вход через
# Google») с адресом возврата <адрес сайта>/auth/google/callback; из консоли берутся идентификатор клиента
# (…apps.googleusercontent.com) и секрет клиента (виден только при создании).
# Что делает: спрашивает идентификатор и секрет (секрет не показывается и нигде не печатается), делает копию .env
# (.env.bak-ДАТА, права 600), правит только ключи Google и адрес сайта, предлагает перезапустить панель и проверяет,
# что кнопка «Войти через Google» включилась.
# Прав root не нужно. Проверка без перезапуска: DASH_ENV_FILE=/путь/к/пробному.env bash configure-google.sh
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env-lib.sh"

echo "Вход через Google: настройки пишутся в $ENV_FILE. Enter оставляет значение в скобках, «-» очищает поле."
ask DASH_PUBLIC_URL "Адрес сайта (из него строится адрес возврата)" "https://dash.dvinsk.lat"
ask DASH_GOOGLE_CLIENT_ID "Идентификатор клиента (…apps.googleusercontent.com)" ""
ask_secret DASH_GOOGLE_CLIENT_SECRET "Секрет клиента (из консоли Google, вид GOCSPX-…)"

NEW_DASH_PUBLIC_URL="${NEW_DASH_PUBLIC_URL%/}"; export NEW_DASH_PUBLIC_URL
check_url "$NEW_DASH_PUBLIC_URL"
if [ -n "$NEW_DASH_GOOGLE_CLIENT_ID$NEW_DASH_GOOGLE_CLIENT_SECRET" ]; then
  id_re='^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$'; secret_re='^[!-~]{10,}$'
  ( LC_ALL=C; [[ $NEW_DASH_GOOGLE_CLIENT_ID =~ $id_re ]] ) || {
    echo "Идентификатор клиента должен быть вида 123456-abc.apps.googleusercontent.com (латиница, цифры, точки и дефисы). Ничего не записано." >&2; exit 1; }
  ( LC_ALL=C; [[ $NEW_DASH_GOOGLE_CLIENT_SECRET =~ $secret_re ]] ) || {
    echo "Секрет клиента слишком короткий, не задан или в нём есть пробелы (он нужен вместе с идентификатором). Ничего не записано." >&2; exit 1; }
  echo "В консоли Google у клиента должен быть адрес возврата: $NEW_DASH_PUBLIC_URL/auth/google/callback"
fi

write_env "# Вход через Google (записано configure-google.sh)"
restart_panel

if [ -z "${DASH_ENV_FILE:-}" ]; then
  port="$(current DASH_PORT)"
  answer="$(curl -fsS --max-time 5 "http://127.0.0.1:${port:-8790}/api/registration" 2>/dev/null || true)"
  case "$answer" in
    *'"google":true'*) echo "Кнопка «Войти через Google» включена: $answer";;
    '') echo "Панель не ответила: проверьте pm2 logs dash --lines 30";;
    *) echo "Вход через Google не включён: $answer. Проверьте ключи в .env и pm2 logs dash --lines 30";;
  esac
fi
