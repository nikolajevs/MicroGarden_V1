#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Заполняет базу выдуманной неделей телеметрии, чтобы посмотреть страницу без платы.

    python dashboard/tools/seed_demo.py --db work/dash-demo.sqlite3 --days 7

Данные синтетические: суточный ход температуры, свет с 06:00 до 18:00, полив в 08:00,
нагреватель ночью, просадка питания, простой связи на 40 минут и несколько сообщений
об ошибках. В рабочую базу на VPS их заливать не нужно.
"""
import argparse
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import Store  # noqa: E402

BOOT = "demo0001"


def build(store, days=7, now=None, device="microgarden-demo", step=60):
    now = int(time.time() if now is None else now)
    rnd = random.Random(7)
    start = now - days * 86400
    outage = (now - 30 * 3600, now - 30 * 3600 + 40 * 60)       # плата без связи
    sag = (now - 20 * 3600, now - 20 * 3600 + 12 * 60)          # просадка питания
    sensor_fail = (now - 9 * 3600, now - 9 * 3600 + 6 * 60)     # датчик не отвечает
    offset = 0
    text_at = {}
    text_at[start + 60] = ["[I2C] Отвечают устройств: 3: 0x40 (INA238) 0x44 (SHT4x) 0x6F (MCP7940N)",
                           "[INA238] Найден на 0x40, измерение мощности включено."]
    text_at[sag[0]] = ["[INA238] Не отвечает — мощность не показывается, повтор раз в 30 с."]
    text_at[sensor_fail[0]] = ["[SHT4x] Датчик перестал отвечать (getEvent вернул ошибку).",
                               "[SHT4x] Диагностика: нет ответа на 0x44 (код I2C 2)",
                               "[АВАРИЙНЫЙ РЕЖИМ]: Отказ SHT4x! Климат зафиксирован на безопасных уровнях."]
    text_at[sensor_fail[1] + 60] = ["[SHT4x] Попытка восстановления 3/3 успешна",
                                    "[SHT4x] Датчик восстановлен, выходим из аварийного режима."]
    pending = sorted(text_at.items())
    for ts in range(start, now, step):
        if outage[0] <= ts < outage[1]:
            continue
        local = (ts % 86400) / 3600.0
        day = 6 <= local < 18
        temp = 22.5 + 3.2 * math.sin((local - 9) / 24 * 2 * math.pi) + rnd.uniform(-.15, .15)
        hum = 58 - 8 * math.sin((local - 9) / 24 * 2 * math.pi) + rnd.uniform(-.8, .8)
        led = int(210 + 30 * math.sin(ts / 5000)) if day else 0
        fan = int(max(0, min(255, 90 + (temp - 22) * 40)))
        volts = 24.0 + rnd.uniform(-.12, .12)
        if sag[0] <= ts < sag[1]:
            volts = 21.1 + rnd.uniform(-.1, .1)
            led = 0
        watts = (led / 255.0) * 68 + fan / 255.0 * 6 + 3.5 + rnd.uniform(-.4, .4)
        heater = (not day) and temp < 21.8
        if heater:
            watts += 88
        sht = not (sensor_fail[0] <= ts < sensor_fail[1])
        sample = {
            "temp": round(temp, 2), "hum": round(hum, 1), "led": led, "fan1": fan, "fan2": fan,
            "bus_v": round(volts, 2), "current_a": round(watts / volts, 3), "power_w": round(watts, 1),
            "energy_kwh": round(watts * (ts % 86400) / 3600000.0, 3),
            "pump": 8 * 3600 <= ts % 86400 < 8 * 3600 + 60, "heater": heater,
            "led_blocked": sag[0] <= ts < sag[1], "sht_ok": sht, "rtc_ok": True, "clock_trusted": True,
            "sd_ok": True, "power_ok": not (sag[0] <= ts < sag[0] + 60), "water_sensor": True, "water_present": True,
            "rssi": -58 + rnd.randint(-4, 4), "uptime_s": ts - start, "fw": "demo",
        }
        lines = []
        while pending and pending[0][0] <= ts:
            lines += pending.pop(0)[1]
        body = {"device": device, "sample": sample}
        if lines:
            text = "\n".join(lines) + "\n"
            body["log"] = {"boot": BOOT, "from": offset, "text": text}
            offset += len(text.encode("utf-8"))
        store.ingest(device, body, now=ts)
    return device


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="путь к файлу SQLite (будет создан)")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--step", type=int, default=60, help="интервал между записями, с (по умолчанию 60)")
    args = parser.parse_args()
    store = Store(args.db)
    device = build(store, args.days, step=args.step)
    print("готово: плата %s, %d сут., база %s" % (device, args.days, args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
