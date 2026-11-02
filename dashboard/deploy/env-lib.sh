# Общие функции скриптов configure-mail.sh и configure-google.sh: ввод значений и запись в ~/dash/.env. Сам по себе не
# запускается (его подключают через source). Пароли и ключи вводятся без показа и нигде не печатаются.
# Для проверки без перезапуска и без боевого файла: DASH_ENV_FILE=/путь/к/пробному.env, DASH_PYTHON=python.

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

# Только латиница, цифры и обычные знаки: русская буква, похожая на латинскую, или невидимый знак при вставке сделали бы
# значение (например, адрес сайта) неверным, а побитый байт мог испортить сам .env. Любой байт вне диапазона от пробела
# до «~» не проходит. Диапазон записан явно, а не классом [[:print:]]: в некоторых оболочках класс пропускает байты
# выше 0x7F (в Git Bash на Windows проверено).
ascii_ok() ( LC_ALL=C; re='^[ -~]*$'; [[ $1 =~ $re ]] )

BAD_INPUT="В ответе есть символы вне латиницы и обычных знаков (так бывает при вставке: невидимый знак или русская буква, похожая на латинскую). Введите значение ещё раз"

# ask КЛЮЧ "подпись" "значение по умолчанию": Enter оставляет текущее значение из .env, а если его нет, значение по умолчанию;
# «-» очищает поле. Ответ попадает в переменную NEW_КЛЮЧ, которую потом читает write_env. Неподходящие символы в ответе
# не принимаются: вопрос повторяется (три попытки, потом выход без записи).
ask() {
  local key="$1" label="$2" fallback="$3" now answer tries=0
  now="$(current "$key")"
  now="${now:-$fallback}"
  while :; do
    read -r -p "$label${now:+ [$now]}: " answer || answer=""
    answer="$(trim "$answer")"
    ascii_ok "$answer" && break
    tries=$((tries + 1))
    [ "$tries" -lt 3 ] || { echo "$BAD_INPUT: три попытки не вышли. Ничего не записано." >&2; exit 1; }
    echo "$BAD_INPUT (Enter оставит значение в скобках)." >&2
  done
  if [ "$answer" = "-" ]; then export "NEW_$key="; else export "NEW_$key=${answer:-$now}"; fi
}

# ask_secret КЛЮЧ "подпись": ввод не виден; Enter оставляет текущее значение. Само значение никогда не печатается
ask_secret() {
  local key="$1" label="$2" now answer tries=0
  now="$(current "$key")"
  while :; do
    read -r -s -p "$label${now:+ (сейчас задан, Enter оставит его)}: " answer || answer=""
    echo
    answer="$(trim "$answer")"
    ascii_ok "$answer" && break
    tries=$((tries + 1))
    [ "$tries" -lt 3 ] || { echo "$BAD_INPUT: три попытки не вышли. Ничего не записано." >&2; exit 1; }
    echo "$BAD_INPUT." >&2
  done
  export "NEW_$key=${answer:-$now}"
}

# check_url "адрес": адрес сайта без пути и слэша на конце (как требует сервер, server.py: PUBLIC_URL_RE)
check_url() {
  local re='^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$'
  ( LC_ALL=C; [[ $1 =~ $re ]] ) || { echo "Адрес сайта должен быть вида https://dash.dvinsk.lat (без пути и без слэша на конце). Ничего не записано." >&2; exit 1; }
  case "$1" in https://*|http://localhost*|http://127.0.0.1*) ;; *) echo "Адрес сайта должен начинаться с https://. Ничего не записано." >&2; exit 1;; esac
}

# Запись: ключи NEW_DASH_* заменяют или добавляют строки в .env, пустое значение убирает ключ, остальные строки остаются
# как были. Перед записью рядом остаётся копия (.env.bak-ДАТА, права 600), сам файл остаётся с правами 600.
write_env() {
  "$PY" - "$ENV_FILE" "$1" <<'PY'
import os, sys, time
path, header = sys.argv[1], sys.argv[2]
# Значения читаются как байты и строго как UTF-8: побитый байт не должен попасть в .env (сервер не смог бы его прочитать)
raw_env = os.environb if hasattr(os, "environb") else {k.encode(): v.encode("utf-8", "surrogateescape") for k, v in os.environ.items()}
updates = {}
for name, raw in raw_env.items():
    if name.startswith(b"NEW_DASH_"):
        try:
            updates[name[4:].decode("ascii")] = raw.decode("utf-8")
        except UnicodeDecodeError:
            print("В значении %s есть недопустимые байты. Ничего не записано: запустите скрипт заново и введите значение ещё раз."
                  % name[4:].decode("ascii", "replace"), file=sys.stderr)
            sys.exit(1)
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except UnicodeDecodeError:
    print("Файл %s не читается как UTF-8 (в нём есть побитые байты). Ничего не записано." % path, file=sys.stderr)
    sys.exit(1)
secret = lambda key: key.endswith(("PASSWORD", "SECRET"))
changed, seen, out = [], set(), []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
    if key in updates:
        seen.add(key)
        if updates[key] == "":
            changed.append("%s: убран" % key)
            continue
        if line.split("=", 1)[1].strip().strip("\"'") != updates[key]:
            changed.append("%s: %s" % (key, "изменён" if secret(key) else "→ " + updates[key]))
        out.append("%s=%s" % (key, updates[key]))
    else:
        out.append(line)
missing = [key for key in updates if key not in seen and updates[key] != ""]
if missing:
    out.append("")
    out.append(header)
    for key in missing:
        out.append("%s=%s" % (key, updates[key]))
        changed.append("%s: %s" % (key, "задан" if secret(key) else "→ " + updates[key]))
if not changed:
    print("Изменений нет: значения те же, файл не тронут.")
    sys.exit(0)
data = ("\n".join(out) + "\n").encode("utf-8")      # до создания любых файлов: если не кодируется, на диске ничего не меняется
backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as handle:
    handle.write(("\n".join(lines) + "\n").encode("utf-8"))
tmp = path + ".tmp"
try:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.remove(tmp)
os.chmod(path, 0o600)
print("Записано в %s (копия прежнего файла: %s)." % (path, os.path.basename(backup)))
print("Изменения:")
for item in changed:
    print("  " + item)
PY
}

# Перезапуск нужен, чтобы панель прочитала новые настройки; на пробной копии .env его не предлагаем
restart_panel() {
  [ -z "${DASH_ENV_FILE:-}" ] || return 0
  local pm2 go
  pm2="$(ls -d "$HOME"/.nvm/versions/node/*/bin/pm2 2>/dev/null | tail -n 1 || true)"
  [ -n "$pm2" ] || pm2="$(command -v pm2 || true)"
  if [ -z "$pm2" ]; then
    echo "pm2 не найден: перезапустите панель вручную, чтобы она прочитала новые настройки."
    return 0
  fi
  read -r -p "Перезапустить панель сейчас? [Y/n] " go || go=""
  if [ "${go:-Y}" != "n" ] && [ "${go:-Y}" != "N" ]; then
    "$pm2" restart dash >/dev/null && echo "Панель перезапущена."
    sleep 2
  fi
}
