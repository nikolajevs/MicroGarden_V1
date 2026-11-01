#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MicroGarden V1: приём телеметрии и страница с графиками (замена Ubidots).

Плата раз в минуту шлёт POST /api/ingest: последние показания и новые строки своего
журнала (того же, что идёт в Serial). Сервер кладёт их в SQLite и отдаёт страницу с
графиками и списком сообщений об ошибках. Только стандартная библиотека Python 3.9+.

Настройки читаются из переменных окружения DASH_* и из файла .env рядом со скриптом
(переменные окружения главнее). Без пароля просмотра сервер не стартует. Платы регистрируются, а их
токены приёма создаются и отзываются на странице /boards; токен из DASH_INGEST_TOKEN, если он задан,
подхватывается сам как общий. Данные принимаются только от зарегистрированных плат.
"""
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
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

# Платы и токены приёма. Плата должна быть зарегистрирована на странице /boards, иначе её данные не
# принимаются. В базе лежит только SHA-256 токена, сам токен виден один раз при создании.
TOKEN_PREFIX = "mgd_"          # по нему токен узнаётся в списке и в сканерах утечек секретов
NAME_MAX = 40                  # название платы и токена
MAX_BOARDS = 20
MAX_ACTIVE_TOKENS = 40         # с запасом на замену токена: новый создаётся, пока старый ещё действует
MAX_TOKEN_ROWS = 200           # с отозванными: их можно удалить на странице
TOKEN_TOUCH_SEC = 60           # «использовался» обновляется не чаще раза в минуту: меньше записей в базу
MAX_ACTION_BODY = 2048         # тело запроса со страницы плат
ENV_TOKEN_NAME = "Общий токен из .env"
FIRST_TOKEN_NAME = "Основной"

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
    "op_mode": (0, 1, True),  # 0 — выращивание, 1 — сушка
}
EXTRA_BOOL_FIELDS = ("clock_trusted", "clock_usable", "water_present", "water_sensor", "water_skipped",
                     "sd_ok", "power_ok")

RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}
BUCKETS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400)
MAX_SPAN = 400 * 86400

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/alerts.js": ("alerts.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/boards": ("boards.html", "text/html; charset=utf-8"),
    "/boards.html": ("boards.html", "text/html; charset=utf-8"),
    "/boards-lib.js": ("boards-lib.js", "text/javascript; charset=utf-8"),
    "/boards.js": ("boards.js", "text/javascript; charset=utf-8"),
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
        # Токен из .env теперь необязателен: токены приёма создаются на странице /boards.
        if self.ingest_token and len(self.ingest_token) < 24:
            problems.append("DASH_INGEST_TOKEN: нужен токен не короче 24 символов (или уберите его из .env)")
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
CREATE TABLE IF NOT EXISTS boards(
  device TEXT PRIMARY KEY,            -- имя, с которым плата присылает данные (её настройка «Имя платы»)
  name TEXT NOT NULL,                 -- как назвал владелец: «Теплица»
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tokens(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,                 -- как назвал владелец: «Основной», «Запасной»
  device TEXT,                        -- NULL: общий токен (из .env), подходит любой плате; иначе только этой плате
  hash TEXT NOT NULL UNIQUE,          -- SHA-256 токена; самого токена в базе нет
  prefix TEXT NOT NULL,               -- первые символы токена: чтобы узнать его в списке
  created_at INTEGER NOT NULL,
  last_used_at INTEGER, last_ip TEXT,
  revoked_at INTEGER,
  source TEXT NOT NULL DEFAULT 'ui'); -- 'ui' (страница токенов) или 'env' (DASH_INGEST_TOKEN)
"""
TOKEN_COLUMNS = "id, name, device, prefix, created_at, last_used_at, last_ip, revoked_at, source"
SAMPLE_COLS = ("temp", "hum", "led", "fan1", "fan2", "power_w", "bus_v", "current_a", "energy_kwh",
               "pump", "heater", "led_blocked", "sht_ok", "rtc_ok")


