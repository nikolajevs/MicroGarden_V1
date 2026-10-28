# MicroGarden_V1

Самостоятельный аппаратный проект контроллера гроубокса на основе **EU-LITE**:
24 В, ESP32-WROOM-32E, один LED-канал LM3429 класса 69 Вт, PTC, помпа,
два PWM-вентилятора, INA238, RTC, microSD, SHT4x и RGB-индикатор.
Плата 100 × 100 мм, четыре медных слоя.

**Статус на 28.10.2026:** проект предназначен для разработки и первого прототипа.
Актуальный комплект Gerber — [r5](hardware/PCB_EU_LITE/fabrication/2026-10-28-r5/README.md):
в нём исправлены земля шунтов INA238, коммутационная ячейка LED и подводка C508. Полный профиль
пройден на KiCad 10.0.5, целевая 10.0.7 этим выпуском не перепроверена.
Стендовая квалификация не закрыта. Подробности — в [аппаратном README](hardware/PCB_EU_LITE/README.md#проверки).

## Открыть проект

1. Скачайте этот репозиторий целиком.
2. Установите KiCad 10 с его стандартными библиотеками. Целевая версия полного
   аппаратного профиля — **10.0.7**; Python `pcbnew` должен быть той же версии.
3. Откройте [PCB_EU_LITE.kicad_pro](hardware/PCB_EU_LITE/PCB_EU_LITE.kicad_pro).
   Нужные собственные символы, посадки и модели включены в `libraries/`.

Клонировать PCB_EU или ai не требуется. Технические имена `PCB_EU_LITE` и UUID
сохранены для совместимости схемы, платы и генераторов.

## Документация и файлы

| Раздел | Содержимое |
|---|---|
| [Аппаратная часть](hardware/PCB_EU_LITE/README.md) | Подключения, закупка, размещение и состояние проверок |
| [Проектные решения](hardware/PCB_EU_LITE/DESIGN.md) | Расчёты, допуски и открытые инженерные вопросы |
| [BOM CSV](hardware/PCB_EU_LITE/BOM_allocated.csv) | Склад и докупка; цены — датированный снимок, не текущая оферта |
| [Первое включение](hardware/PCB_EU_LITE/review/Bench_checklist.md) | Пуск, измерения, нагрев и проверка нагрузок |
| [Производственные файлы](hardware/PCB_EU_LITE/fabrication/README.md) | Условия нового выпуска и ссылки на старые архивы |
| [Происхождение импорта](IMPORT.md) | Исходная ревизия, сохранённые данные и состав очистки |
| [Проверка импорта](VALIDATION.md) | Результаты нового локального прогона и его ограничения |
| [Прошивка](firmware/README.md) | Сборка ESP32, загрузка интерфейса и отличия EU-LITE |
| [Панель телеметрии](dashboard/README.md) | Сервер приёма телеметрии платы и страница с графиками и сообщениями Serial (dash.dvinsk.lat) |
| [Лицензии и источники библиотек](hardware/PCB_EU_LITE/libraries/SOURCES.md) | Атрибуции посадок, символов и моделей |

## Проверить сохранённые исходники

Для проверок спецификации достаточно Python 3.10+. Команды запускаются из корня;
новые результаты сохраняются в `work/`, исторические отчёты не перезаписываются.

```sh
python -c "from pathlib import Path; Path('work').mkdir(exist_ok=True)"
python tools/check_standalone.py
python hardware/PCB_EU_LITE/tools/verify.py --output work/connectivity.txt
python hardware/PCB_EU_LITE/tools/test_verify.py
python hardware/PCB_EU_LITE/tools/check_schematics.py
python -m unittest discover -s hardware/PCB_EU_LITE/tools -p test_led_boost_simulation.py
python -m unittest discover -s hardware/PCB_EU_LITE/tools -p test_snapshot_prices.py
```

Для аппаратных проверок используйте Python из KiCad с зависимостями
`Shapely>=2,<3` и `gerbonara>=1.5,<2`. Установите их в отдельное окружение или
локальный каталог; системный Python сам по себе не содержит `pcbnew`.
В Windows встроенный Python KiCad может игнорировать `PYTHONPATH`. В таком случае
запускайте скрипты через включённую обёртку, явно передавая интерпретатор и каталог
установленных зависимостей:

```powershell
python tools/run_kicad.py --python "C:/Program Files/KiCad/10.0/bin/python.exe" --dependencies work/deps hardware/PCB_EU_LITE/tools/check_power_paths.py
```

В командах ниже `python` означает настроенный Python KiCad (либо такую обёртку),
а `kicad-cli` — исполняемый файл из той же установки KiCad.

```sh
kicad-cli sch export netlist --format kicadxml -o work/netlist.xml hardware/PCB_EU_LITE/PCB_EU_LITE.kicad_sch
kicad-cli sch erc --exit-code-violations --format json -o work/erc.json hardware/PCB_EU_LITE/PCB_EU_LITE.kicad_sch
kicad-cli pcb drc --schematic-parity --format json -o work/drc.json hardware/PCB_EU_LITE/PCB_EU_LITE.kicad_pcb
python hardware/PCB_EU_LITE/tools/check_placement.py work/netlist.xml
python hardware/PCB_EU_LITE/tools/check_power_paths.py
python hardware/PCB_EU_LITE/tools/silkscreen.py --check --output work/silkscreen.json
```

Команда DRC без `--exit-code-violations` может завершиться с кодом 0 при найденных
проблемах: проверяйте содержимое отчёта. Это не признак успешного выпуска.

Полный профиль выполняется в новый каталог:

```sh
python hardware/PCB_EU_LITE/tools/fabricate.py --kicad-cli /path/to/kicad-cli --output work/release
```

Профиль проверяет схему, BOM, геометрию, силовые пути, Kelvin-съём, DRC и
паритет, затем экспортирует Gerber/Excellon и повторно читает их CAM-парсером.
Иная версия KiCad допускается только явно через `--allow-other-version` и не
считается проверкой целевой версии 10.0.7.

## Разработка и границы проекта

Схемы и BOM формирует `hardware/PCB_EU_LITE/tools/generate.py`. Для изменения
генерируемых данных сначала меняйте генератор, затем проверяйте результат.
Регенерацию выполняйте в отдельной рабочей копии: она записывает схемы и отчёты.
`build_board.py NETLIST.xml --output work/unrouted.kicad_pcb` создаёт только
неразведённую заготовку и защищает сохранённую разводку от перезаписи.
Этапные скрипты разводки не являются полным воспроизведением финальной платы.

Прошивка EU-0.1 для EU-LITE находится в [firmware/](firmware/README.md).
Она поддерживает INA238, один LED-канал, отсутствие тахометров, RGB-индикатор
и блокировку света при низком или недостоверном напряжении. Сборка исходника и
программные регрессии не подтверждают работу собранной платы: нагрев, устойчивость
регуляторов, световой каскад и физические защиты проверяются по плану первого
включения до подключения штатной нагрузки.

Временные результаты, зависимости, кэши и локальные настройки исключены через
`.gitignore`. Устаревшие производственные архивы доступны в истории источника.
