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
            "[Heater] Off: temperature reached the 60 C hard limit.": "error",
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
        self.assertFalse(self.store.status("m", 180, now=NOW + 600)["online"])
        self.assertIsNone(self.store.status("unknown", 180))

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
        for path in ("/app.js", "/style.css"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
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


class ConfigTests(unittest.TestCase):
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
