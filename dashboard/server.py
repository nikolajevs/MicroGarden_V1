#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MicroGarden V1: приём телеметрии и страница с графиками (замена Ubidots).

Плата раз в минуту шлёт POST /api/ingest: последние показания и новые строки своего
журнала (того же, что идёт в Serial). Сервер кладёт их в SQLite и отдаёт страницу с
графиками и списком сообщений об ошибках. Только стандартная библиотека Python 3.9+.

Настройки читаются из переменных окружения DASH_* и из файла .env рядом со скриптом
(переменные окружения главнее). Без пароля администратора сервер не стартует. Люди регистрируются сами
(страница /register), входят по имени и паролю и добавляют свои платы на странице /boards: у каждой
платы свой токен приёма. Данные принимаются только от зарегистрированных плат; человек видит только
свои платы, администратор (DASH_VIEW_USER, пароль из .env) видит все. Токен из DASH_INGEST_TOKEN, если
он задан, подхватывается сам как общий токен администратора.
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
from urllib.parse import parse_qs, quote, urlsplit

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
MAX_BOARDS = 100               # всего на сервере
MAX_BOARDS_PER_USER = 3        # у одного человека (у администратора предела нет)
MAX_ACTIVE_TOKENS = 200        # всего; у одной платы их 1-2: новый создаётся, пока старый ещё действует
MAX_TOKEN_ROWS = 500           # с отозванными: их можно удалить на странице
TOKEN_TOUCH_SEC = 60           # «использовался» обновляется не чаще раза в минуту: меньше записей в базу
MAX_ACTION_BODY = 2048         # тело запроса со страниц панели
ENV_TOKEN_NAME = "Общий токен из .env"
FIRST_TOKEN_NAME = "Основной"
INGEST_MIN_GAP = 5             # плата шлёт раз в минуту; чаще раза в 5 секунд приём отклоняется (429)

# Люди. Регистрация открыта для всех, поэтому у неё есть пределы, а пароли хранятся только как scrypt.
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,32}")
RESERVED_USERNAMES = frozenset({"admin", "administrator", "root", "system", "support", "api", "null"})
PASSWORD_MIN, PASSWORD_MAX = 10, 128   # верхний предел: чтобы огромный «пароль» не грузил сервер
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SESSION_COOKIE = "dash_session"
SESSION_DAYS = 30
SESSION_TOUCH_SEC = 3600       # срок сессии продлевается не чаще раза в час
MAX_USERS = 200
REGISTRATIONS_PER_HOUR = 10    # попыток регистрации с одного адреса
ADMIN_PASSWORD_MARK = "env"    # у администратора пароль берётся из .env, а не из базы
_SCRYPT_GATE = threading.Semaphore(2)   # одновременно не больше двух хешей: каждый занимает около 16 МБ

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

# Настройки платы из панели. Плата в каждой отправке сообщает свои настройки (раздел cfg), а в ответе на отправку
# получает запрос на их изменение, если владелец разрешил это на самой плате. Список ниже — то, что можно менять
# издалека: прошивка ведёт тот же список (CloudSettings.h, CLOUD_PARAMS), тест сверяет имена и пределы с таблицей
# SETTING_DEFS в firmware.ino. Сеть, адрес и токен панели, поправка тока и датчик уровня воды сюда не входят:
# ошибка в них оставила бы плату без связи, с неверными замерами или без защиты насоса.
CFG_TTL = 600                  # запрос ждёт плату не дольше десяти минут: устаревшее изменение применять нельзя
CFG_HISTORY = 30               # сколько последних запросов по каждой плате хранится
CFG_ID_RE = re.compile(r"[0-9a-f]{1,16}")
CFG_RES_RE = re.compile(r"[A-Za-z0-9_:.-]{0,40}")
UTC_2024, UTC_2100 = 1704067200, 4102444800   # дата начала цикла: полночь между этими датами или 0
CFG_SPECS = {                  # настройка -> (вид, минимум, максимум); вид: int, float или date (полночь, unixtime)
    "operation_mode": ("int", 0, 1),
    "temp_target": ("float", 0, 40), "temp_target_night": ("float", 0, 40), "temp_delta": ("float", 0.1, 20),
    "hum_target_night": ("float", 0, 100), "hum_band_night": ("float", 0.1, 25),
    "drying_temp_target": ("float", 0, 40), "drying_temp_delta": ("float", 0.1, 20),
    "drying_hum_target": ("float", 0, 100), "drying_hum_band": ("float", 0.1, 25),
    "led_on_hour": ("int", 0, 23), "led_on_minute": ("int", 0, 59),
    "led_off_hour": ("int", 0, 23), "led_off_minute": ("int", 0, 59),
    "led_min_limit": ("int", 0, 100), "led_max_limit": ("int", 0, 100),
    "fan1_min_limit": ("int", 0, 100), "fan1_max_limit": ("int", 0, 100),
    "fan2_min_limit": ("int", 0, 100), "fan2_max_limit": ("int", 0, 100),
    "heater_mode": ("int", 0, 3),
    "rgb_brightness": ("int", 0, 100), "rgb_night_mode": ("int", 0, 2),
    "watering_days": ("int", 0, 127), "watering_hour": ("int", 0, 23), "watering_minute": ("int", 0, 59),
    "watering_duration": ("int", 1, 300),   # на самой плате до 3600 с; из панели не больше пяти минут
    "start_time": ("date", 0, UTC_2100),
}
CFG_LOCAL_HI = {"watering_duration": 3600}  # то, что плата может показывать, хотя из панели столько не поставить
CFG_READONLY = ("water_sensor_enabled",)    # плата сообщает, менять нельзя: отключение датчика снимает защиту насоса
CFG_PAIRS = (("led_min_limit", "led_max_limit", "Мощность лампы"), ("fan1_min_limit", "fan1_max_limit", "Вентилятор 1"),
             ("fan2_min_limit", "fan2_max_limit", "Вентилятор 2"))   # (минимум, максимум, подпись): минимум не больше максимума