class Conflict(Exception):
    """Запрос верный, но состояние не позволяет его выполнить (HTTP 409)."""


def clean_name(value):
    """Название платы или токена: пробелы схлопываются, 1-40 печатных символов."""
    if not isinstance(value, str):
        raise ValueError("name: нужна строка")
    name = " ".join(value.split())
    if not 1 <= len(name) <= NAME_MAX or not name.isprintable():
        raise ValueError("name: от 1 до %d символов" % NAME_MAX)
    return name


def clean_device(value):
    """Имя платы в системе (то, что плата присылает в поле device)."""
    if not isinstance(value, str) or not DEVICE_RE.fullmatch(value):
        raise ValueError("device: 1-32 символа, латиница, цифры, _ . -")
    return value


def token_item(row):
    token_id, name, device, prefix, created, used, ip, revoked, source = row
    return {"id": token_id, "name": name, "device": device, "prefix": prefix, "created_at": created,
            "last_used_at": used, "last_ip": ip, "revoked_at": revoked, "source": source, "active": revoked is None}


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
        self._env_digest = None  # отпечаток токена, который сейчас записан в .env (см. sync_env_token)
        with closing(self._connect()) as cx:
            cx.execute("PRAGMA journal_mode=WAL")
            cx.executescript(SCHEMA)
        self._migrate_boards()

    def _migrate_boards(self):
        """При первом запуске с реестром плат уже присылавшие данные платы вносятся в него сами, чтобы
        они продолжили работать. Один раз: удалённая плата после перезапуска не возвращается."""
        with self._write_lock, closing(self._connect()) as cx, cx:
            if cx.execute("SELECT 1 FROM meta WHERE key='boards_migrated'").fetchone():
                return
            cx.execute("INSERT OR IGNORE INTO boards(device, name, created_at)"
                       " SELECT device, device, first_seen FROM devices")
            cx.execute("INSERT INTO meta(key, value) VALUES('boards_migrated', '1')")

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
                # В базе флаги лежат как 0/1, а странице нужны настоящие true/false.
                flags = BOOL_FIELDS + EXTRA_BOOL_FIELDS
                shown = {k: (bool(v) if k in flags else v) for k, v in cols.items() if v is not None}
                shown.update({k: (bool(v) if k in flags else v) for k, v in extra.items()})
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

    # --- токены приёма --------------------------------------------------------------------
    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def sync_env_token(self, env_token, now=None):
        """Токен из DASH_INGEST_TOKEN попадает в список как «из .env» и действует, пока записан в .env
        и не отозван на странице. Заменили токен в .env или убрали его: прежний перестаёт действовать,
        как было до появления менеджера. Отозванный на странице токен перезапуск не воскрешает."""
        now = int(time.time() if now is None else now)
        digest = self.token_hash(env_token) if env_token else None
        self._env_digest = digest
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("UPDATE tokens SET revoked_at=? WHERE source='env' AND revoked_at IS NULL AND hash IS NOT ?",
                       (now, digest))
            if digest and cx.execute("SELECT 1 FROM tokens WHERE hash=?", (digest,)).fetchone() is None:
                cx.execute("INSERT INTO tokens(name, device, hash, prefix, created_at, source)"
                           " VALUES (?, NULL, ?, ?, ?, 'env')", (ENV_TOKEN_NAME, digest, env_token[:8], now))

    def active_token_count(self):
        with closing(self._connect()) as cx:
            return cx.execute("SELECT COUNT(*) FROM tokens WHERE revoked_at IS NULL").fetchone()[0]

    def _insert_token(self, cx, name, device, now):
        """Запись токена в открытой транзакции. -> (токен открытым текстом, запись списка).
        Открытый токен больше нигде не хранится: в базу идёт только его отпечаток."""
        total, active = cx.execute("SELECT COUNT(*), COALESCE(SUM(revoked_at IS NULL), 0) FROM tokens").fetchone()
        if active >= MAX_ACTIVE_TOKENS:
            raise Conflict("уже %d действующих токенов: отзовите ненужные" % MAX_ACTIVE_TOKENS)
        if total >= MAX_TOKEN_ROWS:
            raise Conflict("в списке %d записей: удалите отозванные токены" % MAX_TOKEN_ROWS)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = cx.execute(
            "INSERT INTO tokens(name, device, hash, prefix, created_at, source) VALUES (?, ?, ?, ?, ?, 'ui')",
            (name, device, self.token_hash(token), token[:8], now)).lastrowid
        row = cx.execute("SELECT %s FROM tokens WHERE id=?" % TOKEN_COLUMNS, (token_id,)).fetchone()
        return token, token_item(row)

    # --- платы ----------------------------------------------------------------------------
    def board_exists(self, device):
        with closing(self._connect()) as cx:
            return cx.execute("SELECT 1 FROM boards WHERE device=?", (device,)).fetchone() is not None

    def add_board(self, name, device, now=None):
        """Регистрирует плату и выдаёт ей первый токен, привязанный к её имени.
        -> (токен открытым текстом, плата, запись токена)."""
        name, device = clean_name(name), clean_device(device)
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:
            if cx.execute("SELECT COUNT(*) FROM boards").fetchone()[0] >= MAX_BOARDS:
                raise Conflict("уже %d плат: удалите ненужные" % MAX_BOARDS)
            if cx.execute("SELECT 1 FROM boards WHERE device=?", (device,)).fetchone():
                raise Conflict("плата с именем %s уже зарегистрирована" % device)
            cx.execute("INSERT INTO boards(device, name, created_at) VALUES (?, ?, ?)", (device, name, now))
            token, item = self._insert_token(cx, FIRST_TOKEN_NAME, device, now)
        return token, {"device": device, "name": name, "created_at": now}, item

    def add_board_token(self, device, name=None, now=None):
        """Ещё один токен для зарегистрированной платы: так токен заменяют, не прерывая приём (старый
        отзывают, когда новый введён в плату). -> (токен, запись списка) или None, если платы нет."""
        device = clean_device(device)
        name = clean_name(name or "Новый токен")
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:
            if cx.execute("SELECT 1 FROM boards WHERE device=?", (device,)).fetchone() is None:
                return None
            return self._insert_token(cx, name, device, now)

    def rename_board(self, device, name):
        name = clean_name(name)
        with self._write_lock, closing(self._connect()) as cx, cx:
            if cx.execute("UPDATE boards SET name=? WHERE device=?", (name, device)).rowcount == 0:
                return None
            row = cx.execute("SELECT device, name, created_at FROM boards WHERE device=?", (device,)).fetchone()
        return {"device": row[0], "name": row[1], "created_at": row[2]}

    def delete_board(self, device):
        """Удаляет плату вместе с её токенами, показаниями и журналом. Пока у платы есть действующий
        токен, удалить её нельзя: сначала доступ отзывают. -> False, если такой платы нет."""
        with self._write_lock, closing(self._connect()) as cx, cx:
            if cx.execute("SELECT 1 FROM boards WHERE device=?", (device,)).fetchone() is None:
                return False
            if cx.execute("SELECT 1 FROM tokens WHERE device=? AND revoked_at IS NULL", (device,)).fetchone():
                raise Conflict("у платы есть действующие токены: сначала отзовите их")
            for table in ("samples", "logs", "devices", "tokens", "boards"):
                cx.execute("DELETE FROM %s WHERE device=?" % table, (device,))
        return True

    def list_boards(self, online_sec, now=None):
        """Платы с их токенами и состоянием связи, а также общие токены (из .env), которые подходят любой плате."""
        now = int(time.time() if now is None else now)
        with closing(self._connect()) as cx:
            boards = cx.execute(
                "SELECT b.device, b.name, b.created_at, d.last_seen, d.last_sample FROM boards b"
                " LEFT JOIN devices d ON d.device=b.device ORDER BY b.created_at, b.device").fetchall()
            tokens = [token_item(row) for row in
                      cx.execute("SELECT %s FROM tokens ORDER BY id DESC" % TOKEN_COLUMNS).fetchall()]
        own = {}
        shared = []
        for item in tokens:
            if item["device"] is None:
                shared.append(item)
            else:
                own.setdefault(item["device"], []).append(item)
        out = []
        for device, name, created, last_seen, sample in boards:
            fw = json.loads(sample).get("fw") if sample else None
            out.append({"device": device, "name": name, "created_at": created, "last_seen": last_seen,
                        "online": last_seen is not None and now - last_seen <= online_sec,
                        "fw": fw if isinstance(fw, str) else None, "tokens": own.get(device, [])})
        return {"boards": out, "shared_tokens": shared}

    def list_tokens(self):
        with closing(self._connect()) as cx:
            rows = cx.execute("SELECT %s FROM tokens ORDER BY id DESC" % TOKEN_COLUMNS).fetchall()
        return [token_item(row) for row in rows]

    def revoke_token(self, token_id, now=None):
        """Отзывает токен (повторный отзыв ничего не меняет). -> запись списка или None, если такого нет."""
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("UPDATE tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (now, token_id))
            row = cx.execute("SELECT %s FROM tokens WHERE id=?" % TOKEN_COLUMNS, (token_id,)).fetchone()
        return token_item(row) if row else None

    def delete_token(self, token_id):
        """Удаляет отозванный токен из списка. -> False, если такого нет."""
        with self._write_lock, closing(self._connect()) as cx, cx:
            row = cx.execute("SELECT revoked_at, hash FROM tokens WHERE id=?", (token_id,)).fetchone()
            if row is None:
                return False
            if row[0] is None:
                raise Conflict("действующий токен удалить нельзя: сначала отзовите его")
            if row[1] == self._env_digest:
                # Иначе при перезапуске токен, который всё ещё записан в .env, появился бы в списке заново.
                raise Conflict("этот токен записан в .env на сервере: уберите его оттуда, потом его можно удалить")
            cx.execute("DELETE FROM tokens WHERE id=?", (token_id,))
        return True

    def authenticate(self, token, ip=None, now=None):
        """Проверка токена приёма. -> {"id", "device"} или None (нет такого или отозван)."""
        now = int(time.time() if now is None else now)
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT id, device, last_used_at, revoked_at FROM tokens WHERE hash=?",
                             (self.token_hash(token),)).fetchone()
        if row is None or row[3] is not None:
            return None
        token_id, device, used, _ = row
        if used is None or now - used >= TOKEN_TOUCH_SEC:
            with self._write_lock, closing(self._connect()) as cx, cx:
                cx.execute("UPDATE tokens SET last_used_at=?, last_ip=? WHERE id=? AND revoked_at IS NULL",
                           (now, ip, token_id))
        return {"id": token_id, "device": device}

    # --- чтение ---------------------------------------------------------------------------
    def devices(self, now=None):
        now = int(time.time() if now is None else now)
        with closing(self._connect()) as cx:
            rows = cx.execute("SELECT d.device, d.last_seen, b.name FROM devices d"
                              " LEFT JOIN boards b ON b.device=d.device ORDER BY d.last_seen DESC").fetchall()
        return [{"device": d, "name": name or d, "last_seen": ts, "age": now - ts} for d, ts, name in rows]

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
            errors_1h = cx.execute("SELECT COUNT(*) FROM logs WHERE device=? AND level='error' AND ts>=?",
                                   (device, now - 3600)).fetchone()[0]
        last_seen, boot, sample_ts, last_sample = row
        return {"device": device, "now": now, "last_seen": last_seen, "age": now - last_seen,
                "online": now - last_seen <= online_sec, "online_sec": online_sec, "boot": boot,
                "sample_ts": sample_ts, "sample": json.loads(last_sample) if last_sample else None,
                "errors_24h": counts.get("error", 0), "warnings_24h": counts.get("warn", 0),
                "errors_1h": errors_1h}

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
        """-> запись токена ({"id", "device"}) или None, если ответ с ошибкой уже отправлен."""
        ip = self._client_ip()
        limiter = self.server.limiter
        if limiter.blocked(ip):
            self._error(429, "слишком много неудачных попыток", {"Retry-After": "600"})
            return None
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        record = self.server.store.authenticate(token, ip) if token else None
        if record:
            limiter.clear(ip)
            return record
        limiter.fail(ip)
        LOG.warning("приём: неверный или отозванный токен с %s", ip)
        self._error(401, "неверный токен")
        return None

    def do_HEAD(self):
        self._route()

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def _route(self):
        try:
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            if self.command in ("POST", "DELETE"):
                # Тело запроса читают только приём и создание токена, и то после проверки доступа. Во всех
                # остальных случаях оно остаётся в потоке, поэтому соединение закрывается после ответа.
                self.close_connection = True
            if path == "/healthz" and self.command in ("GET", "HEAD"):
                with closing(self.server.store._connect()) as cx:
                    cx.execute("SELECT 1")
                return self._send(200, b"ok", "text/plain; charset=utf-8")
            if path == "/api/ingest":
                if self.command != "POST":
                    return self._error(405, "нужен POST", {"Allow": "POST"})
                record = self._check_ingest_token()
                if record is None:
                    return None
                return self._ingest(record)
            if path in ("/api/boards", "/api/tokens") or path.startswith(("/api/boards/", "/api/tokens/")):
                if not self._check_viewer():
                    return None
                return self._boards_api(path)
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
        except Conflict as exc:
            return self._error(409, str(exc))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            return None
        except Exception:  # noqa: BLE001 - наружу уходит только общий ответ, подробности в журнал
            LOG.exception("ошибка обработки %s %s", self.command, self.path)
            return self._error(500, "внутренняя ошибка")

    def _ingest(self, record):
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            return self._error(411, "нужен Content-Length")
        if int(length) > MAX_BODY:
            return self._error(413, "запрос слишком большой")
        raw = self.rfile.read(int(length))
        self.close_connection = False  # тело прочитано целиком, соединение можно держать
        try:
            # Обрезанная прошивкой строка не должна навсегда блокировать приём: битые байты UTF-8
            # заменяются знаком, а не ломают весь запрос (строгое чтение дало бы 400 на каждый повтор).
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return self._error(400, "это не JSON")
        if not isinstance(body, dict):
            return self._error(400, "ожидается JSON-объект")
        device = body.get("device", "microgarden")
        if not isinstance(device, str) or not DEVICE_RE.match(device):
            return self._error(400, "device: 1-32 символа, латиница, цифры, _ . -")
        if record["device"] and device != record["device"]:
            # Токен выдан одной плате: с ним нельзя писать данные от имени другой
            return self._error(403, "этот токен выдан для платы %s" % record["device"])
        if not self.server.store.board_exists(device):
            return self._error(403, "плата %s не зарегистрирована: добавьте её на странице /boards" % device)
        log = body.get("log")
        if isinstance(log, dict) and isinstance(log.get("text"), str) and len(log["text"].encode("utf-8")) > MAX_LOG_BYTES:
            return self._error(413, "порция журнала слишком большая")
        return self._json(200, self.server.store.ingest(device, body))

    def _check_action(self):
        """Защита от чужих страниц (CSRF): браузер сам подставляет сохранённый вход, поэтому запрос,
        который меняет токены, должен прийти именно со страницы токенов. Заголовок X-Dash-Action чужая
        страница без разрешения сервера (CORS) отправить не может, а Sec-Fetch-Site отсекает кросс-сайтовые
        запросы там, где браузер его присылает."""
        site = self.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin", "none") or self.headers.get("X-Dash-Action") != "1":
            self._error(403, "запрос отклонён: он должен идти со страницы токенов")
            return False
        return True

    def _read_json(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValueError("нужен Content-Type: application/json")
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            raise ValueError("нужен Content-Length")
        if int(length) > MAX_ACTION_BODY:
            raise ValueError("запрос слишком большой")
        raw = self.rfile.read(int(length))
        self.close_connection = False  # тело прочитано целиком, соединение можно держать
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:  # и UnicodeDecodeError, он его потомок
            raise ValueError("это не JSON")
        if not isinstance(body, dict):
            raise ValueError("ожидается JSON-объект")
        return body

    def _boards_api(self, path):
        """Платы и их токены приёма: список, регистрация, замена токена, переименование, удаление, отзыв.
        Вход по паролю просмотра уже проверен."""
        parts = path.strip("/").split("/")   # api, boards[, имя платы[, tokens | rename]] или api, tokens, номер[, revoke]
        if parts[:2] == ["api", "boards"]:
            return self._boards_routes(parts[2:])
        if len(parts) in (3, 4) and re.fullmatch(r"[0-9]{1,9}", parts[2]):
            return self._token_routes(int(parts[2]), parts[3:])
        return self._error(404, "не найдено")

    def _action(self, method, allow):
        """Метод запроса подходит, и он пришёл со страницы (защита от чужих страниц). Иначе ответ уже отправлен."""
        if self.command != method:
            self._error(405, "нужен %s" % method, {"Allow": allow})
            return False
        return self._check_action()

    def _boards_routes(self, rest):
        store = self.server.store
        ip = self._client_ip()
        if not rest:
            if self.command in ("GET", "HEAD"):
                data = store.list_boards(self.server.cfg.online_sec)
                data["max_boards"] = MAX_BOARDS
                return self._json(200, data)
            if not self._action("POST", "GET, HEAD, POST"):
                return None
            body = self._read_json()
            token, board, item = store.add_board(body.get("name"), body.get("device"))
            LOG.info("плата зарегистрирована: %s (%r) с %s", board["device"], board["name"], ip)
            return self._json(201, {"ok": True, "token": token, "board": board, "item": item})
        device = rest[0]
        if not DEVICE_RE.fullmatch(device) or len(rest) > 2:
            return self._error(404, "не найдено")
        if len(rest) == 1:
            if not self._action("DELETE", "DELETE"):
                return None
            if not store.delete_board(device):
                return self._error(404, "такой платы нет")
            LOG.info("плата удалена: %s с %s", device, ip)
            return self._json(200, {"ok": True})
        if rest[1] == "tokens":
            if not self._action("POST", "POST"):
                return None
            body = self._read_json()
            made = store.add_board_token(device, body.get("name"))
            if made is None:
                return self._error(404, "такой платы нет")
            LOG.info("токен создан: плата %s, id=%d с %s", device, made[1]["id"], ip)
            return self._json(201, {"ok": True, "token": made[0], "item": made[1]})
        if rest[1] == "rename":
            if not self._action("POST", "POST"):
                return None
            board = store.rename_board(device, self._read_json().get("name"))
            if board is None:
                return self._error(404, "такой платы нет")
            return self._json(200, {"ok": True, "board": board})
        return self._error(404, "не найдено")

    def _token_routes(self, token_id, rest):
        store = self.server.store
        if rest == ["revoke"]:
            if not self._action("POST", "POST"):
                return None
            item = store.revoke_token(token_id)
            if item is None:
                return self._error(404, "такого токена нет")
            LOG.info("токен отозван: id=%d (%r) с %s", token_id, item["name"], self._client_ip())
            return self._json(200, {"ok": True, "item": item})
        if not rest:
            if not self._action("DELETE", "DELETE"):
                return None
            if not store.delete_token(token_id):
                return self._error(404, "такого токена нет")
            LOG.info("токен удалён: id=%d с %s", token_id, self._client_ip())
            return self._json(200, {"ok": True})
        return self._error(404, "не найдено")

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
        # Токен из .env попадает в список токенов, чтобы у платы, которая уже его использует, ничего не сломалось
        store.sync_env_token(cfg.ingest_token)

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
    if not store.active_token_count():
        LOG.warning("нет ни одного действующего токена приёма: плата не сможет отправлять данные. "
                    "Добавьте плату на странице /boards")
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
