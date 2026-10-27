#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MicroGarden V1: приём телеметрии и страница с графиками (замена Ubidots).

Плата раз в минуту шлёт POST /api/ingest: последние показания и новые строки своего
журнала (того же, что идёт в Serial). Сервер кладёт их в SQLite и отдаёт страницу с
графиками и списком сообщений об ошибках. Только стандартная библиотека Python 3.9+.

Настройки читаются из переменных окружения DASH_* и из файла .env рядом со скриптом
(переменные окружения главнее). Без токена приёма и пароля просмотра сервер не стартует.
"""
import base64
import csv
import hmac
import io
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
LOG = logging.getLogger("dash")

MAX_BODY = 64 * 1024        # весь запрос приёма
MAX_LOG_BYTES = 24 * 1024   # новая порция журнала в одном запросе
MAX_LINE_CHARS = 400
MAX_EXPORT_ROWS = 100_000
DEVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

# Числовые поля показаний: имя -> (минимум, максимум, целое). Всё, что вне диапазона или не
# число, отбрасывается: плата с повреждённым ответом датчика не должна ломать графики.
NUM_FIELDS = {
    "temp": (-40, 125, False), "hum": (0, 100, False),
    "led": (0, 255, True), "fan1": (0, 255, True), "fan2": (0, 255, True),
    "power_w": (-100, 5000, False), "bus_v": (0, 100, False), "current_a": (-100, 100, False),
    "energy_kwh": (0, 1000, False),
}
BOOL_FIELDS = ("pump", "heater", "led_blocked", "sht_ok", "rtc_ok")
EXTRA_NUM_FIELDS = {
    "rssi": (-130, 0, True), "heap_free": (0, 10_000_000, True), "uptime_s": (0, 2 ** 32, True),
}
EXTRA_BOOL_FIELDS = ("clock_trusted", "water_present", "water_sensor", "sd_ok", "power_ok")

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}
BUCKETS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400)
MAX_SPAN = 400 * 86400

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

# Классификация строк журнала. Правила здесь, а не в прошивке: их можно поправить без
# перепрошивки. Порядок важен: сначала сообщения об успехе (в них встречаются слова вроде
# «аварийного режима»), потом ожидаемые предупреждения, потом ошибки.
INFO_FIRST = re.compile(r"восстановлен|успешна|записано и проверено|найден на|подключено", re.I)
WARN_FIRST = re.compile(r"попытка инициализации|диагностика: .*crc ok/ok", re.I)
ERROR_RE = re.compile(
    r"ошибка|критика|аварийн|не удалось|не удалась|не отвечает|перестал отвечать|нет ответа|"
    r"не принял|не остановил|не запустил|вне рабочего диапазона|вне диапазона|hard limit|failed|"
    r"cannot|некорректн|не найден|не совпало", re.I)
WARN_RE = re.compile(
    r"sd-карта не обнаружена|пропущен|из будущего|sda удерживается|нет связи|с опозданием|nvs write",
    re.I)


def classify(line):
    """'error' | 'warn' | 'info' для строки журнала платы."""
    if INFO_FIRST.search(line):
        return "info"
    if WARN_FIRST.search(line):
        return "warn"
    if ERROR_RE.search(line):
        return "error"
    if WARN_RE.search(line):
        return "warn"
    return "info"


def load_env_file(path):
    """KEY=VALUE построчно; уже заданные переменные окружения не перезаписываются."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