# Как страница показывает настройки: группы и подписи (пределы берутся из CFG_SPECS). "modes" — при каком режиме
# платы (operation_mode) группа работает и показывается; без него группа видна всегда. Так же устроена страница
# самой платы (data-modes и data-mode-only в firmware/data/settings.html), тест сверяет оба списка.
GROWING, DRYING = 0, 1
CFG_GROUPS = [
    {"id": "mode", "title": "Режим работы", "fields": [
        {"type": "select", "param": "operation_mode", "label": "Режим", "options": [[GROWING, "Выращивание"], [DRYING, "Сушка"]],
         "hint": "В режиме сушки свет и полив выключены, поэтому их настройки не показываются."}]},
    {"id": "climate", "title": "Климат при выращивании", "modes": [GROWING], "fields": [
        {"type": "number", "param": "temp_target", "label": "Дневная целевая температура", "unit": "°C", "step": 0.1},
        {"type": "number", "param": "temp_target_night", "label": "Ночная целевая температура", "unit": "°C", "step": 0.1},
        {"type": "number", "param": "temp_delta", "label": "Гистерезис (дельта ±)", "unit": "°C", "step": 0.1},
        {"type": "number", "param": "hum_target_night", "label": "Ночью: влажность, выше которой вентиляторы разгоняются",
         "unit": "% RH", "step": 1},
        {"type": "number", "param": "hum_band_night", "label": "Ночью: диапазон разгона вентиляторов", "unit": "% RH", "step": 0.1}]},
    {"id": "drying", "title": "Климат сушки", "modes": [DRYING], "fields": [
        {"type": "number", "param": "drying_temp_target", "label": "Целевая температура", "unit": "°C", "step": 0.1},
        {"type": "number", "param": "drying_temp_delta", "label": "Гистерезис нагрева", "unit": "°C", "step": 0.1},
        {"type": "number", "param": "drying_hum_target", "label": "Целевая влажность", "unit": "% RH", "step": 1},
        {"type": "number", "param": "drying_hum_band", "label": "Диапазон разгона вентиляторов", "unit": "% RH", "step": 0.1}]},
    {"id": "light", "title": "Освещение", "modes": [GROWING], "fields": [
        {"type": "time", "params": ["led_on_hour", "led_on_minute"], "label": "Свет включается"},
        {"type": "time", "params": ["led_off_hour", "led_off_minute"], "label": "Свет выключается"},
        {"type": "number", "param": "led_min_limit", "label": "Минимальная мощность лампы", "unit": "%", "step": 1},
        {"type": "number", "param": "led_max_limit", "label": "Максимальная мощность лампы", "unit": "%", "step": 1}]},
    {"id": "fans", "title": "Вентиляция", "fields": [
        {"type": "number", "param": "fan1_min_limit", "label": "Вентилятор 1: минимум", "unit": "%", "step": 1},
        {"type": "number", "param": "fan1_max_limit", "label": "Вентилятор 1: максимум", "unit": "%", "step": 1},
        {"type": "number", "param": "fan2_min_limit", "label": "Вентилятор 2: минимум", "unit": "%", "step": 1},
        {"type": "number", "param": "fan2_max_limit", "label": "Вентилятор 2: максимум", "unit": "%", "step": 1}]},
    {"id": "heater", "title": "Обогрев", "fields": [
        {"type": "select", "param": "heater_mode", "label": "Режим обогревателя",
         "options": [[0, "Только днём"], [1, "Только ночью"], [2, "Всегда"], [3, "Никогда"]]}]},
    {"id": "watering", "title": "Автополив", "modes": [GROWING], "fields": [
        {"type": "days", "param": "watering_days", "label": "Дни полива"},
        {"type": "time", "params": ["watering_hour", "watering_minute"], "label": "Время полива"},
        {"type": "number", "param": "watering_duration", "label": "Длительность полива", "unit": "с", "step": 1,
         "hint": "Из панели — не больше 300 с: на самой плате можно до 3600 с. Насос не должен качать часами из-за опечатки."},
        {"type": "flag", "param": "water_sensor_enabled", "label": "Датчик уровня воды",
         "hint": "Включается и выключается только на самой плате: без него насос не знает, что бак пуст."}]},
    {"id": "indicator", "title": "RGB-индикатор", "fields": [
        {"type": "number", "param": "rgb_brightness", "label": "Яркость", "unit": "%", "step": 1},
        {"type": "select", "param": "rgb_night_mode", "label": "Ночью",
         "options": [[0, "Как днём"], [1, "Только неисправности"], [2, "Выключен"]]}]},
    {"id": "cycle", "title": "Цикл выращивания", "modes": [GROWING], "fields": [
        {"type": "date", "param": "start_time", "label": "Дата начала", "hint": "Пустая дата — цикл не начат."}]},
]


def _cfg_schema():
    """Группы с пределами для страницы настроек и подписи настроек для сообщений об ошибках."""
    labels, groups = {}, []
    for group in CFG_GROUPS:
        fields = []
        for field in group["fields"]:
            item = dict(field)
            params = item.get("params") or [item["param"]]
            for param in params:
                labels[param] = item["label"]
            if "param" in item and item["param"] in CFG_SPECS:
                kind, lo, hi = CFG_SPECS[item["param"]]
                item.update({"kind": kind, "lo": lo, "hi": hi})
            fields.append(item)
        groups.append(dict({"id": group["id"], "title": group["title"], "fields": fields},
                           **({"modes": group["modes"]} if "modes" in group else {})))
    return groups, labels


CFG_SCHEMA, CFG_LABELS = _cfg_schema()


def _num_text(value):
    return ("%g" % value) if isinstance(value, float) else str(value)


def clean_cfg_value(param, value, reported=False):
    """Значение настройки: число в пределах таблицы (reported: то, что плата сообщает о себе, пределы местные)."""
    spec = CFG_SPECS.get(param)
    if spec is None:
        raise ValueError("%s: такую настройку нельзя менять из панели" % param)
    kind, lo, hi = spec
    label = CFG_LABELS.get(param, param)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("«%s»: нужно число" % label)
    if kind == "float":
        value = round(float(value), 2)
    else:
        if value != int(value):
            raise ValueError("«%s»: нужно целое число" % label)
        value = int(value)
    if reported:
        hi = CFG_LOCAL_HI.get(param, hi)
    if kind == "date" and not reported:
        if value != 0 and (value % 86400 or not UTC_2024 <= value <= UTC_2100):
            raise ValueError("«%s»: нужна дата с 2024 года" % label)
    elif not lo <= value <= hi:
        raise ValueError("«%s»: от %s до %s" % (label, _num_text(lo), _num_text(hi)))
    return value


def clean_cfg_set(raw):
    """Запрос на изменение: {настройка: значение}. Любое неподходящее значение отклоняет весь запрос."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("set: нужен объект {настройка: значение}")
    if len(raw) > len(CFG_SPECS):
        raise ValueError("set: слишком много настроек")
    return {param: clean_cfg_value(param, value) for param, value in raw.items()}


def check_cfg_order(values):
    """Минимум не больше максимума. Плата отклонила бы запрос с перевёрнутыми границами, поэтому не отправляем его."""
    for low, high, title in CFG_PAIRS:
        if low in values and high in values and values[low] > values[high]:
            raise ValueError("%s: минимум не может быть больше максимума" % title)


def clean_cfg_report(raw):
    """Раздел cfg телеметрии -> (разрешено ли менять из панели, номер обработанного запроса, итог, настройки)."""
    last_id = raw.get("id")
    last_id = last_id if isinstance(last_id, str) and CFG_ID_RE.fullmatch(last_id) else ""
    res = raw.get("res")
    res = res if isinstance(res, str) and CFG_RES_RE.fullmatch(res) else ""
    values = {}
    shown = raw.get("v")
    if isinstance(shown, dict):
        for param, value in shown.items():
            if param in CFG_READONLY:
                if isinstance(value, bool):
                    values[param] = value
                continue
            try:
                values[param] = clean_cfg_value(param, value, reported=True)
            except ValueError:
                pass  # значение вне пределов не показываем: оно только введёт в заблуждение
    return raw.get("cloud") is True, last_id, res, values


RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}
BUCKETS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400)
MAX_SPAN = 400 * 86400

HTML, JS, CSS = "text/html; charset=utf-8", "text/javascript; charset=utf-8", "text/css; charset=utf-8"
# Страницы, которые видит только вошедший (без входа ведут на /login); /admin только администратору.
PAGES = {
    "/": "index.html", "/index.html": "index.html",
    "/boards": "boards.html", "/boards.html": "boards.html",
    "/account": "account.html", "/account.html": "account.html",
    "/settings": "settings.html", "/settings.html": "settings.html",
}
ADMIN_PAGES = {"/admin": "admin.html", "/admin.html": "admin.html"}
# Страницы входа и регистрации открыты всем
PUBLIC_PAGES = {"/login": "login.html", "/login.html": "login.html",
                "/register": "register.html", "/register.html": "register.html"}
# Скрипты и стили открыты: в них нет секретов, а странице входа они нужны до входа
ASSETS = {
    "/style.css": ("style.css", CSS),
    "/app.js": ("app.js", JS), "/alerts.js": ("alerts.js", JS),
    "/boards-lib.js": ("boards-lib.js", JS), "/boards.js": ("boards.js", JS),
    "/auth-lib.js": ("auth-lib.js", JS), "/login.js": ("login.js", JS), "/register.js": ("register.js", JS),
    "/account.js": ("account.js", JS), "/admin.js": ("admin.js", JS),
    "/settings-lib.js": ("settings-lib.js", JS), "/settings.js": ("settings.js", JS),
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
    r"sd-карта не обнаружена|пропущен|из будущего|sda удерживается|нет связи|с опозданием|nvs write|отклонён",
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
        # Люди: регистрация открыта, пока администратор не закроет её на странице /admin (или здесь, до первого запуска)
        self.registration = env.get("DASH_REGISTRATION", "open").lower()
        self.max_users = int(env.get("DASH_MAX_USERS", str(MAX_USERS)))
        self.max_boards = int(env.get("DASH_MAX_BOARDS", str(MAX_BOARDS)))
        self.max_boards_per_user = int(env.get("DASH_MAX_BOARDS_PER_USER", str(MAX_BOARDS_PER_USER)))
        self.session_days = int(env.get("DASH_SESSION_DAYS", str(SESSION_DAYS)))
        self.cookie_secure = env.get("DASH_COOKIE_SECURE", "") == "1"  # иначе Secure ставится по заголовку X-Forwarded-Proto

    def validate(self):
        problems = []
        # Токен из .env необязателен: токены приёма создаются на странице /boards.
        if self.ingest_token and len(self.ingest_token) < 24:
            problems.append("DASH_INGEST_TOKEN: нужен токен не короче 24 символов (или уберите его из .env)")
        if len(self.view_password) < 12:
            problems.append("DASH_VIEW_PASSWORD: нужен пароль администратора не короче 12 символов")
        if not USERNAME_RE.fullmatch(self.view_user):
            problems.append("DASH_VIEW_USER: имя администратора от 3 до 32 символов, латиница, цифры, _ . -")
        if self.registration not in ("open", "closed"):
            problems.append("DASH_REGISTRATION: open или closed")
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
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- Ivan и ivan — один человек
  pass_hash TEXT NOT NULL,            -- scrypt$N$r$p$соль$хеш; у администратора 'env': его пароль в .env
  role TEXT NOT NULL DEFAULT 'user',  -- 'admin' (владелец сервера) или 'user'
  created_at INTEGER NOT NULL, last_login_at INTEGER, reg_ip TEXT,
  disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,        -- SHA-256 значения из cookie; самого значения в базе нет
  user_id INTEGER NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, last_seen INTEGER NOT NULL, ip TEXT);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
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
CREATE TABLE IF NOT EXISTS device_cfg(
  device TEXT PRIMARY KEY,
  reported TEXT NOT NULL DEFAULT '{}', -- настройки платы так, как она сама их сообщила
  reported_at INTEGER NOT NULL,
  cloud INTEGER NOT NULL DEFAULT 0,    -- разрешила ли плата менять настройки из панели
  last_id TEXT NOT NULL DEFAULT '',    -- последний запрос панели, который плата обработала...
  last_res TEXT NOT NULL DEFAULT '');  -- ...и итог: ok или причина отказа
CREATE TABLE IF NOT EXISTS cfg_changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT NOT NULL, user_id INTEGER, username TEXT,   -- кто просил; имя хранится на случай удаления человека
  change_id TEXT NOT NULL,             -- номер запроса: плата возвращает его как подтверждение
  patch TEXT NOT NULL,                 -- JSON {настройка: значение}
  status TEXT NOT NULL,                -- pending | applied | rejected | expired | cancelled
  created_at INTEGER NOT NULL, done_at INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS cfg_changes_device ON cfg_changes(device, id);
"""
TOKEN_COLUMNS = "id, name, device, prefix, created_at, last_used_at, last_ip, revoked_at, source"
SAMPLE_COLS = ("temp", "hum", "led", "fan1", "fan2", "power_w", "bus_v", "current_a", "energy_kwh",
               "pump", "heater", "led_blocked", "sht_ok", "rtc_ok")


