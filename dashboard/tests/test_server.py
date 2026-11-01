# -*- coding: utf-8 -*-
"""Тесты сервера телеметрии: python -m unittest discover -s dashboard/tests -v"""
import base64
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402
from tools import seed_demo  # noqa: E402,F401  (проверяем, что заливщик демо-данных импортируется)

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
        status, headers, _ = self.request("GET", "/", auth=False)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.request("GET", "/", password="wrong-password")[0], 401)
        self.assertEqual(self.request("GET", "/", user="admin")[0], 401)
        self.assertEqual(self.request("GET", "/api/status", auth=False)[0], 401)
        # токен платы не открывает страницу
        self.assertEqual(self.request("GET", "/", headers={"Authorization": "Bearer " + TOKEN}, auth=False)[0], 401)

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
        old = server.MAX_BOARDS
        self.addCleanup(setattr, server, "MAX_BOARDS", old)
        server.MAX_BOARDS = 2
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
        self.assertEqual(self.request("GET", "/boards", auth=False)[0], 401)
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
        old = server.MAX_BOARDS
        server.MAX_BOARDS = 0
        self.addCleanup(setattr, server, "MAX_BOARDS", old)
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


class ConfigTests(unittest.TestCase):
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
