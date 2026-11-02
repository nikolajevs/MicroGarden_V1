# -*- coding: utf-8 -*-
"""Тесты сервера телеметрии: python -m unittest discover -s dashboard/tests -v"""
import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402
from tools import seed_demo  # noqa: E402,F401  (проверяем, что заливщик демо-данных импортируется)

server.SCRYPT_N = 2 ** 10   # тесты считают пароли быстрее; рабочую стоимость проверяет PasswordTests.test_production_cost
TOKEN = "t" * 32
PASSWORD = "p" * 16
NOW = 1_800_000_000


def make_config(db):
    return server.Config({"DASH_INGEST_TOKEN": TOKEN, "DASH_VIEW_PASSWORD": PASSWORD,
                          "DASH_PORT": "0", "DASH_DB": str(db)})


class ClassifyTests(unittest.TestCase):
    def test_real_board_messages(self):
        cases = {
            "ОШИБКА: SHT4x не найден после 5 попыток!": "error",
            "[КРИТИКА] RTC вернул некорректную дату! Переход на программный таймер.": "error",
            "[SHT4x] Датчик перестал отвечать (getEvent вернул ошибку).": "error",
            "[АВАРИЙНЫЙ РЕЖИМ]: Отказ SHT4x! Климат зафиксирован на безопасных уровнях.": "error",
            "[INA238] Не отвечает — мощность не показывается, повтор раз в 30 с.": "error",
            "[ПОЛИВ] Аварийная остановка: вода закончилась во время полива": "error",
            "[Heater] Off: temperature reached the 40 C hard limit.": "error",
            "[Watering] NVS write failed; pump remains off.": "error",
            "[RTC] Установка: кварц Y301 не запустился за 3 с (OSCRUN=0). Проверьте кварц и его пайку.": "error",
            "[RTC] Установка: часы пошли, но проверочное чтение не совпало со временем.": "error",
            "[I2C] Восстановить шину не удалось — SDA всё ещё LOW.": "error",
            "[SHT4x] Попытка инициализации 2/5 не удалась, повтор через 200мс...": "warn",
            "SD-карта не обнаружена.": "warn",
            "[I2C] SDA удерживается в LOW — пробуем восстановить шину...": "warn",
            "[ПОЛИВ] Пропущен: датчик не видит воду в баке": "warn",
            "[Core 0] Нет связи с роутером — повторим через 2 минуты.": "warn",
            "[SHT4x] Датчик восстановлен, выходим из аварийного режима.": "info",
            "[RTC] MCP7940N восстановлен, выходим из программного таймера.": "info",
            "[SHT4x] Попытка восстановления 1/3 успешна": "info",
            "[INA238] Найден на 0x40, измерение мощности включено.": "info",
            "[ПОЛИВ] Старт автополива": "info",
            "[Drying] Полив остановлен при выборе режима сушки.": "info",
            "[Core 0] Роутер: подключено, веб-интерфейс на http://192.168.1.50/": "info",
        }
        for line, level in cases.items():
            self.assertEqual(server.classify(line), level, line)