class Conflict(Exception):
    """Запрос верный, но состояние не позволяет его выполнить (HTTP 409)."""


class WrongPassword(ValueError):
    """Неверный текущий пароль при его смене: такие попытки считаются как неудачные входы."""


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


def hash_password(password):
    """scrypt с случайной солью: 'scrypt$N$r$p$соль$хеш'. Одновременно считаются не больше двух хешей."""
    salt = secrets.token_bytes(16)
    with _SCRYPT_GATE:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, salt.hex(), digest.hex())


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        want = bytes.fromhex(digest_hex)
        with _SCRYPT_GATE:
            got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                 n=int(n), r=int(r), p=int(p), dklen=len(want))
    except (ValueError, TypeError):  # повреждённая запись в базе: вход по ней закрыт
        return False
    return hmac.compare_digest(got, want)


_DUMMY_HASH = []


def dummy_hash():
    """Хеш для сравнения, когда такого человека нет: ответ приходит за то же время, что и при неверном
    пароле, и по скорости ответа нельзя узнать, занято ли имя."""
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password("нет такого человека"))
    return _DUMMY_HASH[0]


def clean_username(value):
    if not isinstance(value, str) or not USERNAME_RE.fullmatch(value):
        raise ValueError("Имя: от 3 до 32 символов, латиница, цифры, _ . -")
    if value.lower() in RESERVED_USERNAMES:
        raise ValueError("Это имя зарезервировано: выберите другое")
    return value


