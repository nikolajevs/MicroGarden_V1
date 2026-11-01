#pragma once
#include <stddef.h>
#include <stdint.h>
#include "Telemetry.h"

// Настройки платы из панели (dashboard/). Здесь только чистые функции без железа: список того, что вообще
// можно менять издалека, и разбор ответа сервера. Их проверяют static_assert в SafetyTests.h. Применение к
// Settings и запись в NVS — в firmware.ino. Контракт обмена описан в dashboard/README.md.

constexpr size_t CLOUD_ID_MAX = 16;      // номер запроса: 1-16 шестнадцатеричных цифр
constexpr size_t CLOUD_NAME_MAX = 24;    // имя настройки с нулём в конце; самое длинное, drying_temp_target, 18 знаков
constexpr size_t CLOUD_MAX_FIELDS = 32;  // настроек в одном запросе; всего их меняется 28
constexpr size_t CLOUD_REPLY_MAX = 2048; // ответ длиннее не читается: настоящий не бывает больше 1 КБ
constexpr double CLOUD_WATERING_MAX_SEC = 300; // из панели полив не длиннее: насос не должен качать часами из-за опечатки
constexpr double CLOUD_START_MAX = 4102444800.0; // дата начала цикла не позже 2100-01-01

// Что можно менять из панели. В список не входят сеть, адрес и токен панели (ошибка оставила бы плату без связи),
// поправка тока (искажает замеры) и датчик уровня воды (его отключение снимает защиту насоса): всё это меняется
// только на самой плате. Тот же список ведёт сервер панели (server.py: CFG_SPECS), тест сверяет их.
constexpr const char *const CLOUD_PARAMS[] = {
  "operation_mode",
  "temp_target", "temp_target_night", "temp_delta", "hum_target_night", "hum_band_night",
  "drying_temp_target", "drying_temp_delta", "drying_hum_target", "drying_hum_band",
  "led_on_hour", "led_on_minute", "led_off_hour", "led_off_minute", "led_min_limit", "led_max_limit",
  "fan1_min_limit", "fan1_max_limit", "fan2_min_limit", "fan2_max_limit",
  "heater_mode", "rgb_brightness", "rgb_night_mode",
  "watering_days", "watering_hour", "watering_minute", "watering_duration",
  "start_time",
};
constexpr size_t CLOUD_PARAM_COUNT = sizeof(CLOUD_PARAMS) / sizeof(CLOUD_PARAMS[0]);
static_assert(CLOUD_PARAM_COUNT <= CLOUD_MAX_FIELDS, "one request must be able to carry every changeable setting");

// Плата сообщает панели и эти настройки, но менять их издалека нельзя
constexpr const char *const CLOUD_REPORT_ONLY[] = {"water_sensor_enabled"};
constexpr size_t CLOUD_REPORT_ONLY_COUNT = sizeof(CLOUD_REPORT_ONLY) / sizeof(CLOUD_REPORT_ONLY[0]);

constexpr bool sameText(const char *a, const char *b) {
  while (*a != '\0' && *a == *b) {
    ++a;
    ++b;
  }
  return *a == *b;
}

constexpr bool cloudParamAllowed(const char *name) {
  for (size_t i = 0; i < CLOUD_PARAM_COUNT; ++i) {
    if (sameText(CLOUD_PARAMS[i], name)) return true;
  }
  return false;
}

// Верхняя граница значения при изменении из панели: у полива она жёстче местной (до 3600 с)
constexpr double cloudHiLimit(const char *name, double localHi) {
  return sameText(name, "watering_duration") && localHi > CLOUD_WATERING_MAX_SEC ? CLOUD_WATERING_MAX_SEC : localHi;
}

// Как проверять значение. Whole — целое, Flag — только 0 или 1, Date — полночь выбранной даты (unixtime) или 0.
enum class CloudKind : uint8_t { Float, Whole, Flag, Date };

constexpr bool wholeNumber(double v) {
  return v >= -2147483648.0 && v <= 4294967295.0 && v == double(int64_t(v));
}

// NaN не проходит ни одну проверку: все сравнения с ним ложны
constexpr bool cloudValueOk(CloudKind kind, double v, double lo, double hi) {
  switch (kind) {
    case CloudKind::Float: return v >= lo && v <= hi;
    case CloudKind::Whole: return wholeNumber(v) && v >= lo && v <= hi;
    case CloudKind::Flag: return v == 0.0 || v == 1.0;
    case CloudKind::Date:
      return v == 0.0 || (wholeNumber(v) && v >= double(UTC_2024) && v <= CLOUD_START_MAX && int64_t(v) % 86400 == 0);
  }
  return false;
}

struct CloudField {
  char name[CLOUD_NAME_MAX];
  double value; // флаг — 0 или 1
};

struct CloudPatch {
  char id[CLOUD_ID_MAX + 1];
  size_t count;
  CloudField items[CLOUD_MAX_FIELDS];
};

enum class CloudReply : uint8_t {
  None,  // в ответе сервера нет раздела cfg: ничего менять не просят
  Patch, // разобран целиком
  Bad,   // раздел есть, но поврежден или не по правилам: не применяется ни одна настройка
};

constexpr bool hexId(const char *s) {
  const size_t n = cstrLen(s);
  if (n < 1 || n > CLOUD_ID_MAX) return false;
  for (size_t i = 0; i < n; ++i) {
    if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return false;
  }
  return true;
}

constexpr bool cloudNameValid(const char *s) {
  const size_t n = cstrLen(s);
  if (n < 1 || n >= CLOUD_NAME_MAX) return false;
  for (size_t i = 0; i < n; ++i) {
    if (!((s[i] >= 'a' && s[i] <= 'z') || (s[i] >= '0' && s[i] <= '9') || s[i] == '_')) return false;
  }
  return true;
}

