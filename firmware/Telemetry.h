#pragma once
#include <stddef.h>
#include <stdint.h>
#include "Safety.h"

// Отправка телеметрии на сервер панели (dashboard/, адрес по умолчанию https://dash.dvinsk.lat).
// Здесь только чистые функции без железа: их проверяют static_assert в SafetyTests.h.
// Контракт обмена описан в dashboard/README.md.
constexpr uint32_t TELEMETRY_FIRST_DELAY_MS = 30000; // после старта успевает пройти первый цикл климата
constexpr uint32_t TELEMETRY_PERIOD_MS = 60000;
constexpr uint32_t TELEMETRY_REPORT_REPEAT_MS = 1800000; // та же ошибка повторяется в журнале раз в 30 мин
constexpr size_t TELEMETRY_MAX_LOG_CHUNK = 3000;         // сервер принимает до 24 КБ, плате хватает и этого
constexpr size_t TELEMETRY_URL_MIN = 12;
constexpr size_t TELEMETRY_URL_MAX = 96;
constexpr size_t TELEMETRY_TOKEN_MIN = 24; // как на сервере: DASH_INGEST_TOKEN не короче 24
constexpr size_t TELEMETRY_TOKEN_MAX = 128;
constexpr size_t DEVICE_LABEL_MAX = 32;    // как на сервере: 1-32 символа [A-Za-z0-9_.-]
constexpr uint32_t UTC_2024 = 1704067200;  // раньше этого системное время считается не выставленным

struct LogChunk {
  uint32_t from; // позиция первого байта порции в потоке журнала с момента запуска
  size_t len;    // 0 — отправлять нечего
};

// Какую порцию журнала отправить. window — последние n байт потока (позиции windowStart ...
// windowStart+n-1), sent — сколько байт сервер уже принял.
// Правила, от которых зависит, что сервер вообще сможет разобрать запрос:
//  * порция всегда заканчивается концом строки, поэтому символ UTF-8 не режется посередине;
//  * если часть журнала уже затёрта кольцевым буфером, начало сдвигается на первую целую
//    строку: иначе порция началась бы с обрывка символа, и сервер отклонял бы её навсегда;
//  * строка длиннее порции режется по границе символа, чтобы отправка не застряла.
constexpr LogChunk chooseLogChunk(const char *window, size_t n, uint32_t windowStart, uint32_t sent, size_t maxChunk) {
  uint32_t start = sent;
  const uint32_t windowEnd = windowStart + uint32_t(n);
  if (start < windowStart) {
    size_t i = 0;
    while (i < n && window[i] != '\n') ++i;
    if (i >= n) return {windowEnd, 0};
    start = windowStart + uint32_t(i) + 1;
  }
  if (start >= windowEnd) return {windowEnd, 0};
  const size_t off = size_t(start - windowStart);
  size_t take = n - off;
  if (take > maxChunk) take = maxChunk;
  size_t end = take;
  while (end > 0 && window[off + end - 1] != '\n') --end;
  if (end == 0 && take == maxChunk) {
    end = take;
    while (end > 0 && off + end < n && (uint8_t(window[off + end]) & 0xC0) == 0x80) --end;
  }
  return {start, end};
}

constexpr size_t cstrLen(const char *s) {
  size_t n = 0;
  while (s[n] != '\0') ++n;
  return n;
}

constexpr bool startsWith(const char *s, const char *prefix) {
  for (size_t i = 0; prefix[i] != '\0'; ++i) {
    if (s[i] != prefix[i]) return false;
  }
  return true;
}

// Метка платы попадает в JSON и в проверку сервера: латиница, цифры и _ . -
constexpr bool deviceLabelValid(const char *s) {
  const size_t n = cstrLen(s);
  if (n < 1 || n > DEVICE_LABEL_MAX) return false;
  for (size_t i = 0; i < n; ++i) {
    const char c = s[i];
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                    c == '_' || c == '.' || c == '-';
    if (!ok) return false;
  }
  return true;
}

// Токен приёма уходит в заголовке: только печатные символы без пробелов.
constexpr bool telemetryTokenValid(const char *s) {
  const size_t n = cstrLen(s);
  if (n < TELEMETRY_TOKEN_MIN || n > TELEMETRY_TOKEN_MAX) return false;
  for (size_t i = 0; i < n; ++i) {
    if (s[i] <= ' ' || s[i] >= 0x7F) return false;
  }
  return true;
}

// Только https: токен по обычному http уходил бы открытым текстом. Пробелы и управляющие
// символы в адресе не допускаются.
constexpr bool telemetryUrlValid(const char *s) {
  const size_t n = cstrLen(s);
  if (n < TELEMETRY_URL_MIN || n > TELEMETRY_URL_MAX || !startsWith(s, "https://")) return false;
  for (size_t i = 0; i < n; ++i) {
    if (s[i] <= ' ' || s[i] >= 0x7F) return false;
  }
  return true;
}

// Писать ли итог отправки в журнал платы. Успех пишется один раз (после неудач или после
// старта), неудача — при смене кода и затем раз в TELEMETRY_REPORT_REPEAT_MS: иначе каждую
// минуту без связи журнал и сервер заполнялись бы одной и той же строкой.
constexpr bool telemetryReportDue(int lastCode, int code, uint32_t nowMs, uint32_t lastReportMs, uint32_t repeatMs) {
  if (code != lastCode) return true;
  return code != 200 && elapsedMs(nowMs, lastReportMs) >= repeatMs;
}