class Config:
    def __init__(self, env):
        self.host = env.get("DASH_HOST", "127.0.0.1")
        self.port = int(env.get("DASH_PORT", "8790"))
        self.db = Path(env.get("DASH_DB", str(HERE / "data" / "dash.sqlite3")))
        self.ingest_token = env.get("DASH_INGEST_TOKEN", "")
        self.view_user = env.get("DASH_VIEW_USER", "garden")
        self.view_password = env.get("DASH_VIEW_PASSWORD", "")
        self.retention_days = int(env.get("DASH_RETENTION_DAYS", "400"))
        self.log_retention_days = int(env.get("DASH_LOG_RETENTION_DAYS", "90"))
        self.online_sec = int(env.get("DASH_ONLINE_SEC", "180"))

    def validate(self):
        problems = []
        if len(self.ingest_token) < 24:
            problems.append("DASH_INGEST_TOKEN: нужен токен не короче 24 символов")
        if len(self.view_password) < 12:
            problems.append("DASH_VIEW_PASSWORD: нужен пароль не короче 12 символов")
        if problems:
            raise SystemExit("Сервер не запущен:\n  " + "\n  ".join(problems))


def as_number(value, lo, hi, integer):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < lo or value > hi:
        return None
    return int(round(value)) if integer else round(float(value), 3)


def as_flag(value):
    if isinstance(value, bool):
        return int(value)
    return None


def clean_sample(raw):
    """Оставляет только известные поля с допустимыми значениями. -> (колонки, extra)."""
    cols = {name: as_number(raw.get(name), *spec) for name, spec in NUM_FIELDS.items()}
    cols.update({name: as_flag(raw.get(name)) for name in BOOL_FIELDS})
    # Датчик не отвечает — значения температуры и влажности устаревшие, на график не идут.
    if cols["sht_ok"] == 0:
        cols["temp"] = cols["hum"] = None
    extra = {name: as_number(raw.get(name), *spec) for name, spec in EXTRA_NUM_FIELDS.items()}
    extra.update({name: as_flag(raw.get(name)) for name in EXTRA_BOOL_FIELDS})
    fw = raw.get("fw")
    if isinstance(fw, str):
        extra["fw"] = fw[:32]
    return cols, {k: v for k, v in extra.items() if v is not None}


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
  device TEXT PRIMARY KEY, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  boot TEXT, log_next INTEGER NOT NULL DEFAULT 0, sample_ts INTEGER, last_sample TEXT);
