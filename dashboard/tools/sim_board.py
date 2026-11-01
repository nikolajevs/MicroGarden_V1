#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Имитация платы с настройками из панели: без железа проверяет страницу «Настройки платы» и обмен с сервером.

Раз в --period секунд шлёт на сервер показания и раздел cfg (настройки платы по умолчанию), как это делает прошивка,
а запросы на изменение из ответа применяет по тем же правилам: только если разрешено (--cloud on), целиком или
никак, с проверкой пределов. Состояние держит в памяти: после перезапуска настройки снова по умолчанию.

  python dashboard/tools/sim_board.py --url http://127.0.0.1:8790/api/ingest --token mgd_... --device microgarden-sim
  python dashboard/tools/sim_board.py ... --cloud off      # плата не разрешает менять настройки из панели
  python dashboard/tools/sim_board.py ... --refuse         # плата отклоняет каждый запрос (проверка вида отказа)
Токен выдаёт страница /boards при добавлении платы с этим именем. Только стандартная библиотека.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402  (пределы и подписи настроек те же, что на сервере)

DEFAULTS = {
    "operation_mode": 0, "temp_target": 25.0, "temp_target_night": 17.0, "temp_delta": 2.0,
    "hum_target_night": 55.0, "hum_band_night": 10.0,
    "drying_temp_target": 16.0, "drying_temp_delta": 0.5, "drying_hum_target": 55.0, "drying_hum_band": 5.0,
    "led_on_hour": 6, "led_on_minute": 0, "led_off_hour": 18, "led_off_minute": 0,
    "led_min_limit": 10, "led_max_limit": 100, "fan1_min_limit": 20, "fan1_max_limit": 100,
    "fan2_min_limit": 20, "fan2_max_limit": 100, "heater_mode": 3, "rgb_brightness": 19, "rgb_night_mode": 0,
    "watering_days": 0, "watering_hour": 8, "watering_minute": 0, "watering_duration": 30,
    "start_time": 0, "water_sensor_enabled": False,
}


class SimBoard:
    def __init__(self, cloud, refuse):
        self.values = dict(DEFAULTS)
        self.cloud, self.refuse = cloud, refuse
        self.last_id, self.result = "", ""

    def report(self):
        return {"cloud": self.cloud, "id": self.last_id, "res": self.result, "v": self.values}

    def handle(self, request):
        """Запрос панели {"id", "set"} -> применён ли. Как в прошивке: нет разрешения или тот же номер — ничего."""
        if not self.cloud or request.get("id") == self.last_id:
            return
        patch = request.get("set") or {}
        reason = ""
        try:
            if self.refuse:
                raise ValueError("range:" + next(iter(patch), "x"))
            clean = {name: server.clean_cfg_value(name, value) for name, value in patch.items()}
            server.check_cfg_order({**self.values, **clean})
        except ValueError as err:
            reason = str(err) if str(err).startswith("range:") else "range:" + next(iter(patch), "x")
        self.last_id, self.result = request["id"], reason or "ok"
        if not reason:
            self.values.update(clean)
        print("[Cloud] %s %s: %s" % ("применено" if not reason else "отклонено (" + reason + ")", request["id"], patch))


def send(url, token, device, board):
    body = {"device": device, "cfg": board.report(),
            "sample": {"temp": 23.4, "hum": 61.0, "led": 128, "fan1": 80, "fan2": 80, "sht_ok": True, "rtc_ok": True,
                       "op_mode": board.values["operation_mode"], "fw": "sim-1"}}
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json",
                                                                  "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8790/api/ingest")
    parser.add_argument("--token", required=True)
    parser.add_argument("--device", default="microgarden-sim")
    parser.add_argument("--period", type=float, default=5.0, help="секунд между отправками (плата шлёт раз в 60)")
    parser.add_argument("--cloud", choices=("on", "off"), default="on")
    parser.add_argument("--refuse", action="store_true")
    args = parser.parse_args()
    board = SimBoard(args.cloud == "on", args.refuse)
    print("Плата %s шлёт на %s каждые %g с; настройки из панели: %s" % (args.device, args.url, args.period, args.cloud))
    while True:
        try:
            reply = send(args.url, args.token, args.device, board)
            if isinstance(reply.get("cfg"), dict):
                board.handle(reply["cfg"])
        except (urllib.error.URLError, OSError, ValueError) as err:
            print("отправка не удалась:", err)
        time.sleep(args.period)


if __name__ == "__main__":
    main()