class SampleTests(unittest.TestCase):
    def test_out_of_range_and_wrong_types_are_dropped(self):
        cols, extra = server.clean_sample({
            "temp": 500, "hum": "55", "led": 300, "fan1": 128.4, "power_w": float("nan"),
            "bus_v": True, "pump": 1, "heater": False, "sht_ok": True, "rssi": -61, "fw": "x" * 99})
        self.assertIsNone(cols["temp"])
        self.assertIsNone(cols["hum"])
        self.assertIsNone(cols["led"])
        self.assertEqual(cols["fan1"], 128)
        self.assertIsNone(cols["power_w"])
        self.assertIsNone(cols["bus_v"])
        self.assertIsNone(cols["pump"])          # 1 — число, а не флаг
        self.assertEqual(cols["heater"], 0)
        self.assertEqual(extra["rssi"], -61)
        self.assertEqual(len(extra["fw"]), 32)

    def test_alert_fields_are_accepted_and_typed(self):
        _, extra = server.clean_sample({"op_mode": 1, "clock_usable": True, "water_skipped": False,
                                        "clock_trusted": False})
        self.assertEqual(extra["op_mode"], 1)
        self.assertIs(extra["clock_usable"], 1)      # в базе флаги 0/1, булевыми их делает status()
        self.assertEqual(extra["water_skipped"], 0)
        _, bad = server.clean_sample({"op_mode": 2, "clock_usable": "yes", "water_skipped": 1})
        self.assertNotIn("op_mode", bad)              # режимы бывают 0 и 1
        self.assertNotIn("clock_usable", bad)         # флаг — только true/false
        self.assertNotIn("water_skipped", bad)        # число 1 — не флаг

    def test_dead_sensor_hides_climate(self):
        cols, _ = server.clean_sample({"temp": 25.0, "hum": 50.0, "sht_ok": False})
        self.assertIsNone(cols["temp"])
        self.assertIsNone(cols["hum"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = server.Store(Path(self.tmp.name) / "t.sqlite3")

    def lines(self, level="all"):
        return [r["line"] for r in reversed(self.store.logs("m", level))]

    def test_series_buckets_and_flags(self):
        for i in range(10):
            self.store.ingest("m", {"sample": {"temp": 20 + i, "pump": i == 3, "sht_ok": True, "led": 255}}, now=NOW + i * 60)
        data = self.store.series("m", NOW, NOW + 3600)
        self.assertEqual(data["bucket"], 60)
        self.assertEqual(data["temp"][:3], [20, 21, 22])
        self.assertEqual(data["pump"][3], 1)
        self.assertEqual(data["led"][0], 255)
        wide = self.store.series("m", NOW - 6 * 86400, NOW + 3600)
        self.assertGreater(wide["bucket"], 60)
        self.assertEqual(len(wide["t"]), 1)
        self.assertAlmostEqual(wide["temp"][0], 24.5)

    def test_status_counts_and_last_sample(self):
        self.store.ingest("m", {"sample": {"temp": 24.5, "sht_ok": True, "water_present": True},
                                "log": {"boot": "a", "from": 0, "text": "ОШИБКА: что-то\n[ПОЛИВ] Пропущен: нет воды\nобычная строка\n"}}, now=NOW)
        st = self.store.status("m", 180, now=NOW + 30)
        self.assertTrue(st["online"])
        self.assertEqual(st["age"], 30)
        self.assertEqual(st["sample"]["temp"], 24.5)
        self.assertIs(st["sample"]["water_present"], True)   # страница сравнивает с настоящим false
        self.assertIs(st["sample"]["sht_ok"], True)
        self.assertEqual((st["errors_24h"], st["warnings_24h"]), (1, 1))
        self.assertEqual(st["errors_1h"], 1)
        self.assertFalse(self.store.status("m", 180, now=NOW + 600)["online"])
        self.assertIsNone(self.store.status("unknown", 180))

    def test_status_recent_errors_window(self):
        old_line = "ОШИБКА: давно\n"
        self.store.ingest("m", {"log": {"boot": "a", "from": 0, "text": old_line}}, now=NOW)
        later = NOW + 2 * 3600
        self.store.ingest("m", {"log": {"boot": "a", "from": len(old_line.encode("utf-8")),
                                        "text": "ОШИБКА: недавно\n[ПОЛИВ] Пропущен: нет воды\n"}}, now=later)
        st = self.store.status("m", 180, now=later + 60)
        self.assertEqual(st["errors_24h"], 2)
        self.assertEqual(st["errors_1h"], 1)          # старая ошибка колокольчик уже не показывает
        self.assertEqual(self.store.status("m", 180, now=later + 3600 + 61)["errors_1h"], 0)

    def test_status_alert_flags_are_real_booleans(self):
        self.store.ingest("m", {"sample": {"op_mode": 0, "clock_usable": True, "water_skipped": False,
                                           "clock_trusted": False, "rtc_ok": False}}, now=NOW)
        sample = self.store.status("m", 180, now=NOW)["sample"]
        self.assertIs(sample["clock_usable"], True)
        self.assertIs(sample["water_skipped"], False)
        self.assertIs(sample["clock_trusted"], False)
        self.assertEqual(sample["op_mode"], 0)

    def test_log_resend_is_idempotent(self):
        first = "строка раз\nстрока два\n"
        self.store.ingest("m", {"log": {"boot": "a", "from": 0, "text": first}}, now=NOW)
        self.store.ingest("m", {"log": {"boot": "a", "from": 0, "text": first}}, now=NOW + 60)   # ответ потерялся
        more = first + "строка три\n"
        self.store.ingest("m", {"log": {"boot": "a", "from": 0, "text": more}}, now=NOW + 120)  # пересечение
        self.assertEqual(self.lines(), ["строка раз", "строка два", "строка три"])

    def test_log_gap_and_reboot_markers(self):
        self.store.ingest("m", {"log": {"boot": "a", "from": 0, "text": "one\n"}}, now=NOW)
        end = len("one\n")
        self.store.ingest("m", {"log": {"boot": "a", "from": end + 500, "text": "two\n"}}, now=NOW + 60)
        self.store.ingest("m", {"log": {"boot": "b", "from": 0, "text": "three\n"}}, now=NOW + 120)
        lines = self.lines()
        self.assertEqual(lines[0], "one")
        self.assertIn("пропущено 500 байт", lines[1])
        self.assertEqual(lines[2], "two")
        self.assertIn("перезагрузилась", lines[3])
        self.assertEqual(lines[4], "three")

    def test_first_contact_has_no_gap_marker(self):
        self.store.ingest("m", {"log": {"boot": "a", "from": 5000, "text": "late\n"}}, now=NOW)
        self.assertEqual(self.lines(), ["late"])

    def test_log_without_boot_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.ingest("m", {"log": {"from": 0, "text": "x\n"}}, now=NOW)
        self.assertEqual(self.lines(), [])

    def test_long_line_is_truncated_and_levels_filter(self):
        self.store.ingest("m", {"log": {"boot": "a", "from": 0, "text": "ОШИБКА: " + "я" * 1000 + "\nинфо\n"}}, now=NOW)
        errors = self.store.logs("m", "error")
        self.assertEqual(len(errors), 1)
        self.assertLessEqual(len(errors[0]["line"]), server.MAX_LINE_CHARS)
        self.assertEqual(len(self.store.logs("m", "all")), 2)
        self.assertEqual(len(self.store.logs("m", "all", limit=1)), 1)
        newest = self.store.logs("m", "all")[0]["id"]
        self.assertEqual(len(self.store.logs("m", "all", before=newest)), 1)

    def test_export_csv_and_limit(self):
        self.store.ingest("m", {"sample": {"temp": 21.5, "sht_ok": True, "pump": False}}, now=NOW)
        text = self.store.export_csv("m", NOW - 1, NOW + 1).decode("utf-8")
        head, row = text.strip().split("\n")
        self.assertTrue(head.startswith("timestamp_utc;temp;hum;"))
        self.assertTrue(row.startswith("2027-01-15T08:00:00Z;21.5;;"))
        old = server.MAX_EXPORT_ROWS
        server.MAX_EXPORT_ROWS = 0
        self.addCleanup(setattr, server, "MAX_EXPORT_ROWS", old)
        with self.assertRaises(ValueError):
            self.store.export_csv("m", NOW - 1, NOW + 1)

    def test_prune(self):
        old_text = "старое\n"
        self.store.ingest("m", {"sample": {"temp": 20}, "log": {"boot": "a", "from": 0, "text": old_text}}, now=NOW - 100 * 86400)
        self.store.ingest("m", {"sample": {"temp": 21}, "log": {"boot": "a", "from": len(old_text.encode("utf-8")), "text": "свежее\n"}}, now=NOW)
        self.store.prune(retention_days=400, log_retention_days=90, now=NOW)
        self.assertEqual(self.lines(), ["свежее"])
        self.assertEqual(len(self.store.series("m", NOW - 200 * 86400, NOW + 1)["t"]), 2)
        self.store.prune(retention_days=30, log_retention_days=90, now=NOW)
        self.assertEqual(len(self.store.series("m", NOW - 200 * 86400, NOW + 1)["t"]), 1)

    def test_demo_seed_runs(self):
        # Шаг 20 минут: тест проверяет, что заливщик работает, а не наполняет базу. Каждая запись —
        # отдельная транзакция SQLite, на Windows это около 30 мс.
        device = seed_demo.build(self.store, days=1, now=NOW, step=1200)
        data = self.store.series(device, NOW - 86400, NOW + 1)
        self.assertGreater(len(data["t"]), 50)
        self.assertGreaterEqual(len(self.store.logs(device, "all")), 1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_config(Path(cls.tmp.name) / "h.sqlite3")
        cls.cfg = cfg
        cls.store = server.Store(cfg.db)
        for device in ("m", "http-m", "utf8-m"):    # принимаются только зарегистрированные платы
            cls.store.add_board("Плата " + device, device)
        cls.httpd = server.DashServer(cfg, cls.store)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.httpd.limiter = server.FailureLimiter()
        self.httpd.name_limiter = server.FailureLimiter(limit=20, window=600)
        self.httpd.reg_limiter = server.FailureLimiter(limit=server.REGISTRATIONS_PER_HOUR, window=3600)
        self.httpd.ingest_gate = server.RateGate(0)    # частоту приёма проверяет отдельный тест

    def request(self, method, path, body=None, headers=None, user="garden", password=PASSWORD, auth=True):
        headers = dict(headers or {})
        if auth and "Authorization" not in headers:
            headers["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def ingest(self, payload, token=TOKEN, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        return self.request("POST", "/api/ingest", body, headers, auth=False)

    def test_healthz_is_public(self):
        status, _, body = self.request("GET", "/healthz", auth=False)
        self.assertEqual((status, body), (200, b"ok"))

    def test_ingest_requires_the_right_token(self):
        self.assertEqual(self.ingest({"device": "m"}, token=None)[0], 401)
        self.assertEqual(self.ingest({"device": "m"}, token="x" * 32)[0], 401)
        # просмотровый пароль не годится как токен приёма
        self.assertEqual(self.ingest({"device": "m"}, token=PASSWORD)[0], 401)

    def test_ingest_and_read_back(self):
        status, _, body = self.ingest({"device": "http-m", "sample": {"temp": 23.5, "hum": 61, "sht_ok": True, "led": 128},
                                       "log": {"boot": "z", "from": 0, "text": "ОШИБКА: проверка\n"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["log_lines"], 1)
        status, headers, body = self.request("GET", "/api/status?device=http-m")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sample"]["temp"], 23.5)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, body = self.request("GET", "/api/series?device=http-m&range=24h")
        self.assertEqual(json.loads(body)["temp"][-1], 23.5)
        status, _, body = self.request("GET", "/api/logs?device=http-m&level=error")
        self.assertEqual(json.loads(body)["rows"][0]["line"], "ОШИБКА: проверка")
        status, headers, body = self.request("GET", "/api/export.csv?device=http-m&range=24h")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("23.5", body.decode("utf-8"))
        status, _, body = self.request("GET", "/api/devices")
        self.assertIn("http-m", [d["device"] for d in json.loads(body)["devices"]])

    def test_ingest_survives_broken_utf8(self):
        # Строка журнала, обрезанная посреди русской буквы, не должна вызывать 400 на каждый повтор.
        broken = '{"device":"utf8-m","log":{"boot":"u","from":0,"text":"ОШИБКА: обрыв '.encode("utf-8") + b"\xd0" + '\\nдальше\\n"}}'.encode("utf-8")
        status, _, body = self.ingest(None, raw=broken)
        self.assertEqual(status, 200, body)
        status, _, body = self.request("GET", "/api/logs?device=utf8-m&level=all")
        lines = [row["line"] for row in json.loads(body)["rows"]]
        self.assertIn("дальше", lines)
        self.assertTrue(any(line.startswith("ОШИБКА: обрыв") for line in lines))

    def test_ingest_rejects_bad_requests(self):
        self.assertEqual(self.ingest(None, raw=b"{oops")[0], 400)
        self.assertEqual(self.ingest(None, raw=b"[1,2]")[0], 400)
        self.assertEqual(self.ingest({"device": "../etc"})[0], 400)
        self.assertEqual(self.ingest({"device": "m", "log": {"from": 0, "text": "x\n"}})[0], 400)  # нет boot
        too_big = {"device": "m", "log": {"boot": "a", "from": 0, "text": "x" * (server.MAX_LOG_BYTES + 1)}}
        self.assertEqual(self.ingest(too_big)[0], 413)
        self.assertEqual(self.ingest(None, raw=b" " * (server.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.request("GET", "/api/ingest")[0], 405)

    def test_keep_alive_survives_rejected_post(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/api/ingest", body=b'{"device":"m"}', headers={"Authorization": "Bearer nope" + "x" * 30})
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 401)
            conn.request("GET", "/healthz")   # соединение закрыто сервером, http.client откроет новое
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()

    def test_viewer_needs_password(self):
        # без входа страница ведёт на форму входа, а запросы данных получают 401 без окна с паролем
        status, headers, _ = self.request("GET", "/", auth=False)
        self.assertEqual((status, headers["Location"]), (302, "/login?next=%2F"))
        status, headers, _ = self.request("GET", "/api/status", auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn("WWW-Authenticate", headers)
        # неверный пароль администратора в заголовке (так ходят скрипты) отклоняется
        self.assertEqual(self.request("GET", "/", password="wrong-password")[0], 401)
        self.assertEqual(self.request("GET", "/", user="admin")[0], 401)
        # токен платы не открывает страницу
        self.assertEqual(self.request("GET", "/", headers={"Authorization": "Bearer " + TOKEN}, auth=False)[0], 302)
        self.assertEqual(self.request("GET", "/api/status", headers={"Authorization": "Bearer " + TOKEN}, auth=False)[0], 401)

    def test_page_assets_and_security_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        html = body.decode("utf-8")
        self.assertNotIn("<script>", html)      # CSP запрещает встроенные скрипты
        self.assertNotIn(" style=", html)
        for path in ("/app.js", "/alerts.js", "/style.css"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        self.assertIn('<script src="/alerts.js">', html)   # правила оповещений подключены до app.js
        self.assertLess(html.index("/alerts.js"), html.index("/app.js"))
        self.assertEqual(self.request("GET", "/server.py")[0], 404)
        self.assertEqual(self.request("GET", "/../server.py")[0], 404)
        self.assertEqual(self.request("GET", "/static/app.js")[0], 404)

    def test_bad_query_values(self):
        self.assertEqual(self.request("GET", "/api/series?range=5y")[0], 400)
        self.assertEqual(self.request("GET", "/api/series?from=10&to=5")[0], 400)
        self.assertEqual(self.request("GET", "/api/series?from=abc")[0], 400)
        self.assertEqual(self.request("GET", "/api/logs?level=debug")[0], 400)
        self.assertEqual(self.request("GET", "/api/logs?limit=9999")[0], 400)
        self.assertEqual(self.request("GET", "/api/status?device=bad/name")[0], 400)

    def test_wrong_password_locks_out_the_address(self):
        for _ in range(10):
            self.assertEqual(self.request("GET", "/", password="wrong-password")[0], 401)
        status, headers, _ = self.request("GET", "/")      # даже верный пароль после блокировки
        self.assertEqual(status, 429)
        self.assertEqual(headers["Retry-After"], "600")

    def test_limiter_window(self):
        limiter = server.FailureLimiter(limit=3, window=60)
        for i in range(3):
            limiter.fail("1.2.3.4", now=1000 + i)
        self.assertTrue(limiter.blocked("1.2.3.4", now=1010))
        self.assertFalse(limiter.blocked("1.2.3.4", now=1100))
        self.assertFalse(limiter.blocked("5.6.7.8", now=1010))


class BoardStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.sqlite3"
        self.store = server.Store(self.path)

    def test_registration_issues_a_token_once_and_stores_only_a_hash(self):
        token, board, item = self.store.add_board("  Теплица   два ", "microgarden-2", now=NOW)
        self.assertTrue(token.startswith(server.TOKEN_PREFIX))
        self.assertTrue(24 <= len(token) <= 128)              # плата принимает токены от 24 до 128 символов
        self.assertTrue(all(32 < ord(c) < 127 for c in token))
        self.assertEqual(board, {"device": "microgarden-2", "name": "Теплица два", "created_at": NOW})
        self.assertEqual((item["name"], item["device"], item["prefix"], item["source"]),
                         (server.FIRST_TOKEN_NAME, "microgarden-2", token[:8], "ui"))
        self.assertNotIn("hash", item)
        # открытого токена нет ни в списке, ни в файлах базы (вместе с журналом WAL)
        listing = self.store.list_boards(180, now=NOW)
        self.assertNotIn(token, json.dumps(listing))
        for path in Path(self.tmp.name).iterdir():
            self.assertNotIn(token.encode(), path.read_bytes(), path.name)
        self.assertEqual(self.store.authenticate(token, now=NOW), {"id": item["id"], "device": "microgarden-2"})
        self.assertTrue(self.store.board_exists("microgarden-2"))
        self.assertFalse(self.store.board_exists("microgarden-3"))

    def test_board_name_and_device_are_validated(self):
        for bad in ("", "   ", "x" * 41, "a\x00b", 5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.store.add_board(bad, "ok-device")
        for bad in ("", "../x", "bad name", "m\n", "x" * 33, 7, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.store.add_board("ok", bad)
        self.assertEqual(self.store.list_boards(180)["boards"], [])

    def test_device_names_are_unique_and_boards_are_limited(self):
        self.store.add_board("первая", "a-1")
        with self.assertRaises(server.Conflict):
            self.store.add_board("вторая", "a-1")
        self.store.max_boards = 2
        self.store.add_board("вторая", "a-2")
        with self.assertRaises(server.Conflict):
            self.store.add_board("третья", "a-3")

    def test_unknown_empty_and_revoked_tokens_are_rejected(self):
        token, _, item = self.store.add_board("a", "a-1", now=NOW)
        self.assertIsNone(self.store.authenticate(token + "x", now=NOW))
        self.assertIsNone(self.store.authenticate("", now=NOW))
        revoked = self.store.revoke_token(item["id"], now=NOW + 5)
        self.assertEqual((revoked["active"], revoked["revoked_at"]), (False, NOW + 5))
        self.assertIsNone(self.store.authenticate(token, now=NOW + 6))
        self.assertEqual(self.store.revoke_token(item["id"], now=NOW + 99)["revoked_at"], NOW + 5)  # повторный отзыв ничего не меняет
        self.assertIsNone(self.store.revoke_token(9999))

    def test_last_use_is_written_at_most_once_a_minute(self):
        token, _, _ = self.store.add_board("a", "a-1", now=NOW)
        self.store.authenticate(token, "1.1.1.1", now=NOW)
        self.store.authenticate(token, "2.2.2.2", now=NOW + 30)     # меньше минуты: запись не обновляется
        row = self.store.list_tokens()[0]
        self.assertEqual((row["last_used_at"], row["last_ip"]), (NOW, "1.1.1.1"))
        self.store.authenticate(token, "2.2.2.2", now=NOW + 61)
        row = self.store.list_tokens()[0]
        self.assertEqual((row["last_used_at"], row["last_ip"]), (NOW + 61, "2.2.2.2"))

    def test_a_second_token_replaces_the_first_without_a_gap(self):
        first, _, first_item = self.store.add_board("a", "a-1", now=NOW)
        second, second_item = self.store.add_board_token("a-1", "Запасной", now=NOW + 10)
        self.assertEqual((second_item["name"], second_item["device"]), ("Запасной", "a-1"))
        self.assertIsNotNone(self.store.authenticate(first, now=NOW + 11))      # пока старый не отозван, плата работает
        self.assertIsNotNone(self.store.authenticate(second, now=NOW + 11))
        self.store.revoke_token(first_item["id"], now=NOW + 12)
        self.assertIsNone(self.store.authenticate(first, now=NOW + 13))
        self.assertIsNotNone(self.store.authenticate(second, now=NOW + 13))
        self.assertEqual(self.store.add_board_token("a-1", None, now=NOW + 14)[1]["name"], "Новый токен")
        self.assertIsNone(self.store.add_board_token("no-such-board"))           # платы нет: токен не выдаётся
        with self.assertRaises(ValueError):
            self.store.add_board_token("../x")

    def test_token_limits(self):
        old = server.MAX_ACTIVE_TOKENS, server.MAX_TOKEN_ROWS
        self.addCleanup(lambda: setattr(server, "MAX_ACTIVE_TOKENS", old[0]) or setattr(server, "MAX_TOKEN_ROWS", old[1]))
        server.MAX_ACTIVE_TOKENS, server.MAX_TOKEN_ROWS = 2, 3
        _, _, first = self.store.add_board("a", "a-1", now=NOW)
        self.store.add_board_token("a-1")
        with self.assertRaises(server.Conflict):                    # действующих не больше двух
            self.store.add_board_token("a-1")
        self.store.revoke_token(first["id"])
        self.store.add_board_token("a-1")                           # место освободилось
        self.store.revoke_token(self.store.list_tokens()[0]["id"])
        with self.assertRaises(server.Conflict):                    # всего записей не больше трёх, отозванные тоже считаются
            self.store.add_board_token("a-1")
        self.assertTrue(self.store.delete_token(first["id"]))
        self.store.add_board_token("a-1")

    def test_only_revoked_tokens_can_be_deleted(self):
        _, _, item = self.store.add_board("a", "a-1")
        with self.assertRaises(server.Conflict):
            self.store.delete_token(item["id"])
        self.store.revoke_token(item["id"])
        self.assertTrue(self.store.delete_token(item["id"]))
        self.assertFalse(self.store.delete_token(item["id"]))
        self.assertEqual(self.store.list_tokens(), [])

    def test_rename(self):
        self.store.add_board("старое", "a-1", now=NOW)
        self.assertEqual(self.store.rename_board("a-1", " Новое  имя "),
                         {"device": "a-1", "name": "Новое имя", "created_at": NOW})
        self.assertIsNone(self.store.rename_board("no-such-board", "x"))
        with self.assertRaises(ValueError):
            self.store.rename_board("a-1", "")
        self.store.ingest("a-1", {"sample": {"temp": 20.0, "sht_ok": True}}, now=NOW)    # имя видно и в списке плат панели
        self.assertEqual(self.store.devices(now=NOW)[0]["name"], "Новое имя")

    def test_deleting_a_board_removes_its_data_and_keeps_the_others(self):
        _, _, mine = self.store.add_board("моя", "a-1", now=NOW)
        other_token, _, _ = self.store.add_board("чужая", "a-2", now=NOW)
        for device in ("a-1", "a-2"):
            self.store.ingest(device, {"sample": {"temp": 21.0, "sht_ok": True},
                                       "log": {"boot": "b", "from": 0, "text": "ОШИБКА: что-то\n"}}, now=NOW)
        with self.assertRaises(server.Conflict):                   # пока токен действует, платы не удалить
            self.store.delete_board("a-1")
        self.store.revoke_token(mine["id"])
        self.assertTrue(self.store.delete_board("a-1"))
        self.assertFalse(self.store.delete_board("a-1"))
        self.assertFalse(self.store.board_exists("a-1"))
        self.assertIsNone(self.store.status("a-1", 180, now=NOW))
        self.assertEqual(self.store.logs("a-1", "all"), [])
        self.assertEqual(self.store.series("a-1", NOW - 10, NOW + 10)["t"], [])
        self.assertEqual([t["device"] for t in self.store.list_tokens()], ["a-2"])
        self.assertTrue(self.store.board_exists("a-2"))
        self.assertIsNotNone(self.store.status("a-2", 180, now=NOW))
        self.assertIsNotNone(self.store.authenticate(other_token, now=NOW))

    def test_listing_shows_state_firmware_and_shared_tokens(self):
        self.store.sync_env_token(TOKEN, now=NOW)
        _, _, item = self.store.add_board("Теплица", "a-1", now=NOW)
        self.store.add_board("Новая", "a-2", now=NOW + 1)
        self.store.ingest("a-1", {"sample": {"temp": 20.0, "sht_ok": True, "fw": "lite-1"}}, now=NOW + 5)
        listing = self.store.list_boards(180, now=NOW + 60)
        first, second = listing["boards"]
        self.assertEqual((first["device"], first["name"], first["online"], first["fw"]), ("a-1", "Теплица", True, "lite-1"))
        self.assertEqual([t["id"] for t in first["tokens"]], [item["id"]])
        self.assertEqual((second["device"], second["last_seen"], second["online"], second["fw"]), ("a-2", None, False, None))
        self.assertFalse(self.store.list_boards(180, now=NOW + 5 + 181)["boards"][0]["online"])
        self.assertEqual([t["source"] for t in listing["shared_tokens"]], ["env"])
        self.assertEqual(listing["shared_tokens"][0]["name"], server.ENV_TOKEN_NAME)

    def test_boards_already_sending_data_are_registered_once_on_upgrade(self):
        self.store.ingest("old-m", {"sample": {"temp": 20.0}}, now=NOW)     # плата работала до реестра плат
        with closing(self.store._connect()) as cx, cx:
            cx.execute("DELETE FROM boards")
            cx.execute("DELETE FROM meta")
        upgraded = server.Store(self.path)
        boards = upgraded.list_boards(180, now=NOW)["boards"]
        self.assertEqual([(b["device"], b["name"], b["created_at"]) for b in boards], [("old-m", "old-m", NOW)])
        self.assertTrue(upgraded.delete_board("old-m"))                     # токенов у платы нет, удалить можно сразу
        self.assertFalse(server.Store(self.path).board_exists("old-m"))     # удалённая плата при перезапуске не возвращается


class EnvTokenTests(unittest.TestCase):
    """Токен из DASH_INGEST_TOKEN: подхватывается сам, чтобы уже настроенная плата продолжала работать."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = server.Store(Path(self.tmp.name) / "t.sqlite3")

    def test_env_token_is_imported_once_and_works(self):
        self.store.sync_env_token(TOKEN, now=NOW)
        self.store.sync_env_token(TOKEN, now=NOW + 5)               # перезапуск сервера
        items = self.store.list_tokens()
        self.assertEqual([(i["name"], i["source"], i["device"], i["prefix"]) for i in items],
                         [(server.ENV_TOKEN_NAME, "env", None, TOKEN[:8])])
        self.assertEqual(self.store.authenticate(TOKEN, now=NOW + 6)["device"], None)   # общий: подходит любой плате

    def test_replacing_the_env_token_stops_the_old_one(self):
        self.store.sync_env_token(TOKEN, now=NOW)
        new = "n" * 32
        self.store.sync_env_token(new, now=NOW + 10)
        self.assertIsNone(self.store.authenticate(TOKEN, now=NOW + 11))
        self.assertIsNotNone(self.store.authenticate(new, now=NOW + 11))

    def test_removing_the_env_token_keeps_the_tokens_of_boards(self):
        board_token, _, _ = self.store.add_board("a", "a-1", now=NOW)
        self.store.sync_env_token(TOKEN, now=NOW)
        self.store.sync_env_token("", now=NOW + 10)
        self.assertIsNone(self.store.authenticate(TOKEN, now=NOW + 11))
        self.assertIsNotNone(self.store.authenticate(board_token, now=NOW + 11))

    def test_revoked_env_token_is_not_revived_by_a_restart(self):
        self.store.sync_env_token(TOKEN, now=NOW)
        env_id = self.store.list_tokens()[0]["id"]
        self.store.revoke_token(env_id, now=NOW + 1)
        self.store.sync_env_token(TOKEN, now=NOW + 2)
        self.assertIsNone(self.store.authenticate(TOKEN, now=NOW + 3))
        self.assertEqual(len(self.store.list_tokens()), 1)
        with self.assertRaises(server.Conflict):                    # пока токен записан в .env, удаление вернуло бы его при запуске
            self.store.delete_token(env_id)
        self.store.sync_env_token("", now=NOW + 4)                  # убрали из .env
        self.assertTrue(self.store.delete_token(env_id))


class BoardHttpTests(unittest.TestCase):
    ACTION = {"X-Dash-Action": "1", "Content-Type": "application/json"}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_config(Path(cls.tmp.name) / "k.sqlite3")
        cls.store = server.Store(cfg.db)
        cls.httpd = server.DashServer(cfg, cls.store)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.httpd.limiter = server.FailureLimiter()
        self.httpd.name_limiter = server.FailureLimiter(limit=20, window=600)
        self.httpd.reg_limiter = server.FailureLimiter(limit=server.REGISTRATIONS_PER_HOUR, window=3600)
        self.httpd.ingest_gate = server.RateGate(0)    # частоту приёма проверяет отдельный тест

    def request(self, method, path, body=None, headers=None, auth=True):
        headers = dict(headers or {})
        if auth and "Authorization" not in headers:
            headers["Authorization"] = "Basic " + base64.b64encode(("garden:%s" % PASSWORD).encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def ingest(self, payload, token):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
        return self.request("POST", "/api/ingest", json.dumps(payload).encode("utf-8"), headers, auth=False)

    def post(self, path, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        return self.request("POST", path, body, headers or self.ACTION)

    def register(self, name, device):
        status, _, body = self.post("/api/boards", {"name": name, "device": device})
        return status, json.loads(body)

    def boards(self):
        return json.loads(self.request("GET", "/api/boards")[2])

    def test_manager_needs_the_viewer_password(self):
        for method, path in (("GET", "/api/boards"), ("POST", "/api/boards"), ("POST", "/api/boards/a-1/tokens"),
                             ("POST", "/api/boards/a-1/rename"), ("DELETE", "/api/boards/a-1"),
                             ("POST", "/api/tokens/1/revoke"), ("DELETE", "/api/tokens/1")):
            body = b"{}" if method == "POST" else None
            self.assertEqual(self.request(method, path, body, self.ACTION, auth=False)[0], 401, (method, path))
        # страница без входа ведёт на форму входа, а не на окно с паролем
        status, headers, _ = self.request("GET", "/boards", auth=False)
        self.assertEqual((status, headers["Location"]), (302, "/login?next=%2Fboards"))
        # токен приёма не открывает менеджер плат
        self.assertEqual(self.request("GET", "/api/boards", headers={"Authorization": "Bearer " + TOKEN}, auth=False)[0], 401)

    def test_register_a_board_use_its_token_and_remove_it(self):
        status, created = self.register("Теплица", "reg-m")
        self.assertEqual(status, 201, created)
        token, item = created["token"], created["item"]
        self.assertEqual(created["board"]["name"], "Теплица")
        # новая плата сразу присылает данные под своим именем
        self.assertEqual(self.ingest({"device": "reg-m", "sample": {"temp": 21.0, "sht_ok": True, "fw": "lite-2"}}, token)[0], 200)
        # токен выдан этой плате: от имени другой он не годится, даже если та зарегистрирована
        self.register("Другая", "reg-other")
        status, _, body = self.ingest({"device": "reg-other"}, token)
        self.assertEqual(status, 403)
        self.assertIn("reg-m", json.loads(body)["error"])
        # список: плата, её токен и состояние связи; самого токена в ответе нет
        status, headers, body = self.request("GET", "/api/boards")
        self.assertEqual((status, headers["Cache-Control"]), (200, "no-store"))
        self.assertNotIn(token, body.decode("utf-8"))
        mine = [b for b in json.loads(body)["boards"] if b["device"] == "reg-m"][0]
        self.assertEqual((mine["name"], mine["online"], mine["fw"]), ("Теплица", True, "lite-2"))
        self.assertEqual([t["id"] for t in mine["tokens"]], [item["id"]])
        # у платы есть действующий токен: удалить её нельзя
        self.assertEqual(self.request("DELETE", "/api/boards/reg-m", None, self.ACTION)[0], 409)
        # отзыв токена действует сразу, потом плату можно удалить вместе с данными
        status, _, body = self.post("/api/tokens/%d/revoke" % item["id"])
        self.assertEqual((status, json.loads(body)["item"]["active"]), (200, False))
        self.assertEqual(self.ingest({"device": "reg-m"}, token)[0], 401)
        self.assertEqual(self.request("DELETE", "/api/boards/reg-m", None, self.ACTION)[0], 200)
        self.assertEqual(self.request("DELETE", "/api/boards/reg-m", None, self.ACTION)[0], 404)
        self.assertEqual(self.request("GET", "/api/status?device=reg-m")[0], 404)

    def test_only_registered_boards_can_send_data(self):
        # даже с действующим общим токеном из .env незарегистрированная плата отклоняется
        status, _, body = self.ingest({"device": "stranger", "sample": {"temp": 20.0}}, TOKEN)
        self.assertEqual(status, 403)
        self.assertIn("не зарегистрирована", json.loads(body)["error"])
        self.assertEqual(self.request("GET", "/api/status?device=stranger")[0], 404)
        # после регистрации тот же общий токен принимается: старые платы без своего токена продолжают работать
        self.register("Гость", "stranger")
        self.assertEqual(self.ingest({"device": "stranger", "sample": {"temp": 20.0}}, TOKEN)[0], 200)
        listed = self.boards()
        self.assertEqual([t["name"] for t in listed["shared_tokens"]], [server.ENV_TOKEN_NAME])

    def test_duplicate_registration_and_bad_input(self):
        self.assertEqual(self.register("Первая", "dup-m")[0], 201)
        self.assertEqual(self.register("Вторая", "dup-m")[0], 409)
        before = len(self.boards()["boards"])
        self.assertEqual(self.post("/api/boards", {"name": "", "device": "ok-m"})[0], 400)
        self.assertEqual(self.post("/api/boards", {"name": "ok", "device": "../x"})[0], 400)
        self.assertEqual(self.post("/api/boards", {"name": "ok"})[0], 400)                        # имя платы обязательно
        self.assertEqual(self.request("POST", "/api/boards", b"{oops", self.ACTION)[0], 400)
        self.assertEqual(self.request("POST", "/api/boards", b"[1]", self.ACTION)[0], 400)
        self.assertEqual(self.request("POST", "/api/boards", b"{}", {"X-Dash-Action": "1", "Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("POST", "/api/boards", b" " * (server.MAX_ACTION_BODY + 1), self.ACTION)[0], 400)
        self.assertEqual(len(self.boards()["boards"]), before)

    def test_replace_a_token_and_rename_a_board(self):
        _, created = self.register("Старое имя", "swap-m")
        first = created["token"]
        status, _, body = self.post("/api/boards/swap-m/tokens", {"name": "Запасной"})
        self.assertEqual(status, 201)
        second = json.loads(body)
        self.assertEqual((second["item"]["name"], second["item"]["device"]), ("Запасной", "swap-m"))
        self.assertEqual(self.ingest({"device": "swap-m"}, first)[0], 200)      # оба токена действуют, пока старый не отозван
        self.assertEqual(self.ingest({"device": "swap-m"}, second["token"])[0], 200)
        self.assertEqual(self.post("/api/tokens/%d/revoke" % created["item"]["id"])[0], 200)
        self.assertEqual(self.ingest({"device": "swap-m"}, first)[0], 401)
        self.assertEqual(self.ingest({"device": "swap-m"}, second["token"])[0], 200)
        status, _, body = self.post("/api/boards/swap-m/rename", {"name": "Новое имя"})
        self.assertEqual((status, json.loads(body)["board"]["name"]), (200, "Новое имя"))
        self.assertEqual(self.post("/api/boards/no-such-board/tokens", {})[0], 404)
        self.assertEqual(self.post("/api/boards/no-such-board/rename", {"name": "x"})[0], 404)
        status, _, body = self.request("GET", "/api/devices")
        self.assertIn(("swap-m", "Новое имя"), [(d["device"], d["name"]) for d in json.loads(body)["devices"]])

    def test_changes_must_come_from_the_boards_page(self):
        body = json.dumps({"name": "чужой запрос", "device": "csrf-m"}).encode("utf-8")
        before = len(self.boards()["boards"])
        # чужая страница не может отправить заголовок X-Dash-Action без разрешения сервера
        self.assertEqual(self.request("POST", "/api/boards", body, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.request("DELETE", "/api/boards/csrf-m")[0], 403)
        self.assertEqual(self.request("POST", "/api/tokens/1/revoke")[0], 403)
        # и браузер помечает такие запросы как кросс-сайтовые
        for site in ("cross-site", "same-site"):
            headers = dict(self.ACTION, **{"Sec-Fetch-Site": site})
            self.assertEqual(self.request("POST", "/api/boards", body, headers)[0], 403, site)
            self.assertEqual(self.request("POST", "/api/tokens/1/revoke", None, headers)[0], 403, site)
            self.assertEqual(self.request("DELETE", "/api/boards/csrf-m", None, headers)[0], 403, site)
        self.assertEqual(len(self.boards()["boards"]), before)
        # свой запрос со страницы проходит
        headers = dict(self.ACTION, **{"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(self.request("POST", "/api/boards", body, headers)[0], 201)
        self.assertEqual(len(self.boards()["boards"]), before + 1)

    def test_wrong_methods_and_paths(self):
        self.assertEqual(self.request("GET", "/api/tokens/abc")[0], 404)
        self.assertEqual(self.request("GET", "/api/tokens/5")[0], 405)
        self.assertEqual(self.request("POST", "/api/tokens/5", None, self.ACTION)[0], 405)
        self.assertEqual(self.request("POST", "/api/tokens/99999/revoke", None, self.ACTION)[0], 404)
        self.assertEqual(self.request("GET", "/api/boards/bad%20name")[0], 404)
        self.assertEqual(self.request("GET", "/api/boards/a-1/unknown")[0], 404)
        self.assertEqual(self.post("/api/boards/a-1/unknown", {})[0], 404)
        self.assertEqual(self.request("GET", "/api/boards/a-1/tokens")[0], 405)     # токен создают только запросом POST
        self.assertEqual(self.request("GET", "/api/boards/a-1")[0], 405)             # плату удаляют только запросом DELETE

    def test_limits_give_409(self):
        old = self.store.max_boards
        self.store.max_boards = 0
        self.addCleanup(setattr, self.store, "max_boards", old)
        self.assertEqual(self.register("Лишняя", "limit-m")[0], 409)

    def test_keep_alive_survives_registering_a_board(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = dict(self.ACTION, Authorization="Basic " + base64.b64encode(("garden:%s" % PASSWORD).encode()).decode())
        try:
            conn.request("POST", "/api/boards", body=json.dumps({"name": "ka", "device": "keep-m"}).encode(), headers=headers)
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 201)
            conn.request("GET", "/api/boards", headers=headers)
            second = conn.getresponse()
            second.read()
            self.assertEqual(second.status, 200)
        finally:
            conn.close()

    def test_boards_page_is_served_without_inline_code(self):
        status, headers, body = self.request("GET", "/boards")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        html = body.decode("utf-8")
        self.assertNotIn("<script>", html)
        self.assertNotIn(" style=", html)
        self.assertLess(html.index("/boards-lib.js"), html.index("/boards.js"))
        for path in ("/boards.html", "/boards-lib.js", "/boards.js"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        self.assertIn('href="/boards"', self.request("GET", "/")[2].decode("utf-8"))   # со страницы графиков есть ссылка


class PasswordTests(unittest.TestCase):
    def test_hash_is_salted_scrypt_and_verifies(self):
        first, second = server.hash_password("correct horse battery"), server.hash_password("correct horse battery")
        self.assertTrue(first.startswith("scrypt$%d$8$1$" % server.SCRYPT_N))
        self.assertNotEqual(first, second)                                 # у каждого хеша своя соль
        self.assertNotIn("correct", first)
        self.assertTrue(server.verify_password("correct horse battery", first))
        self.assertFalse(server.verify_password("correct horse batterY", first))
        for broken in ("", "scrypt", "md5$1$2$3$aa$bb", "scrypt$x$8$1$aa$bb", "scrypt$16384$8$1$zz$bb", first + "$extra", "env"):
            self.assertFalse(server.verify_password("correct horse battery", broken), broken)

    def test_production_cost(self):
        # В тестах N занижено ради скорости; в самой программе параметры scrypt должны остаться рабочими
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1", source)

    def test_username_rules(self):
        for good in ("ivan", "Ivan_2", "a.b-c", "abc", "x" * 32):
            self.assertEqual(server.clean_username(good), good)
        for bad in ("", "ab", "x" * 33, "с кириллицей", "bad name", "a\nb", "ivan\n", "<b>", None, 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                server.clean_username(bad)
        for reserved in ("admin", "Admin", "ROOT", "system"):
            with self.assertRaises(ValueError, msg=reserved):
                server.clean_username(reserved)

    def test_password_rules(self):
        self.assertEqual(server.clean_password("x" * 10), "x" * 10)
        self.assertEqual(server.clean_password("x" * 128), "x" * 128)
        for bad in ("x" * 9, "x" * 129, " " * 12, "", None, 123456789012):
            with self.assertRaises(ValueError, msg=repr(bad)):
                server.clean_password(bad)
        with self.assertRaises(ValueError):
            server.clean_password("Ivan-Petrov-1", "ivan-petrov-1")       # пароль равен имени, регистр не важен
        self.assertEqual(server.clean_password(" пароль с пробелами ", "ivan"), " пароль с пробелами ")


class AccountStoreTests(unittest.TestCase):
    PW = "correct horse battery"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "a.sqlite3"
        self.store = server.Store(self.path)
        self.admin_id = self.store.ensure_admin("garden", "admin-password-123", now=NOW)

    def person(self, name, **kw):
        return self.store.register_user(name, self.PW, "1.2.3.4", now=NOW, **kw)

    def test_registration_and_login_ignore_name_case(self):
        ivan = self.person("Ivan")
        self.assertEqual((ivan["username"], ivan["role"]), ("Ivan", "user"))
        with self.assertRaises(server.Conflict):
            self.person("ivan")
        self.assertEqual(self.store.login_user("IVAN", self.PW, now=NOW + 5)["username"], "Ivan")
        self.assertIsNone(self.store.login_user("ivan", self.PW + "x"))
        self.assertIsNone(self.store.login_user("nobody", self.PW))
        row = [u for u in self.store.list_users() if u["username"] == "Ivan"][0]
        self.assertEqual((row["last_login_at"], row["reg_ip"], row["disabled"], row["boards"]), (NOW + 5, "1.2.3.4", False, 0))

    def test_password_is_never_stored_in_the_clear(self):
        self.person("ivan")
        for path in Path(self.tmp.name).iterdir():
            self.assertNotIn(self.PW.encode(), path.read_bytes(), path.name)
        self.assertNotIn("pass_hash", json.dumps(self.store.list_users()))

    def test_user_limit(self):
        self.store.max_users = 3                    # вместе с администратором
        self.person("one")
        self.person("two")
        with self.assertRaises(server.Conflict):
            self.person("three")

    def test_admin_is_bootstrapped_from_env(self):
        self.assertEqual(self.store.admin_principal(), {"id": self.admin_id, "username": "garden", "role": "admin"})
        self.assertEqual(self.store.login_user("Garden", "admin-password-123")["role"], "admin")
        self.assertIsNone(self.store.login_user("garden", "wrong-password-123"))
        self.assertEqual(self.store.ensure_admin("garden", "admin-password-123"), self.admin_id)   # перезапуск ничего не дублирует
        self.assertEqual(len([u for u in self.store.list_users() if u["role"] == "admin"]), 1)
        with self.assertRaises(server.Conflict):
            self.person("garden")                   # имя администратора занято
        with self.assertRaises(server.Conflict):
            self.store.change_password(self.admin_id, "admin-password-123", "another-password-1")
        with self.assertRaises(server.Conflict):
            self.store.reset_password(self.admin_id)
        with self.assertRaises(server.Conflict):
            self.store.set_user_disabled(self.admin_id, True)
        with self.assertRaises(server.Conflict):
            self.store.delete_user(self.admin_id)

    def test_changing_the_admin_name_blocks_the_old_admin_and_refuses_a_taken_name(self):
        self.person("ivan")
        with self.assertRaises(SystemExit):
            self.store.ensure_admin("ivan", "admin-password-123")           # имя занято обычным человеком
        new_id = self.store.ensure_admin("owner", "admin-password-123")
        old = [u for u in self.store.list_users() if u["username"] == "garden"][0]
        self.assertEqual((old["role"], old["disabled"]), ("user", True))
        self.assertIsNone(self.store.login_user("garden", "admin-password-123"))
        self.assertEqual(self.store.login_user("owner", "admin-password-123")["id"], new_id)

    def test_boards_without_an_owner_go_to_the_admin_on_upgrade(self):
        old_path = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old_path)) as cx, cx:                    # база версии до учётных записей
            cx.execute("CREATE TABLE boards(device TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL)")
            cx.execute("INSERT INTO boards VALUES('old-m', 'Старая', ?)", (NOW,))
        upgraded = server.Store(old_path)
        admin_id = upgraded.ensure_admin("garden", "admin-password-123")
        self.assertTrue(upgraded.can_manage({"id": admin_id, "role": "admin"}, "old-m"))
        owner = upgraded.list_boards(180, now=NOW)["boards"][0]["owner"]
        self.assertEqual(owner, "garden")

    def test_sessions_expire_slide_and_end(self):
        ivan = self.person("ivan")
        token = self.store.create_session(ivan["id"], "1.2.3.4", now=NOW)
        self.assertNotIn(token, "".join(p.read_bytes().decode("latin-1") for p in Path(self.tmp.name).iterdir()))
        self.assertEqual(self.store.session_user(token, now=NOW + 10), ivan)
        self.assertIsNone(self.store.session_user(token + "x", now=NOW + 10))
        self.assertIsNone(self.store.session_user("", now=NOW + 10))
        # срок продлевается, пока человек пользуется панелью (не чаще раза в час)
        self.assertIsNotNone(self.store.session_user(token, now=NOW + 25 * 86400))
        self.assertIsNotNone(self.store.session_user(token, now=NOW + 50 * 86400))
        self.assertIsNone(self.store.session_user(token, now=NOW + 50 * 86400 + self.store.session_days * 86400 + 1))
        again = self.store.create_session(ivan["id"], now=NOW)
        self.store.end_session(again)
        self.assertIsNone(self.store.session_user(again, now=NOW + 1))
        old = self.store.create_session(ivan["id"], now=NOW)
        self.store.purge_sessions(now=NOW + self.store.session_days * 86400 + 5)
        self.assertIsNone(self.store.session_user(old, now=NOW + 1))

    def test_blocking_ends_sessions_and_stops_the_boards(self):
        ivan = self.person("ivan")
        token, _, _ = self.store.add_board("Теплица", "iv-1", ivan["id"], now=NOW)
        session = self.store.create_session(ivan["id"], now=NOW)
        self.assertIsNotNone(self.store.authenticate(token, now=NOW))
        self.assertEqual(self.store.set_user_disabled(ivan["id"], True)["disabled"], True)
        self.assertIsNone(self.store.session_user(session, now=NOW + 1))
        self.assertIsNone(self.store.login_user("ivan", self.PW))
        self.assertIsNone(self.store.authenticate(token, now=NOW + 1))      # платы заблокированного данные не присылают
        self.assertEqual(self.store.set_user_disabled(ivan["id"], False)["disabled"], False)
        self.assertIsNotNone(self.store.login_user("ivan", self.PW))
        self.assertIsNotNone(self.store.authenticate(token, now=NOW + 120))
        self.assertIsNone(self.store.set_user_disabled(9999, True))

    def test_change_and_reset_password(self):
        ivan = self.person("ivan")
        keep = self.store.create_session(ivan["id"], now=NOW)
        other = self.store.create_session(ivan["id"], now=NOW)
        with self.assertRaises(server.WrongPassword):
            self.store.change_password(ivan["id"], "wrong-password-1", "brand new password")
        with self.assertRaises(ValueError):
            self.store.change_password(ivan["id"], self.PW, "short")
        self.store.change_password(ivan["id"], self.PW, "brand new password", keep_token=keep)
        self.assertIsNone(self.store.login_user("ivan", self.PW))
        self.assertIsNotNone(self.store.login_user("ivan", "brand new password"))
        self.assertIsNotNone(self.store.session_user(keep, now=NOW + 1))      # текущая сессия остаётся
        self.assertIsNone(self.store.session_user(other, now=NOW + 1))        # остальные закрыты
        temp = self.store.reset_password(ivan["id"])
        self.assertGreaterEqual(len(temp), server.PASSWORD_MIN)
        self.assertIsNone(self.store.login_user("ivan", "brand new password"))
        self.assertIsNotNone(self.store.login_user("ivan", temp))
        self.assertIsNone(self.store.session_user(keep, now=NOW + 2))
        self.assertIsNone(self.store.reset_password(9999))

    def test_people_see_only_their_own_boards_data_and_tokens(self):
        ann, bob = self.person("ann"), self.person("bob")
        a_token, _, a_item = self.store.add_board("Анны", "ann-1", ann["id"], now=NOW)
        _, _, b_item = self.store.add_board("Боба", "bob-1", bob["id"], now=NOW)
        self.store.sync_env_token(TOKEN, now=NOW)
        for device in ("ann-1", "bob-1"):
            self.store.ingest(device, {"sample": {"temp": 21.0, "sht_ok": True}}, now=NOW)
        as_ann = self.store.list_boards(180, ann, now=NOW)
        self.assertEqual([b["device"] for b in as_ann["boards"]], ["ann-1"])
        self.assertEqual(as_ann["shared_tokens"], [])
        self.assertNotIn("bob-1", json.dumps(as_ann))
        self.assertEqual([d["device"] for d in self.store.devices(ann, now=NOW)], ["ann-1"])
        as_admin = self.store.list_boards(180, self.store.admin_principal(), now=NOW)
        self.assertEqual({b["device"]: b["owner"] for b in as_admin["boards"]}, {"ann-1": "ann", "bob-1": "bob"})
        self.assertEqual(len(as_admin["shared_tokens"]), 1)
        self.assertEqual({d["device"] for d in self.store.devices(self.store.admin_principal(), now=NOW)}, {"ann-1", "bob-1"})
        self.assertTrue(self.store.can_manage(ann, "ann-1"))
        self.assertFalse(self.store.can_manage(ann, "bob-1"))
        self.assertFalse(self.store.can_manage(ann, "no-such"))
        self.assertTrue(self.store.can_manage(self.store.admin_principal(), "bob-1"))
        self.assertTrue(self.store.can_manage_token(ann, a_item["id"]))
        self.assertFalse(self.store.can_manage_token(ann, b_item["id"]))
        shared_id = as_admin["shared_tokens"][0]["id"]
        self.assertFalse(self.store.can_manage_token(ann, shared_id))          # общий токен только администратору
        self.assertTrue(self.store.can_manage_token(self.store.admin_principal(), shared_id))
        self.assertIsNone(self.store.can_manage_token(ann, 9999))
        self.assertIsNotNone(self.store.authenticate(a_token, now=NOW))

    def test_board_limit_per_person_does_not_apply_to_the_admin(self):
        self.store.max_boards_per_user = 1
        ann = self.person("ann")
        self.store.add_board("Первая", "ann-1", ann["id"], limit_owner=True)
        with self.assertRaises(server.Conflict):
            self.store.add_board("Вторая", "ann-2", ann["id"], limit_owner=True)
        self.store.add_board("Админская", "adm-1", self.admin_id)
        self.store.add_board("Ещё админская", "adm-2", self.admin_id)

    def test_deleting_a_person_removes_the_boards_data_and_sessions(self):
        ann, bob = self.person("ann"), self.person("bob")
        self.store.add_board("Анны", "ann-1", ann["id"], now=NOW)
        bob_token, _, _ = self.store.add_board("Боба", "bob-1", bob["id"], now=NOW)
        for device in ("ann-1", "bob-1"):
            self.store.ingest(device, {"sample": {"temp": 20.0, "sht_ok": True},
                                       "log": {"boot": "b", "from": 0, "text": "ОШИБКА: что-то\n"}}, now=NOW)
        session = self.store.create_session(ann["id"], now=NOW)
        self.assertTrue(self.store.delete_user(ann["id"]))
        self.assertFalse(self.store.delete_user(ann["id"]))
        self.assertIsNone(self.store.session_user(session, now=NOW + 1))
        self.assertFalse(self.store.board_exists("ann-1"))
        self.assertIsNone(self.store.status("ann-1", 180, now=NOW))
        self.assertEqual(self.store.logs("ann-1", "all"), [])
        self.assertEqual([u["username"] for u in self.store.list_users()], ["garden", "bob"])
        self.assertIsNotNone(self.store.status("bob-1", 180, now=NOW))        # чужие данные не тронуты
        self.assertIsNotNone(self.store.authenticate(bob_token, now=NOW))

    def test_registration_switch(self):
        self.assertTrue(self.store.registration_open())
        self.store.set_registration(False)
        self.assertFalse(self.store.registration_open())
        self.store.set_registration(True)
        self.assertTrue(self.store.registration_open())
        closed = server.Store(Path(self.tmp.name) / "closed.sqlite3")
        closed.configure(server.Config({"DASH_VIEW_PASSWORD": "p" * 16, "DASH_REGISTRATION": "closed"}))
        self.assertFalse(closed.registration_open())                          # начальное значение из .env
        closed.set_registration(True)
        self.assertTrue(closed.registration_open())                           # выбор администратора главнее


class AccountHttpTests(unittest.TestCase):
    ACTION = {"X-Dash-Action": "1", "Content-Type": "application/json"}
    PW = "correct horse battery"
    counter = 0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_config(Path(cls.tmp.name) / "p.sqlite3")
        cls.store = server.Store(cfg.db)
        cls.httpd = server.DashServer(cfg, cls.store)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.httpd.limiter = server.FailureLimiter()
        self.httpd.name_limiter = server.FailureLimiter(limit=20, window=600)
        self.httpd.reg_limiter = server.FailureLimiter(limit=server.REGISTRATIONS_PER_HOUR, window=3600)
        self.httpd.ingest_gate = server.RateGate(0)
        self.store.set_registration(True)
        self.store.max_users, self.store.max_boards_per_user = server.MAX_USERS, server.MAX_BOARDS_PER_USER

    def unique(self, prefix="user"):
        type(self).counter += 1
        return "%s%d" % (prefix, type(self).counter)

    def request(self, method, path, body=None, headers=None, cookie=None, admin=False):
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = server.SESSION_COOKIE + "=" + cookie
        if admin:
            headers["Authorization"] = "Basic " + base64.b64encode(("garden:%s" % PASSWORD).encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def call(self, method, path, payload=None, cookie=None, admin=False, extra=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = dict(self.ACTION, **(extra or {}))
        status, resp_headers, raw = self.request(method, path, body, headers, cookie, admin)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        return status, data, resp_headers

    @staticmethod
    def cookie_of(headers):
        value = headers["Set-Cookie"].split(";")[0]
        return value.split("=", 1)[1]

    def register(self, name=None, password=None):
        name = name or self.unique()
        status, data, headers = self.call("POST", "/api/register", {"username": name, "password": password or self.PW})
        return status, data, (self.cookie_of(headers) if status == 201 else None), name

    def person(self):
        status, data, cookie, name = self.register()
        self.assertEqual(status, 201, data)
        return cookie, name

    def ingest(self, device, token):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
        return self.request("POST", "/api/ingest", json.dumps({"device": device, "sample": {"temp": 22.0, "sht_ok": True}}).encode(), headers)[0]

    def test_login_and_register_pages_and_assets_are_public_and_the_rest_is_not(self):
        for path in ("/login", "/register", "/style.css", "/auth-lib.js", "/login.js", "/register.js", "/boards.js", "/app.js"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        for path in ("/login", "/register"):
            html = self.request("GET", path)[2].decode("utf-8")
            self.assertNotIn("<script>", html)
            self.assertNotIn(" style=", html)
        for path in ("/", "/boards", "/account", "/admin"):
            self.assertEqual(self.request("GET", path)[0], 302, path)
        self.assertEqual(json.loads(self.request("GET", "/api/registration")[2]), {"open": True, "mail": False, "google": False})
        self.assertEqual(self.request("GET", "/api/me")[0], 401)
        self.assertEqual(self.request("GET", "/api/devices")[0], 401)
        # устаревший вход в браузере не закрывает страницу входа
        stale = {"Authorization": "Basic " + base64.b64encode(b"garden:old-password-xx").decode()}
        self.assertEqual(self.request("GET", "/login", headers=stale)[0], 200)
        self.assertEqual(self.request("GET", "/api/me", headers=stale)[0], 401)

    def test_registering_signs_the_person_in_with_a_safe_cookie(self):
        status, data, headers = self.call("POST", "/api/register", {"username": "Ivan.P", "password": self.PW})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["user"]["username"], "Ivan.P")
        self.assertNotIn("pass", json.dumps(data))
        cookie = headers["Set-Cookie"]
        for part in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=%d" % (server.SESSION_DAYS * 86400)):
            self.assertIn(part, cookie)
        self.assertNotIn("Secure", cookie)                                 # по голому http браузер Secure-cookie не вернёт
        token = self.cookie_of(headers)
        self.assertEqual(json.loads(self.request("GET", "/api/me", cookie=token)[2])["user"]["username"], "Ivan.P")
        self.assertEqual(self.request("GET", "/", cookie=token)[0], 200)
        self.assertEqual(self.request("GET", "/login", cookie=token)[0], 302)    # уже вошёл: на главную
        # за прокси по HTTPS cookie получает Secure
        _, _, headers = self.call("POST", "/api/login", {"username": "ivan.p", "password": self.PW}, extra={"X-Forwarded-Proto": "https"})
        self.assertIn("; Secure", headers["Set-Cookie"])

    def test_registration_input_is_checked(self):
        body = json.dumps({"username": "csrf-user", "password": self.PW}).encode()
        self.assertEqual(self.request("POST", "/api/register", body, {"Content-Type": "application/json"})[0], 403)   # нет заголовка страницы
        self.assertEqual(self.request("POST", "/api/register", body, dict(self.ACTION, **{"Sec-Fetch-Site": "cross-site"}))[0], 403)
        self.assertEqual(self.call("POST", "/api/register", {"username": "ab", "password": self.PW})[0], 400)
        self.assertEqual(self.call("POST", "/api/register", {"username": "с-кириллицей", "password": self.PW})[0], 400)
        self.assertEqual(self.call("POST", "/api/register", {"username": "admin", "password": self.PW})[0], 400)
        self.assertEqual(self.call("POST", "/api/register", {"username": self.unique(), "password": "short"})[0], 400)
        self.assertEqual(self.call("POST", "/api/register", {"password": self.PW})[0], 400)
        self.assertEqual(self.request("POST", "/api/register", b"{oops", self.ACTION)[0], 400)
        self.assertEqual(self.call("POST", "/api/register", {"username": "garden", "password": self.PW})[0], 409)   # имя администратора занято
        name = self.unique()
        self.assertEqual(self.call("POST", "/api/register", {"username": name, "password": self.PW})[0], 201)
        status, data, _ = self.call("POST", "/api/register", {"username": name.upper(), "password": self.PW})
        self.assertEqual((status, "занято" in data["error"]), (409, True))

    def test_open_registration_has_limits_and_a_switch(self):
        self.httpd.reg_limiter = server.FailureLimiter(limit=3, window=3600)
        for _ in range(3):
            self.assertIn(self.call("POST", "/api/register", {"username": self.unique(), "password": "x"})[0], (400, 201))
        status, _, headers = self.call("POST", "/api/register", {"username": self.unique(), "password": self.PW})
        self.assertEqual((status, headers["Retry-After"]), (429, "3600"))
        self.httpd.reg_limiter = server.FailureLimiter(limit=server.REGISTRATIONS_PER_HOUR, window=3600)
        # администратор закрывает регистрацию одним нажатием
        self.assertEqual(self.call("POST", "/api/admin/registration", {"open": False}, admin=True)[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/api/registration")[2]), {"open": False, "mail": False, "google": False})
        self.assertEqual(self.call("POST", "/api/register", {"username": self.unique(), "password": self.PW})[0], 403)
        self.assertEqual(self.call("POST", "/api/admin/registration", {"open": True}, admin=True)[0], 200)
        self.assertEqual(self.call("POST", "/api/register", {"username": self.unique(), "password": self.PW})[0], 201)
        self.assertEqual(self.call("POST", "/api/admin/registration", {"open": "yes"}, admin=True)[0], 400)
        # предел числа людей
        self.store.max_users = len(self.store.list_users())
        status, data, _ = self.call("POST", "/api/register", {"username": self.unique(), "password": self.PW})
        self.assertEqual(status, 409)

    def test_without_mail_settings_the_old_flow_is_unchanged(self):
        # Почта не настроена: адрес не просят, платы добавляются сразу, а письма отправить нечем
        name = self.unique()
        status, data, headers = self.call("POST", "/api/register", {"username": name, "password": self.PW, "email": "ivan@example.com"})
        self.assertEqual((status, data["mail_sent"]), (201, False))
        cookie = self.cookie_of(headers)
        me = json.loads(self.request("GET", "/api/me", cookie=cookie)[2])
        self.assertEqual((me["mail_enabled"], me["email"]), (False, {"address": None, "verified": False, "pending": None}))   # адрес без почты не запоминается
        self.assertEqual(self.call("POST", "/api/boards", {"name": "Теплица", "device": name + "-1"}, cookie=cookie)[0], 201)
        self.assertEqual(self.call("POST", "/api/forgot", {"email": "ivan@example.com"})[0], 409)
        self.assertEqual(self.call("POST", "/api/account/email", {"email": "ivan@example.com", "password": self.PW}, cookie=cookie)[0], 409)
        self.assertEqual(self.call("POST", "/api/account/email/resend", {}, cookie=cookie)[0], 409)
        self.assertEqual(self.call("POST", "/api/login", {"username": "ivan@example.com", "password": self.PW})[0], 401)
        for path in ("/verify", "/reset", "/forgot"):
            self.assertEqual(self.request("GET", path)[0], 200, path)                  # страницы есть всегда, они сами скажут, что почты нет

    def test_login_logout_and_generic_failures(self):
        cookie, name = self.person()
        status, data, headers = self.call("POST", "/api/login", {"username": name, "password": self.PW})
        self.assertEqual(status, 200)
        second = self.cookie_of(headers)
        wrong = self.call("POST", "/api/login", {"username": name, "password": self.PW + "x"})
        unknown = self.call("POST", "/api/login", {"username": "no-such-user", "password": self.PW})
        self.assertEqual((wrong[0], unknown[0]), (401, 401))
        self.assertEqual(wrong[1], unknown[1])                              # по ответу нельзя узнать, есть ли такое имя
        self.assertEqual(self.call("POST", "/api/login", {"username": name})[0], 400)
        self.assertEqual(self.request("POST", "/api/login", json.dumps({"username": name, "password": self.PW}).encode(),
                                      {"Content-Type": "application/json"})[0], 403)
        status, _, headers = self.call("POST", "/api/logout", cookie=second)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/me", cookie=second)[0], 401)       # эта сессия закрыта
        self.assertEqual(self.request("GET", "/api/me", cookie=cookie)[0], 200)       # другая жива
        self.assertEqual(self.request("POST", "/api/logout", b"{}", {"Content-Type": "application/json"}, cookie)[0], 403)

    def test_wrong_passwords_lock_the_address_and_the_name(self):
        _, name = self.person()
        for _ in range(10):
            self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": "wrong-password-1"})[0], 401)
        status, _, headers = self.call("POST", "/api/login", {"username": name, "password": self.PW})   # верный пароль после блокировки
        self.assertEqual((status, headers["Retry-After"]), (429, "600"))
        # на другом адресе не отключают имя навсегда, но подбор одного имени ограничен отдельно
        self.httpd.limiter = server.FailureLimiter()
        self.httpd.name_limiter = server.FailureLimiter(limit=2, window=600)
        self.call("POST", "/api/login", {"username": name, "password": "wrong-password-1"})
        self.call("POST", "/api/login", {"username": name, "password": "wrong-password-2"})
        self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": self.PW})[0], 429)
        # успешный вход не обнуляет счётчик: свой вход между подборами обойти лимит не позволяет
        self.httpd.limiter = server.FailureLimiter(limit=3, window=600)
        self.httpd.name_limiter = server.FailureLimiter()
        mine = self.person()[1]
        for index in range(3):
            self.call("POST", "/api/login", {"username": name, "password": "wrong-password-%d" % index})
            self.call("POST", "/api/login", {"username": mine, "password": self.PW})
        self.assertEqual(self.call("POST", "/api/login", {"username": mine, "password": self.PW})[0], 429)

    def test_people_cannot_use_http_basic_but_the_admin_can(self):
        _, name = self.person()
        person_basic = {"Authorization": "Basic " + base64.b64encode(("%s:%s" % (name, self.PW)).encode()).decode()}
        self.assertEqual(self.request("GET", "/api/me", headers=person_basic)[0], 401)
        self.assertEqual(json.loads(self.request("GET", "/api/me", admin=True)[2])["user"]["role"], "admin")
        status, data, headers = self.call("POST", "/api/login", {"username": "garden", "password": PASSWORD})
        self.assertEqual((status, data["user"]["role"]), (200, "admin"))        # администратор входит той же формой

    def test_people_work_only_with_their_own_boards(self):
        ann_cookie, ann = self.person()
        bob_cookie, bob = self.person()
        status, data, _ = self.call("POST", "/api/boards", {"name": "Теплица Анны", "device": ann + "-1"}, cookie=ann_cookie)
        self.assertEqual(status, 201, data)
        ann_token, ann_item = data["token"], data["item"]
        _, data, _ = self.call("POST", "/api/boards", {"name": "Шкаф Боба", "device": bob + "-1"}, cookie=bob_cookie)
        bob_token, bob_item = data["token"], data["item"]
        self.assertEqual(self.ingest(ann + "-1", ann_token), 200)
        self.assertEqual(self.ingest(bob + "-1", bob_token), 200)
        # списки: у каждого своя плата, общих токенов человек не видит
        listing = json.loads(self.request("GET", "/api/boards", cookie=ann_cookie)[2])
        self.assertEqual([b["device"] for b in listing["boards"]], [ann + "-1"])
        self.assertEqual((listing["shared_tokens"], listing["max_boards"]), ([], server.MAX_BOARDS_PER_USER))
        self.assertNotIn(bob, json.dumps(listing))
        self.assertEqual([d["device"] for d in json.loads(self.request("GET", "/api/devices", cookie=ann_cookie)[2])["devices"]], [ann + "-1"])
        # данные чужой платы недоступны всеми способами и выглядят как «нет данных»
        for path in ("/api/status", "/api/series?range=24h", "/api/logs", "/api/export.csv?range=24h"):
            sep = "&" if "?" in path else "?"
            self.assertEqual(self.request("GET", path + sep + "device=" + bob + "-1", cookie=ann_cookie)[0], 404, path)
            self.assertEqual(self.request("GET", path + sep + "device=" + ann + "-1", cookie=ann_cookie)[0], 200, path)
        self.assertEqual(json.loads(self.request("GET", "/api/status", cookie=ann_cookie)[2])["device"], ann + "-1")   # без device — своя плата
        # чужую плату нельзя ни изменить, ни удалить, ни выпустить для неё токен, ни отозвать чужой токен
        self.assertEqual(self.call("POST", "/api/boards/%s-1/tokens" % bob, {}, cookie=ann_cookie)[0], 404)
        self.assertEqual(self.call("POST", "/api/boards/%s-1/rename" % bob, {"name": "мой"}, cookie=ann_cookie)[0], 404)
        self.assertEqual(self.call("DELETE", "/api/boards/%s-1" % bob, cookie=ann_cookie)[0], 404)
        self.assertEqual(self.call("POST", "/api/tokens/%d/revoke" % bob_item["id"], cookie=ann_cookie)[0], 404)
        self.assertEqual(self.call("DELETE", "/api/tokens/%d" % bob_item["id"], cookie=ann_cookie)[0], 404)
        self.assertEqual(self.ingest(bob + "-1", bob_token), 200)               # токен Боба цел
        # общий токен из .env человек тоже не трогает
        shared = json.loads(self.request("GET", "/api/boards", admin=True)[2])["shared_tokens"][0]
        self.assertEqual(self.call("POST", "/api/tokens/%d/revoke" % shared["id"], cookie=ann_cookie)[0], 404)
        # а свою плату человек ведёт сам: замена токена, переименование, отзыв, удаление
        self.assertEqual(self.call("POST", "/api/boards/%s-1/rename" % ann, {"name": "Новое имя"}, cookie=ann_cookie)[0], 200)
        status, data, _ = self.call("POST", "/api/boards/%s-1/tokens" % ann, {"name": "Запасной"}, cookie=ann_cookie)
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", "/api/tokens/%d/revoke" % ann_item["id"], cookie=ann_cookie)[0], 200)
        self.assertEqual(self.call("POST", "/api/tokens/%d/revoke" % data["item"]["id"], cookie=ann_cookie)[0], 200)
        self.assertEqual(self.call("DELETE", "/api/boards/%s-1" % ann, cookie=ann_cookie)[0], 200)
        # администратор видит всё и всех, с владельцами
        everything = json.loads(self.request("GET", "/api/boards", admin=True)[2])
        owners = {b["device"]: b["owner"] for b in everything["boards"]}
        self.assertEqual(owners.get(bob + "-1"), bob)
        self.assertNotIn(ann + "-1", owners)
        self.assertIn(bob + "-1", [d["device"] for d in json.loads(self.request("GET", "/api/devices", admin=True)[2])["devices"]])

    def test_a_person_has_a_limited_number_of_boards(self):
        cookie, name = self.person()
        self.store.max_boards_per_user = 2
        for index in range(2):
            self.assertEqual(self.call("POST", "/api/boards", {"name": "Плата", "device": "%s-%d" % (name, index)}, cookie=cookie)[0], 201)
        status, data, _ = self.call("POST", "/api/boards", {"name": "Лишняя", "device": name + "-9"}, cookie=cookie)
        self.assertEqual((status, "предел" in data["error"]), (409, True))
        self.assertEqual(self.call("POST", "/api/boards", {"name": "Админская", "device": name + "-adm"}, admin=True)[0], 201)

    def test_names_of_boards_are_global_so_nobody_can_take_over_someone_elses_name(self):
        ann_cookie, _ = self.person()
        bob_cookie, _ = self.person()
        name = self.unique("shared-name")
        self.assertEqual(self.call("POST", "/api/boards", {"name": "Анны", "device": name}, cookie=ann_cookie)[0], 201)
        self.assertEqual(self.call("POST", "/api/boards", {"name": "Боба", "device": name}, cookie=bob_cookie)[0], 409)

    def test_the_ingest_rate_is_limited_per_board(self):
        cookie, name = self.person()
        _, data, _ = self.call("POST", "/api/boards", {"name": "Плата", "device": name + "-r"}, cookie=cookie)
        _, other, _ = self.call("POST", "/api/boards", {"name": "Вторая", "device": name + "-s"}, cookie=cookie)
        self.httpd.ingest_gate = server.RateGate(5)
        self.assertEqual(self.ingest(name + "-r", data["token"]), 200)
        status, headers, _ = self.request("POST", "/api/ingest", json.dumps({"device": name + "-r"}).encode(),
                                          {"Content-Type": "application/json", "Authorization": "Bearer " + data["token"]})
        self.assertEqual((status, headers["Retry-After"]), (429, "5"))
        self.assertEqual(self.ingest(name + "-s", other["token"]), 200)       # другая плата не задета

    def test_admin_page_and_api_are_for_the_admin_only(self):
        cookie, name = self.person()
        status, headers, _ = self.request("GET", "/admin", cookie=cookie)
        self.assertEqual((status, headers["Location"]), (302, "/"))
        self.assertEqual(self.request("GET", "/api/admin/users", cookie=cookie)[0], 403)
        self.assertEqual(self.call("POST", "/api/admin/registration", {"open": False}, cookie=cookie)[0], 403)
        self.assertEqual(self.call("POST", "/api/admin/users/1/disable", cookie=cookie)[0], 403)
        self.assertEqual(self.request("GET", "/admin", admin=True)[0], 200)
        data = json.loads(self.request("GET", "/api/admin/users", admin=True)[2])
        row = [u for u in data["users"] if u["username"] == name][0]
        self.assertEqual((row["role"], row["disabled"], row["boards"]), ("user", False, 0))
        self.assertNotIn("pass", json.dumps(data))
        self.assertTrue(data["registration_open"])

    def test_admin_can_block_reset_and_delete_a_person(self):
        cookie, name = self.person()
        _, data, _ = self.call("POST", "/api/boards", {"name": "Плата", "device": name + "-b"}, cookie=cookie)
        user_id = [u for u in self.store.list_users() if u["username"] == name][0]["id"]
        self.assertEqual(self.ingest(name + "-b", data["token"]), 200)
        # блокировка: вход закрыт, сессия закрыта, плата данные присылать перестаёт
        status, answer, _ = self.call("POST", "/api/admin/users/%d/disable" % user_id, admin=True)
        self.assertEqual((status, answer["user"]["disabled"]), (200, True))
        self.assertEqual(self.request("GET", "/api/me", cookie=cookie)[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": self.PW})[0], 401)
        self.assertEqual(self.ingest(name + "-b", data["token"]), 401)
        self.assertEqual(self.call("POST", "/api/admin/users/%d/enable" % user_id, admin=True)[0], 200)
        self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": self.PW})[0], 200)
        # новый пароль вместо забытого
        status, answer, _ = self.call("POST", "/api/admin/users/%d/reset-password" % user_id, admin=True)
        temp = answer["password"]
        self.assertEqual(status, 200)
        self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": self.PW})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": temp})[0], 200)
        # удаление вместе с платами
        self.assertEqual(self.call("DELETE", "/api/admin/users/%d" % user_id, admin=True)[0], 200)
        self.assertEqual(self.call("DELETE", "/api/admin/users/%d" % user_id, admin=True)[0], 404)
        self.assertFalse(self.store.board_exists(name + "-b"))
        self.assertEqual(self.ingest(name + "-b", data["token"]), 401)
        # администратора трогать нельзя
        admin_id = self.store.admin_id
        for action, method in (("disable", "POST"), ("reset-password", "POST"), ("", "DELETE")):
            path = "/api/admin/users/%d%s" % (admin_id, "/" + action if action else "")
            self.assertEqual(self.call(method, path, admin=True)[0], 409, path)
        self.assertEqual(self.call("POST", "/api/admin/users/99999/disable", admin=True)[0], 404)
        self.assertEqual(self.call("POST", "/api/admin/users/abc/disable", admin=True)[0], 404)

    def test_a_person_changes_the_own_password(self):
        cookie, name = self.person()
        other = self.cookie_of(self.call("POST", "/api/login", {"username": name, "password": self.PW})[2])
        self.assertEqual(self.call("POST", "/api/account/password", {"old": "wrong-password-1", "new": "brand new password"}, cookie=cookie)[0], 400)
        self.assertEqual(self.call("POST", "/api/account/password", {"old": self.PW, "new": "short"}, cookie=cookie)[0], 400)
        self.assertEqual(self.request("POST", "/api/account/password", b"{}", {"Content-Type": "application/json"}, cookie)[0], 403)
        self.assertEqual(self.call("POST", "/api/account/password", {"old": self.PW, "new": "brand new password"}, cookie=cookie)[0], 200)
        self.assertEqual(self.request("GET", "/api/me", cookie=cookie)[0], 200)
        self.assertEqual(self.request("GET", "/api/me", cookie=other)[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": self.PW})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": "brand new password"})[0], 200)
        # у администратора пароль в .env
        self.assertEqual(self.call("POST", "/api/account/password", {"old": PASSWORD, "new": "another password 1"}, admin=True)[0], 409)

    def test_the_login_redirect_keeps_the_page_and_encodes_it(self):
        status, headers, _ = self.request("GET", "/boards?a=1&b=http://evil.example")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/login?next=%2Fboards%3Fa%3D1%26b%3Dhttp%3A%2F%2Fevil.example")

    def test_broken_cookies_mean_nobody_is_signed_in(self):
        for value in ("", "x", "x" * 500, "a=b;c", "../../etc/passwd"):
            self.assertEqual(self.request("GET", "/api/me", headers={"Cookie": server.SESSION_COOKIE + "=" + value})[0], 401, value)
        self.assertEqual(self.request("GET", "/api/me", headers={"Cookie": "garbage"})[0], 401)

    def test_a_person_cannot_use_the_admin_boards_or_board_names_of_the_admin(self):
        cookie, name = self.person()
        _, data, _ = self.call("POST", "/api/boards", {"name": "Админская", "device": name + "-adm"}, admin=True)
        self.assertEqual(self.request("GET", "/api/status?device=%s-adm" % name, cookie=cookie)[0], 404)
        self.assertEqual(self.call("DELETE", "/api/boards/%s-adm" % name, cookie=cookie)[0], 404)
        self.assertEqual(self.call("POST", "/api/tokens/%d/revoke" % data["item"]["id"], cookie=cookie)[0], 404)


# Настройки, как их сообщает плата с настройками по умолчанию (прошивка: таблица SETTING_DEFS)
BOARD_VALUES = {
    "operation_mode": 0, "temp_target": 25.0, "temp_target_night": 17.0, "temp_delta": 2.0,
    "hum_target_night": 55.0, "hum_band_night": 10.0,
    "drying_temp_target": 16.0, "drying_temp_delta": 0.5, "drying_hum_target": 55.0, "drying_hum_band": 5.0,
    "led_on_hour": 6, "led_on_minute": 0, "led_off_hour": 18, "led_off_minute": 0,
    "led_min_limit": 10, "led_max_limit": 100, "fan1_min_limit": 20, "fan1_max_limit": 100,
    "fan2_min_limit": 20, "fan2_max_limit": 100, "heater_mode": 3, "rgb_brightness": 19, "rgb_night_mode": 0,
    "watering_days": 0, "watering_hour": 8, "watering_minute": 0, "watering_duration": 30,
    "start_time": 0, "water_sensor_enabled": False,
}
ADMIN = {"id": 1, "username": "garden", "role": "admin"}


def board_report(cloud=True, last_id="", res="", **values):
    """Раздел cfg отправки платы."""
    return {"cloud": cloud, "id": last_id, "res": res, "v": dict(BOARD_VALUES, **values)}


class CloudSettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = server.Store(Path(self.tmp.name) / "c.sqlite3")
        self.who = {"id": 7, "username": "ivan", "role": "user"}

    def send(self, now, cfg, device="box"):
        """Отправка платы с разделом cfg -> ответ сервера."""
        return self.store.ingest(device, {"sample": {"temp": 22.0, "sht_ok": True}, "cfg": cfg}, now=now)

    def test_the_board_report_is_stored_and_cleaned(self):
        self.assertFalse(self.store.cfg_state("box", NOW)["supported"])
        reply = self.send(NOW, board_report(temp_target=26.5, rgb_brightness=500))
        self.assertNotIn("cfg", reply)
        state = self.store.cfg_state("box", NOW + 5)
        self.assertEqual((state["supported"], state["cloud"], state["reported_at"]), (True, True, NOW))
        self.assertEqual(state["values"]["temp_target"], 26.5)
        self.assertNotIn("rgb_brightness", state["values"])              # вне пределов: не показывается
        self.assertIs(state["values"]["water_sensor_enabled"], False)
        junk = {"cloud": True, "v": {"wifi_pass": "x", "unknown": 1, "temp_target": True, "led_on_hour": "7",
                                      "temp_delta": float("nan"), "heater_mode": 2, "water_sensor_enabled": 1}}
        self.send(NOW + 60, junk)
        self.assertEqual(self.store.cfg_state("box", NOW + 61)["values"], {"heater_mode": 2})   # прочее отброшено
        self.send(NOW + 120, {"cloud": True, "v": {"junk": 1}})
        self.assertEqual(self.store.cfg_state("box", NOW + 121)["values"], {"heater_mode": 2})  # негодное не стирает прежнее

    def test_old_firmware_and_a_closed_switch_refuse_requests(self):
        with self.assertRaises(server.Conflict) as caught:
            self.store.cfg_submit("box", {"temp_target": 24}, self.who, NOW)
        self.assertIn("прошивка", str(caught.exception))
        self.send(NOW, board_report(cloud=False))
        with self.assertRaises(server.Conflict) as caught:
            self.store.cfg_submit("box", {"temp_target": 24}, self.who, NOW)
        self.assertIn("на самой плате", str(caught.exception))

    def test_a_request_travels_with_the_next_sends_until_the_board_confirms_it(self):
        self.send(NOW, board_report())
        state = self.store.cfg_submit("box", {"temp_target": 24.5, "led_on_hour": 7}, self.who, NOW + 10)
        self.assertEqual(state["pending"]["set"], {"led_on_hour": 7, "temp_target": 24.5})
        self.assertEqual((state["pending"]["by"], state["pending"]["expires_at"]), ("ivan", NOW + 10 + server.CFG_TTL))
        change_id = state["pending"]["id"]
        self.assertRegex(change_id, r"^[0-9a-f]{8}$")
        for minute in (1, 2):                                            # ответ потерялся: запрос приходит снова
            reply = self.send(NOW + 60 * minute, board_report())
            self.assertEqual(reply["cfg"], {"id": change_id, "set": {"led_on_hour": 7, "temp_target": 24.5}})
        self.assertEqual(self.store.cfg_state("box", NOW + 130)["pending"]["id"], change_id)
        reply = self.send(NOW + 180, board_report(last_id=change_id, res="ok", temp_target=24.5, led_on_hour=7))
        self.assertNotIn("cfg", reply)
        state = self.store.cfg_state("box", NOW + 181)
        self.assertIsNone(state["pending"])
        self.assertEqual([(h["status"], h["by"]) for h in state["history"]], [("applied", "ivan")])
        self.assertEqual(state["history"][0]["done_at"], NOW + 180)
        self.assertEqual(state["values"]["temp_target"], 24.5)
        self.assertNotIn("cfg", self.send(NOW + 240, board_report(last_id=change_id, res="ok")))   # подтверждение не повторяется

    def test_a_refusal_from_the_board_is_recorded_with_its_reason(self):
        self.send(NOW, board_report())
        change_id = self.store.cfg_submit("box", {"temp_target": 24}, self.who, NOW)["pending"]["id"]
        self.assertIn("cfg", self.send(NOW + 60, board_report()))
        self.assertNotIn("cfg", self.send(NOW + 120, board_report(last_id=change_id, res="range:temp_target")))
        state = self.store.cfg_state("box", NOW + 121)
        self.assertEqual((state["history"][0]["status"], state["history"][0]["note"]), ("rejected", "range:temp_target"))
        self.assertIsNone(state["pending"])

    def test_switching_the_permission_off_rejects_the_waiting_request(self):
        self.send(NOW, board_report())
        self.store.cfg_submit("box", {"temp_target": 24}, self.who, NOW)
        self.assertNotIn("cfg", self.send(NOW + 60, board_report(cloud=False)))   # плата запросов не принимает: не отправляем
        state = self.store.cfg_state("box", NOW + 61)
        self.assertEqual((state["history"][0]["status"], state["history"][0]["note"]), ("rejected", "off"))
        self.assertFalse(state["cloud"])

    def test_a_request_that_nobody_took_expires(self):
        self.send(NOW, board_report())
        self.store.cfg_submit("box", {"temp_target": 24}, self.who, NOW)
        late = NOW + server.CFG_TTL + 1
        self.assertNotIn("cfg", self.send(late, board_report()))                  # плата вернулась поздно: изменение устарело
        state = self.store.cfg_state("box", late)
        self.assertEqual((state["history"][0]["status"], state["pending"]), ("expired", None))
        self.store.cfg_submit("box", {"temp_target": 23}, self.who, late + 1)   # новый запрос после просрочки возможен
        self.assertIn("cfg", self.send(late + 60, board_report()))

    def test_state_reading_also_closes_expired_requests(self):
        self.send(NOW, board_report())
        self.store.cfg_submit("box", {"temp_target": 24}, self.who, NOW)
        state = self.store.cfg_state("box", NOW + server.CFG_TTL + 5)             # плата молчит: страница видит просрочку
        self.assertEqual((state["history"][0]["status"], state["pending"]), ("expired", None))

    def test_values_that_already_stand_on_the_board_are_not_sent(self):
        self.send(NOW, board_report())
        with self.assertRaises(ValueError) as caught:
            self.store.cfg_submit("box", {"temp_target": 25, "led_on_hour": 6}, self.who, NOW)
        self.assertIn("нет изменений", str(caught.exception))
        state = self.store.cfg_submit("box", {"temp_target": 25, "led_on_hour": 7}, self.who, NOW)
        self.assertEqual(state["pending"]["set"], {"led_on_hour": 7})
        state = self.store.cfg_submit("box", {"led_on_hour": 6}, self.who, NOW + 1)   # возврат к прежнему: запрос больше не нужен
        self.assertIsNone(state["pending"])
        self.assertEqual(state["history"][0]["status"], "cancelled")

    def test_requests_waiting_for_the_board_are_merged(self):
        self.send(NOW, board_report())
        first = self.store.cfg_submit("box", {"temp_target": 24}, self.who, NOW)["pending"]
        other = {"id": 9, "username": "olga", "role": "user"}
        second = self.store.cfg_submit("box", {"temp_target": 23, "heater_mode": 2}, other, NOW + 30)["pending"]
        self.assertEqual(second["set"], {"heater_mode": 2, "temp_target": 23.0})
        self.assertNotEqual(second["id"], first["id"])                           # новый номер: старое подтверждение не годится
        self.assertEqual((second["by"], second["created_at"]), ("olga", NOW + 30))
        self.assertEqual(len(self.store.cfg_state("box", NOW + 31)["history"]), 1)
        self.assertEqual(self.send(NOW + 60, board_report())["cfg"]["id"], second["id"])

    def test_a_late_confirmation_of_a_cancelled_request_is_still_recorded(self):
        self.send(NOW, board_report())
        change_id = self.store.cfg_submit("box", {"temp_target": 24}, self.who, NOW)["pending"]["id"]
        self.store.cfg_cancel("box", NOW + 5)
        self.assertEqual(self.store.cfg_state("box", NOW + 6)["history"][0]["status"], "cancelled")
        self.send(NOW + 60, board_report(last_id=change_id, res="ok", temp_target=24))   # плата успела применить
        self.assertEqual(self.store.cfg_state("box", NOW + 61)["history"][0]["status"], "applied")

    def test_every_value_is_checked_against_the_table(self):
        self.send(NOW, board_report())
        bad = [{}, [], None, "x", {"unknown_setting": 1}, {"wifi_pass": "12345678"}, {"telemetry_token": "x" * 30},
               {"cloud_control": True}, {"water_sensor_enabled": True}, {"power_cal_pct": 3}, {"wifi_power": 1},
               {"temp_target": 41}, {"temp_target": -1}, {"temp_delta": 0.05}, {"hum_band_night": 26},
               {"temp_target": "25"}, {"temp_target": True}, {"temp_target": None}, {"temp_target": float("nan")},
               {"temp_target": float("inf")}, {"led_on_hour": 24}, {"led_on_hour": 6.5}, {"led_on_minute": 60},
               {"heater_mode": 4}, {"rgb_night_mode": 3}, {"watering_days": 128}, {"watering_duration": 301},
               {"watering_duration": 0}, {"start_time": 1793491201}, {"start_time": 1000}, {"start_time": 4102531200},
               {"start_time": -86400}, {"operation_mode": 2}]
        for patch in bad:
            with self.assertRaises(ValueError, msg=repr(patch)):
                self.store.cfg_submit("box", patch, self.who, NOW)
        self.assertIsNone(self.store.cfg_state("box", NOW)["pending"])
        good = {"temp_target": 0, "led_on_hour": 23, "watering_duration": 300, "watering_days": 127, "start_time": 1793491200,
                "hum_band_night": 25, "temp_delta": 0.1, "operation_mode": 1}
        self.assertEqual(self.store.cfg_submit("box", good, self.who, NOW)["pending"]["set"],
                         {"hum_band_night": 25, "led_on_hour": 23, "operation_mode": 1, "start_time": 1793491200,
                          "temp_delta": 0.1, "temp_target": 0, "watering_days": 127, "watering_duration": 300})
        self.assertIsNone(self.store.cfg_submit("box", {"start_time": 0}, self.who, NOW + 1)["pending"]["set"].get("start_time"))   # 0 — цикл не начат

    def test_a_flat_minimum_above_the_maximum_is_refused_before_it_is_sent(self):
        self.send(NOW, board_report())
        for patch in ({"led_min_limit": 101}, {"led_min_limit": 100, "led_max_limit": 50}, {"fan1_min_limit": 30, "fan1_max_limit": 20},
                      {"fan2_max_limit": 10}):
            with self.assertRaises(ValueError, msg=repr(patch)):
                self.store.cfg_submit("box", patch, self.who, NOW)
        state = self.store.cfg_submit("box", {"led_min_limit": 50, "led_max_limit": 60, "fan2_min_limit": 0, "fan2_max_limit": 5}, self.who, NOW)
        self.assertEqual(state["pending"]["set"]["led_max_limit"], 60)

    def test_reported_values_may_exceed_what_the_panel_can_set(self):
        self.send(NOW, board_report(watering_duration=900, start_time=1793491234))   # поставлено на самой плате
        values = self.store.cfg_state("box", NOW)["values"]
        self.assertEqual((values["watering_duration"], values["start_time"]), (900, 1793491234))
        with self.assertRaises(ValueError):
            self.store.cfg_submit("box", {"watering_duration": 900}, self.who, NOW)

    def test_history_is_limited_and_deleting_a_board_removes_everything(self):
        self.send(NOW, board_report())
        for step in range(server.CFG_HISTORY + 5):
            self.store.cfg_submit("box", {"temp_target": 20 + (step % 2)}, self.who, NOW + step)
            self.store.cfg_cancel("box", NOW + step)
        state = self.store.cfg_state("box", NOW + 100)
        self.assertEqual(len(state["history"]), server.CFG_HISTORY)
        self.assertIsNone(state["pending"])
        _, _, item = self.store.add_board("Ящик", "box")
        self.store.revoke_token(item["id"])                                      # пока токен действует, плату не удалить
        self.assertTrue(self.store.delete_board("box"))
        state = self.store.cfg_state("box", NOW + 100)
        self.assertEqual((state["supported"], state["history"]), (False, []))

    def test_the_reply_can_be_read_by_the_strict_parser_of_the_board(self):
        """Тот же разбор, что в CloudSettings.h: порядок id, set; имена [a-z0-9_]; числа без порядка; не больше 32."""
        self.send(NOW, board_report())
        full = {"operation_mode": 1, "temp_target": 24.55, "temp_target_night": 0, "temp_delta": 0.1, "hum_target_night": 100,
                "hum_band_night": 25, "drying_temp_target": 39.9, "drying_temp_delta": 20, "drying_hum_target": 1,
                "drying_hum_band": 0.1, "led_on_hour": 23, "led_on_minute": 59, "led_off_hour": 0, "led_off_minute": 1,
                "led_min_limit": 1, "led_max_limit": 99, "fan1_min_limit": 1, "fan1_max_limit": 99, "fan2_min_limit": 1,
                "fan2_max_limit": 99, "heater_mode": 0, "rgb_brightness": 100, "rgb_night_mode": 2, "watering_days": 127,
                "watering_hour": 23, "watering_minute": 59, "watering_duration": 300, "start_time": 4102444800}
        self.assertEqual(set(full), set(server.CFG_SPECS))
        self.store.cfg_submit("box", full, self.who, NOW)
        body = json.dumps(self.send(NOW + 60, board_report()), ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(body.encode("utf-8")), 2048)                      # CLOUD_REPLY_MAX в прошивке
        match = re.search(r'"cfg":\{"id":"([0-9a-f]{1,16})","set":\{([^{}]*)\}\}', body)
        self.assertIsNotNone(match, body)
        pairs = match.group(2).split(",")
        self.assertEqual(len(pairs), 28)
        for pair in pairs:
            self.assertRegex(pair, r'^"[a-z0-9_]{1,23}":-?[0-9]{1,15}(\.[0-9]{1,14})?$')

    def test_the_sets_of_names_in_the_schema_and_in_the_table_are_the_same(self):
        shown = []
        for group in server.CFG_SCHEMA:
            for field in group["fields"]:
                shown += field.get("params") or [field["param"]]
        self.assertCountEqual(shown, list(server.CFG_SPECS) + list(server.CFG_READONLY))
        self.assertEqual(len({g["id"] for g in server.CFG_SCHEMA}), len(server.CFG_SCHEMA))
        for group in server.CFG_SCHEMA:
            for field in group["fields"]:
                if field["type"] in ("number", "select", "date", "days"):
                    self.assertEqual((field["kind"], field["lo"], field["hi"]), server.CFG_SPECS[field["param"]])
                if field["type"] == "select":
                    lo, hi = server.CFG_SPECS[field["param"]][1:]
                    self.assertEqual([o[0] for o in field["options"]], list(range(lo, hi + 1)))

    def test_groups_that_do_not_work_in_a_mode_say_so(self):
        """В сушке свет и полив выключены, поэтому страница прячет их настройки (и климат выращивания, и цикл)."""
        self.assertEqual((server.GROWING, server.DRYING), (0, 1))
        modes = {group["id"]: group.get("modes") for group in server.CFG_SCHEMA}
        self.assertEqual(modes, {"mode": None, "climate": [0], "drying": [1], "light": [0], "fans": None, "heater": None,
                                 "watering": [0], "indicator": None, "cycle": [0]})
        spec_mode = server.CFG_SPECS["operation_mode"]
        for group in server.CFG_SCHEMA:
            for mode in group.get("modes", []):
                self.assertTrue(spec_mode[1] <= mode <= spec_mode[2], group["id"])
        # у группы без режима ключа нет, у группы с режимом он на месте

        self.assertNotIn("modes", next(g for g in server.CFG_SCHEMA if g["id"] == "fans"))
        self.assertEqual(next(g for g in server.CFG_SCHEMA if g["id"] == "light")["modes"], [0])


class FirmwareContractTests(unittest.TestCase):
    """Список и пределы настроек из панели на сервере совпадают с прошивкой (firmware/CloudSettings.h и SETTING_DEFS)."""
    FIRMWARE = Path(__file__).resolve().parents[2] / "firmware"

    def setUp(self):
        if not (self.FIRMWARE / "firmware.ino").exists():
            self.skipTest("каталога firmware рядом нет")
        self.ino = (self.FIRMWARE / "firmware.ino").read_text(encoding="utf-8")
        self.cloud = (self.FIRMWARE / "CloudSettings.h").read_text(encoding="utf-8")
        self.constants = {"HEATER_HARD_LIMIT_C": 40.0, "OP_MODE_GROWING": 0, "OP_MODE_DRYING": 1,
                          "RGB_NIGHT_SAME": 0, "RGB_NIGHT_OFF": 2, "WIFI_POWER_WEAK": 0, "WIFI_POWER_STRONG": 2}

    def number(self, text):
        text = text.strip()
        if text in self.constants:
            return self.constants[text]
        try:
            return float(text.rstrip("f"))
        except ValueError:
            return text                       # имя константы, которой нет в списке: сравнивать с ним нечего

    def table(self):
        """SETTING_DEFS из firmware.ino -> {имя: (тип, минимум, максимум)}."""
        rows = {}
        for macro, param, lo, hi in re.findall(r'SET_(FLOAT|INT|U8)\s*\(\s*"(\w+)"\s*,\s*"\w+"\s*,\s*\w+\s*,\s*([^,]+),\s*([^,]+),', self.ino):
            rows[param] = ("float" if macro == "FLOAT" else "int", self.number(lo), self.number(hi))
        for param in re.findall(r'SET_BOOL\s*\(\s*"(\w+)"', self.ino):
            rows[param] = ("flag", 0, 1)
        for param in re.findall(r'SET_U32_NOFORM\s*\(\s*"(\w+)"', self.ino):
            rows[param] = ("date", 0, 0)
        return rows

    def list_in_header(self, name):
        block = re.search(r"%s\[\] = \{(.*?)\};" % name, self.cloud, re.S).group(1)
        return re.findall(r'"(\w+)"', block)

    def test_the_lists_of_names_are_the_same(self):
        self.assertCountEqual(self.list_in_header("CLOUD_PARAMS"), list(server.CFG_SPECS))
        self.assertCountEqual(self.list_in_header("CLOUD_REPORT_ONLY"), list(server.CFG_READONLY))

    def test_ranges_follow_the_table_of_the_board(self):
        board = self.table()
        cap = float(re.search(r"CLOUD_WATERING_MAX_SEC = (\d+)", self.cloud).group(1))
        for param, (kind, lo, hi) in server.CFG_SPECS.items():
            self.assertIn(param, board, param)
            if kind == "date":
                self.assertEqual(board[param][0], "date", param)
                continue
            local = board[param]
            self.assertEqual(local[0], kind, param)
            self.assertEqual(lo, local[1], param)
            expected_hi = min(local[2], cap) if param == "watering_duration" else local[2]
            self.assertEqual(hi, expected_hi, param)
        self.assertEqual(server.CFG_LOCAL_HI["watering_duration"], board["watering_duration"][2])
        for param in server.CFG_READONLY:
            self.assertEqual(board[param][0], "flag", param)

    def test_groups_hidden_by_mode_match_the_page_of_the_board(self):
        """Что страница платы прячет в сушке (data-modes и data-mode-only), то панель не показывает."""
        html = (self.FIRMWARE / "data" / "settings.html").read_text(encoding="utf-8")
        tabs = dict(re.findall(r'data-tab="tab-(\w+)" data-modes="([a-z,]+)"', html))
        names = {"growing": [server.GROWING], "drying": [server.DRYING], "growing,drying": None}
        modes = {group["id"]: group.get("modes") for group in server.CFG_SCHEMA}
        for group in ("light", "watering", "fans", "heater"):                       # вкладки платы совпадают с группами панели
            self.assertEqual(modes[group], names[tabs[group]], group)
        self.assertRegex(html, r'data-mode-only="growing">\s*<div class="form-section">\s*<h3>Температура и влажность')
        self.assertRegex(html, r'data-mode-only="drying">\s*<h3>Климат сушки')
        self.assertRegex(html, r'data-mode-only="growing">\s*<h3>Гровинг')           # дата начала цикла
        self.assertEqual((modes["climate"], modes["drying"], modes["cycle"]), ([server.GROWING], [server.DRYING], [server.GROWING]))
        self.assertIsNone(modes["indicator"])                                      # индикатор работает в обоих режимах
        self.assertIsNone(modes["mode"])

    def test_the_switch_and_the_secrets_are_not_in_the_list(self):
        names = self.list_in_header("CLOUD_PARAMS")
        for forbidden in ("cloud_control", "wifi_ssid", "wifi_pass", "wifi_power", "ap_ssid", "ap_pass", "telemetry_url",
                          "telemetry_token", "device_label", "power_cal_pct", "water_sensor_enabled"):
            self.assertNotIn(forbidden, names)
        self.assertRegex(self.ino, r'SET_BOOL \("cloud_control",\s*"cloud_ctl",\s*cloud_control,\s*false\)')   # по умолчанию выключено


class CloudSettingsHttpTests(unittest.TestCase):
    ACTION = {"X-Dash-Action": "1", "Content-Type": "application/json"}
    PW = "correct horse battery"
    counter = 0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = make_config(Path(cls.tmp.name) / "h.sqlite3")
        cls.store = server.Store(cfg.db)
        cls.httpd = server.DashServer(cfg, cls.store)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.httpd.limiter = server.FailureLimiter()
        self.httpd.reg_limiter = server.FailureLimiter(limit=1000, window=3600)
        self.httpd.ingest_gate = server.RateGate(0)
        self.httpd.cfg_gate = server.RateGate(0)
        self.store.set_registration(True)

    def request(self, method, path, body=None, headers=None, cookie=None, admin=False):
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = server.SESSION_COOKIE + "=" + cookie
        if admin:
            headers["Authorization"] = "Basic " + base64.b64encode(("garden:%s" % PASSWORD).encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def call(self, method, path, payload=None, cookie=None, admin=False, extra=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        status, headers, raw = self.request(method, path, body, dict(self.ACTION, **(extra or {})), cookie, admin)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        return status, data, headers

    def person(self):
        type(self).counter += 1
        name = "cfg%d" % type(self).counter
        status, data, headers = self.call("POST", "/api/register", {"username": name, "password": self.PW})
        self.assertEqual(status, 201, data)
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1], name

    def board(self, cookie, name):
        """Плата человека -> (имя платы, токен)."""
        device = name + "-box"
        status, data, _ = self.call("POST", "/api/boards", {"name": "Ящик", "device": device}, cookie=cookie)
        self.assertEqual(status, 201, data)
        return device, data["token"]

    def send(self, device, token, cfg):
        body = {"device": device, "sample": {"temp": 22.0, "sht_ok": True}, "cfg": cfg}
        status, _, raw = self.request("POST", "/api/ingest", json.dumps(body).encode(),
                                      {"Content-Type": "application/json", "Authorization": "Bearer " + token})
        return status, json.loads(raw)

    def test_everything_is_closed_without_a_login(self):
        self.assertEqual(self.request("GET", "/api/settings")[0], 401)
        self.assertEqual(self.call("POST", "/api/settings", {"device": "x", "set": {"temp_target": 20}})[0], 401)
        self.assertEqual(self.call("POST", "/api/settings/cancel", {"device": "x"})[0], 401)
        self.assertEqual(self.request("GET", "/settings")[0], 302)
        for path in ("/settings-lib.js", "/settings.js"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        cookie, _ = self.person()
        self.assertEqual(self.request("GET", "/settings", cookie=cookie)[0], 200)

    def test_the_person_changes_the_settings_of_their_board_end_to_end(self):
        cookie, name = self.person()
        device, token = self.board(cookie, name)
        self.assertEqual(self.request("GET", "/api/settings?device=" + device, cookie=cookie)[0], 404)   # плата ещё молчала
        self.send(device, token, board_report())
        status, data, _ = self.call("GET", "/api/settings?device=" + device, cookie=cookie)
        self.assertEqual(status, 200, data)
        self.assertEqual((data["device"], data["name"], data["supported"], data["cloud"], data["online"]),
                         (device, "Ящик", True, True, True))
        self.assertEqual(data["values"]["temp_target"], 25.0)
        self.assertEqual(data["ttl"], server.CFG_TTL)
        self.assertEqual([g["id"] for g in data["schema"]][:2], ["mode", "climate"])
        status, data, _ = self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 23.5, "heater_mode": 2}}, cookie=cookie)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["pending"]["set"], {"heater_mode": 2, "temp_target": 23.5})
        self.assertEqual(data["pending"]["by"], name)
        status, reply = self.send(device, token, board_report())
        self.assertEqual(reply["cfg"], {"id": data["pending"]["id"], "set": {"heater_mode": 2, "temp_target": 23.5}})
        status, reply = self.send(device, token, board_report(last_id=data["pending"]["id"], res="ok", temp_target=23.5, heater_mode=2))
        self.assertNotIn("cfg", reply)
        _, data, _ = self.call("GET", "/api/settings?device=" + device, cookie=cookie)
        self.assertEqual((data["pending"], data["history"][0]["status"], data["values"]["heater_mode"]), (None, "applied", 2))

    def test_a_stranger_sees_and_changes_nothing_but_the_admin_can(self):
        owner, name = self.person()
        device, token = self.board(owner, name)
        self.send(device, token, board_report())
        stranger, _ = self.person()
        self.assertEqual(self.request("GET", "/api/settings?device=" + device, cookie=stranger)[0], 404)
        self.assertEqual(self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 20}}, cookie=stranger)[0], 404)
        self.assertEqual(self.call("POST", "/api/settings/cancel", {"device": device}, cookie=stranger)[0], 404)
        self.assertEqual(self.call("POST", "/api/settings", {"device": "no-such-box", "set": {"temp_target": 20}}, cookie=owner)[0], 404)
        self.assertEqual(self.call("POST", "/api/settings", {"device": 5, "set": {"temp_target": 20}}, cookie=owner)[0], 404)
        self.assertEqual(self.call("POST", "/api/settings", {"device": "../x", "set": {"temp_target": 20}}, cookie=owner)[0], 404)
        status, data, _ = self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 20}}, admin=True)
        self.assertEqual((status, data["pending"]["by"]), (200, "garden"))
        _, data, _ = self.call("GET", "/api/settings?device=" + device, cookie=owner)
        self.assertEqual(data["history"][0]["by"], "garden")                  # владелец видит, кто менял его плату

    def test_requests_from_other_sites_and_without_the_page_header_are_refused(self):
        cookie, name = self.person()
        device, token = self.board(cookie, name)
        self.send(device, token, board_report())
        body = json.dumps({"device": device, "set": {"temp_target": 20}}).encode()
        self.assertEqual(self.request("POST", "/api/settings", body, {"Content-Type": "application/json"}, cookie)[0], 403)
        self.assertEqual(self.request("POST", "/api/settings", body, dict(self.ACTION, **{"Sec-Fetch-Site": "cross-site"}), cookie)[0], 403)
        self.assertEqual(self.request("POST", "/api/settings", body, {"X-Dash-Action": "1", "Content-Type": "text/plain"}, cookie)[0], 400)
        self.assertEqual(self.request("POST", "/api/settings", b"{oops", self.ACTION, cookie)[0], 400)
        self.assertEqual(self.request("GET", "/api/settings/cancel", cookie=cookie)[0], 405)
        huge = json.dumps({"device": device, "set": {"temp_target": 20}, "pad": "x" * 3000}).encode()
        self.assertEqual(self.request("POST", "/api/settings", huge, self.ACTION, cookie)[0], 400)
        self.assertIsNone(self.store.cfg_state(device)["pending"])

    def test_refusals_explain_themselves(self):
        cookie, name = self.person()
        device, token = self.board(cookie, name)
        status, data, _ = self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 20}}, cookie=cookie)
        self.assertEqual(status, 409)                                         # плата ещё не сообщала настройки
        self.assertIn("прошивка", data["error"])
        self.send(device, token, board_report(cloud=False))
        status, data, _ = self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 20}}, cookie=cookie)
        self.assertEqual(status, 409)
        self.assertIn("на самой плате", data["error"])
        self.send(device, token, board_report())
        status, data, _ = self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 99}}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertIn("Дневная целевая температура", data["error"])
        status, data, _ = self.call("POST", "/api/settings", {"device": device, "set": {"wifi_pass": "secret-pass"}}, cookie=cookie)
        self.assertEqual(status, 400)
        status, data, _ = self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 25}}, cookie=cookie)
        self.assertEqual((status, "нет изменений" in data["error"]), (400, True))
        status, data, _ = self.call("POST", "/api/settings", {"device": device, "set": {"led_min_limit": 90, "led_max_limit": 80}}, cookie=cookie)
        self.assertEqual((status, "минимум" in data["error"]), (400, True))

    def test_requests_come_no_more_often_than_every_two_seconds(self):
        cookie, name = self.person()
        device, token = self.board(cookie, name)
        self.send(device, token, board_report())
        self.httpd.cfg_gate = server.RateGate(2)
        self.assertEqual(self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 20}}, cookie=cookie)[0], 200)
        status, _, headers = self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 21}}, cookie=cookie)
        self.assertEqual((status, headers["Retry-After"]), (429, "2"))

    def test_cancel_removes_the_waiting_request(self):
        cookie, name = self.person()
        device, token = self.board(cookie, name)
        self.send(device, token, board_report())
        self.call("POST", "/api/settings", {"device": device, "set": {"temp_target": 20}}, cookie=cookie)
        status, data, _ = self.call("POST", "/api/settings/cancel", {"device": device}, cookie=cookie)
        self.assertEqual((status, data["pending"], data["history"][0]["status"]), (200, None, "cancelled"))
        self.assertNotIn("cfg", self.send(device, token, board_report())[1])

    def test_ingest_without_a_settings_section_is_unchanged(self):
        cookie, name = self.person()
        device, token = self.board(cookie, name)
        body = json.dumps({"device": device, "sample": {"temp": 22.0, "sht_ok": True}}).encode()
        status, _, raw = self.request("POST", "/api/ingest", body, {"Content-Type": "application/json", "Authorization": "Bearer " + token})
        self.assertEqual((status, set(json.loads(raw))), (200, {"ok", "log_next", "sample", "log_lines"}))
        status, _, raw = self.request("POST", "/api/ingest", json.dumps({"device": device, "cfg": "junk"}).encode(),
                                      {"Content-Type": "application/json", "Authorization": "Bearer " + token})
        self.assertEqual(status, 200)                                         # старая или испорченная плата приём не ломает

    def test_a_request_for_one_board_never_reaches_another(self):
        a, name_a = self.person()
        b, name_b = self.person()
        dev_a, tok_a = self.board(a, name_a)
        dev_b, tok_b = self.board(b, name_b)
        self.send(dev_a, tok_a, board_report(temp_target=21))
        self.send(dev_b, tok_b, board_report(temp_target=29))
        self.call("POST", "/api/settings", {"device": dev_a, "set": {"heater_mode": 1}}, cookie=a)
        self.assertNotIn("cfg", self.send(dev_b, tok_b, board_report(temp_target=29))[1])   # запрос чужой платы ей не уходит
        self.assertEqual(self.call("GET", "/api/settings?device=" + dev_b, cookie=b)[1]["values"]["temp_target"], 29.0)
        self.assertEqual(self.call("GET", "/api/settings", cookie=b)[1]["device"], dev_b)    # без device — первая своя плата


def mail_config(db, **extra):
    env = {"DASH_INGEST_TOKEN": TOKEN, "DASH_VIEW_PASSWORD": PASSWORD, "DASH_PORT": "0", "DASH_DB": str(db),
           "DASH_PUBLIC_URL": "https://dash.example.org", "DASH_SMTP_HOST": "smtp.example.org",
           "DASH_MAIL_FROM": "MicroGarden <noreply@example.org>"}
    env.update(extra)
    return server.Config(env)


class Outbox:
    """Вместо SMTP: письма копятся в списке, ссылка достаётся из текста письма."""

    def __init__(self):
        self.sent = []

    def __call__(self, message):
        self.sent.append(message)

    @staticmethod
    def token_of(message):
        match = re.search(r"#token=([A-Za-z0-9_-]+)", message.get_content())
        assert match, message.get_content()
        return match.group(1)

    def to(self, address):
        return [m for m in self.sent if m["To"] == address]

    def last_token(self, address):
        return self.token_of(self.to(address)[-1])


class FakeSmtp:
    """Минимальный SMTP-сервер, чтобы проверить настоящий smtplib: без шифрования, вход PLAIN, по письму за раз."""

    def __init__(self, login=("mailer", "s3cret"), can_starttls=False):
        self.login, self.can_starttls = login, can_starttls
        self.messages, self.commands = [], []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        with conn, conn.makefile("rwb") as stream:
            def say(text):
                stream.write((text + "\r\n").encode())
                stream.flush()

            say("220 fake ESMTP")
            sender, recipients, authed = None, [], False
            while True:
                raw = stream.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                verb = line.split(" ")[0].upper()
                self.commands.append(verb)
                if verb == "EHLO":
                    say("250-fake")
                    if self.can_starttls:
                        say("250-STARTTLS")
                    say("250-AUTH PLAIN")
                    say("250 8BITMIME")
                elif verb == "AUTH":
                    decoded = base64.b64decode(line.split(" ")[2]).split(b"\0")
                    authed = (decoded[1].decode(), decoded[2].decode()) == self.login
                    say("235 2.7.0 ok" if authed else "535 5.7.8 bad credentials")
                elif verb == "MAIL":
                    sender = re.search(r"<([^>]*)>", line).group(1)
                    say("250 ok")
                elif verb == "RCPT":
                    recipients.append(re.search(r"<([^>]*)>", line).group(1))
                    say("250 ok")
                elif verb == "DATA":
                    say("354 go")
                    lines = []
                    while True:
                        row = stream.readline().decode("utf-8", "replace").rstrip("\r\n")
                        if row == ".":
                            break
                        lines.append(row[1:] if row.startswith("..") else row)
                    self.messages.append({"from": sender, "to": list(recipients), "authed": authed, "data": "\r\n".join(lines).encode()})
                    say("250 queued")
                elif verb == "QUIT":
                    say("221 bye")
                    return
                else:
                    say("250 ok")


class MailConfigTests(unittest.TestCase):
    def test_mail_is_off_until_configured(self):
        cfg = server.Config({"DASH_VIEW_PASSWORD": PASSWORD})
        self.assertEqual((cfg.mail_enabled, cfg.mail_problems()), (False, []))
        self.assertFalse(server.Mailer(cfg).send("ivan@example.com", "тема", "текст"))

    def test_settings_turn_mail_on(self):
        cfg = mail_config("x.sqlite3", DASH_SMTP_USER="mailer", DASH_SMTP_PASSWORD="s3cret")
        self.assertEqual((cfg.mail_enabled, cfg.mail_problems(), cfg.smtp_mode, cfg.smtp_port), (True, [], "starttls", 587))
        self.assertEqual(mail_config("x", DASH_SMTP_PORT="465").smtp_mode, "ssl")                   # порт 465 — шифрование с первого байта
        self.assertEqual(mail_config("x", DASH_SMTP_PORT="465", DASH_SMTP_SECURITY="starttls").smtp_mode, "starttls")
        self.assertEqual(mail_config("x", DASH_SMTP_HOST="localhost", DASH_SMTP_SECURITY="none").mail_problems(), [])
        self.assertEqual(mail_config("x", DASH_PUBLIC_URL="http://localhost:8790").mail_problems(), [])
        self.assertEqual(mail_config("x", DASH_PUBLIC_URL="https://dash.example.org/").public_url, "https://dash.example.org")

    def test_wrong_settings_are_named(self):
        cases = {
            "DASH_PUBLIC_URL": [{"DASH_PUBLIC_URL": ""}, {"DASH_PUBLIC_URL": "http://dash.example.org"},
                                {"DASH_PUBLIC_URL": "https://dash.example.org/app"}, {"DASH_PUBLIC_URL": "dash.example.org"}],
            "DASH_MAIL_FROM": [{"DASH_MAIL_FROM": ""}, {"DASH_MAIL_FROM": "noreply"}, {"DASH_MAIL_FROM": "a@example.org\nBcc: x@example.org"}],
            "DASH_SMTP_HOST": [{"DASH_SMTP_HOST": ""}],
            "DASH_SMTP_PORT": [{"DASH_SMTP_PORT": "0"}, {"DASH_SMTP_PORT": "70000"}],
            "DASH_SMTP_SECURITY": [{"DASH_SMTP_SECURITY": "plain"}, {"DASH_SMTP_SECURITY": "none"}],   # none только для localhost
            "DASH_SMTP_USER": [{"DASH_SMTP_USER": "mailer"}, {"DASH_SMTP_PASSWORD": "s3cret"}],
            "DASH_MAIL_REPLY_TO": [{"DASH_MAIL_REPLY_TO": "not an address"}],
            "DASH_MAIL_DAILY_LIMIT": [{"DASH_MAIL_DAILY_LIMIT": "0"}],
        }
        for name, variants in cases.items():
            for extra in variants:
                problems = mail_config("x", **extra).mail_problems()
                self.assertTrue(any(p.startswith(name) for p in problems), (extra, problems))
        with self.assertRaises(SystemExit):
            mail_config("x", DASH_MAIL_FROM="").validate()         # сервер с неверной почтой не запускается
        mail_config("x").validate()

    def test_partial_mail_settings_do_not_turn_mail_on(self):
        cfg = server.Config({"DASH_VIEW_PASSWORD": PASSWORD, "DASH_PUBLIC_URL": "https://dash.example.org"})
        self.assertEqual((cfg.mail_enabled, cfg.mail_problems()), (False, []))       # адрес сайта нужен и для входа через Google
        self.assertEqual(cfg.public_url, "https://dash.example.org")


class EmailRulesTests(unittest.TestCase):
    def test_clean_email(self):
        self.assertEqual(server.clean_email("  Ivan.P+garden@Example.COM "), "ivan.p+garden@example.com")
        for good in ("a@b.co", "x" * 64 + "@example.com", "a.b-c_d%e@sub.example.org"):
            server.clean_email(good)
        for bad in ("", "ivan", "ivan@", "@example.com", "ivan@example", "ivan@@example.com", "iv an@example.com", "ivan@exa_mple.com",
                    ".ivan@example.com", "ivan.@example.com", "iv..an@example.com", "ivan@example..com", "ivan@-example.com",
                    "иван@example.com", "ivan@пример.рф", "ivan@example.com\r\nBcc: x@example.org", "<ivan@example.com>",
                    "x" * 65 + "@example.com", "a@" + "b" * 250 + ".com", None, 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                server.clean_email(bad)

    def test_addresses_are_removed_from_log_text(self):
        text = server.redact_emails("550 5.1.1 <ivan@example.com>: user unknown; also a.b+c@mail.example.org")
        self.assertNotIn("ivan", text)
        self.assertNotIn("example", text)
        self.assertIn("user unknown", text)


class MailerTests(unittest.TestCase):
    def mailer(self, **extra):
        box = Outbox()
        return server.Mailer(mail_config("x.sqlite3", **extra), transport=box), box

    def test_message_headers_and_body(self):
        mailer, _ = self.mailer(DASH_MAIL_REPLY_TO="owner@example.org")
        message = mailer.build("ivan@example.com", "MicroGarden: тема", "Привет, Иван!\nhttps://dash.example.org/verify#token=abc\n")
        self.assertEqual(message["From"], "MicroGarden <noreply@example.org>")
        self.assertEqual((message["To"], message["Reply-To"], message["Auto-Submitted"]),
                         ("ivan@example.com", "owner@example.org", "auto-generated"))
        self.assertEqual(message["Subject"], "MicroGarden: тема")
        self.assertTrue(message["Message-ID"].endswith("@example.org>"))
        self.assertLess(abs(parsedate_to_datetime(str(message["Date"])).timestamp() - time.time()), 60)
        self.assertIn("Привет, Иван!", message.get_content())
        self.assertIsNone(self.mailer()[0].build("ivan@example.com", "t", "b")["Reply-To"])
        with self.assertRaises(ValueError):                       # перевод строки в адресе не превращается во второй заголовок
            mailer.build("ivan@example.com\r\nBcc: x@example.org", "t", "b")

    def test_letters_are_sent_in_the_background(self):
        mailer, box = self.mailer()
        self.assertTrue(mailer.send("ivan@example.com", "тема", "текст"))
        deadline = time.time() + 5
        while not box.sent and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual([m["To"] for m in box.sent], ["ivan@example.com"])

    def test_failed_delivery_is_retried_and_logged_without_addresses(self):
        calls = []

        def flaky(message):
            calls.append(message["To"])
            if len(calls) == 1:
                raise smtplib.SMTPResponseException(451, b"4.3.0 try later for <ivan@example.com>")

        mailer = server.Mailer(mail_config("x"), transport=flaky)
        mailer.synchronous = True
        with self.assertLogs("dash", level="WARNING") as logs:
            self.assertTrue(mailer.send("ivan@example.com", "t", "b"))
        self.assertEqual(len(calls), 2)
        text = "\n".join(logs.output)
        self.assertIn("451", text)
        self.assertNotIn("ivan", text)

        def broken(message):
            raise OSError("down")

        mailer = server.Mailer(mail_config("x"), transport=broken)
        mailer.synchronous = True
        with self.assertLogs("dash", level="WARNING") as logs:
            mailer.send("ivan@example.com", "t", "b")                      # письмо потеряно, но сервер продолжает работать
        self.assertEqual(len(logs.output), server.MAIL_ATTEMPTS)

    def test_daily_limit(self):
        mailer, box = self.mailer(DASH_MAIL_DAILY_LIMIT="2")
        mailer.synchronous = True
        with self.assertLogs("dash", level="WARNING"):
            self.assertEqual([mailer.send("a@example.com", "t", "b", now=NOW) for _ in range(3)], [True, True, False])
        self.assertEqual(len(box.sent), 2)
        self.assertTrue(mailer.send("a@example.com", "t", "b", now=NOW + 86400))             # на следующие сутки счёт с нуля

    def test_error_texts_do_not_leak_addresses_or_passwords(self):
        describe = server.describe_mail_error
        text = describe(smtplib.SMTPAuthenticationError(535, b"5.7.8 bad s3cret"))
        self.assertIn("535", text)
        self.assertNotIn("s3cret", text)
        text = describe(smtplib.SMTPRecipientsRefused({"ivan@example.com": (550, b"no mailbox ivan@example.com")}))
        self.assertIn("550", text)
        self.assertNotIn("ivan", text)
        self.assertIn("DASH_MAIL_FROM", describe(smtplib.SMTPSenderRefused(550, b"sender not verified", "noreply@example.org")))
        self.assertIn("DASH_SMTP_HOST", describe(socket.gaierror(-2, "Name or service not known")))
        self.assertIn("не ответил", describe(TimeoutError()))
        self.assertIn("нет соединения", describe(ConnectionRefusedError()))
        self.assertIn("шифрования", describe(ssl.SSLError("bad certificate")))
        self.assertNotIn("ivan", describe(ValueError("boom ivan@example.com")))


class SmtpWireTests(unittest.TestCase):
    """Письмо проходит через настоящий smtplib до поддельного SMTP-сервера."""

    def setUp(self):
        self.smtp = FakeSmtp()
        self.addCleanup(self.smtp.close)

    def mailer(self, **extra):
        env = dict(DASH_SMTP_HOST="127.0.0.1", DASH_SMTP_PORT=str(self.smtp.port), DASH_SMTP_SECURITY="none",
                   DASH_SMTP_USER="mailer", DASH_SMTP_PASSWORD="s3cret")
        env.update(extra)
        return server.Mailer(mail_config("x.sqlite3", **env))

    def test_message_goes_through_smtp_with_login(self):
        mailer = self.mailer()
        body = "Привет, Иван!\nhttps://dash.example.org/verify#token=abc\n"
        mailer.transport(mailer.build("ivan@example.com", "MicroGarden: подтвердите почту", body))
        sent = self.smtp.messages[0]
        self.assertEqual((sent["from"], sent["to"], sent["authed"]), ("noreply@example.org", ["ivan@example.com"], True))
        import email
        import email.policy
        parsed = email.message_from_bytes(sent["data"], policy=email.policy.default)
        self.assertEqual((parsed["Subject"], parsed["To"]), ("MicroGarden: подтвердите почту", "ivan@example.com"))
        self.assertIn("Привет, Иван!", parsed.get_content())

    def test_wrong_password_is_reported_and_nothing_is_sent(self):
        mailer = self.mailer(DASH_SMTP_PASSWORD="wrong-password")
        with self.assertRaises(smtplib.SMTPAuthenticationError) as caught:
            mailer.transport(mailer.build("ivan@example.com", "t", "b"))
        text = server.describe_mail_error(caught.exception)
        self.assertIn("логин или пароль", text)
        self.assertNotIn("wrong-password", text)
        self.assertEqual(self.smtp.messages, [])

    def test_starttls_is_required_when_the_server_cannot_encrypt(self):
        cfg = mail_config("x.sqlite3", DASH_SMTP_HOST="localhost", DASH_SMTP_PORT=str(self.smtp.port), DASH_SMTP_SECURITY="starttls",
                          DASH_SMTP_USER="mailer", DASH_SMTP_PASSWORD="s3cret")
        mailer = server.Mailer(cfg)
        with self.assertRaises(smtplib.SMTPNotSupportedError):
            mailer.transport(mailer.build("ivan@example.com", "t", "b"))
        self.assertNotIn("AUTH", self.smtp.commands)                       # пароль открытым текстом не отправлен
        self.assertEqual(self.smtp.messages, [])

    def test_test_mail_command(self):
        cfg = mail_config("x.sqlite3", DASH_SMTP_HOST="127.0.0.1", DASH_SMTP_PORT=str(self.smtp.port), DASH_SMTP_SECURITY="none",
                          DASH_SMTP_USER="mailer", DASH_SMTP_PASSWORD="s3cret")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(server.test_mail(cfg, "Ivan@Example.com"), 0)
        self.assertIn("принято", out.getvalue())
        self.assertEqual(self.smtp.messages[0]["to"], ["ivan@example.com"])
        bad = mail_config("x.sqlite3", DASH_SMTP_HOST="127.0.0.1", DASH_SMTP_PORT=str(self.smtp.port), DASH_SMTP_SECURITY="none",
                          DASH_SMTP_USER="mailer", DASH_SMTP_PASSWORD="nope")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(server.test_mail(bad, "ivan@example.com"), 1)
        self.assertIn("логин или пароль", out.getvalue())
        self.assertNotIn("nope", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(server.test_mail(server.Config({"DASH_VIEW_PASSWORD": PASSWORD}), "ivan@example.com"), 2)
            self.assertEqual(server.test_mail(cfg, "не адрес"), 2)
            self.assertEqual(server.main(["что-то лишнее"]), 2)


class EmailStoreTests(unittest.TestCase):
    PW = "correct horse battery"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = server.Store(Path(self.tmp.name) / "m.sqlite3")
        self.admin_id = self.store.ensure_admin("garden", "admin-password-123", now=NOW)

    def person(self, name, email=None):
        return self.store.register_user(name, self.PW, "1.2.3.4", now=NOW, email=email)

    def verified(self, name, email):
        ivan = self.person(name, email)
        self.store.confirm_email(self.store.issue_mail_token(ivan["id"], "email", email, NOW), NOW + 1)
        return ivan

    def test_registered_address_waits_for_confirmation_and_cannot_log_in_yet(self):
        ivan = self.person("ivan", "ivan@example.com")
        self.assertEqual(self.store.email_state(ivan["id"], NOW),
                         {"address": "ivan@example.com", "verified": False, "pending": "ivan@example.com"})
        self.assertFalse(self.store.email_verified(ivan["id"]))
        self.assertIsNone(self.store.login_user("ivan@example.com", self.PW))        # по неподтверждённой почте не входят
        self.assertEqual(self.store.login_user("ivan", self.PW)["id"], ivan["id"])
        self.assertEqual(self.store.email_state(self.person("ann")["id"], NOW), {"address": None, "verified": False, "pending": None})

    def test_confirming_the_link_verifies_the_address(self):
        ivan = self.person("ivan", "ivan@example.com")
        link = self.store.issue_mail_token(ivan["id"], "email", "ivan@example.com", NOW)
        self.assertNotIn(link, "".join(p.read_bytes().decode("latin-1") for p in Path(self.tmp.name).iterdir()))   # в базе только отпечаток
        self.assertEqual(self.store.confirm_email(link, NOW + 60), {"email": "ivan@example.com", "username": "ivan"})
        self.assertEqual(self.store.email_state(ivan["id"], NOW + 61), {"address": "ivan@example.com", "verified": True, "pending": None})
        self.assertEqual(self.store.login_user("IVAN@Example.com", self.PW, now=NOW + 62)["username"], "ivan")
        self.assertIsNone(self.store.login_user("ivan@example.com", self.PW + "x"))
        self.assertIsNone(self.store.confirm_email(link, NOW + 63))                  # ссылка одноразовая
        self.assertIsNone(self.store.confirm_email("не-ссылка", NOW + 63))

    def test_links_expire_and_a_new_one_replaces_the_old_one(self):
        ivan = self.person("ivan", "ivan@example.com")
        old = self.store.issue_mail_token(ivan["id"], "email", "ivan@example.com", NOW)
        new = self.store.issue_mail_token(ivan["id"], "email", "ivan@example.com", NOW + 10)
        self.assertIsNone(self.store.confirm_email(old, NOW + 11))
        self.assertIsNone(self.store.confirm_email(new, NOW + 10 + server.MAIL_TOKEN_TTL["email"]))       # срок вышел ровно сейчас
        self.assertEqual(self.store.email_state(ivan["id"], NOW + 20 + server.MAIL_TOKEN_TTL["email"])["verified"], False)
        fresh = self.store.issue_mail_token(ivan["id"], "email", "ivan@example.com", NOW + 100)
        self.assertEqual(self.store.confirm_email(fresh, NOW + 101)["email"], "ivan@example.com")

    def test_first_to_confirm_owns_the_address(self):
        ann, bob = self.person("ann", "x@example.com"), self.person("bob", "x@example.com")      # заявки не мешают друг другу
        ann_link = self.store.issue_mail_token(ann["id"], "email", "x@example.com", NOW)
        bob_link = self.store.issue_mail_token(bob["id"], "email", "x@example.com", NOW)
        self.assertEqual(self.store.confirm_email(bob_link, NOW + 5)["username"], "bob")
        self.assertEqual(self.store.email_state(ann["id"], NOW + 5), {"address": None, "verified": False, "pending": None})
        self.assertIsNone(self.store.confirm_email(ann_link, NOW + 6))
        with self.assertRaises(server.Conflict):
            self.person("carol", "x@example.com")
        with self.assertRaises(server.Conflict):
            self.store.set_email(ann["id"], "x@example.com", self.PW, NOW + 7)
        with closing(sqlite3.connect(self.store.path)) as cx:                        # уникальность держит и сама база
            with self.assertRaises(sqlite3.IntegrityError):
                cx.execute("UPDATE users SET email='x@example.com', email_verified_at=1 WHERE id=?", (ann["id"],))

    def test_changing_a_verified_address_keeps_the_old_one_until_the_new_is_confirmed(self):
        ivan = self.verified("ivan", "old@example.com")
        with self.assertRaises(server.WrongPassword):
            self.store.set_email(ivan["id"], "new@example.com", "wrong password 123", NOW + 5)
        with self.assertRaises(server.Conflict):
            self.store.set_email(ivan["id"], "old@example.com", self.PW, NOW + 5)      # уже подтверждён
        link = self.store.set_email(ivan["id"], "New@Example.com", self.PW, NOW + 10)
        self.assertEqual(self.store.email_state(ivan["id"], NOW + 11),
                         {"address": "old@example.com", "verified": True, "pending": "new@example.com"})
        self.assertEqual(self.store.login_user("old@example.com", self.PW)["id"], ivan["id"])
        self.assertIsNone(self.store.login_user("new@example.com", self.PW))
        self.store.confirm_email(link, NOW + 12)
        self.assertEqual(self.store.email_state(ivan["id"], NOW + 13), {"address": "new@example.com", "verified": True, "pending": None})
        self.assertIsNone(self.store.login_user("old@example.com", self.PW))
        self.assertEqual(self.store.login_user("new@example.com", self.PW)["id"], ivan["id"])

    def test_an_unconfirmed_address_is_replaced_at_once_and_the_admin_can_bind_one(self):
        ivan = self.person("ivan", "typo@example.com")
        self.store.set_email(ivan["id"], "right@example.com", self.PW, NOW + 5)
        self.assertEqual(self.store.email_state(ivan["id"], NOW + 6),
                         {"address": "right@example.com", "verified": False, "pending": "right@example.com"})
        link = self.store.set_email(self.admin_id, "owner@example.com", "admin-password-123", NOW + 7)     # пароль администратора из .env
        self.assertEqual(self.store.confirm_email(link, NOW + 8)["username"], "garden")
        with self.assertRaises(server.WrongPassword):
            self.store.set_email(self.admin_id, "other@example.com", "wrong-password-123", NOW + 9)

    def test_resend_goes_to_the_address_that_waits_for_confirmation(self):
        ivan = self.person("ivan", "ivan@example.com")
        address, link = self.store.resend_verification(ivan["id"], NOW + 5)
        self.assertEqual(address, "ivan@example.com")
        self.store.confirm_email(link, NOW + 6)
        self.assertIsNone(self.store.resend_verification(ivan["id"], NOW + 7))        # ждать нечего
        self.assertIsNone(self.store.resend_verification(self.person("ann")["id"], NOW + 7))

    def test_reset_by_link_sets_the_password_closes_sessions_and_verifies_the_address(self):
        ivan = self.person("ivan", "ivan@example.com")
        session = self.store.create_session(ivan["id"], "1.2.3.4", now=NOW)
        target = self.store.find_reset_targets("ivan@example.com")[0]
        self.assertEqual(target, {"id": ivan["id"], "username": "ivan"})
        link = self.store.issue_mail_token(target["id"], "reset", "ivan@example.com", NOW)
        self.assertTrue(self.store.check_reset_token(link, NOW + 60))
        with self.assertRaises(ValueError):
            self.store.reset_with_token(link, "short", NOW + 61)                      # слабый пароль ссылку не расходует
        with self.assertRaises(ValueError):
            self.store.reset_with_token(link, "IVAN", NOW + 61)
        self.assertTrue(self.store.check_reset_token(link, NOW + 62))
        self.assertEqual(self.store.reset_with_token(link, "brand new password", NOW + 63), {"username": "ivan"})
        self.assertIsNone(self.store.session_user(session, now=NOW + 64))             # прежние входы закрыты
        self.assertIsNone(self.store.login_user("ivan", self.PW))
        self.assertEqual(self.store.login_user("ivan", "brand new password")["username"], "ivan")
        self.assertEqual(self.store.login_user("ivan@example.com", "brand new password")["username"], "ivan")   # ссылка пришла на этот адрес
        self.assertIsNone(self.store.reset_with_token(link, "another new password", NOW + 70))
        self.assertFalse(self.store.check_reset_token(link, NOW + 70))
        self.assertFalse(self.store.check_reset_token("не-ссылка", NOW + 70))

    def test_reset_link_expires_and_dies_when_the_password_is_changed_another_way(self):
        ivan = self.verified("ivan", "ivan@example.com")
        link = self.store.issue_mail_token(ivan["id"], "reset", "ivan@example.com", NOW)
        self.assertFalse(self.store.check_reset_token(link, NOW + server.MAIL_TOKEN_TTL["reset"]))
        self.assertIsNone(self.store.reset_with_token(link, "brand new password", NOW + server.MAIL_TOKEN_TTL["reset"] + 1))
        link = self.store.issue_mail_token(ivan["id"], "reset", "ivan@example.com", NOW)
        self.store.change_password(ivan["id"], self.PW, "changed by owner 1")
        self.assertFalse(self.store.check_reset_token(link, NOW + 5))
        link = self.store.issue_mail_token(ivan["id"], "reset", "ivan@example.com", NOW)
        self.assertIsNotNone(self.store.reset_password(ivan["id"]))                   # временный пароль от администратора
        self.assertFalse(self.store.check_reset_token(link, NOW + 5))

    def test_who_can_be_reset(self):
        names = lambda email: [t["username"] for t in self.store.find_reset_targets(email)]
        self.assertEqual(names("nobody@example.com"), [])
        ann = self.person("ann", "shared@example.com")                                 # заявка без подтверждения
        self.assertEqual(names("shared@example.com"), ["ann"])
        self.person("bob", "shared@example.com")                                       # заявили двое: письма получат обе записи
        self.assertEqual(names("shared@example.com"), ["bob", "ann"])                  # настоящий владелец адреса узнает свою по имени
        self.person("carol", "shared@example.com")
        self.person("dave", "shared@example.com")
        self.assertEqual(names("shared@example.com"), ["dave", "carol", "bob"])        # не больше трёх
        self.store.confirm_email(self.store.issue_mail_token(ann["id"], "email", "shared@example.com", NOW), NOW + 1)
        self.assertEqual(names("shared@example.com"), ["ann"])                         # подтверждённая одна: адрес принадлежит ей
        self.store.set_user_disabled(ann["id"], True)
        self.assertEqual(names("shared@example.com"), [])
        link = self.store.issue_mail_token(ann["id"], "reset", "shared@example.com", NOW)
        self.assertFalse(self.store.check_reset_token(link, NOW + 5))                  # у заблокированного ссылка не работает
        admin_link = self.store.set_email(self.admin_id, "owner@example.com", "admin-password-123", NOW)
        self.store.confirm_email(admin_link, NOW + 1)
        self.assertEqual(names("owner@example.com"), [])                               # пароль администратора живёт в .env

    def test_people_list_shows_the_address_and_deleting_a_person_removes_the_links(self):
        ivan = self.verified("ivan", "ivan@example.com")
        self.person("ann", "ann@example.com")
        rows = {u["username"]: u for u in self.store.list_users()}
        self.assertEqual((rows["ivan"]["email"], rows["ivan"]["email_verified"]), ("ivan@example.com", True))
        self.assertEqual((rows["ann"]["email"], rows["ann"]["email_verified"]), ("ann@example.com", False))
        self.assertEqual((rows["garden"]["email"], rows["garden"]["email_verified"]), (None, False))
        link = self.store.issue_mail_token(ivan["id"], "reset", "ivan@example.com", NOW)
        self.assertTrue(self.store.delete_user(ivan["id"]))
        with closing(sqlite3.connect(self.store.path)) as cx:
            self.assertEqual(cx.execute("SELECT COUNT(*) FROM mail_tokens WHERE user_id=?", (ivan["id"],)).fetchone()[0], 0)
        self.assertFalse(self.store.check_reset_token(link, NOW + 5))
        self.person("ivan2", "ivan@example.com")                                      # адрес освободился

    def test_old_database_gets_the_email_columns(self):
        old = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old)) as cx, cx:
            cx.execute("CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,"
                       " pass_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', created_at INTEGER NOT NULL,"
                       " last_login_at INTEGER, reg_ip TEXT, disabled INTEGER NOT NULL DEFAULT 0)")
            cx.execute("INSERT INTO users(username, pass_hash, created_at) VALUES('old', 'x', ?)", (NOW,))
        store = server.Store(old)
        self.assertEqual(store.email_state(1), {"address": None, "verified": False, "pending": None})
        server.Store(old)                                                              # повторный запуск ничего не ломает
        with closing(sqlite3.connect(old)) as cx:
            self.assertIn("users_email_verified", {row[1] for row in cx.execute("PRAGMA index_list(users)")})


class MailHttpTests(unittest.TestCase):
    """Сервер с настроенной почтой: письма копятся в Outbox вместо SMTP."""
    ACTION = {"X-Dash-Action": "1", "Content-Type": "application/json"}
    PW = "correct horse battery"
    counter = 0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = mail_config(Path(cls.tmp.name) / "p.sqlite3")
        cls.store = server.Store(cfg.db)
        cls.httpd = server.DashServer(cfg, cls.store)
        cls.outbox = Outbox()
        cls.httpd.mailer.transport = cls.outbox
        cls.httpd.mailer.synchronous = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.httpd.limiter = server.FailureLimiter()
        self.httpd.name_limiter = server.FailureLimiter(limit=20, window=600)
        self.httpd.reg_limiter = server.FailureLimiter(limit=server.REGISTRATIONS_PER_HOUR, window=3600)
        self.httpd.mail_ip_limiter = server.FailureLimiter(limit=server.MAIL_PER_IP, window=3600)
        self.httpd.mail_addr_limiter = server.FailureLimiter(limit=server.MAIL_PER_ADDRESS, window=3600)
        self.httpd.token_limiter = server.FailureLimiter(limit=20, window=600)
        self.httpd.mailer._day, self.httpd.mailer._sent_today = "", 0
        self.outbox.sent.clear()
        self.store.set_registration(True)

    def unique(self, prefix="mail"):
        type(self).counter += 1
        return "%s%d" % (prefix, type(self).counter)

    def request(self, method, path, body=None, headers=None, cookie=None, admin=False):
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = server.SESSION_COOKIE + "=" + cookie
        if admin:
            headers["Authorization"] = "Basic " + base64.b64encode(("garden:%s" % PASSWORD).encode()).decode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def call(self, method, path, payload=None, cookie=None, admin=False, ip=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = dict(self.ACTION, **({"X-Real-IP": ip} if ip else {}))
        status, resp_headers, raw = self.request(method, path, body, headers, cookie, admin)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        return status, data, resp_headers

    @staticmethod
    def cookie_of(headers):
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def register(self, name=None, email=None, password=None, ip=None):
        name = name or self.unique()
        email = email or "%s@example.com" % name.lower()
        status, data, headers = self.call("POST", "/api/register", {"username": name, "email": email, "password": password or self.PW}, ip=ip)
        return status, data, (self.cookie_of(headers) if status == 201 else None), name, email

    def person(self, verified=False):
        status, data, cookie, name, email = self.register()
        self.assertEqual(status, 201, data)
        if verified:
            self.assertEqual(self.call("POST", "/api/verify", {"token": self.outbox.last_token(email)})[0], 200)
        return cookie, name, email

    def test_registration_info_and_the_public_pages(self):
        info = json.loads(self.request("GET", "/api/registration")[2])
        self.assertEqual(info, {"open": True, "mail": True, "google": False})
        for path in ("/forgot", "/reset", "/verify", "/forgot.js", "/reset.js", "/verify.js"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200, path)
            if path.endswith(("forgot", "reset", "verify")):
                self.assertNotIn(b"<script>", body)
                self.assertNotIn(b" style=", body)
                self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        cookie, _, _ = self.person()
        self.assertEqual(self.request("GET", "/verify", cookie=cookie)[0], 200)        # по ссылке из письма страница открывается и вошедшему
        self.assertEqual(self.request("GET", "/reset", cookie=cookie)[0], 200)
        self.assertEqual(self.request("GET", "/forgot", cookie=cookie)[0], 302)        # а «Забыли пароль» вошедшему не нужна

    def test_registration_asks_for_an_address_and_sends_the_link(self):
        name = self.unique()
        self.assertEqual(self.call("POST", "/api/register", {"username": name, "password": self.PW})[0], 400)          # адрес обязателен
        for bad in ("not-an-address", "иван@example.com", "x@y"):
            self.assertEqual(self.call("POST", "/api/register", {"username": name, "email": bad, "password": self.PW})[0], 400, bad)
        self.assertEqual(self.outbox.sent, [])
        status, data, cookie, name, email = self.register(name, "Ivan.Mail@Example.com")
        self.assertEqual((status, data["mail_sent"]), (201, True))
        letter = self.outbox.to("ivan.mail@example.com")[0]                             # адрес приведён к нижнему регистру
        self.assertEqual(letter["Subject"], "MicroGarden: подтвердите почту")
        self.assertRegex(letter.get_content(), r"https://dash\.example\.org/verify#token=[A-Za-z0-9_-]{40,}")
        self.assertIn(name, letter.get_content())
        me = json.loads(self.request("GET", "/api/me", cookie=cookie)[2])
        self.assertEqual((me["mail_enabled"], me["email"]),
                         (True, {"address": "ivan.mail@example.com", "verified": False, "pending": "ivan.mail@example.com"}))
        self.assertEqual(self.call("POST", "/api/register", {"username": self.unique(), "email": "ivan.mail@example.com", "password": self.PW})[0], 201)   # заявка без подтверждения не блокирует
        self.assertEqual(self.call("POST", "/api/verify", {"token": self.outbox.last_token("ivan.mail@example.com")})[0], 200)
        status, data, _ = self.call("POST", "/api/register", {"username": self.unique(), "email": "ivan.mail@example.com", "password": self.PW})
        self.assertEqual((status, "уже привязана" in data["error"]), (409, True))      # подтверждённый адрес занят

    def test_new_boards_need_a_confirmed_address(self):
        cookie, name, email = self.person()
        status, data, _ = self.call("POST", "/api/boards", {"name": "Теплица", "device": name + "-1"}, cookie=cookie)
        self.assertEqual((status, data["code"]), (403, "email_unverified"))
        self.assertEqual(self.call("GET", "/api/boards", cookie=cookie)[0], 200)        # смотреть можно
        self.assertEqual(self.call("POST", "/api/verify", {"token": self.outbox.last_token(email)})[1]["email"], email)
        self.assertEqual(self.call("POST", "/api/boards", {"name": "Теплица", "device": name + "-1"}, cookie=cookie)[0], 201)
        self.assertEqual(self.call("POST", "/api/boards", {"name": "Админская", "device": self.unique("adm")}, admin=True)[0], 201)   # администратора это не касается

    def test_verify_checks_the_page_header_and_counts_wrong_links(self):
        _, _, email = self.person()
        token = self.outbox.last_token(email)
        body = json.dumps({"token": token}).encode()
        self.assertEqual(self.request("POST", "/api/verify", body, {"Content-Type": "application/json"})[0], 403)    # нет заголовка страницы
        self.assertEqual(self.request("GET", "/api/verify")[0], 405)
        for bad in ("", "короткая", "x" * 19, "x" * 65, "a b" * 10, None, 5):
            status, data, _ = self.call("POST", "/api/verify", {"token": bad}, ip="10.9.9.1")
            self.assertEqual(status, 400, repr(bad))
        self.assertIn("недействительна", data["error"])
        self.assertEqual(self.call("POST", "/api/verify", {"token": token}, ip="10.9.9.2")[0], 200)
        self.assertEqual(self.call("POST", "/api/verify", {"token": token}, ip="10.9.9.2")[0], 400)                    # второй раз нельзя
        self.httpd.token_limiter = server.FailureLimiter(limit=3, window=600)
        for _ in range(3):
            self.call("POST", "/api/verify", {"token": "x" * 30}, ip="10.9.9.3")
        status, _, headers = self.call("POST", "/api/verify", {"token": "x" * 30}, ip="10.9.9.3")
        self.assertEqual((status, headers["Retry-After"]), (429, "600"))
        self.assertEqual(self.call("POST", "/api/verify", {"token": "x" * 30}, ip="10.9.9.4")[0], 400)                  # у другого адреса счёт свой

    def test_forgot_answers_the_same_for_every_address_and_mails_only_known_ones(self):
        _, _, confirmed = self.person(verified=True)
        _, _, waiting = self.person()
        self.outbox.sent.clear()
        answers = [self.call("POST", "/api/forgot", {"email": address}) for address in (confirmed, waiting, "nobody@example.com")]
        self.assertEqual({(status, json.dumps(data)) for status, data, _ in answers}, {(200, '{"ok": true}')})
        self.assertEqual(sorted(m["To"] for m in self.outbox.sent), sorted([confirmed, waiting]))
        self.assertEqual(self.outbox.sent[0]["Subject"], "MicroGarden: восстановление пароля")
        self.assertEqual(self.call("POST", "/api/forgot", {"email": "не адрес"})[0], 400)
        self.assertEqual(self.request("POST", "/api/forgot", b"{}", {"Content-Type": "application/json"})[0], 403)

    def test_forgot_reaches_every_claimant_of_an_unconfirmed_address(self):
        _, first, email = self.person()                                                # адрес не подтверждён
        status, _, _, second, _ = self.register(email=email)                           # второй заявил тот же адрес
        self.assertEqual(status, 201)
        self.outbox.sent.clear()
        self.httpd.mail_addr_limiter = server.FailureLimiter(limit=server.MAIL_PER_ADDRESS, window=3600)
        self.assertEqual(self.call("POST", "/api/forgot", {"email": email})[0], 200)
        letters = self.outbox.to(email)
        self.assertEqual(sorted(re.search(r"Здравствуйте, (\S+)!", m.get_content()).group(1) for m in letters), sorted([first, second]))
        for letter in letters:                                                         # настоящий владелец адреса выберет свою запись по имени
            self.assertEqual(self.call("POST", "/api/reset", {"token": Outbox.token_of(letter), "password": "brand new password"})[0], 200)
        self.assertEqual(self.call("POST", "/api/login", {"username": email, "password": "brand new password"})[1]["user"]["username"],
                         re.search(r"Здравствуйте, (\S+)!", letters[0].get_content()).group(1))   # адрес остался у той записи, чью ссылку открыли первой

    def test_forgot_is_limited_per_address_and_per_caller(self):
        _, _, email = self.person(verified=True)
        self.outbox.sent.clear()
        self.httpd.mail_addr_limiter = server.FailureLimiter(limit=server.MAIL_PER_ADDRESS, window=3600)   # письмо при регистрации уже учтено
        for _ in range(server.MAIL_PER_ADDRESS + 2):
            self.assertEqual(self.call("POST", "/api/forgot", {"email": email}, ip="10.8.8.1")[0], 200)     # ответ всегда один
        self.assertEqual(len(self.outbox.to(email)), server.MAIL_PER_ADDRESS)           # а писем не больше трёх в час
        self.httpd.mail_addr_limiter = server.FailureLimiter(limit=100, window=3600)
        for _ in range(server.MAIL_PER_IP - server.MAIL_PER_ADDRESS - 2):
            self.call("POST", "/api/forgot", {"email": email}, ip="10.8.8.1")
        status, _, headers = self.call("POST", "/api/forgot", {"email": email}, ip="10.8.8.1")
        self.assertEqual((status, headers["Retry-After"]), (429, "3600"))
        self.assertEqual(self.call("POST", "/api/forgot", {"email": email}, ip="10.8.8.2")[0], 200)

    def test_new_password_by_the_link_from_the_letter(self):
        cookie, name, email = self.person(verified=True)
        self.outbox.sent.clear()
        self.call("POST", "/api/forgot", {"email": email})
        token = self.outbox.last_token(email)
        self.assertEqual(self.call("POST", "/api/reset/check", {"token": token})[0], 200)
        self.assertEqual(self.call("POST", "/api/reset/check", {"token": "x" * 40})[0], 400)
        status, data, _ = self.call("POST", "/api/reset", {"token": token, "password": "short"})
        self.assertEqual(status, 400)                                                   # слабый пароль ссылку не тратит
        self.assertIn("Пароль", data["error"])
        self.assertEqual(self.call("POST", "/api/reset", {"token": token, "password": "brand new password"})[0], 200)
        self.assertEqual(self.request("GET", "/api/me", cookie=cookie)[0], 401)         # прежний вход закрыт
        self.assertEqual(self.call("POST", "/api/login", {"username": name, "password": self.PW})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"username": email, "password": "brand new password"})[0], 200)
        self.assertEqual(self.call("POST", "/api/reset", {"token": token, "password": "another new password"})[0], 400)  # ссылка одноразовая

    def test_reset_by_the_link_also_confirms_the_address(self):
        _, name, email = self.person()
        self.assertEqual(self.call("POST", "/api/login", {"username": email, "password": self.PW})[0], 401)           # пока не подтверждена
        self.outbox.sent.clear()
        self.call("POST", "/api/forgot", {"email": email})
        self.call("POST", "/api/reset", {"token": self.outbox.last_token(email), "password": "brand new password"})
        status, data, _ = self.call("POST", "/api/login", {"username": email.upper(), "password": "brand new password"})
        self.assertEqual((status, data["user"]["username"]), (200, name))

    def test_login_by_address_gives_the_same_failure_as_by_name(self):
        _, name, email = self.person(verified=True)
        with self.assertLogs("dash", level="WARNING") as logs:
            wrong = self.call("POST", "/api/login", {"username": email, "password": self.PW + "x"})
            unknown = self.call("POST", "/api/login", {"username": "nobody@example.com", "password": self.PW})
        self.assertEqual((wrong[0], unknown[0]), (401, 401))
        self.assertEqual(wrong[1], unknown[1])
        self.assertEqual(len(logs.output), 2)
        self.assertNotIn("example.com", "\n".join(logs.output))                     # адреса почты в журнал сервера не попадают
        self.assertEqual(self.call("POST", "/api/login", {"username": "  " + email + " ", "password": self.PW})[0], 200)
        self.assertEqual(self.call("POST", "/api/login", {"username": "x" * 300, "password": self.PW})[0], 400)

    def test_address_in_the_account_can_be_changed_and_resent(self):
        cookie, name, email = self.person(verified=True)
        new = "new-" + email
        self.outbox.sent.clear()
        self.assertEqual(self.request("POST", "/api/account/email", json.dumps({"email": new, "password": self.PW}).encode(),
                                      {"Content-Type": "application/json"}, cookie)[0], 403)
        self.assertEqual(self.call("POST", "/api/account/email", {"email": new, "password": self.PW}, ip="10.7.7.1")[0], 401)   # без входа нельзя
        status, data, _ = self.call("POST", "/api/account/email", {"email": new, "password": "wrong-password-123"}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertEqual(self.outbox.sent, [])
        status, data, _ = self.call("POST", "/api/account/email", {"email": new, "password": self.PW}, cookie=cookie)
        self.assertEqual((status, data["mail_sent"], data["email"]["pending"]), (200, True, new))
        self.assertEqual(len(self.outbox.to(new)), 1)
        first = self.outbox.last_token(new)
        status, data, _ = self.call("POST", "/api/account/email/resend", {}, cookie=cookie)
        self.assertEqual((status, data["mail_sent"]), (200, True))
        self.assertEqual(self.call("POST", "/api/verify", {"token": first})[0], 400)    # прежняя ссылка погасла
        self.assertEqual(self.call("POST", "/api/verify", {"token": self.outbox.last_token(new)})[0], 200)
        self.assertEqual(json.loads(self.request("GET", "/api/me", cookie=cookie)[2])["email"]["address"], new)
        self.assertEqual(self.call("POST", "/api/account/email/resend", {}, cookie=cookie)[0], 409)    # подтверждать нечего

    def test_address_limit_does_not_burn_the_working_link(self):
        cookie, name, email = self.person()
        for _ in range(server.MAIL_PER_ADDRESS):
            self.httpd.mail_addr_limiter.fail(email)
        status, _, headers = self.call("POST", "/api/account/email/resend", {}, cookie=cookie)
        self.assertEqual((status, headers["Retry-After"]), (429, "3600"))
        self.httpd.mail_addr_limiter = server.FailureLimiter(limit=server.MAIL_PER_ADDRESS, window=3600)
        self.assertEqual(self.call("POST", "/api/verify", {"token": self.outbox.last_token(email)})[0], 200)   # письмо при регистрации работает

    def test_admin_sees_addresses_and_the_admin_page_lists_them(self):
        _, name, email = self.person(verified=True)
        users = {u["username"]: u for u in self.call("GET", "/api/admin/users", admin=True)[1]["users"]}
        self.assertEqual((users[name]["email"], users[name]["email_verified"]), (email, True))

    def test_mail_failure_does_not_break_registration(self):
        def broken(message):
            raise OSError("down")

        self.httpd.mailer.transport = broken
        try:
            with self.assertLogs("dash", level="WARNING"):
                status, data, cookie, _, _ = self.register()
        finally:
            self.httpd.mailer.transport = self.outbox
        self.assertEqual(status, 201)                                                   # человек зарегистрирован, письмо можно запросить ещё раз
        self.assertEqual(self.call("POST", "/api/account/email/resend", {}, cookie=cookie)[0], 200)


DROP = object()      # утверждение, которое нужно убрать из ID-токена


def google_config(db, **extra):
    env = {"DASH_GOOGLE_CLIENT_ID": "1234567890-abcdefghijklmnop.apps.googleusercontent.com",
           "DASH_GOOGLE_CLIENT_SECRET": "GOCSPX-test-secret-value"}
    env.update(extra)
    return mail_config(db, **env)


def google_token(cfg, **claims):
    """ID-токен вроде того, что выдаёт Google (подпись не настоящая: сервер её и не проверяет, см. GoogleClient)."""
    now = int(time.time())
    payload = {"iss": "https://accounts.google.com", "aud": cfg.google_client_id, "sub": "1000000001", "email": "ivan@example.com",
               "email_verified": True, "iat": now, "exp": now + 3600}
    payload.update(claims)

    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return "%s.%s.%s" % (part({"alg": "RS256", "typ": "JWT"}), part({k: v for k, v in payload.items() if v is not DROP}), part("signature"))


class FakeGoogle:
    """Вместо Google: принимает обмен кода, сверяет секрет клиента, адрес возврата и PKCE и отдаёт ID-токен с заданными утверждениями."""

    def __init__(self, cfg):
        self.cfg, self.calls, self.claims, self.error, self.answer = cfg, [], {}, None, None
        self.nonce = self.challenge = None

    def started(self, location):
        """Разбор адреса, на который сервер отправил человека; -> state."""
        query = parse_qs(urlsplit(location).query)
        self.nonce, self.challenge = query["nonce"][0], query["code_challenge"][0]
        return query["state"][0]

    def __call__(self, form):
        self.calls.append(dict(form))
        if self.error:
            raise self.error
        if self.answer is not None:
            return self.answer
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        expected = (self.cfg.google_client_secret, self.cfg.public_url + "/auth/google/callback", "authorization_code", self.challenge)
        if (form["client_secret"], form["redirect_uri"], form["grant_type"], challenge) != expected:
            return {"error": "invalid_grant"}
        return {"id_token": google_token(self.cfg, **dict({"nonce": self.nonce}, **self.claims)), "access_token": "ya29.fake", "token_type": "Bearer"}


class GoogleConfigTests(unittest.TestCase):
    def test_google_is_off_until_configured(self):
        cfg = mail_config("x.sqlite3")
        self.assertEqual((cfg.google_enabled, cfg.google_problems()), (False, []))
        cfg = google_config("x.sqlite3")
        self.assertEqual((cfg.google_enabled, cfg.google_problems()), (True, []))
        self.assertFalse(server.GoogleClient(mail_config("x.sqlite3")).enabled)
        self.assertEqual(server.GoogleClient(cfg).redirect_uri, "https://dash.example.org/auth/google/callback")

    def test_wrong_google_settings_are_named(self):
        for extra, name in (({"DASH_GOOGLE_CLIENT_ID": "not-an-id"}, "DASH_GOOGLE_CLIENT_ID"),
                            ({"DASH_GOOGLE_CLIENT_ID": ""}, "DASH_GOOGLE_CLIENT_ID"),
                            ({"DASH_GOOGLE_CLIENT_SECRET": ""}, "DASH_GOOGLE_CLIENT_SECRET"),
                            ({"DASH_GOOGLE_CLIENT_SECRET": "short"}, "DASH_GOOGLE_CLIENT_SECRET"),
                            ({"DASH_PUBLIC_URL": ""}, "DASH_PUBLIC_URL")):
            problems = google_config("x.sqlite3", **extra).google_problems()
            self.assertTrue(any(p.startswith(name) for p in problems), (extra, problems))
        with self.assertRaises(SystemExit):
            google_config("x.sqlite3", DASH_GOOGLE_CLIENT_ID="not-an-id").validate()       # сервер с неверной настройкой не запускается
        google_config("x.sqlite3").validate()
        self.assertFalse(google_config("x.sqlite3", DASH_PUBLIC_URL="").google_enabled)    # без адреса сайта адреса возврата нет


class GoogleClientTests(unittest.TestCase):
    def setUp(self):
        self.cfg = google_config("x.sqlite3")
        self.fake = FakeGoogle(self.cfg)
        self.client = server.GoogleClient(self.cfg, post=self.fake)

    def begin(self, next_path="/boards"):
        state, url = self.client.begin(next_path)
        self.assertEqual(self.fake.started(url), state)
        return state

    def test_authorization_address(self):
        state, url = self.client.begin()
        parts = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        self.assertEqual("%s://%s%s" % (parts.scheme, parts.netloc, parts.path), "https://accounts.google.com/o/oauth2/v2/auth")
        self.assertEqual((query["client_id"], query["redirect_uri"], query["response_type"], query["scope"]),
                         (self.cfg.google_client_id, "https://dash.example.org/auth/google/callback", "code", "openid email"))
        self.assertEqual((query["code_challenge_method"], query["prompt"], query["state"]), ("S256", "select_account", state))
        self.assertGreaterEqual(len(state), 40)
        self.assertGreaterEqual(len(query["nonce"]), 24)
        self.assertEqual(len(base64.urlsafe_b64decode(query["code_challenge"] + "=")), 32)      # SHA-256 от проверочной строки
        self.assertNotIn(self.cfg.google_client_secret, url)                                    # секрет в адрес страницы не попадает

    def test_good_sign_in(self):
        state = self.begin("/settings")
        claims = self.client.finish(state, "one-time-code")
        self.assertEqual(claims, {"sub": "1000000001", "email": "ivan@example.com", "next": "/settings"})
        form = self.fake.calls[0]
        self.assertEqual((form["code"], form["client_id"], form["client_secret"], form["grant_type"]),
                         ("one-time-code", self.cfg.google_client_id, self.cfg.google_client_secret, "authorization_code"))
        self.assertEqual(form["redirect_uri"], "https://dash.example.org/auth/google/callback")

    def test_address_is_normalised_and_audience_may_be_a_list(self):
        state = self.begin()
        self.fake.claims = {"email": "Ivan.P@Example.COM", "aud": [self.cfg.google_client_id, "other"], "azp": self.cfg.google_client_id,
                            "email_verified": "true"}
        self.assertEqual(self.client.finish(state, "c")["email"], "ivan.p@example.com")

    def test_a_start_is_used_once_and_expires(self):
        state = self.begin()
        self.client.finish(state, "c")
        with self.assertRaises(server.GoogleError) as caught:
            self.client.finish(state, "c")
        self.assertEqual(caught.exception.code, "state")
        for bad in ("", "unknown", None):
            with self.assertRaises(server.GoogleError):
                self.client.finish(bad, "c")
        state = self.begin()
        with self.assertRaises(server.GoogleError) as caught:
            self.client.finish(state, "c", now=time.time() + server.GOOGLE_STATE_TTL + 5)
        self.assertEqual(caught.exception.code, "state")
        self.assertEqual(len(self.fake.calls), 1)                              # Google не спрашивали ни разу лишнего

    def test_token_claims_are_checked(self):
        now = int(time.time())
        cases = [
            ({"iss": "https://evil.example"}, "error"), ({"iss": DROP}, "error"),
            ({"aud": "someone-else"}, "error"), ({"aud": DROP}, "error"),
            ({"aud": [self.cfg.google_client_id, "other"], "azp": "other"}, "error"),
            ({"exp": now - 3600}, "error"), ({"exp": DROP}, "error"), ({"exp": True}, "error"), ({"exp": "soon"}, "error"),
            ({"iat": now + 3600}, "error"),
            ({"nonce": "forged"}, "state"), ({"nonce": DROP}, "state"),
            ({"sub": DROP}, "error"), ({"sub": ""}, "error"), ({"sub": 12345}, "error"), ({"sub": "9" * 256}, "error"),
            ({"email_verified": False}, "unverified"), ({"email_verified": "false"}, "unverified"), ({"email_verified": 1}, "unverified"),
            ({"email_verified": DROP}, "unverified"),
            ({"email": DROP}, "email"), ({"email": "not-an-address"}, "email"), ({"email": "иван@example.com"}, "email"), ({"email": 7}, "email"),
        ]
        for claims, code in cases:
            state = self.begin()
            self.fake.claims = claims
            with self.assertRaises(server.GoogleError, msg=str(claims)) as caught:
                self.client.finish(state, "c")
            self.assertEqual(caught.exception.code, code, str(claims))

    def test_broken_tokens_and_exchange_failures(self):
        for answer in ({"id_token": "no-dots"}, {"id_token": "a.b"}, {"id_token": "a.!!!.c"}, {"id_token": "a.%s.c" % base64.urlsafe_b64encode(b"[1]").decode()},
                       {"id_token": 5}, {"error": "invalid_client"}, {}, ["x"], "text"):
            state = self.begin()
            self.fake.answer = answer
            with self.assertRaises(server.GoogleError, msg=str(answer)) as caught:
                self.client.finish(state, "c")
            self.assertEqual(caught.exception.code, "error")
        self.fake.answer = None
        for failure in (OSError("down"), ValueError("not json"), TimeoutError()):
            state = self.begin()
            self.fake.error = failure
            with self.assertRaises(server.GoogleError) as caught:
                self.client.finish(state, "c")
            self.assertEqual(caught.exception.code, "error")
            self.assertNotIn(self.cfg.google_client_secret, str(caught.exception))
        self.fake.error = None
        state = self.begin()
        with self.assertRaises(server.GoogleError):
            self.client.finish(state, "x" * 3000)                              # слишком длинный код

    def test_wrong_secret_or_verifier_is_refused_by_google(self):
        state = self.begin()
        self.client.cfg = google_config("x.sqlite3", DASH_GOOGLE_CLIENT_SECRET="GOCSPX-another-secret")   # у Google записан другой секрет
        with self.assertRaises(server.GoogleError) as caught:
            self.client.finish(state, "c")
        self.assertIn("invalid_grant", str(caught.exception))                    # причину Google видно в журнале, секрета в тексте нет
        self.assertNotIn("another-secret", str(caught.exception))

    def test_too_many_unfinished_sign_ins(self):
        with mock.patch.object(server, "GOOGLE_STATES_MAX", 3):
            for _ in range(3):
                self.client.begin()
            with self.assertRaises(server.GoogleError) as caught:
                self.client.begin()
            self.assertEqual(caught.exception.code, "busy")
            later = time.time() + server.GOOGLE_STATE_TTL + 1
            self.client.begin(now=later)                                       # старые начатые входы вытесняются по сроку

    def test_next_path_rule_matches_the_page(self):
        for good in ("/", "/boards", "/boards?x=1", "/settings?device=a-1", "/account#top"):
            self.assertEqual(server.safe_next(good), good)
        for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)", "boards", "", None, 5, "/a\nb",
                    "/a\x00b", "/" + "x" * 400, "/login", "/login?next=/", "/register", "/login.html", "/forgot", "/forgot.html"):
            self.assertEqual(server.safe_next(bad), "/", repr(bad))


class GoogleStoreTests(unittest.TestCase):
    PW = "correct horse battery"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = server.Store(Path(self.tmp.name) / "g.sqlite3")
        self.admin_id = self.store.ensure_admin("garden", "admin-password-123", now=NOW)

    def test_new_person_gets_a_record_without_a_password(self):
        who, created = self.store.google_login("sub-1", "ivan.petrov@example.com", "1.2.3.4", now=NOW)
        self.assertEqual((who["username"], who["role"], created), ("ivan.petrov", "user", True))
        self.assertEqual(self.store.email_state(who["id"], NOW), {"address": "ivan.petrov@example.com", "verified": True, "pending": None})
        self.assertEqual(self.store.sign_in_state(who["id"]), {"has_password": False, "google": True})
        self.assertTrue(self.store.email_verified(who["id"]))
        for attempt in ("", "!", self.PW, "scrypt", "x" * 200):                  # пароля нет: подойти нечему
            self.assertIsNone(self.store.login_user("ivan.petrov", attempt))
            self.assertIsNone(self.store.login_user("ivan.petrov@example.com", attempt))
        again, created = self.store.google_login("sub-1", "changed@example.com", "1.2.3.4", now=NOW + 5)
        self.assertEqual((again["id"], created), (who["id"], False))             # постоянный идентификатор главнее адреса
        row = [u for u in self.store.list_users() if u["username"] == "ivan.petrov"][0]
        self.assertEqual((row["google"], row["email"], row["email_verified"], row["last_login_at"], row["reg_ip"]),
                         (True, "ivan.petrov@example.com", True, NOW + 5, "1.2.3.4"))

    def test_usernames_are_valid_and_unique(self):
        names = [self.store.google_login("s%d" % n, email, now=NOW)[0]["username"] for n, email in enumerate((
            "a@example.com", "admin@example.com", "first.last+tag@example.com", "x" * 40 + "@example.com", "ivan@a.example.com",
            "ivan@b.example.com", "ivan@c.example.com", "root@example.com"))]
        self.assertEqual(names[:5], ["axx", "g-admin", "first.last_tag", "x" * 24, "ivan"])
        self.assertTrue(re.fullmatch(r"ivan-[0-9]{4}", names[5]) and re.fullmatch(r"ivan-[0-9]{4}", names[6]), names)
        self.assertNotEqual(names[5], names[6])
        self.assertEqual(names[7], "g-root")
        for name in names:
            self.assertTrue(server.USERNAME_RE.fullmatch(name), name)
            server.clean_username(name)                                           # не зарезервированные имена

    def test_verified_address_links_the_existing_record(self):
        ivan = self.store.register_user("ivan", self.PW, "1.2.3.4", now=NOW, email="ivan@example.com")
        self.store.confirm_email(self.store.issue_mail_token(ivan["id"], "email", "ivan@example.com", NOW), NOW + 1)
        who, created = self.store.google_login("sub-ivan", "ivan@example.com", now=NOW + 2)
        self.assertEqual((who["id"], who["username"], created), (ivan["id"], "ivan", False))
        self.assertEqual(self.store.sign_in_state(ivan["id"]), {"has_password": True, "google": True})
        self.assertEqual(self.store.login_user("ivan", self.PW)["id"], ivan["id"])      # пароль остался
        self.assertEqual(self.store.google_login("sub-ivan", "ivan@example.com", now=NOW + 3)[0]["id"], ivan["id"])
        with self.assertRaises(server.GoogleRefused) as caught:                  # тот же адрес у другого человека Google: в чужую запись не пускаем
            self.store.google_login("sub-someone-else", "ivan@example.com", now=NOW + 4)
        self.assertEqual(caught.exception.code, "other")
        self.assertEqual(len(self.store.list_users()), 2)

    def test_an_unconfirmed_claim_on_the_address_does_not_open_the_google_record(self):
        # Заранее созданная чужая запись с адресом человека не должна получить вход в запись, которую он потом заведёт через Google
        mallory = self.store.register_user("mallory", self.PW, "6.6.6.6", now=NOW, email="victim@example.com")
        token = self.store.issue_mail_token(mallory["id"], "email", "victim@example.com", NOW)
        who, created = self.store.google_login("sub-victim", "victim@example.com", now=NOW + 5)
        self.assertTrue(created)
        self.assertNotEqual(who["id"], mallory["id"])
        self.assertEqual(self.store.email_state(mallory["id"], NOW + 6), {"address": None, "verified": False, "pending": None})
        self.assertIsNone(self.store.confirm_email(token, NOW + 6))               # её ссылка тоже снята
        self.assertIsNone(self.store.login_user("victim@example.com", self.PW))   # по этому адресу и её паролю никто не войдёт
        self.assertEqual(self.store.login_user("mallory", self.PW)["id"], mallory["id"])
        self.assertEqual([t["username"] for t in self.store.find_reset_targets("victim@example.com")], [who["username"]])

    def test_refusals_create_nothing(self):
        who, _ = self.store.google_login("sub-blocked", "blocked@example.com", now=NOW)
        self.store.set_user_disabled(who["id"], True)
        with self.assertRaises(server.GoogleRefused) as caught:
            self.store.google_login("sub-blocked", "blocked@example.com", now=NOW + 1)
        self.assertEqual(caught.exception.code, "blocked")
        # администратор входит только по паролю, даже если на его запись вернулась почта Google
        link = self.store.set_email(self.admin_id, "owner@example.com", "admin-password-123", NOW)
        self.store.confirm_email(link, NOW + 1)
        with self.assertRaises(server.GoogleRefused) as caught:
            self.store.google_login("sub-owner", "owner@example.com", now=NOW + 2)
        self.assertEqual(caught.exception.code, "admin")
        count = len(self.store.list_users())
        self.store.set_registration(False)
        with self.assertRaises(server.GoogleRefused) as caught:
            self.store.google_login("sub-new", "new@example.com", now=NOW + 3)
        self.assertEqual(caught.exception.code, "closed")
        self.store.set_registration(True)
        self.store.max_users = count
        with self.assertRaises(server.GoogleRefused) as caught:
            self.store.google_login("sub-new", "new@example.com", now=NOW + 4)
        self.assertEqual(caught.exception.code, "full")
        self.assertEqual(len(self.store.list_users()), count)
        self.store.set_registration(False)
        self.store.max_users = server.MAX_USERS
        self.store.set_user_disabled(who["id"], False)
        self.assertEqual(self.store.google_login("sub-blocked", "blocked@example.com", now=NOW + 5)[0]["id"], who["id"])   # прежний входит и при закрытой регистрации

    def test_a_google_record_can_set_a_password_and_an_address_without_the_old_ones(self):
        who, _ = self.store.google_login("sub-1", "anna@example.com", now=NOW)
        link = self.store.set_email(who["id"], "anna@new.example.org", None, NOW + 1)                      # подтверждением служит вход
        self.assertEqual(self.store.email_state(who["id"], NOW + 2)["pending"], "anna@new.example.org")
        self.assertEqual(self.store.confirm_email(link, NOW + 3)["email"], "anna@new.example.org")
        self.store.change_password(who["id"], None, "brand new password")                                  # первый пароль без текущего
        self.assertEqual(self.store.sign_in_state(who["id"]), {"has_password": True, "google": True})
        self.assertEqual(self.store.login_user("anna", "brand new password")["id"], who["id"])
        with self.assertRaises(server.WrongPassword):                                                       # теперь пароль есть и спрашивается
            self.store.change_password(who["id"], "wrong password 1", "another new password")
        with self.assertRaises(server.WrongPassword):
            self.store.set_email(who["id"], "other@example.org", "wrong password 1", NOW + 4)
        self.assertEqual(self.store.google_login("sub-1", "anna@new.example.org", now=NOW + 5)[0]["id"], who["id"])

    def test_a_google_record_gets_a_password_by_mail_too(self):
        who, _ = self.store.google_login("sub-1", "anna@example.com", now=NOW)
        target = self.store.find_reset_targets("anna@example.com")[0]
        self.assertEqual(target["id"], who["id"])
        link = self.store.issue_mail_token(target["id"], "reset", "anna@example.com", NOW)
        self.assertEqual(self.store.reset_with_token(link, "brand new password", NOW + 1), {"username": "anna"})
        self.assertEqual(self.store.login_user("anna@example.com", "brand new password")["id"], who["id"])

    def test_old_database_gets_the_google_column(self):
        old = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old)) as cx, cx:
            cx.execute("CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,"
                       " pass_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', created_at INTEGER NOT NULL,"
                       " last_login_at INTEGER, reg_ip TEXT, disabled INTEGER NOT NULL DEFAULT 0)")
            cx.execute("INSERT INTO users(username, pass_hash, created_at) VALUES('old', 'x', ?)", (NOW,))
        store = server.Store(old)
        self.assertEqual(store.sign_in_state(1), {"has_password": True, "google": False})
        server.Store(old)
        with closing(sqlite3.connect(old)) as cx:
            self.assertIn("users_google_sub", {row[1] for row in cx.execute("PRAGMA index_list(users)")})
            with cx:
                cx.execute("UPDATE users SET google_sub='dup' WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                with cx:
                    cx.execute("INSERT INTO users(username, pass_hash, created_at, google_sub) VALUES('other', 'x', 1, 'dup')")


class GoogleHttpTests(unittest.TestCase):
    """Сервер с настроенным входом через Google; вместо Google отвечает FakeGoogle, вместо SMTP копит письма Outbox."""
    ACTION = {"X-Dash-Action": "1", "Content-Type": "application/json"}
    PW = "correct horse battery"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = google_config(Path(cls.tmp.name) / "g.sqlite3")
        cls.cfg = cfg
        cls.store = server.Store(cfg.db)
        cls.httpd = server.DashServer(cfg, cls.store)
        cls.outbox = Outbox()
        cls.httpd.mailer.transport = cls.outbox
        cls.httpd.mailer.synchronous = True
        cls.fake = FakeGoogle(cfg)
        cls.httpd.google.post = cls.fake
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.httpd.limiter = server.FailureLimiter()
        self.httpd.name_limiter = server.FailureLimiter(limit=20, window=600)
        self.httpd.reg_limiter = server.FailureLimiter(limit=server.REGISTRATIONS_PER_HOUR, window=3600)
        self.httpd.google_ip_limiter = server.FailureLimiter(limit=server.GOOGLE_PER_IP, window=3600)
        self.httpd.mail_ip_limiter = server.FailureLimiter(limit=server.MAIL_PER_IP, window=3600)
        self.httpd.mail_addr_limiter = server.FailureLimiter(limit=server.MAIL_PER_ADDRESS, window=3600)
        self.httpd.token_limiter = server.FailureLimiter(limit=20, window=600)
        self.outbox.sent.clear()
        self.fake.calls.clear()
        self.fake.claims, self.fake.error, self.fake.answer = {}, None, None
        self.store.set_registration(True)
        self.store.max_users = server.MAX_USERS

    def get(self, path, cookies=None, headers=None):
        """-> (код, заголовки без повторов, список всех Set-Cookie, тело)."""
        sent = dict(headers or {})
        if cookies:
            sent["Cookie"] = "; ".join("%s=%s" % pair for pair in cookies.items())
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers=sent)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.msg.get_all("Set-Cookie") or [], resp.read()
        finally:
            conn.close()

    def call(self, method, path, payload=None, cookie=None, ip=None):
        headers = dict(self.ACTION, **({"X-Real-IP": ip} if ip else {}))
        if cookie:
            headers["Cookie"] = server.SESSION_COOKIE + "=" + cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=None if payload is None else json.dumps(payload).encode(), headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None), dict(resp.getheaders())
        finally:
            conn.close()

    @staticmethod
    def cookie_value(cookies, name):
        for line in cookies:
            if line.startswith(name + "="):
                return line.split(";")[0].split("=", 1)[1]
        return None

    def begin(self, query="", ip=None):
        status, headers, cookies, _ = self.get("/auth/google" + query, headers={"X-Real-IP": ip} if ip else None)
        self.assertEqual(status, 302)
        state = self.fake.started(headers["Location"])
        self.assertEqual(self.cookie_value(cookies, server.OAUTH_COOKIE), state)
        return state

    def finish(self, state, cookie=None, extra="", code="one-time-code", ip=None):
        cookies = {server.OAUTH_COOKIE: state if cookie is None else cookie} if cookie != "" else None
        query = "/auth/google/callback?state=%s&code=%s%s" % (state, code, extra)
        return self.get(query, cookies, {"X-Real-IP": ip} if ip else None)

    def sign_in(self, sub="1000000001", email="ivan@example.com", query="", **claims):
        self.fake.claims = dict({"sub": sub, "email": email}, **claims)
        status, headers, cookies, _ = self.finish(self.begin(query))
        self.assertEqual(status, 302, headers)
        return headers["Location"], self.cookie_value(cookies, server.SESSION_COOKIE), cookies

    def test_the_start_sends_the_person_to_google_with_a_bound_cookie(self):
        status, headers, cookies, _ = self.get("/auth/google")
        self.assertEqual(status, 302)
        query = parse_qs(urlsplit(headers["Location"]).query)
        self.assertTrue(headers["Location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertEqual((query["client_id"], query["redirect_uri"], query["code_challenge_method"]),
                         ([self.cfg.google_client_id], ["https://dash.example.org/auth/google/callback"], ["S256"]))
        line = [c for c in cookies if c.startswith(server.OAUTH_COOKIE + "=")][0]
        for part in ("HttpOnly", "SameSite=Lax", "Path=/auth/google", "Max-Age=%d" % server.GOOGLE_STATE_TTL):
            self.assertIn(part, line)
        self.assertNotIn("Secure", line)                                     # по голому http браузер Secure-cookie не вернёт
        _, _, cookies, _ = self.get("/auth/google", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("; Secure", [c for c in cookies if c.startswith(server.OAUTH_COOKIE)][0])
        self.assertEqual(json.loads(self.get("/api/registration")[3]), {"open": True, "mail": True, "google": True})

    def test_a_new_person_signs_in_and_lands_on_the_boards_page(self):
        self.fake.claims = {"sub": "g-100", "email": "Anna.Lee@Example.com"}
        state = self.begin()
        status, headers, cookies, _ = self.finish(state)
        self.assertEqual((status, headers["Location"]), (302, "/boards"))
        session = [c for c in cookies if c.startswith(server.SESSION_COOKIE + "=")][0]
        for part in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=%d" % (server.SESSION_DAYS * 86400)):
            self.assertIn(part, session)
        cleared = [c for c in cookies if c.startswith(server.OAUTH_COOKIE + "=")][0]
        self.assertIn("Max-Age=0", cleared)                                  # cookie входа одноразовая
        token = self.cookie_value(cookies, server.SESSION_COOKIE)
        me = self.call("GET", "/api/me", cookie=token)[1]
        self.assertEqual((me["user"]["username"], me["has_password"], me["google"]), ("anna.lee", False, True))
        self.assertEqual(me["email"], {"address": "anna.lee@example.com", "verified": True, "pending": None})
        # почта уже подтверждена Google: платы добавлять можно сразу, письмо не нужно
        self.assertEqual(self.call("POST", "/api/boards", {"name": "Теплица", "device": "anna-1"}, cookie=token)[0], 201)
        self.assertEqual(self.outbox.sent, [])
        self.assertEqual(self.call("GET", "/api/admin/users", cookie=token)[0], 403)

    def test_a_returning_person_goes_where_they_were_going(self):
        self.assertEqual(self.sign_in("g-200", "bob@example.com")[0], "/boards")
        self.assertEqual(self.sign_in("g-200", "bob@example.com")[0], "/")                                  # прежний человек: на главную
        self.assertEqual(self.sign_in("g-200", "bob@example.com", query="?next=%2Fsettings%3Fdevice%3Dbob-1")[0], "/settings?device=bob-1")
        for evil in ("%2F%2Fevil.example", "https%3A%2F%2Fevil.example%2F", "%2Flogin", "javascript%3Aalert(1)"):
            self.assertEqual(self.sign_in("g-200", "bob@example.com", query="?next=" + evil)[0], "/", evil)

    def test_the_callback_needs_the_cookie_and_the_started_sign_in(self):
        self.fake.claims = {"sub": "g-300", "email": "carol@example.com"}
        state = self.begin()
        for kwargs in ({"cookie": ""}, {"cookie": "another-browser-state"}):      # без cookie и с чужой: вход, начатый в другом браузере
            status, headers, cookies, _ = self.finish(state, **kwargs)
            self.assertEqual((status, headers["Location"]), (302, "/login?google=state"), kwargs)
            self.assertIsNone(self.cookie_value(cookies, server.SESSION_COOKIE))
        self.assertEqual(self.fake.calls, [])                                       # к Google за токеном не ходили
        status, headers, _, _ = self.finish(self.begin())
        self.assertEqual(headers["Location"], "/boards")
        # тот же адрес возврата во второй раз: начатый вход израсходован
        state = self.begin()
        self.assertEqual(self.finish(state)[1]["Location"], "/")
        self.assertEqual(self.finish(state)[1]["Location"], "/login?google=state")
        status, headers, cookies, _ = self.get("/auth/google/callback?state=never-started&code=c", {server.OAUTH_COOKIE: "never-started"})
        self.assertEqual((status, headers["Location"]), (302, "/login?google=state"))
        self.assertEqual(self.get("/auth/google/callback")[1]["Location"], "/login?google=state")           # вообще без параметров
        self.assertEqual(self.get("/auth/google/callback", headers={"Referer": "https://evil.example"})[1]["Location"], "/login?google=state")

    def test_a_refusal_at_google_and_failures_return_to_the_login_page(self):
        state = self.begin()
        status, headers, cookies, _ = self.get("/auth/google/callback?error=access_denied&state=" + state, {server.OAUTH_COOKIE: state})
        self.assertEqual((status, headers["Location"]), (302, "/login?google=denied"))
        self.assertIn("Max-Age=0", [c for c in cookies if c.startswith(server.OAUTH_COOKIE)][0])
        self.assertEqual(self.finish(state)[1]["Location"], "/login?google=state")                          # начатый вход забыт
        cases = [({"email_verified": False}, "unverified"), ({"aud": "someone-else"}, "error"), ({"nonce": "forged"}, "state"),
                 ({"email": DROP}, "email")]
        people = len(self.store.list_users())
        for claims, code in cases:
            self.fake.claims = dict({"sub": "g-400", "email": "dave@example.com"}, **claims)
            status, headers, cookies, _ = self.finish(self.begin())
            self.assertEqual(headers["Location"], "/login?google=" + code, claims)
            self.assertIsNone(self.cookie_value(cookies, server.SESSION_COOKIE))
        self.fake.claims = {}
        self.fake.error = OSError("Google недоступен")
        self.assertEqual(self.finish(self.begin())[1]["Location"], "/login?google=error")
        self.fake.error = None
        self.assertEqual(len(self.store.list_users()), people)                      # ни одной новой записи

    def test_people_who_cannot_sign_in_are_told_why(self):
        who = self.sign_in("g-500", "erin@example.com")[0]
        token = self.sign_in("g-500", "erin@example.com")[1]
        self.assertEqual(self.call("POST", "/api/logout", cookie=token)[0], 200)
        erin = [u for u in self.store.list_users() if u["username"] == "erin"][0]
        self.store.set_user_disabled(erin["id"], True)
        self.fake.claims = {"sub": "g-500", "email": "erin@example.com"}
        self.assertEqual(self.finish(self.begin())[1]["Location"], "/login?google=blocked")
        self.store.set_registration(False)
        self.fake.claims = {"sub": "g-501", "email": "frank@example.com"}
        self.assertEqual(self.finish(self.begin())[1]["Location"], "/login?google=closed")
        self.assertEqual(who, "/boards")

    def test_the_admin_is_never_signed_in_through_google(self):
        link = self.store.set_email(self.store.admin_id, "owner@example.com", PASSWORD)
        self.store.confirm_email(link)
        self.fake.claims = {"sub": "g-600", "email": "owner@example.com"}
        status, headers, cookies, _ = self.finish(self.begin())
        self.assertEqual(headers["Location"], "/login?google=admin")
        self.assertIsNone(self.cookie_value(cookies, server.SESSION_COOKIE))

    def test_a_google_record_sets_a_first_password_without_the_old_one(self):
        _, token, _ = self.sign_in("g-700", "gina@example.com")
        self.assertEqual(self.call("POST", "/api/account/password", {"new": "short"}, cookie=token)[0], 400)
        self.assertEqual(self.call("POST", "/api/account/password", {"new": "brand new password"}, cookie=token)[0], 200)
        self.assertEqual(self.call("GET", "/api/me", cookie=token)[1]["has_password"], True)
        self.assertEqual(self.call("POST", "/api/login", {"username": "gina", "password": "brand new password"})[0], 200)
        self.assertEqual(self.call("POST", "/api/login", {"username": "gina@example.com", "password": "brand new password"})[0], 200)
        self.assertEqual(self.call("POST", "/api/account/password", {"new": "another new password"}, cookie=token)[0], 400)   # теперь нужен текущий

    def test_passwords_never_open_a_record_without_one_and_forgot_gives_it_a_password(self):
        self.sign_in("g-800", "hugo@example.com")
        for attempt in ("", "!", self.PW):
            self.assertEqual(self.call("POST", "/api/login", {"username": "hugo", "password": attempt}, ip="10.5.5.5")[0], 401)
        self.assertEqual(self.call("POST", "/api/forgot", {"email": "hugo@example.com"})[0], 200)
        token = self.outbox.last_token("hugo@example.com")
        self.assertEqual(self.call("POST", "/api/reset", {"token": token, "password": "brand new password"})[0], 200)
        self.assertEqual(self.call("POST", "/api/login", {"username": "hugo@example.com", "password": "brand new password"})[0], 200)

    def test_it_links_the_record_that_has_the_same_confirmed_address(self):
        status, data, headers = self.call("POST", "/api/register", {"username": "ivan-pw", "email": "ivan.pw@example.com", "password": self.PW})
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", "/api/verify", {"token": self.outbox.last_token("ivan.pw@example.com")})[0], 200)
        self.fake.claims = {"sub": "g-900", "email": "ivan.pw@example.com"}
        status, headers, cookies, _ = self.finish(self.begin())
        self.assertEqual(headers["Location"], "/")
        me = self.call("GET", "/api/me", cookie=self.cookie_value(cookies, server.SESSION_COOKIE))[1]
        self.assertEqual((me["user"]["username"], me["has_password"], me["google"]), ("ivan-pw", True, True))
        self.assertEqual(self.call("POST", "/api/login", {"username": "ivan-pw", "password": self.PW})[0], 200)   # пароль остался

    def test_starts_are_limited_per_address(self):
        self.httpd.google_ip_limiter = server.FailureLimiter(limit=3, window=3600)
        for _ in range(3):
            self.assertTrue(self.get("/auth/google", headers={"X-Real-IP": "10.6.6.6"})[1]["Location"].startswith("https://accounts"))
        self.assertEqual(self.get("/auth/google", headers={"X-Real-IP": "10.6.6.6"})[1]["Location"], "/login?google=limit")
        self.assertTrue(self.get("/auth/google", headers={"X-Real-IP": "10.6.6.7"})[1]["Location"].startswith("https://accounts"))

    def test_only_get_starts_a_sign_in(self):
        for method in ("POST", "DELETE"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request(method, "/auth/google", body=b"{}", headers=self.ACTION)
            self.assertIn(conn.getresponse().status, (401, 404, 405))
            conn.close()


def find_bash():
    """Настоящий bash (не заглушка WSL) для проверки скриптов deploy/configure-*.sh; None, если его нет."""
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if candidate and Path(candidate).exists():
            try:
                probe = subprocess.run([candidate, "-c", "echo $BASH_VERSION; uname -r"], capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                continue
            if probe.returncode == 0 and probe.stdout.strip() and b"microsoft" not in probe.stdout.lower():
                return candidate
    return None


BASH = find_bash()


@unittest.skipUnless(BASH, "нужна оболочка bash")
class ConfigureScriptTests(unittest.TestCase):
    """Скрипты, которыми владелец вводит ключи на VPS (deploy/configure-mail.sh и configure-google.sh), на пробном .env."""
    DEPLOY = Path(server.__file__).resolve().parent / "deploy"
    ID = b"1234567890-abcdefghijklmnop.apps.googleusercontent.com"
    SECRET = b"GOCSPX-test-secret-0123456789"
    START = (b"DASH_VIEW_USER=garden\nDASH_PUBLIC_URL=https://dash.dvinsk.lat\nDASH_SMTP_HOST=smtp.resend.com\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / "x.env"
        self.env.write_bytes(self.START)

    @staticmethod
    def posix(path):
        return str(path).replace("\\", "/")

    def run_script(self, name, answers):
        env = dict(os.environ, DASH_ENV_FILE=self.posix(self.env), DASH_PYTHON=self.posix(sys.executable), PYTHONUTF8="1")
        done = subprocess.run([BASH, self.posix(self.DEPLOY / name)], input=answers, capture_output=True, env=env, timeout=90)
        return done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace")

    def leftovers(self):
        return sorted(p.name for p in Path(self.tmp.name).iterdir() if p.name != "x.env")

    def test_a_stray_byte_in_the_site_address_is_asked_again_instead_of_breaking_the_file(self):
        # Так упал первый запуск на VPS: в адресе сайта оказался одинокий байт 0xD0 («dvinsk.la\xd0t»)
        code, output = self.run_script("configure-google.sh", b"https://dash.dvinsk.la\xd0t\n\n" + self.ID + b"\n" + self.SECRET + b"\n")
        self.assertEqual(code, 0, output)
        self.assertIn("символы вне латиницы", output)
        text = self.env.read_bytes().decode("utf-8")                                 # файл читается как UTF-8
        self.assertIn("DASH_PUBLIC_URL=https://dash.dvinsk.lat\n", text)
        self.assertIn("DASH_GOOGLE_CLIENT_ID=" + self.ID.decode(), text)
        self.assertIn("DASH_GOOGLE_CLIENT_SECRET=" + self.SECRET.decode(), text)
        self.assertNotIn(self.SECRET.decode(), output)                               # секрет нигде не напечатан
        self.assertEqual(len(self.leftovers()), 1, self.leftovers())                 # одна копия прежнего файла, временных файлов нет
        self.assertTrue(self.leftovers()[0].startswith("x.env.bak-"))
        self.assertEqual(server.Config(self.read_env()).google_problems(), [])

    def read_env(self):
        env = {}
        for line in self.env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.startswith("DASH_"):
                env[key] = value
        return env

    def test_an_invisible_character_in_the_secret_stops_the_script_without_touching_the_file(self):
        bad = b"GOCSPX-bad\xe2\x80\x8b-0123456789\n"                                  # невидимый знак нулевой ширины при вставке
        code, output = self.run_script("configure-google.sh", b"\n" + self.ID + b"\n" + bad * 3)
        self.assertNotEqual(code, 0)
        self.assertIn("три попытки не вышли", output)
        self.assertEqual(self.env.read_bytes(), self.START)
        self.assertEqual(self.leftovers(), [])

    def test_the_second_try_may_be_right(self):
        code, output = self.run_script("configure-google.sh", b"\n" + self.ID + b"\nGOCSPX-bad\xd0-0123456789\n" + self.SECRET + b"\n")
        self.assertEqual(code, 0, output)
        self.assertIn("DASH_GOOGLE_CLIENT_SECRET=" + self.SECRET.decode(), self.env.read_text(encoding="utf-8"))

    def test_wrong_values_write_nothing(self):
        for answers in (b"https://dash.dvinsk.lat/app\n" + self.ID + b"\n" + self.SECRET + b"\n",            # адрес с путём
                        b"http://dash.dvinsk.lat\n" + self.ID + b"\n" + self.SECRET + b"\n",               # без https
                        b"\nnot-an-id\n" + self.SECRET + b"\n",                                           # идентификатор не оттуда
                        b"\n" + self.ID + b"\nshort\n",                                                   # короткий секрет
                        b"\n" + self.ID + b"\nGOCSPX test secret value\n",                                # пробелы в секрете
                        b"\n" + self.ID + b"\n\n"):                                                       # секрет не задан
            code, output = self.run_script("configure-google.sh", answers)
            self.assertNotEqual(code, 0, (answers, output))
            self.assertIn("Ничего не записано", output)
            self.assertEqual(self.env.read_bytes(), self.START, answers)
            self.assertEqual(self.leftovers(), [], answers)

    def test_a_trailing_slash_in_the_address_is_dropped(self):
        code, output = self.run_script("configure-google.sh", b"https://dash.dvinsk.lat/\n" + self.ID + b"\n" + self.SECRET + b"\n")
        self.assertEqual(code, 0, output)
        self.assertIn("DASH_PUBLIC_URL=https://dash.dvinsk.lat\n", self.env.read_text(encoding="utf-8"))

    def test_nothing_changes_when_nothing_is_new(self):
        self.run_script("configure-google.sh", b"\n" + self.ID + b"\n" + self.SECRET + b"\n")
        before = self.env.read_bytes()
        files = self.leftovers()
        code, output = self.run_script("configure-google.sh", b"\n\n\n")
        self.assertEqual(code, 0, output)
        self.assertIn("Изменений нет", output)
        self.assertEqual((self.env.read_bytes(), self.leftovers()), (before, files))                        # ни переписывания, ни новых копий

    def test_the_mail_script_writes_the_mail_keys_and_hides_the_password(self):
        key = b"re_test_key_value_12345"
        code, output = self.run_script("configure-mail.sh", b"\n\n\n\n" + key + b"\n\n-\n\n")
        self.assertEqual(code, 0, output)
        text = self.env.read_text(encoding="utf-8")
        for line in ("DASH_SMTP_HOST=smtp.resend.com", "DASH_SMTP_PORT=587", "DASH_SMTP_USER=resend", "DASH_SMTP_PASSWORD=" + key.decode(),
                     "DASH_MAIL_FROM=MicroGarden <noreply@dvinsk.lat>"):
            self.assertIn(line, text)
        self.assertNotIn("DASH_MAIL_REPLY_TO", text)
        self.assertNotIn(key.decode(), output)
        self.assertEqual(server.Config(self.read_env()).mail_problems(), [])
        code, output = self.run_script("configure-mail.sh", b"\n\n\xd0\n")                                 # байт вместо порта: вопрос повторяется
        self.assertIn("символы вне латиницы", output)


class ConfigTests(unittest.TestCase):
    def test_accounts_settings_are_validated(self):
        base = {"DASH_VIEW_PASSWORD": PASSWORD}
        server.Config(dict(base, DASH_REGISTRATION="closed", DASH_VIEW_USER="owner")).validate()
        for bad in ({"DASH_REGISTRATION": "maybe"}, {"DASH_VIEW_USER": "ab"}, {"DASH_VIEW_USER": "с кириллицей"}):
            with self.assertRaises(SystemExit, msg=str(bad)):
                server.Config(dict(base, **bad)).validate()
        cfg = server.Config(dict(base, DASH_MAX_USERS="5", DASH_MAX_BOARDS_PER_USER="1", DASH_MAX_BOARDS="7", DASH_SESSION_DAYS="2"))
        self.assertEqual((cfg.registration, cfg.max_users, cfg.max_boards_per_user, cfg.max_boards, cfg.session_days),
                         ("open", 5, 1, 7, 2))

    def test_ingest_token_is_optional_but_never_weak(self):
        server.Config({"DASH_VIEW_PASSWORD": PASSWORD}).validate()            # токены создаются на странице /boards
        with self.assertRaises(SystemExit):
            server.Config({"DASH_INGEST_TOKEN": "short", "DASH_VIEW_PASSWORD": PASSWORD}).validate()

    def test_refuses_weak_secrets(self):
        with self.assertRaises(SystemExit):
            server.Config({"DASH_INGEST_TOKEN": "short", "DASH_VIEW_PASSWORD": PASSWORD}).validate()
        with self.assertRaises(SystemExit):
            server.Config({"DASH_INGEST_TOKEN": TOKEN, "DASH_VIEW_PASSWORD": "short"}).validate()
        server.Config({"DASH_INGEST_TOKEN": TOKEN, "DASH_VIEW_PASSWORD": PASSWORD}).validate()

    def test_env_file_with_broken_bytes_stops_the_start_with_a_clear_message(self):
        # Побитый байт (скрипт настройки раньше мог его записать) не должен ронять сервер трассировкой, а значение не должно попасть в текст
        import os
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("DASH_TEST_OK", "DASH_TEST_AFTER")])
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_bytes(b"DASH_TEST_OK=\xd0\xb4\xd0\xb0\nDASH_TEST_PUBLIC_URL=https://dash.example.la\xd0t\nDASH_TEST_AFTER=1\n")
            with self.assertRaises(SystemExit) as caught:
                server.load_env_file(env)
            message = str(caught.exception)
            self.assertIn("строке 2", message)
            self.assertIn("DASH_TEST_PUBLIC_URL", message)
            self.assertNotIn("dash.example", message)                                 # значение в сообщение не попадает
            self.assertEqual(os.environ.get("DASH_TEST_OK"), "да")                    # строки до сбоя прочитаны, после сбоя нет
            self.assertIsNone(os.environ.get("DASH_TEST_AFTER"))

    def test_env_file_does_not_override_environment(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# комментарий\nDASH_TEST_A=из файла\nDASH_TEST_B='в кавычках'\nDASH_TEST_C=из файла\n", encoding="utf-8")
            os.environ["DASH_TEST_C"] = "из окружения"
            self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("DASH_TEST_A", "DASH_TEST_B", "DASH_TEST_C")])
            server.load_env_file(env)
            self.assertEqual(os.environ["DASH_TEST_A"], "из файла")
            self.assertEqual(os.environ["DASH_TEST_B"], "в кавычках")
            self.assertEqual(os.environ["DASH_TEST_C"], "из окружения")


if __name__ == "__main__":
    unittest.main()
