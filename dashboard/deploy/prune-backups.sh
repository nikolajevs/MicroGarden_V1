#!/usr/bin/env bash
# Удаляет старые копии базы панели. Политика конфиденциальности обещает хранить резервные копии не дольше 90 суток, а данные,
# которые человек стёр на сайте, остаются в копиях, пока те не удалены. Автоматически скрипт не запускается: его запускает
# владелец, например раз в месяц. По умолчанию он только показывает, что удалил бы:
#
#   ssh kicad-vps 'bash ~/dash/deploy/prune-backups.sh'            # показать
#   ssh kicad-vps 'bash ~/dash/deploy/prune-backups.sh --delete'   # удалить
#
# Какие файлы считаются копиями: ~/dash-backup-before-*.sqlite3 (так их называют копии перед выкладкой с изменением базы) и
# их «хвосты» -wal и -shm: SQLite создаёт их рядом с копией, когда её открывают (например, чтобы проверить целостность).
# Хвосты копии, которая удаляется, уходят вместе с ней, даже если моложе срока; хвост без копии удаляется по своему возрасту.
# Возраст считается по времени изменения файла. Срок: DASH_BACKUP_DAYS (по умолчанию 90). Скрипт смотрит только в домашний
# каталог (DASH_BACKUP_DIR меняет его), не заходит в подкаталоги, не трогает ничего, кроме обычных файлов с такими именами,
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
backup_files() {
  find "$DIR" -maxdepth 1 -type f \( -name 'dash-backup-before-*.sqlite3' -o -name 'dash-backup-before-*.sqlite3-wal' -o -name 'dash-backup-before-*.sqlite3-shm' \) "$@"
}

old=()
while IFS= read -r -d '' file; do old+=("$file"); done < <(backup_files -mmin "+$LIMIT" -print0)
copies=0
extra=()
for file in ${old[@]+"${old[@]}"}; do
  case "$file" in
    *.sqlite3)
      copies=$((copies + 1))
      for tail in -wal -shm; do
        if [ -f "$file$tail" ]; then extra+=("$file$tail"); fi
      done;;
  esac
done
all=()
if [ "${#old[@]}" -gt 0 ]; then
  while IFS= read -r -d '' file; do all+=("$file"); done < <(printf '%s\0' "${old[@]}" ${extra[@]+"${extra[@]}"} | sort -zu)
fi
kept="$(backup_files -name '*.sqlite3' ! -mmin "+$LIMIT" -print | wc -l | tr -d ' ')"

if [ "${#all[@]}" -eq 0 ]; then
  echo "Копий старше $DAYS суток нет (новее: $kept). Удалять нечего."
  exit 0
fi
for file in "${all[@]}"; do
  if [ "$MODE" = "delete" ]; then
    rm -f -- "$file"
    echo "удалён: $(basename "$file")"
  else
    echo "старше $DAYS суток: $(basename "$file")"
  fi
done
if [ "$MODE" = "delete" ]; then
  echo "Удалено копий: $copies, файлов: ${#all[@]}, осталось копий: $kept."
else
  echo "Показано копий: $copies, файлов вместе с хвостами -wal и -shm: ${#all[@]} (новее $DAYS суток: $kept). Чтобы удалить их: prune-backups.sh --delete"
fi
