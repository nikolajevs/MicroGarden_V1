#!/usr/bin/env bash
# Удаляет старые копии базы панели. Политика конфиденциальности обещает хранить резервные копии не дольше 90 суток, а данные,
# которые человек стёр на сайте, остаются в копиях, пока те не удалены. Автоматически скрипт не запускается: его запускает
# владелец, например раз в месяц. По умолчанию он только показывает, что удалил бы:
#
#   ssh kicad-vps 'bash ~/dash/deploy/prune-backups.sh'            # показать
#   ssh kicad-vps 'bash ~/dash/deploy/prune-backups.sh --delete'   # удалить
#
# Какие файлы считаются копиями: ~/dash-backup-before-*.sqlite3 (так их называют копии перед выкладкой с изменением базы).
# Возраст считается по времени изменения файла. Срок: DASH_BACKUP_DAYS (по умолчанию 90). Скрипт смотрит только в домашний
# каталог (DASH_BACKUP_DIR меняет его), не заходит в подкаталоги, не трогает ничего, кроме обычных файлов с таким именем,
# и не касается рабочей базы ~/dash/data. Файл удаляется обычным rm: гарантии стирания блоков на диске VPS это не даёт.
set -euo pipefail

DIR="${DASH_BACKUP_DIR:-$HOME}"
DAYS="${DASH_BACKUP_DAYS:-90}"
MODE="show"
case "${1:-}" in
  '') ;;
  --delete) MODE="delete";;
  *) echo "Использование: prune-backups.sh [--delete]" >&2; exit 2;;
esac
[ "$#" -le 1 ] || { echo "Использование: prune-backups.sh [--delete]" >&2; exit 2; }
case "$DAYS" in ''|*[!0-9]*|0|0[0-9]*) echo "DASH_BACKUP_DAYS должно быть целым числом суток больше нуля, сейчас: '$DAYS'" >&2; exit 2;; esac
[ -d "$DIR" ] || { echo "Нет каталога $DIR" >&2; exit 1; }

# Возраст в минутах, а не в сутках: «-mtime +90» оставляет копии до 91 суток, а политика обещает не больше 90
LIMIT=$((DAYS * 1440))
old=()
while IFS= read -r -d '' file; do old+=("$file"); done < <(find "$DIR" -maxdepth 1 -type f -name 'dash-backup-before-*.sqlite3' -mmin "+$LIMIT" -print0 | sort -z)
kept="$(find "$DIR" -maxdepth 1 -type f -name 'dash-backup-before-*.sqlite3' ! -mmin "+$LIMIT" | wc -l | tr -d ' ')"

if [ "${#old[@]}" -eq 0 ]; then
  echo "Копий старше $DAYS суток нет (новее: $kept). Удалять нечего."
  exit 0
fi
for file in "${old[@]}"; do
  if [ "$MODE" = "delete" ]; then
    rm -f -- "$file"
    echo "удалена: $(basename "$file")"
  else
    echo "старше $DAYS суток: $(basename "$file")"
  fi
done
if [ "$MODE" = "delete" ]; then
  echo "Удалено копий: ${#old[@]}, осталось: $kept."
else
  echo "Показано копий: ${#old[@]} (новее $DAYS суток: $kept). Чтобы удалить их: prune-backups.sh --delete"
fi
