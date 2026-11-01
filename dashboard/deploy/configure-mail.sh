#!/usr/bin/env bash
# Записывает настройки почты панели в ~/dash/.env на VPS. Запускается на VPS, нужен терминал, чтобы пароль вводился
# без показа на экране (поэтому ssh с ключом -t):
#
#   ssh -t kicad-vps 'bash ~/dash/deploy/configure-mail.sh'
#
# Что делает:
#   1. спрашивает значения; Enter оставляет текущее, «-» очищает поле. Пароль SMTP (у Resend это API-ключ) не
#      показывается ни при вводе, ни потом; в чат и в историю команд он не попадает;
#   2. делает копию .env рядом (.env.bak-ДАТА, права 600), меняет или добавляет только ключи почты, остальное не трогает;
#   3. предлагает перезапустить панель и отправить пробное письмо (то же, что python3 server.py test-mail адрес).
# Прав root не нужно. Проверка без перезапуска: DASH_ENV_FILE=/путь/к/пробному.env bash configure-mail.sh
set -euo pipefail

DASH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${DASH_ENV_FILE:-$DASH_DIR/.env}"
PY="${DASH_PYTHON:-$(command -v python3 || command -v python || true)}"
[ -n "$PY" ] || { echo "python3 не найден" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "нет файла $ENV_FILE: сначала выложите панель (bash dashboard/deploy/deploy.sh)" >&2; exit 1; }

# Текущее значение ключа из .env (пусто, если ключа нет)
current() {
  "$PY" - "$ENV_FILE" "$1" <<'PY'
import sys
path, key = sys.argv[1], sys.argv[2]
for raw in open(path, encoding="utf-8"):
    line = raw.strip()
    if line.startswith(key + "="):
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        print(value)
        break
PY
}

trim() { local v="${1//$'\r'/}"; v="${v#"${v%%[![:space:]]*}"}"; printf '%s' "${v%"${v##*[![:space:]]}"}"; }

# ask КЛЮЧ "подпись" "значение по умолчанию": Enter оставляет текущее значение из .env, а если его нет, значение по умолчанию
ask() {
  local key="$1" label="$2" fallback="$3" now answer
  now="$(current "$key")"
  now="${now:-$fallback}"
  read -r -p "$label${now:+ [$now]}: " answer || answer=""
  answer="$(trim "$answer")"
  if [ "$answer" = "-" ]; then export "NEW_$key="; else export "NEW_$key=${answer:-$now}"; fi
}

# ask_secret КЛЮЧ "подпись": ввод не виден; Enter оставляет текущее значение. Само значение никогда не печатается
ask_secret() {
  local key="$1" label="$2" now answer
  now="$(current "$key")"
  read -r -s -p "$label${now:+ (сейчас задан, Enter оставит его)}: " answer || answer=""
  echo
  answer="$(trim "$answer")"
  export "NEW_$key=${answer:-$now}"
}

echo "Почта панели: настройки пишутся в $ENV_FILE. Enter оставляет значение в скобках, «-» очищает поле."
ask DASH_PUBLIC_URL "Адрес сайта (в ссылках из писем)" "https://dash.dvinsk.lat"
ask DASH_SMTP_HOST "Сервер SMTP" "smtp.resend.com"
ask DASH_SMTP_PORT "Порт (587 с STARTTLS или 465 с SSL)" "587"
ask DASH_SMTP_USER "Логин SMTP (у Resend: resend)" "resend"
ask_secret DASH_SMTP_PASSWORD "Пароль SMTP (у Resend это API-ключ, начинается с re_)"
ask DASH_MAIL_FROM "Отправитель" "MicroGarden <noreply@dvinsk.lat>"
ask DASH_MAIL_REPLY_TO "Куда писать в ответ (необязательно: этот адрес увидят получатели)" ""

if [ -n "${NEW_DASH_SMTP_USER:-}" ] && [ -z "${NEW_DASH_SMTP_PASSWORD:-}" ]; then
  echo "Нужен пароль SMTP: он задаётся вместе с логином. Ничего не записано." >&2
  exit 1
fi
case "$NEW_DASH_SMTP_PORT" in ''|*[!0-9]*) echo "Порт должен быть числом. Ничего не записано." >&2; exit 1;; esac
case "$NEW_DASH_PUBLIC_URL" in https://*|http://localhost*|http://127.0.0.1*) ;; *) echo "Адрес сайта должен начинаться с https://. Ничего не записано." >&2; exit 1;; esac
NEW_DASH_PUBLIC_URL="${NEW_DASH_PUBLIC_URL%/}"; export NEW_DASH_PUBLIC_URL

# Запись: пустое значение убирает ключ, остальные строки .env остаются как были
"$PY" - "$ENV_FILE" <<'PY'
import os, sys, time
path = sys.argv[1]
updates = {name[4:]: value for name, value in os.environ.items() if name.startswith("NEW_DASH_")}
lines = open(path, encoding="utf-8").read().splitlines()
backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
changed, seen, out = [], set(), []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
    if key in updates:
        seen.add(key)
        if updates[key] == "":
            changed.append("%s: убран" % key)
            continue
        if line.split("=", 1)[1].strip().strip("\"'") != updates[key]:
            changed.append("%s: %s" % (key, "изменён" if key.endswith("PASSWORD") else "→ " + updates[key]))
        out.append("%s=%s" % (key, updates[key]))
    else:
        out.append(line)
missing = [key for key in updates if key not in seen and updates[key] != ""]
if missing:
    out.append("")
    out.append("# Почта (записано configure-mail.sh)")
    for key in missing:
        out.append("%s=%s" % (key, updates[key]))
        changed.append("%s: %s" % (key, "задан" if key.endswith("PASSWORD") else "→ " + updates[key]))
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(out) + "\n")
os.replace(tmp, path)
os.chmod(path, 0o600)
print("Записано в %s (копия прежнего файла: %s)." % (path, os.path.basename(backup)))
print("Изменения:" if changed else "Изменений нет: значения те же.")
for item in changed:
    print("  " + item)
PY

# Перезапуск нужен, чтобы панель прочитала новые настройки; на пробной копии .env его не предлагаем
if [ -z "${DASH_ENV_FILE:-}" ]; then
  PM2="$(ls -d "$HOME"/.nvm/versions/node/*/bin/pm2 2>/dev/null | tail -n 1 || true)"
  [ -n "$PM2" ] || PM2="$(command -v pm2 || true)"
  if [ -z "$PM2" ]; then
    echo "pm2 не найден: перезапустите панель вручную, чтобы она прочитала новые настройки."
  else
    read -r -p "Перезапустить панель сейчас? [Y/n] " go || go=""
    if [ "${go:-Y}" != "n" ] && [ "${go:-Y}" != "N" ]; then
      "$PM2" restart dash >/dev/null && echo "Панель перезапущена."
    fi
  fi
fi

read -r -p "Адрес для пробного письма (Enter — пропустить): " to || to=""
to="$(trim "$to")"
if [ -n "$to" ]; then
  # Настройки читаются из только что записанного файла, а не из окружения оболочки
  (cd "$DASH_DIR" && "$PY" -c 'import os, sys; sys.path.insert(0, "."); import server; server.load_env_file(sys.argv[1]); sys.exit(server.test_mail(server.Config(os.environ), sys.argv[2]))' "$ENV_FILE" "$to") || true
fi