constexpr void skipSpaces(const char *s, size_t n, size_t &i) {
  while (i < n && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n')) ++i;
}

constexpr bool takeChar(const char *s, size_t n, size_t &i, char c) {
  skipSpaces(s, n, i);
  if (i >= n || s[i] != c) return false;
  ++i;
  return true;
}

// Строка в кавычках. Экранирование не поддерживается (в настоящем ответе его не бывает): строка длиннее
// cap - 1 знака или с обратной косой чертой — отказ.
constexpr bool takeString(const char *s, size_t n, size_t &i, char *dst, size_t cap) {
  skipSpaces(s, n, i);
  if (i >= n || s[i] != '"') return false;
  ++i;
  size_t k = 0;
  while (i < n && s[i] != '"') {
    if (s[i] == '\\' || static_cast<unsigned char>(s[i]) < 0x20 || k + 1 >= cap) return false;
    dst[k++] = s[i++];
  }
  if (i >= n) return false;
  dst[k] = '\0';
  ++i;
  return true;
}

// Десятичное число без порядка: -12, 3.5. Не больше 15 цифр, чтобы мантисса точно помещалась в double;
// одно деление на степень десятки даёт ближайшее к записанному значение.
constexpr bool takeNumber(const char *s, size_t n, size_t &i, double &out) {
  skipSpaces(s, n, i);
  bool negative = false;
  if (i < n && s[i] == '-') {
    negative = true;
    ++i;
  }
  uint64_t mantissa = 0;
  int digits = 0;
  int fraction = 0;
  bool point = false;
  bool any = false;
  while (i < n) {
    const char c = s[i];
    if (c >= '0' && c <= '9') {
      if (++digits > 15) return false;
      mantissa = mantissa * 10 + uint64_t(c - '0');
      if (point) ++fraction;
      any = true;
    } else if (c == '.' && !point && any) {
      point = true;
    } else {
      break;
    }
    ++i;
  }
  if (!any || (point && fraction == 0)) return false;
  double scale = 1.0;
  for (int k = 0; k < fraction; ++k) scale *= 10.0;
  const double value = double(mantissa) / scale;
  out = negative ? -value : value;
  return true;
}

constexpr bool takeWord(const char *s, size_t n, size_t &i, const char *word) {
  skipSpaces(s, n, i);
  size_t k = 0;
  while (word[k] != '\0') {
    if (i + k >= n || s[i + k] != word[k]) return false;
    ++k;
  }
  i += k;
  return true;
}

constexpr size_t NO_POSITION = ~size_t(0);

// Место сразу за `"key":`. Ответ собирает сервер панели, поэтому достаточно первой такой пары.
constexpr size_t findMember(const char *s, size_t n, const char *key) {
  const size_t len = cstrLen(key);
  for (size_t at = 0; at + len + 2 <= n; ++at) {
    if (s[at] != '"') continue;
    size_t k = 0;
    while (k < len && s[at + 1 + k] == key[k]) ++k;
    if (k != len || s[at + 1 + len] != '"') continue;
    size_t i = at + len + 2;
    skipSpaces(s, n, i);
    if (i < n && s[i] == ':') return i + 1;
  }
  return NO_POSITION;
}

// Разбор раздела cfg ответа сервера на приём телеметрии:
//   "cfg": {"id": "1a2b3c4d", "set": {"temp_target": 24.5, "led_on_hour": 7, "heater_mode": 2}}
// Правила строгие: порядок id, set; имена из строчных латинских букв, цифр и _; значения — числа или
// true/false; повторов нет; не больше CLOUD_MAX_FIELDS. Любое отступление — Bad, и плата не применяет ничего.
constexpr CloudReply parseCloudReply(const char *s, size_t n, CloudPatch &out) {
  out = CloudPatch{};
  size_t i = findMember(s, n, "cfg");
  if (i == NO_POSITION || takeWord(s, n, i, "null")) return CloudReply::None;
  char key[CLOUD_NAME_MAX] = {};
  if (!takeChar(s, n, i, '{') || !takeString(s, n, i, key, sizeof(key)) || !sameText(key, "id") ||
      !takeChar(s, n, i, ':') || !takeString(s, n, i, out.id, sizeof(out.id)) || !hexId(out.id) ||
      !takeChar(s, n, i, ',') || !takeString(s, n, i, key, sizeof(key)) || !sameText(key, "set") ||
      !takeChar(s, n, i, ':') || !takeChar(s, n, i, '{')) return CloudReply::Bad;
  for (;;) {
    if (out.count >= CLOUD_MAX_FIELDS) return CloudReply::Bad;
    CloudField &field = out.items[out.count];
    if (!takeString(s, n, i, field.name, sizeof(field.name)) || !cloudNameValid(field.name) ||
        !takeChar(s, n, i, ':')) return CloudReply::Bad;
    for (size_t k = 0; k < out.count; ++k) {
      if (sameText(out.items[k].name, field.name)) return CloudReply::Bad;
    }
    if (takeWord(s, n, i, "true")) field.value = 1.0;
    else if (takeWord(s, n, i, "false")) field.value = 0.0;
    else if (!takeNumber(s, n, i, field.value)) return CloudReply::Bad;
    ++out.count;
    if (takeChar(s, n, i, ',')) continue;
    if (takeChar(s, n, i, '}')) break;
    return CloudReply::Bad;
  }
  return takeChar(s, n, i, '}') ? CloudReply::Patch : CloudReply::Bad;
}