def clean_password(value, username=""):
    if not isinstance(value, str) or not PASSWORD_MIN <= len(value) <= PASSWORD_MAX:
        raise ValueError("Пароль: от %d до %d символов" % (PASSWORD_MIN, PASSWORD_MAX))
    if not value.strip():
        raise ValueError("Пароль не может состоять из одних пробелов")
    if username and value.lower() == username.lower():
        raise ValueError("Пароль не должен совпадать с именем")
    return value


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
        # Пределы; DashServer заменяет их значениями из .env. У тестов — значения по умолчанию.
        self.max_boards, self.max_boards_per_user, self.max_users = MAX_BOARDS, MAX_BOARDS_PER_USER, MAX_USERS
        self.session_days = SESSION_DAYS
        self.registration_default = True
        self.admin_id = None          # человек-администратор (ensure_admin); платы без владельца принадлежат ему
        self.admin_user = None
        self._admin_password = ""
        with closing(self._connect()) as cx:
            cx.execute("PRAGMA journal_mode=WAL")
            cx.executescript(SCHEMA)
        self._migrate_boards()
        self._migrate_accounts()

    def _migrate_boards(self):
        """При первом запуске с реестром плат уже присылавшие данные платы вносятся в него сами, чтобы
        они продолжили работать. Один раз: удалённая плата после перезапуска не возвращается."""
        with self._write_lock, closing(self._connect()) as cx, cx:
            if cx.execute("SELECT 1 FROM meta WHERE key='boards_migrated'").fetchone():
                return
            cx.execute("INSERT OR IGNORE INTO boards(device, name, created_at)"
                       " SELECT device, device, first_seen FROM devices")
            cx.execute("INSERT INTO meta(key, value) VALUES('boards_migrated', '1')")

    def _migrate_accounts(self):
        """У платы появляется владелец. Столбец добавляется к уже существующей таблице; платы без владельца
        достаются администратору при запуске (ensure_admin)."""
        with self._write_lock, closing(self._connect()) as cx, cx:
            if "owner_id" not in [row[1] for row in cx.execute("PRAGMA table_info(boards)")]:
                cx.execute("ALTER TABLE boards ADD COLUMN owner_id INTEGER")

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
        reply_cfg = None
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
            if isinstance(body.get("cfg"), dict):
                reply_cfg = self._cfg_ingest(cx, device, now, body["cfg"])
        result = {"ok": True, "log_next": log_next, "sample": cols is not None, "log_lines": lines}
        if reply_cfg:
            result["cfg"] = reply_cfg  # запрос на изменение настроек, который плата применит, если ей это разрешено
        return result

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

    # --- настройки платы из панели ----------------------------------------------------------
    @staticmethod
    def _cfg_expire(cx, device, now):
        """Запрос, который плата не взяла за CFG_TTL секунд, больше не действует: устаревшее изменение не должно
        сработать, когда плата вернётся на связь."""
        cx.execute("UPDATE cfg_changes SET status='expired', done_at=? WHERE device=? AND status='pending' AND created_at<=?",
                   (now, device, now - CFG_TTL))

    def _cfg_ingest(self, cx, device, now, raw):
        """Раздел cfg отправки платы: запоминает её настройки, отмечает обработанный запрос и отдаёт очередной,
        если он есть и плата принимает запросы из панели. -> {"id", "set"} или None."""
        cloud, last_id, res, values = clean_cfg_report(raw)
        previous = cx.execute("SELECT reported FROM device_cfg WHERE device=?", (device,)).fetchone()
        if not values and previous:
            values = json.loads(previous[0])   # все присланные значения негодны: прежние лучше пустых
        cx.execute("INSERT INTO device_cfg(device, reported, reported_at, cloud, last_id, last_res) VALUES (?, ?, ?, ?, ?, ?)"
                   " ON CONFLICT(device) DO UPDATE SET reported=excluded.reported, reported_at=excluded.reported_at,"
                   " cloud=excluded.cloud, last_id=excluded.last_id, last_res=excluded.last_res",
                   (device, json.dumps(values, separators=(",", ":")), now, int(cloud), last_id, res))
        self._cfg_expire(cx, device, now)
        if last_id:
            # Плата обработала запрос. Подтверждение принимается и для отменённого, и для просроченного: если плата его
            # всё же применила, история должна это показать.
            hit = cx.execute("SELECT id FROM cfg_changes WHERE device=? AND change_id=? AND"
                             " status IN ('pending', 'cancelled', 'expired') ORDER BY id DESC LIMIT 1",
                             (device, last_id)).fetchone()
            if hit:
                cx.execute("UPDATE cfg_changes SET status=?, done_at=?, note=? WHERE id=?",
                           ("applied" if res == "ok" else "rejected", now, "" if res == "ok" else res, hit[0]))
        pending = cx.execute("SELECT id, change_id, patch FROM cfg_changes WHERE device=? AND status='pending'"
                             " ORDER BY id DESC LIMIT 1", (device,)).fetchone()
        if pending is None:
            return None
        if not cloud:  # плата не принимает запросы из панели: ждать нечего
            cx.execute("UPDATE cfg_changes SET status='rejected', done_at=?, note='off' WHERE id=?", (now, pending[0]))
            return None
        return {"id": pending[1], "set": json.loads(pending[2])}

    def cfg_state(self, device, now=None):
        """Что плата сообщила о своих настройках и что с запросами на их изменение (новые сверху)."""
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:   # просроченные запросы закрываются и при чтении
            self._cfg_expire(cx, device, now)
            row = cx.execute("SELECT reported, reported_at, cloud FROM device_cfg WHERE device=?", (device,)).fetchone()
            rows = cx.execute("SELECT change_id, username, patch, status, created_at, done_at, note FROM cfg_changes"
                              " WHERE device=? ORDER BY id DESC LIMIT ?", (device, CFG_HISTORY)).fetchall()
        history = [{"id": cid, "by": who, "set": json.loads(patch), "status": status, "created_at": created,
                    "done_at": done, "note": note or ""} for cid, who, patch, status, created, done, note in rows]
        pending = next((dict(item, expires_at=item["created_at"] + CFG_TTL) for item in history
                        if item["status"] == "pending"), None)
        return {"supported": row is not None, "cloud": bool(row[2]) if row else False,
                "reported_at": row[1] if row else None, "values": json.loads(row[0]) if row else {},
                "pending": pending, "history": history}

    def cfg_submit(self, device, raw_set, who, now=None):
        """Ставит в очередь изменение настроек платы; плата заберёт его с очередной отправкой. Запрос, который ещё
        ждёт плату, дополняется. Что уже так стоит на плате, не отправляется. -> состояние, как у cfg_state."""
        now = int(time.time() if now is None else now)
        wanted = clean_cfg_set(raw_set)
        with self._write_lock, closing(self._connect()) as cx, cx:
            self._cfg_expire(cx, device, now)
            row = cx.execute("SELECT reported, cloud FROM device_cfg WHERE device=?", (device,)).fetchone()
            if row is None:
                raise Conflict("плата ещё не сообщала свои настройки: нужна прошивка, которая умеет менять их из панели")
            if not row[1]:
                raise Conflict("менять настройки из панели не разрешено на самой плате: включите это в её настройках "
                               "(вкладка «WiFi», раздел «Телеметрия»)")
            reported = json.loads(row[0])
            pending = cx.execute("SELECT id, patch FROM cfg_changes WHERE device=? AND status='pending'"
                                 " ORDER BY id DESC LIMIT 1", (device,)).fetchone()
            merged = json.loads(pending[1]) if pending else {}
            merged.update(wanted)
            merged = {name: value for name, value in merged.items() if reported.get(name) != value}
            if not merged:
                if pending is None:
                    raise ValueError("нет изменений: эти значения уже стоят на плате")
                # Запрос вернул всё как было: отправлять нечего, ждущий запрос больше не нужен
                cx.execute("UPDATE cfg_changes SET status='cancelled', done_at=? WHERE id=?", (now, pending[0]))
            else:
                check_cfg_order({**reported, **merged})
                patch = json.dumps(merged, separators=(",", ":"), sort_keys=True)
                change_id = secrets.token_hex(4)
                if pending:
                    cx.execute("UPDATE cfg_changes SET change_id=?, patch=?, created_at=?, user_id=?, username=? WHERE id=?",
                               (change_id, patch, now, who["id"], who["username"], pending[0]))
                else:
                    cx.execute("INSERT INTO cfg_changes(device, user_id, username, change_id, patch, status, created_at)"
                               " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                               (device, who["id"], who["username"], change_id, patch, now))
                cx.execute("DELETE FROM cfg_changes WHERE device=? AND id NOT IN"
                           " (SELECT id FROM cfg_changes WHERE device=? ORDER BY id DESC LIMIT ?)",
                           (device, device, CFG_HISTORY))
        return self.cfg_state(device, now)

    def cfg_cancel(self, device, now=None):
        """Снимает запрос, который ещё ждёт плату (если она его уже забрала, отмена ничего не изменит)."""
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("UPDATE cfg_changes SET status='cancelled', done_at=? WHERE device=? AND status='pending'",
                       (now, device))
        return self.cfg_state(device, now)

    def board_name(self, device):
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT name FROM boards WHERE device=?", (device,)).fetchone()
        return row[0] if row else None

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

    def add_board(self, name, device, owner_id=None, now=None, limit_owner=False):
        """Регистрирует плату владельцу и выдаёт ей первый токен, привязанный к её имени (limit_owner: у
        обычного человека плат не больше max_boards_per_user). -> (токен открытым текстом, плата, запись токена)."""
        name, device = clean_name(name), clean_device(device)
        owner_id = self.admin_id if owner_id is None else owner_id
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:
            if cx.execute("SELECT COUNT(*) FROM boards").fetchone()[0] >= self.max_boards:
                raise Conflict("на сервере уже %d плат: места нет" % self.max_boards)
            if limit_owner and cx.execute("SELECT COUNT(*) FROM boards WHERE owner_id=?",
                                          (owner_id,)).fetchone()[0] >= self.max_boards_per_user:
                raise Conflict("у вас уже %d плат (предел): удалите ненужную" % self.max_boards_per_user)
            if cx.execute("SELECT 1 FROM boards WHERE device=?", (device,)).fetchone():
                raise Conflict("плата с именем %s уже зарегистрирована" % device)
            cx.execute("INSERT INTO boards(device, name, created_at, owner_id) VALUES (?, ?, ?, ?)",
                       (device, name, now, owner_id))
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
            self._delete_board_rows(cx, device)
        return True

    @staticmethod
    def _delete_board_rows(cx, device):
        for table in ("samples", "logs", "devices", "tokens", "device_cfg", "cfg_changes", "boards"):
            cx.execute("DELETE FROM %s WHERE device=?" % table, (device,))

    def list_boards(self, online_sec, principal=None, now=None):
        """Платы с токенами и состоянием связи. Человек видит свои платы; администратор (и principal=None)
        все, с именем владельца, и общие токены из .env, которые подходят любой плате."""
        now = int(time.time() if now is None else now)
        sees_all = principal is None or principal["role"] == "admin"
        sql = ("SELECT b.device, b.name, b.created_at, d.last_seen, d.last_sample, u.username FROM boards b"
               " LEFT JOIN devices d ON d.device=b.device LEFT JOIN users u ON u.id=b.owner_id")
        with closing(self._connect()) as cx:
            boards = cx.execute(sql + ("" if sees_all else " WHERE b.owner_id=?") + " ORDER BY b.created_at, b.device",
                                () if sees_all else (principal["id"],)).fetchall()
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
        for device, name, created, last_seen, sample, owner in boards:
            fw = json.loads(sample).get("fw") if sample else None
            out.append({"device": device, "name": name, "created_at": created, "last_seen": last_seen,
                        "online": last_seen is not None and now - last_seen <= online_sec,
                        "fw": fw if isinstance(fw, str) else None, "tokens": own.get(device, []),
                        "owner": owner if sees_all else None})
        return {"boards": out, "shared_tokens": shared if sees_all else []}

    def can_manage(self, principal, device):
        """Плата есть и принадлежит этому человеку (администратору подходит любая)."""
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT owner_id FROM boards WHERE device=?", (device,)).fetchone()
        return row is not None and (principal["role"] == "admin" or row[0] == principal["id"])

    def can_manage_token(self, principal, token_id):
        """-> None, если токена нет; иначе можно ли этому человеку им управлять. Общие токены (без платы) только администратору."""
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT t.device, b.owner_id FROM tokens t LEFT JOIN boards b ON b.device=t.device"
                             " WHERE t.id=?", (token_id,)).fetchone()
        if row is None:
            return None
        return principal["role"] == "admin" or (row[0] is not None and row[1] == principal["id"])

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
            # Платы заблокированного человека данные присылать перестают (общий токен без платы это не касается)
            row = cx.execute("SELECT t.id, t.device, t.last_used_at, t.revoked_at, COALESCE(u.disabled, 0) FROM tokens t"
                             " LEFT JOIN boards b ON b.device=t.device LEFT JOIN users u ON u.id=b.owner_id"
                             " WHERE t.hash=?", (self.token_hash(token),)).fetchone()
        if row is None or row[3] is not None or row[4]:
            return None
        token_id, device, used = row[0], row[1], row[2]
        if used is None or now - used >= TOKEN_TOUCH_SEC:
            with self._write_lock, closing(self._connect()) as cx, cx:
                cx.execute("UPDATE tokens SET last_used_at=?, last_ip=? WHERE id=? AND revoked_at IS NULL",
                           (now, ip, token_id))
        return {"id": token_id, "device": device}

    # --- люди и сессии --------------------------------------------------------------------
    def configure(self, cfg):
        """Пределы и начальное состояние регистрации из настроек сервера."""
        self.max_boards, self.max_boards_per_user, self.max_users = cfg.max_boards, cfg.max_boards_per_user, cfg.max_users
        self.session_days = cfg.session_days
        self.registration_default = cfg.registration == "open"

    def ensure_admin(self, username, password, now=None):
        """Администратор — владелец сервера: имя и пароль берутся из .env. Запись в базе нужна, чтобы у плат был
        владелец и чтобы он входил той же формой, что и остальные. Платы без владельца (появились до учётных
        записей) достаются ему. Прежние администраторы (имя в .env меняли) блокируются: их пароль нигде не проверяется."""
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:
            row = cx.execute("SELECT id, role, username FROM users WHERE username=?", (username,)).fetchone()
            if row is None:
                admin_id = cx.execute("INSERT INTO users(username, pass_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                                      (username, ADMIN_PASSWORD_MARK, now)).lastrowid
                name = username
            elif row[1] != "admin":
                raise SystemExit("Сервер не запущен: имя %s из DASH_VIEW_USER уже занято обычным человеком, выберите другое" % username)
            else:
                admin_id, name = row[0], row[2]
                cx.execute("UPDATE users SET pass_hash=?, disabled=0 WHERE id=?", (ADMIN_PASSWORD_MARK, admin_id))
            cx.execute("UPDATE users SET role='user', disabled=1 WHERE role='admin' AND id != ?", (admin_id,))
            cx.execute("UPDATE boards SET owner_id=? WHERE owner_id IS NULL", (admin_id,))
        self.admin_id, self.admin_user, self._admin_password = admin_id, name, password
        return admin_id

    def admin_principal(self):
        return {"id": self.admin_id, "username": self.admin_user, "role": "admin"}

    def check_admin_password(self, password):
        return bool(self._admin_password) and hmac.compare_digest(password.encode("utf-8"), self._admin_password.encode("utf-8"))

    def registration_open(self):
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT value FROM meta WHERE key='registration'").fetchone()
        return self.registration_default if row is None else row[0] == "open"

    def set_registration(self, is_open):
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('registration', ?)", ("open" if is_open else "closed",))

    def register_user(self, username, password, ip=None, now=None):
        """Новый человек. -> {"id", "username", "role"}. Имена различаются без учёта регистра."""
        username = clean_username(username)
        password = clean_password(password, username)
        now = int(time.time() if now is None else now)
        pass_hash = hash_password(password)  # долго, поэтому до блокировки базы
        with self._write_lock, closing(self._connect()) as cx, cx:
            if cx.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= self.max_users:
                raise Conflict("регистрация закрыта: на сервере уже %d человек" % self.max_users)
            try:
                user_id = cx.execute("INSERT INTO users(username, pass_hash, role, created_at, reg_ip) VALUES (?, ?, 'user', ?, ?)",
                                     (username, pass_hash, now, ip)).lastrowid
            except sqlite3.IntegrityError:
                raise Conflict("имя %s уже занято" % username) from None
        return {"id": user_id, "username": username, "role": "user"}

    def login_user(self, username, password, now=None):
        """-> {"id", "username", "role"} или None (нет такого, неверный пароль или заблокирован). Время ответа
        не зависит от того, есть ли такое имя."""
        now = int(time.time() if now is None else now)
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT id, username, pass_hash, role, disabled FROM users WHERE username=?",
                             (username,)).fetchone()
        if row is None:
            verify_password(password, dummy_hash())
            return None
        user_id, name, pass_hash, role, disabled = row
        ok = self.check_admin_password(password) if pass_hash == ADMIN_PASSWORD_MARK else verify_password(password, pass_hash)
        if not ok or disabled:
            return None
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, user_id))
        return {"id": user_id, "username": name, "role": role}

    @staticmethod
    def _session_hash(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self, user_id, ip=None, now=None):
        """Новая сессия. -> значение для cookie (в базе только его отпечаток)."""
        now = int(time.time() if now is None else now)
        token = secrets.token_urlsafe(32)
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen, ip) VALUES (?, ?, ?, ?, ?, ?)",
                       (self._session_hash(token), user_id, now, now + self.session_days * 86400, now, ip))
        return token

    def session_user(self, token, now=None):
        """-> {"id", "username", "role"} по значению cookie или None (нет, истекла, человек заблокирован).
        Срок сессии продлевается не чаще раза в час, пока человек пользуется панелью."""
        now = int(time.time() if now is None else now)
        digest = self._session_hash(token)
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT s.user_id, s.expires_at, s.last_seen, u.username, u.role, u.disabled FROM sessions s"
                             " JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (digest,)).fetchone()
        if row is None or row[1] <= now or row[5]:
            return None
        if now - row[2] >= SESSION_TOUCH_SEC:
            with self._write_lock, closing(self._connect()) as cx, cx:
                cx.execute("UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?",
                           (now, now + self.session_days * 86400, digest))
        return {"id": row[0], "username": row[3], "role": row[4]}

    def end_session(self, token):
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("DELETE FROM sessions WHERE token_hash=?", (self._session_hash(token),))

    def purge_sessions(self, now=None):
        now = int(time.time() if now is None else now)
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))

    def change_password(self, user_id, old_password, new_password, keep_token=None):
        """Смена своего пароля: остальные сессии этого человека закрываются, текущая остаётся.
        Пароль администратора задаётся в .env и здесь не меняется."""
        with closing(self._connect()) as cx:
            row = cx.execute("SELECT username, pass_hash FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError("человек не найден")
        if row[1] == ADMIN_PASSWORD_MARK:
            raise Conflict("пароль администратора задаётся в файле .env на сервере")
        if not verify_password(old_password if isinstance(old_password, str) else "", row[1]):
            raise WrongPassword("текущий пароль неверен")
        new_hash = hash_password(clean_password(new_password, row[0]))
        keep = self._session_hash(keep_token) if keep_token else ""
        with self._write_lock, closing(self._connect()) as cx, cx:
            cx.execute("UPDATE users SET pass_hash=? WHERE id=?", (new_hash, user_id))
            cx.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (user_id, keep))

    # --- администратор: люди ---------------------------------------------------------------
    @staticmethod
    def _user_item(row):
        user_id, name, role, created, last_login, disabled, reg_ip, boards = row
        return {"id": user_id, "username": name, "role": role, "created_at": created, "last_login_at": last_login,
                "disabled": bool(disabled), "reg_ip": reg_ip, "boards": boards}

    def list_users(self):
        with closing(self._connect()) as cx:
            rows = cx.execute("SELECT u.id, u.username, u.role, u.created_at, u.last_login_at, u.disabled, u.reg_ip,"
                              " (SELECT COUNT(*) FROM boards b WHERE b.owner_id=u.id) FROM users u ORDER BY u.id").fetchall()
        return [self._user_item(row) for row in rows]

    def _user_row(self, cx, user_id):
        return cx.execute("SELECT u.id, u.username, u.role, u.created_at, u.last_login_at, u.disabled, u.reg_ip,"
                          " (SELECT COUNT(*) FROM boards b WHERE b.owner_id=u.id) FROM users u WHERE u.id=?", (user_id,)).fetchone()

    def set_user_disabled(self, user_id, disabled):
        """Блокировка: входить нельзя, сессии закрыты, платы перестают принимать данные. -> запись или None."""
        with self._write_lock, closing(self._connect()) as cx, cx:
            row = self._user_row(cx, user_id)
            if row is None:
                return None
            if row[2] == "admin":
                raise Conflict("администратора заблокировать нельзя")
            cx.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))
            if disabled:
                cx.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return self._user_item(self._user_row(cx, user_id))

    def reset_password(self, user_id):
        """Новый временный пароль человеку, который забыл свой (почты в системе нет). -> пароль открытым текстом или None."""
        temp = secrets.token_urlsafe(12)
        pass_hash = hash_password(temp)
        with self._write_lock, closing(self._connect()) as cx, cx:
            row = self._user_row(cx, user_id)
            if row is None:
                return None
            if row[2] == "admin":
                raise Conflict("пароль администратора задаётся в файле .env на сервере")
            cx.execute("UPDATE users SET pass_hash=? WHERE id=?", (pass_hash, user_id))
            cx.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return temp

    def delete_user(self, user_id):
        """Удаляет человека вместе с его платами, показаниями, журналом и сессиями. -> False, если такого нет."""
        with self._write_lock, closing(self._connect()) as cx, cx:
            row = self._user_row(cx, user_id)
            if row is None:
                return False
            if row[2] == "admin":
                raise Conflict("администратора удалить нельзя")
            for (device,) in cx.execute("SELECT device FROM boards WHERE owner_id=?", (user_id,)).fetchall():
                self._delete_board_rows(cx, device)
            cx.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            cx.execute("DELETE FROM users WHERE id=?", (user_id,))
        return True

    # --- чтение ---------------------------------------------------------------------------
    def devices(self, principal=None, now=None):
        """Платы, которые уже присылали данные: человеку только его, администратору (и principal=None) все."""
        now = int(time.time() if now is None else now)
        sees_all = principal is None or principal["role"] == "admin"
        sql = "SELECT d.device, d.last_seen, b.name FROM devices d LEFT JOIN boards b ON b.device=d.device"
        with closing(self._connect()) as cx:
            rows = cx.execute(sql + ("" if sees_all else " WHERE b.owner_id=?") + " ORDER BY d.last_seen DESC",
                              () if sees_all else (principal["id"],)).fetchall()
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
    """Блокирует ключ (адрес, имя) после limit неудачных попыток за window секунд: по умолчанию 10 за 10 минут."""

    def __init__(self, limit=10, window=600):
        self.limit, self.window = limit, window
        self._lock = threading.Lock()
        self._failures = {}

    def blocked(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            hits = [t for t in self._failures.get(key, ()) if now - t < self.window]
            self._failures[key] = hits
            return len(hits) >= self.limit

    def fail(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._failures.setdefault(key, []).append(now)
            if len(self._failures) > 5000:  # адреса приходят со всего интернета: старые записи не копим
                self._failures = {k: v for k, v in self._failures.items() if v and now - v[-1] < self.window}

    def clear(self, key):
        with self._lock:
            self._failures.pop(key, None)


class RateGate:
    """Не чаще одного запроса за min_gap секунд на ключ (токен и плата)."""

    def __init__(self, min_gap):
        self.min_gap = min_gap
        self._lock = threading.Lock()
        self._last = {}

    def allow(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.min_gap:
                return False
            self._last[key] = now
            if len(self._last) > 2000:
                self._last = {k: t for k, t in self._last.items() if now - t < 60}
            return True


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


class AuthFailed(Exception):
    """В заголовке Authorization неверное имя или пароль."""


class RateLimited(Exception):
    """С этого адреса слишком много неудачных попыток."""


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

    def _redirect(self, location):
        self._send(302, b"", "text/plain; charset=utf-8", {"Location": location})

    def _page(self, name):
        self._send(200, (STATIC_DIR / name).read_bytes(), HTML, {"Cache-Control": "no-cache"})

    def _client_ip(self):
        peer = self.client_address[0]
        if peer in ("127.0.0.1", "::1"):  # за nginx настоящий адрес приходит в заголовке
            return self.headers.get("X-Real-IP", peer)[:64]
        return peer

    # --- кто вошёл ------------------------------------------------------------------------
    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _session_cookie(self, token, max_age):
        # Secure ставится, когда запрос пришёл по HTTPS (Traefik передаёт X-Forwarded-Proto), и всегда при DASH_COOKIE_SECURE=1
        secure = self.headers.get("X-Forwarded-Proto") == "https" or self.server.cfg.cookie_secure
        return {"Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                % (SESSION_COOKIE, token, max_age, "; Secure" if secure else "")}

    def _principal(self):
        """Кто вошёл: {"id", "username", "role"} или None. Сначала сессия по cookie; для скриптов администратор
        ещё может прислать HTTP Basic (имя и пароль из .env). Неверные данные в Authorization дают AuthFailed,
        слишком много неудачных попыток с адреса дают RateLimited."""
        store = self.server.store
        token = self._cookie(SESSION_COOKIE)
        if token:
            who = store.session_user(token)
            if who:
                return who
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        ip, limiter = self._client_ip(), self.server.limiter
        if limiter.blocked(ip):
            raise RateLimited()
        user = password = ""
        try:
            user, _, password = base64.b64decode(header[6:], validate=True).decode("utf-8").partition(":")
        except ValueError:
            pass
        if store.admin_user and hmac.compare_digest(user.encode(), store.admin_user.encode()) \
                and store.check_admin_password(password):
            limiter.clear(ip)
            return store.admin_principal()
        limiter.fail(ip)
        raise AuthFailed()

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
            reading = self.command in ("GET", "HEAD")
            if self.command in ("POST", "DELETE"):
                # Тело запроса читают только приём и запросы со страниц панели, и то после проверки доступа. Во всех
                # остальных случаях оно остаётся в потоке, поэтому соединение закрывается после ответа.
                self.close_connection = True
            if path == "/healthz" and reading:
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

            # --- открыто всем: скрипты и стили, состояние регистрации, вход и регистрация ---
            if path in ASSETS and reading:
                name, ctype = ASSETS[path]
                return self._send(200, (STATIC_DIR / name).read_bytes(), ctype, {"Cache-Control": "no-cache"})
            if path == "/api/registration" and reading:
                return self._json(200, {"open": self.server.store.registration_open()})
            if path == "/api/login":
                return self._login()
            if path == "/api/register":
                return self._register()

            try:
                who = self._principal()
            except (AuthFailed, RateLimited):
                if path not in PUBLIC_PAGES:
                    raise
                who = None  # устаревший вход, сохранённый браузером, не должен закрывать страницу входа
            if path in PUBLIC_PAGES and reading:
                return self._redirect("/") if who else self._page(PUBLIC_PAGES[path])
            if path in PAGES or path in ADMIN_PAGES:
                if not reading:
                    return self._error(405, "метод не поддерживается", {"Allow": "GET, HEAD"})
                if who is None:
                    return self._redirect("/login?next=" + quote(self.path, safe=""))
                if path in ADMIN_PAGES and who["role"] != "admin":
                    return self._redirect("/")
                return self._page(PAGES.get(path) or ADMIN_PAGES[path])
            if who is None:
                return self._error(401, "нужен вход")

            # --- дальше только для вошедших ---
            if path == "/api/me" and reading:
                return self._json(200, {"user": who, "registration_open": self.server.store.registration_open()})
            if path == "/api/logout":
                return self._logout()
            if path == "/api/account/password":
                return self._account_password(who)
            if path.startswith("/api/admin/"):
                return self._admin_api(path, who)
            if path in ("/api/boards", "/api/tokens") or path.startswith(("/api/boards/", "/api/tokens/")):
                return self._boards_api(path, who)
            if path in ("/api/settings", "/api/settings/cancel"):
                return self._settings_api(path, query, who)
            if not reading:
                return self._error(405, "метод не поддерживается", {"Allow": "GET, HEAD"})
            if path == "/api/devices":
                return self._json(200, {"devices": self.server.store.devices(who)})
            if path in ("/api/status", "/api/series", "/api/logs", "/api/export.csv"):
                return self._device_api(path, query, who)
            return self._error(404, "не найдено")
        except AuthFailed:
            return self._error(401, "неверное имя или пароль")
        except RateLimited:
            return self._error(429, "слишком много неудачных попыток входа", {"Retry-After": "600"})
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
        finally:
            self._drain_body()

    def _drain_body(self):
        """Ответ уже отправлен, а тело запроса не прочитано (отказ до чтения): дочитываем небольшое тело, иначе
        закрытие соединения с непрочитанными данными сбрасывает его (RST) и клиент может не получить ответ."""
        if self.close_connection and self.command in ("POST", "DELETE"):
            length = self.headers.get("Content-Length", "")
            if length.isdigit() and 0 < int(length) <= MAX_BODY:
                try:
                    self.rfile.read(int(length))
                except OSError:
                    pass

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
        if not isinstance(device, str) or not DEVICE_RE.fullmatch(device):
            return self._error(400, "device: 1-32 символа, латиница, цифры, _ . -")
        if record["device"] and device != record["device"]:
            # Токен выдан одной плате: с ним нельзя писать данные от имени другой
            return self._error(403, "этот токен выдан для платы %s" % record["device"])
        if not self.server.store.board_exists(device):
            return self._error(403, "плата %s не зарегистрирована: добавьте её на странице /boards" % device)
        log = body.get("log")
        if isinstance(log, dict) and isinstance(log.get("text"), str) and len(log["text"].encode("utf-8")) > MAX_LOG_BYTES:
            return self._error(413, "порция журнала слишком большая")
        if not self.server.ingest_gate.allow((record["id"], device)):
            # Открытая регистрация: любой может завести плату, поэтому писать в базу чаще, чем нужно, нельзя
            return self._error(429, "слишком часто: плата присылает данные раз в минуту", {"Retry-After": str(INGEST_MIN_GAP)})
        return self._json(200, self.server.store.ingest(device, body))

    # --- запросы со страниц панели --------------------------------------------------------
    def _check_action(self):
        """Защита от чужих страниц (CSRF): браузер сам подставляет cookie, поэтому запрос, который что-то
        меняет, должен прийти именно со страницы панели. Заголовок X-Dash-Action чужая страница без разрешения
        сервера (CORS) отправить не может, а Sec-Fetch-Site отсекает кросс-сайтовые запросы там, где браузер
        его присылает."""
        site = self.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin", "none") or self.headers.get("X-Dash-Action") != "1":
            self._error(403, "запрос отклонён: он должен идти со страницы панели")
            return False
        return True

    def _action(self, method, allow):
        """Метод запроса подходит, и он пришёл со страницы панели. Иначе ответ уже отправлен."""
        if self.command != method:
            self._error(405, "нужен %s" % method, {"Allow": allow})
            return False
        return self._check_action()

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

    # --- вход, регистрация, выход, свой пароль --------------------------------------------
    def _login(self):
        if not self._action("POST", "POST"):
            return None
        server, ip = self.server, self._client_ip()
        if server.limiter.blocked(ip):
            return self._error(429, "слишком много неудачных попыток входа", {"Retry-After": "600"})
        body = self._read_json()
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > PASSWORD_MAX:
            raise ValueError("нужны имя и пароль")
        if server.name_limiter.blocked(username.lower()):
            return self._error(429, "слишком много неудачных попыток для этого имени, подождите", {"Retry-After": "600"})
        who = server.store.login_user(username, password)
        if who is None:
            # Успешный вход счётчик не сбрасывает: иначе, чередуя свой вход с чужими подборами, лимит обойти
            server.limiter.fail(ip)
            server.name_limiter.fail(username.lower())
            LOG.warning("вход: неверное имя или пароль, имя %r, с %s", username[:40], ip)
            return self._error(401, "неверное имя или пароль")
        token = server.store.create_session(who["id"], ip)
        return self._json(200, {"ok": True, "user": who}, self._session_cookie(token, server.store.session_days * 86400))

    def _register(self):
        if not self._action("POST", "POST"):
            return None
        server, ip = self.server, self._client_ip()
        if server.reg_limiter.blocked(ip):
            return self._error(429, "слишком много попыток регистрации с вашего адреса, попробуйте позже", {"Retry-After": "3600"})
        server.reg_limiter.fail(ip)  # считается каждая попытка, удачная или нет
        if not server.store.registration_open():
            return self._error(403, "регистрация закрыта")
        body = self._read_json()
        who = server.store.register_user(body.get("username"), body.get("password"), ip)
        LOG.info("зарегистрирован человек %s с %s", who["username"], ip)
        token = server.store.create_session(who["id"], ip)
        return self._json(201, {"ok": True, "user": who}, self._session_cookie(token, server.store.session_days * 86400))

    def _logout(self):
        if not self._action("POST", "POST"):
            return None
        token = self._cookie(SESSION_COOKIE)
        if token:
            self.server.store.end_session(token)
        return self._json(200, {"ok": True}, self._session_cookie("", 0))

    def _account_password(self, who):
        if not self._action("POST", "POST"):
            return None
        server, ip = self.server, self._client_ip()
        if server.limiter.blocked(ip):
            return self._error(429, "слишком много неудачных попыток", {"Retry-After": "600"})
        body = self._read_json()
        try:
            server.store.change_password(who["id"], body.get("old"), body.get("new"), keep_token=self._cookie(SESSION_COOKIE))
        except WrongPassword:
            server.limiter.fail(ip)
            raise
        return self._json(200, {"ok": True})

    # --- администратор: люди и регистрация ------------------------------------------------
    def _admin_api(self, path, who):
        if who["role"] != "admin":
            return self._error(403, "только для администратора")
        store = self.server.store
        parts = path.strip("/").split("/")   # api, admin, users[, номер[, действие]] или api, admin, registration
        if parts == ["api", "admin", "users"]:
            if self.command not in ("GET", "HEAD"):
                return self._error(405, "метод не поддерживается", {"Allow": "GET, HEAD"})
            return self._json(200, {"users": store.list_users(), "registration_open": store.registration_open(),
                                    "max_users": store.max_users, "max_boards_per_user": store.max_boards_per_user})
        if parts == ["api", "admin", "registration"]:
            if not self._action("POST", "POST"):
                return None
            is_open = self._read_json().get("open")
            if not isinstance(is_open, bool):
                raise ValueError("open: true или false")
            store.set_registration(is_open)
            LOG.info("регистрация %s администратором %s", "открыта" if is_open else "закрыта", who["username"])
            return self._json(200, {"ok": True, "registration_open": is_open})
        if len(parts) in (4, 5) and parts[2] == "users" and re.fullmatch(r"[0-9]{1,9}", parts[3]):
            user_id, action = int(parts[3]), (parts[4] if len(parts) == 5 else None)
            if action in ("disable", "enable"):
                if not self._action("POST", "POST"):
                    return None
                item = store.set_user_disabled(user_id, action == "disable")
                if item is None:
                    return self._error(404, "такого человека нет")
                LOG.info("человек %s: %s, администратор %s", item["username"], "заблокирован" if action == "disable" else "разблокирован", who["username"])
                return self._json(200, {"ok": True, "user": item})
            if action == "reset-password":
                if not self._action("POST", "POST"):
                    return None
                temp = store.reset_password(user_id)
                if temp is None:
                    return self._error(404, "такого человека нет")
                LOG.info("сброшен пароль человека id=%d, администратор %s", user_id, who["username"])
                return self._json(200, {"ok": True, "password": temp})
            if action is None:
                if not self._action("DELETE", "DELETE"):
                    return None
                if not store.delete_user(user_id):
                    return self._error(404, "такого человека нет")
                LOG.info("удалён человек id=%d, администратор %s", user_id, who["username"])
                return self._json(200, {"ok": True})
        return self._error(404, "не найдено")

    # --- платы и токены -------------------------------------------------------------------
    def _boards_api(self, path, who):
        """Платы и их токены приёма: список, регистрация, замена токена, переименование, удаление, отзыв.
        Человек управляет только своими платами, администратор всеми; чужая плата для человека «не найдена»."""
        parts = path.strip("/").split("/")   # api, boards[, имя платы[, tokens | rename]] или api, tokens, номер[, revoke]
        if parts[:2] == ["api", "boards"]:
            return self._boards_routes(parts[2:], who)
        if len(parts) in (3, 4) and re.fullmatch(r"[0-9]{1,9}", parts[2]):
            return self._token_routes(int(parts[2]), parts[3:], who)
        return self._error(404, "не найдено")

    def _boards_routes(self, rest, who):
        store = self.server.store
        ip = self._client_ip()
        if not rest:
            if self.command in ("GET", "HEAD"):
                data = store.list_boards(self.server.cfg.online_sec, who)
                data["max_boards"] = store.max_boards if who["role"] == "admin" else store.max_boards_per_user
                return self._json(200, data)
            if not self._action("POST", "GET, HEAD, POST"):
                return None
            body = self._read_json()
            token, board, item = store.add_board(body.get("name"), body.get("device"), who["id"],
                                                 limit_owner=who["role"] != "admin")
            LOG.info("плата зарегистрирована: %s (%r), владелец %s, с %s", board["device"], board["name"], who["username"], ip)
            return self._json(201, {"ok": True, "token": token, "board": board, "item": item})
        device = rest[0]
        if not DEVICE_RE.fullmatch(device) or len(rest) > 2:
            return self._error(404, "не найдено")
        if len(rest) == 1:
            if not self._action("DELETE", "DELETE"):
                return None
            if not store.can_manage(who, device):
                return self._error(404, "такой платы нет")
            store.delete_board(device)
            LOG.info("плата удалена: %s, %s, с %s", device, who["username"], ip)
            return self._json(200, {"ok": True})
        if rest[1] in ("tokens", "rename"):
            if not self._action("POST", "POST"):
                return None
            if not store.can_manage(who, device):
                return self._error(404, "такой платы нет")
            body = self._read_json()
            if rest[1] == "tokens":
                made = store.add_board_token(device, body.get("name"))
                if made is None:  # плату удалили между проверкой и выпуском токена
                    return self._error(404, "такой платы нет")
                LOG.info("токен создан: плата %s, id=%d, %s, с %s", device, made[1]["id"], who["username"], ip)
                return self._json(201, {"ok": True, "token": made[0], "item": made[1]})
            board = store.rename_board(device, body.get("name"))
            if board is None:
                return self._error(404, "такой платы нет")
            return self._json(200, {"ok": True, "board": board})
        return self._error(404, "не найдено")

    def _token_routes(self, token_id, rest, who):
        store = self.server.store
        if rest == ["revoke"]:
            if not self._action("POST", "POST"):
                return None
            if not store.can_manage_token(who, token_id):  # нет токена или он чужой: человеку это одно и то же
                return self._error(404, "такого токена нет")
            item = store.revoke_token(token_id)
            LOG.info("токен отозван: id=%d (%r), %s, с %s", token_id, item["name"], who["username"], self._client_ip())
            return self._json(200, {"ok": True, "item": item})
        if not rest:
            if not self._action("DELETE", "DELETE"):
                return None
            if not store.can_manage_token(who, token_id):
                return self._error(404, "такого токена нет")
            store.delete_token(token_id)
            LOG.info("токен удалён: id=%d, %s, с %s", token_id, who["username"], self._client_ip())
            return self._json(200, {"ok": True})
        return self._error(404, "не найдено")

    # --- настройки платы из панели --------------------------------------------------------
    def _settings_api(self, path, query, who):
        store, cfg = self.server.store, self.server.cfg
        if path == "/api/settings" and self.command in ("GET", "HEAD"):
            device = (query.get("device") or [""])[0]
            if device and not DEVICE_RE.fullmatch(device):
                raise ValueError("device: недопустимое имя")
            # Как у остальных данных плат: человек видит только свои, чужая плата для него то же, что отсутствующая
            visible = {d["device"]: d for d in store.devices(who)}
            device = device or next(iter(visible), "")
            if device not in visible:
                return self._error(404, "плата ещё не присылала данные")
            state = store.cfg_state(device)
            state.update({"device": device, "name": store.board_name(device) or device, "now": int(time.time()),
                          "online": visible[device]["age"] <= cfg.online_sec, "last_seen": visible[device]["last_seen"],
                          "ttl": CFG_TTL, "schema": CFG_SCHEMA})
            return self._json(200, state)
        if not self._action("POST", "GET, HEAD, POST" if path == "/api/settings" else "POST"):
            return None
        body = self._read_json()
        device = body.get("device")
        if not isinstance(device, str) or not DEVICE_RE.fullmatch(device) or not store.can_manage(who, device):
            return self._error(404, "такой платы нет")
        if not self.server.cfg_gate.allow((who["id"], device)):
            return self._error(429, "слишком часто: подождите пару секунд", {"Retry-After": "2"})
        ip = self._client_ip()
        if path == "/api/settings/cancel":
            state = store.cfg_cancel(device)
            LOG.info("настройки платы %s: запрос снят, %s, с %s", device, who["username"], ip)
            return self._json(200, dict(state, ok=True))
        state = store.cfg_submit(device, body.get("set"), who)
        LOG.info("настройки платы %s: %s просит изменить %s, с %s", device, who["username"],
                 json.dumps(state["pending"]["set"] if state["pending"] else {}, ensure_ascii=False), ip)
        return self._json(200, dict(state, ok=True))

    # --- данные плат ----------------------------------------------------------------------
    def _device_api(self, path, query, who):
        store, cfg = self.server.store, self.server.cfg
        device = (query.get("device") or [""])[0]
        if device and not DEVICE_RE.match(device):
            raise ValueError("device: недопустимое имя")
        level = limit = before = None
        if path == "/api/logs":
            level = (query.get("level") or ["error"])[0]
            if level not in ("error", "warn", "all"):
                raise ValueError("level: error, warn или all")
            limit = int_param(query, "limit", 200, 1, 500)
            before = int_param(query, "before", None, 1)
        now = int(time.time())
        if path in ("/api/series", "/api/export.csv"):
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
        # Человек видит данные только своих плат; чужая плата для него то же, что не присылавшая данных
        visible = [d["device"] for d in store.devices(who)]
        device = device or (visible[0] if visible else "")
        if device not in visible:
            return self._error(404, "плата ещё не присылала данные")
        if path == "/api/status":
            status = store.status(device, cfg.online_sec)
            return self._json(200, status) if status else self._error(404, "плата ещё не присылала данные")
        if path == "/api/logs":
            return self._json(200, {"device": device, "level": level, "rows": store.logs(device, level, limit, before)})
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
        self.cfg, self.store = cfg, store
        self.limiter = FailureLimiter()                                  # неверные пароли и токены: 10 за 10 минут с адреса
        self.name_limiter = FailureLimiter(limit=20, window=600)         # неверные пароли одному имени
        self.reg_limiter = FailureLimiter(limit=REGISTRATIONS_PER_HOUR, window=3600)
        self.ingest_gate = RateGate(INGEST_MIN_GAP)
        self.cfg_gate = RateGate(2)                                      # запросы на смену настроек: не чаще раза в 2 с на человека и плату
        store.configure(cfg)
        store.ensure_admin(cfg.view_user, cfg.view_password)
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
            store.purge_sessions()
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
    LOG.info("регистрация людей %s, администратор %s", "открыта" if store.registration_open() else "закрыта", store.admin_user)
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