CREATE TABLE IF NOT EXISTS samples(
  device TEXT NOT NULL, ts INTEGER NOT NULL,
  temp REAL, hum REAL, led INTEGER, fan1 INTEGER, fan2 INTEGER,
  power_w REAL, bus_v REAL, current_a REAL, energy_kwh REAL,
  pump INTEGER, heater INTEGER, led_blocked INTEGER, sht_ok INTEGER, rtc_ok INTEGER,
  PRIMARY KEY(device, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT NOT NULL, ts INTEGER NOT NULL,
  level TEXT NOT NULL, line TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS logs_device_id ON logs(device, id);
CREATE INDEX IF NOT EXISTS logs_device_level_id ON logs(device, level, id);
"""
SAMPLE_COLS = ("temp", "hum", "led", "fan1", "fan2", "power_w", "bus_v", "current_a", "energy_kwh",
               "pump", "heater", "led_blocked", "sht_ok", "rtc_ok")


def pick_bucket(span, target=500):
    need = span / target
    for bucket in BUCKETS:
        if bucket >= need:
            return bucket
    return BUCKETS[-1]


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with closing(self._connect()) as cx:
            cx.execute("PRAGMA journal_mode=WAL")
            cx.executescript(SCHEMA)

    def _connect(self):
        cx = sqlite3.connect(self.path, timeout=10)
        cx.execute("PRAGMA synchronous=NORMAL")
        return cx

    # --- запись ---------------------------------------------------------------------------
    def ingest(self, device, body, now=None):
        now = int(time.time() if now is None else now)
        sample = body.get("sample")
        log = body.get("log")
        cols = extra = None
        if isinstance(sample, dict):
            cols, extra = clean_sample(sample)
        with self._write_lock, closing(self._connect()) as cx, cx:
            row = cx.execute("SELECT boot, log_next FROM devices WHERE device=?", (device,)).fetchone()
            known = row is not None
            boot, log_next = (row[0], row[1]) if known else (None, 0)
            lines = 0
            if isinstance(log, dict):
                boot, log_next, lines = self._ingest_log(cx, device, now, log, known, boot, log_next)
            if cols is not None:
                cx.execute(
                    "INSERT OR REPLACE INTO samples(device, ts, %s) VALUES (?, ?, %s)"
                    % (", ".join(SAMPLE_COLS), ", ".join("?" * len(SAMPLE_COLS))),
                    (device, now) + tuple(cols[name] for name in SAMPLE_COLS))
                shown = {k: v for k, v in cols.items() if v is not None}
                shown.update(extra)
                cx.execute(
                    "INSERT INTO devices(device, first_seen, last_seen, boot, log_next, sample_ts, last_sample)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(device) DO UPDATE SET last_seen=excluded.last_seen,"
                    " boot=excluded.boot, log_next=excluded.log_next, sample_ts=excluded.sample_ts,"
                    " last_sample=excluded.last_sample",
                    (device, now, now, boot, log_next, now, json.dumps(shown, separators=(",", ":"))))
            else:
                cx.execute(
                    "INSERT INTO devices(device, first_seen, last_seen, boot, log_next) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(device) DO UPDATE SET last_seen=excluded.last_seen, boot=excluded.boot,"
                    " log_next=excluded.log_next",
                    (device, now, now, boot, log_next))
        return {"ok": True, "log_next": log_next, "sample": cols is not None, "log_lines": lines}

    def _ingest_log(self, cx, device, now, log, known, boot, log_next):
        text = log.get("text")
        start = as_number(log.get("from"), 0, 2 ** 40, True)
        new_boot = log.get("boot")
        new_boot = new_boot[:16] if isinstance(new_boot, str) else ""
        if not isinstance(text, str) or start is None:
            raise ValueError("log: нужны from (число) и text (строка)")
        if not new_boot:
            # Без номера запуска не отличить перезагрузку платы от повтора уже принятой порции.
            raise ValueError("log.boot обязателен")
        data = text.encode("utf-8")
        markers = []
        if known and boot and new_boot and new_boot != boot:
            # Счётчик смещений начался заново: плата перезагрузилась.
            markers.append("— плата перезагрузилась —")
        elif known and start > log_next:
            markers.append("— пропущено %d байт журнала (буфер платы переполнен или пропала связь) —"
                           % (start - log_next))
        elif known and start < log_next:
            # Повтор уже принятой части (ответ потерялся): берём только новое. Границу
            # режем по байтам, поэтому битый символ на стыке заменяется, а не роняет разбор.
            skip = log_next - start
            data = data[skip:] if skip < len(data) else b""
            start = log_next
        for marker in markers:
            cx.execute("INSERT INTO logs(device, ts, level, line) VALUES (?, ?, 'warn', ?)", (device, now, marker))
        added = len(markers)
        for raw in data.decode("utf-8", errors="replace").split("\n"):
            line = raw.strip("\r")[:MAX_LINE_CHARS].rstrip()
            if line:
                cx.execute("INSERT INTO logs(device, ts, level, line) VALUES (?, ?, ?, ?)",
                           (device, now, classify(line), line))
                added += 1
        return (new_boot or boot), start + len(data), added

    def prune(self, retention_days, log_retention_days, now=None):
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("DELETE FROM samples WHERE ts < ?", (now - retention_days * 86400,))
            cx.execute("DELETE FROM logs WHERE ts < ?", (now - log_retention_days * 86400,))

    # --- чтение ---------------------------------------------------------------------------
    def devices(self, now=None):
        now = int(time.time() if now is None else now)
        with closing(self._connect()) as cx:
            rows = cx.execute("SELECT device, last_seen FROM devices ORDER BY last_seen DESC").fetchall()
        return [{"device": d, "last_seen": ts, "age": now - ts} for d, ts in rows]

    def status(self, device, online_sec, now=None):
        now = int(time.time() if now is None else now)
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT last_seen, boot, sample_ts, last_sample FROM devices WHERE device=?",
                             (device,)).fetchone()
            if row is None:
                return None
            counts = dict(cx.execute(
                "SELECT level, COUNT(*) FROM logs WHERE device=? AND ts>=? GROUP BY level",
                (device, now - 86400)).fetchall())
        last_seen, boot, sample_ts, last_sample = row
        return {"device": device, "now": now, "last_seen": last_seen, "age": now - last_seen,
                "online": now - last_seen <= online_sec, "online_sec": online_sec, "boot": boot,
                "sample_ts": sample_ts, "sample": json.loads(last_sample) if last_sample else None,
                "errors_24h": counts.get("error", 0), "warnings_24h": counts.get("warn", 0)}

    def series(self, device, t_from, t_to):
        bucket = pick_bucket(t_to - t_from)
        with closing(self._connect()) as cx:
            rows = cx.execute(
                "SELECT (ts / :b) * :b AS t, AVG(temp), AVG(hum), AVG(led), AVG(fan1), AVG(fan2), AVG(power_w),"
                " AVG(bus_v), AVG(current_a), MAX(pump), MAX(heater), MAX(led_blocked), MIN(sht_ok)"
                " FROM samples WHERE device=:d AND ts>=:f AND ts<:to GROUP BY t ORDER BY t",
                {"b": bucket, "d": device, "f": t_from, "to": t_to}).fetchall()
        names = ("t", "temp", "hum", "led", "fan1", "fan2", "power_w", "bus_v", "current_a",
                 "pump", "heater", "led_blocked", "sht_ok")
        out = {"device": device, "from": t_from, "to": t_to, "bucket": bucket}
        for index, name in enumerate(names):
            column = [r[index] for r in rows]
            if name in ("temp", "hum", "power_w", "bus_v", "current_a"):
                column = [None if v is None else round(v, 2) for v in column]
            elif name in ("led", "fan1", "fan2"):
                column = [None if v is None else round(v, 1) for v in column]
            out[name] = column
        return out

    def logs(self, device, level="error", limit=200, before=None):
        levels = {"error": ("error",), "warn": ("error", "warn"), "all": ("error", "warn", "info")}[level]
        sql = ("SELECT id, ts, level, line FROM logs WHERE device=? AND level IN (%s)"
               % ", ".join("?" * len(levels)))
        args = [device, *levels]
        if before is not None:
            sql += " AND id < ?"
            args.append(before)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with closing(self._connect()) as cx:
            rows = cx.execute(sql, args).fetchall()
        return [{"id": i, "ts": ts, "level": lv, "line": ln} for i, ts, lv, ln in rows]

    def export_csv(self, device, t_from, t_to):
        with closing(self._connect()) as cx:
            rows = cx.execute(
                "SELECT ts, %s FROM samples WHERE device=? AND ts>=? AND ts<? ORDER BY ts LIMIT ?"
                % ", ".join(SAMPLE_COLS), (device, t_from, t_to, MAX_EXPORT_ROWS + 1)).fetchall()
        if len(rows) > MAX_EXPORT_ROWS:
            raise ValueError("слишком много строк: сузьте интервал (не больше %d)" % MAX_EXPORT_ROWS)
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\n")
        writer.writerow(("timestamp_utc",) + SAMPLE_COLS)
        for ts, *values in rows:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
            writer.writerow([stamp] + ["" if v is None else v for v in values])
        return buf.getvalue().encode("utf-8")


class FailureLimiter:
    """Блокирует адрес после 10 неудачных входов за 10 минут."""

    def __init__(self, limit=10, window=600):
        self.limit, self.window = limit, window
        self._lock = threading.Lock()
        self._failures = {}

    def blocked(self, ip, now=None):
        now = time.time() if now is None else now
        with self._lock:
            hits = [t for t in self._failures.get(ip, ()) if now - t < self.window]
            self._failures[ip] = hits
            return len(hits) >= self.limit

    def fail(self, ip, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._failures.setdefault(ip, []).append(now)

    def clear(self, ip):
        with self._lock:
            self._failures.pop(ip, None)


def int_param(query, name, default=None, lo=None, hi=None):
    values = query.get(name)
    if not values:
        return default
    try:
        number = int(values[0])
    except ValueError:
        raise ValueError("параметр %s должен быть целым числом" % name)
    if (lo is not None and number < lo) or (hi is not None and number > hi):
        raise ValueError("параметр %s вне допустимого диапазона" % name)
    return number


class Handler(BaseHTTPRequestHandler):
    server_version = "MicroGardenDash/1"
    protocol_version = "HTTP/1.1"
    timeout = 20  # зависшее соединение не должно держать поток вечно

    def log_message(self, fmt, *args):  # успешные запросы не пишем: страница опрашивает сервер каждые 30 с
        pass

    def _send(self, status, body=b"", ctype="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:
            self.send_header("Connection", "close")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload, headers=None):
        self._send(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                   headers=headers)

    def _error(self, status, message, headers=None):
        self._json(status, {"ok": False, "error": message}, headers)

    def _client_ip(self):
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):  # за nginx настоящий адрес приходит в заголовке
            return self.headers.get("X-Real-IP", peer)[:64]
        return peer

    def _check_viewer(self):
        ip = self._client_ip()
        limiter = self.server.limiter
        if limiter.blocked(ip):
            self._error(429, "слишком много неудачных попыток входа", {"Retry-After": "600"})
            return False
        user = password = ""
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")
            except ValueError:
                pass
        cfg = self.server.cfg
        ok_user = hmac.compare_digest(user.encode(), cfg.view_user.encode())
        ok_pass = hmac.compare_digest(password.encode(), cfg.view_password.encode())
        if ok_user and ok_pass:
            limiter.clear(ip)
            return True
        if header:
            limiter.fail(ip)
        self._error(401, "нужен вход", {"WWW-Authenticate": 'Basic realm="MicroGarden", charset="UTF-8"'})
        return False

    def _check_ingest_token(self):
        ip = self._client_ip()
        limiter = self.server.limiter
        if limiter.blocked(ip):
            self._error(429, "слишком много неудачных попыток", {"Retry-After": "600"})
            return False
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if hmac.compare_digest(token.encode(), self.server.cfg.ingest_token.encode()):
            limiter.clear(ip)
            return True
        limiter.fail(ip)
        LOG.warning("приём: неверный токен с %s", ip)
        self._error(401, "неверный токен")
        return False

    def do_HEAD(self):
        self._route()

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        try:
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            if self.command == "POST":
                # Тело запроса читает только приём, и то после проверки токена. Во всех остальных
                # случаях оно остаётся в потоке, поэтому соединение закрывается после ответа.
                self.close_connection = True
            if path == "/healthz" and self.command in ("GET", "HEAD"):
                with closing(self.server.store._connect()) as cx:
                    cx.execute("SELECT 1")
                return self._send(200, b"ok", "text/plain; charset=utf-8")
            if path == "/api/ingest":
                if self.command != "POST":
                    return self._error(405, "нужен POST", {"Allow": "POST"})
                if not self._check_ingest_token():
                    return None
                return self._ingest()
            if self.command not in ("GET", "HEAD"):
                return self._error(405, "метод не поддерживается", {"Allow": "GET, HEAD"})
            if not self._check_viewer():
                return None
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                return self._send(200, (STATIC_DIR / name).read_bytes(), ctype, {"Cache-Control": "no-cache"})
            if path == "/api/devices":
                return self._json(200, {"devices": self.server.store.devices()})
            if path in ("/api/status", "/api/series", "/api/logs", "/api/export.csv"):
                return self._device_api(path, query)
            return self._error(404, "не найдено")
        except ValueError as exc:
            return self._error(400, str(exc))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            return None
        except Exception:  # noqa: BLE001 - наружу уходит только общий ответ, подробности в журнал
            LOG.exception("ошибка обработки %s %s", self.command, self.path)
            return self._error(500, "внутренняя ошибка")

    def _ingest(self):
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            return self._error(411, "нужен Content-Length")
        if int(length) > MAX_BODY:
            return self._error(413, "запрос слишком большой")
        raw = self.rfile.read(int(length))
        self.close_connection = False  # тело прочитано целиком, соединение можно держать
        try:
            body = json.loads(raw)
        except ValueError:
            return self._error(400, "это не JSON")
        if not isinstance(body, dict):
            return self._error(400, "ожидается JSON-объект")
        device = body.get("device", "microgarden")
        if not isinstance(device, str) or not DEVICE_RE.match(device):
            return self._error(400, "device: 1-32 символа, латиница, цифры, _ . -")
        log = body.get("log")
        if isinstance(log, dict) and isinstance(log.get("text"), str) and len(log["text"].encode("utf-8")) > MAX_LOG_BYTES:
            return self._error(413, "порция журнала слишком большая")
        return self._json(200, self.server.store.ingest(device, body))

    def _device_api(self, path, query):
        store, cfg = self.server.store, self.server.cfg
        device = (query.get("device") or [""])[0]
        if not device:
            known = store.devices()
            device = known[0]["device"] if known else "microgarden"
        if not DEVICE_RE.match(device):
            raise ValueError("device: недопустимое имя")
        if path == "/api/status":
            status = store.status(device, cfg.online_sec)
            return self._json(200, status) if status else self._error(404, "плата ещё не присылала данные")
        if path == "/api/logs":
            level = (query.get("level") or ["error"])[0]
            if level not in ("error", "warn", "all"):
                raise ValueError("level: error, warn или all")
            limit = int_param(query, "limit", 200, 1, 500)
            before = int_param(query, "before", None, 1)
            return self._json(200, {"device": device, "level": level, "rows": store.logs(device, level, limit, before)})
        now = int(time.time())
        if (query.get("range") or [""])[0]:
            span = RANGES.get(query["range"][0])
            if span is None:
                raise ValueError("range: " + ", ".join(RANGES))
            t_from, t_to = now - span, now + 1
        else:
            t_to = int_param(query, "to", now + 1, 0)
            t_from = int_param(query, "from", t_to - 86400, 0)
        if t_to <= t_from or t_to - t_from > MAX_SPAN:
            raise ValueError("интервал: от 1 секунды до 400 суток")
        if path == "/api/series":
            return self._json(200, store.series(device, t_from, t_to))
        data = store.export_csv(device, t_from, t_to)
        name = "microgarden-%s.csv" % time.strftime("%Y%m%d-%H%M", time.gmtime(now))
        return self._send(200, data, "text/csv; charset=utf-8",
                          {"Content-Disposition": 'attachment; filename="%s"' % name})


class DashServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg, store):
        super().__init__((cfg.host, cfg.port), Handler)
        self.cfg, self.store, self.limiter = cfg, store, FailureLimiter()

    def handle_error(self, request, client_address):
        # Обрыв соединения клиентом (плата ушла в сон, браузер закрыл вкладку) — не ошибка сервера.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        LOG.exception("необработанная ошибка соединения с %s", client_address[0])


def prune_loop(store, cfg, stop):
    while True:
        try:
            store.prune(cfg.retention_days, cfg.log_retention_days)
        except Exception:  # noqa: BLE001
            LOG.exception("очистка старых записей не удалась")
        if stop.wait(3600):
            return


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env_file(HERE / ".env")
    cfg = Config(os.environ)
    cfg.validate()
    store = Store(cfg.db)
    server = DashServer(cfg, store)
    stop = threading.Event()
    threading.Thread(target=prune_loop, args=(store, cfg, stop), daemon=True).start()
    LOG.info("слушаю http://%s:%d, база %s", cfg.host, cfg.port, cfg.db)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
