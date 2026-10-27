#pragma once
#include "Safety.h"
#include "Board.h"
#include "LiteControl.h"
#include "Telemetry.h"
#include "Ina238Tests.h"

// Compile-time regressions: every firmware build checks these, with no runtime cost.
static_assert(validCalendar(2024, 2, 29, 23, 59, 59), "leap day must work");
static_assert(!validCalendar(2025, 2, 29, 12, 0, 0), "reject nonexistent leap day");
static_assert(!validCalendar(2026, 4, 31, 12, 0, 0), "reject day overflow");
static_assert(!validCalendar(2026, 0, 1, 12, 0, 0), "reject month zero");
static_assert(!validCalendar(2026, 13, 1, 12, 0, 0), "reject month overflow");
static_assert(!validCalendar(2026, 1, 0, 12, 0, 0), "reject day zero");
static_assert(!validCalendar(2026, 1, 1, 45, 0, 0), "reject old RTC hour bug");
static_assert(!validCalendar(2026, 1, 1, 24, 0, 0), "reject hour 24");
static_assert(!validCalendar(2026, 1, 1, 12, 60, 0), "reject minute 60");
static_assert(!validCalendar(2026, 1, 1, 12, 0, 60), "reject second 60");
static_assert(!validCalendar(2000, 1, 1, 0, 0, 0), "reject factory reset date");
static_assert(!wateringDayAllowed(false, 20000, 0), "no watering with untrusted boot clock");
static_assert(!wateringDayAllowed(true, 20000, 20000), "no repeat after restoring persisted mark");
static_assert(!wateringDayAllowed(true, 19999, 20000), "no repeat after backwards date adjustment");
static_assert(wateringDayAllowed(true, 20001, 20000), "next day may water");
static_assert(elapsedMs(29999, 0) < 30000, "pump must run for requested duration");
static_assert(elapsedMs(30000, 0) >= 30000, "pump must stop at deadline");
static_assert(elapsedMs(0x20, 0xfffffff0) == 48, "millis wrap must be safe");

static_assert(reconcileWateringMark(true, 20000, 20365) == 20000, "a mark from a future clock is pulled back to today");
static_assert(!wateringDayAllowed(true, 20000, reconcileWateringMark(true, 20000, 20365)), "no repeat on the day of the correction");
static_assert(wateringDayAllowed(true, 20001, reconcileWateringMark(true, 20000, 20365)), "watering resumes the next day");
static_assert(reconcileWateringMark(true, 20000, 19999) == 19999, "a normal mark is left alone");
static_assert(reconcileWateringMark(false, 20000, 20365) == 20365, "an untrusted clock must not rewrite the mark");

// Schedule 08:00 = second 28800 of the day, catch-up window 4 h.
static_assert(wateringDue(28800, 28800, 0, 14400), "starts at the scheduled second");
static_assert(wateringDue(28859, 28800, 0, 14400), "still starts at the end of the scheduled minute");
static_assert(!wateringDue(28799, 28800, 0, 14400), "never starts early");
static_assert(wateringDue(36000, 28800, 0, 14400), "catches up after a two-hour outage");
static_assert(!wateringDue(43201, 28800, 0, 14400), "gives up once the window has passed");
static_assert(!wateringDue(36000, 28800, 36000, 14400), "saving settings two hours later must not start the pump");
static_assert(wateringDue(28800, 28800, 28700, 14400), "a schedule saved shortly before its time still runs");

constexpr bool debounceRegression() {
  Debounce level{true, true, 0};
  debounceUpdate(level, false, 100, 2000);          // The float dips for the first time.
  if (!level.stable) return false;
  debounceUpdate(level, false, 1500, 2000);
  if (!level.stable) return false;                  // Not long enough yet.
  debounceUpdate(level, true, 1600, 2000);          // It bobs back: a slosh, not an empty tank.
  debounceUpdate(level, false, 1700, 2000);
  debounceUpdate(level, false, 3600, 2000);
  if (!level.stable) return false;                  // The hold restarted at 1700.
  debounceUpdate(level, false, 3700, 2000);
  if (level.stable) return false;                   // Held low for 2 s: the tank is empty.
  debounceUpdate(level, true, 0xfffffff0, 2000);    // Refilled just before millis() wraps.
  debounceUpdate(level, true, 2000, 2000);
  return level.stable;
}
static_assert(debounceRegression(), "float switch debounce must ignore sloshing and survive millis wrap");

constexpr uint8_t kSensirionCrcExample[2] = {0xBE, 0xEF};
static_assert(sensirionCrc8(kSensirionCrcExample, 2) == 0x92, "SHT4x CRC must match the datasheet example");

constexpr bool backupRegression() {
  uint32_t anchor = 21600000, timestamp = 1775000000;
  advanceBackupClock(21601500, anchor, timestamp);
  if (timestamp != 1775000001 || anchor != 21601000) return false;
  advanceBackupClock(21602000, anchor, timestamp);
  return timestamp == 1775000002 && anchor == 21602000;
}
static_assert(backupRegression(), "fallback must retain fractional seconds without adding uptime");
constexpr bool backupWrapRegression() {
  uint32_t anchor = 0xffffff00, timestamp = 1775000000;
  advanceBackupClock(744, anchor, timestamp);
  return timestamp == 1775000001 && anchor == 744;
}
static_assert(backupWrapRegression(), "fallback clock must survive millis wrap");

constexpr bool mcp7940nRegression() {
  // 2026-10-21 23:59:59, ST=1, OSCRUN=1, VBATEN=1, weekday=4.
  uint8_t raw[7] = {0xd9, 0x59, 0x23, 0x2c, 0x21, 0x10, 0x26};
  RtcFields result{};
  if (!decodeMcp7940n(raw, result) || result.year != 2026 || result.month != 10 || result.day != 21 ||
      result.hour != 23 || result.minute != 59 || result.second != 59) return false;
  raw[0] &= ~MCP7940N_ST;
  if (decodeMcp7940n(raw, result)) return false; // Plausible date but oscillator disabled.
  raw[0] |= MCP7940N_ST;
  raw[3] &= ~MCP7940N_OSCRUN;
  if (decodeMcp7940n(raw, result)) return false; // ST alone does not prove the crystal runs.
  raw[3] |= MCP7940N_OSCRUN;
  raw[3] &= ~MCP7940N_VBATEN;
  if (decodeMcp7940n(raw, result)) return false;
  raw[3] |= MCP7940N_VBATEN | 0x10;
  if (!decodeMcp7940n(raw, result)) return false; // PWRFAIL after good battery backup is OK.
  raw[2] = 0x63;
  if (decodeMcp7940n(raw, result)) return false; // Do not silently accept 12-hour mode.
  raw[2] = 0x23;
  for (int i = 0; i < 7; ++i) {
    const uint8_t saved = raw[i];
    raw[i] = (raw[i] & 0xf0) | (i == 3 ? 0x08 : 0x0a);
    if (decodeMcp7940n(raw, result)) return false; // Bad BCD in each field, or weekday zero.
    raw[i] = saved;
  }
  raw[0] = 0xe0;
  if (decodeMcp7940n(raw, result)) return false; // Seconds 60.
  raw[0] = 0xd9; raw[1] = 0x60;
  if (decodeMcp7940n(raw, result)) return false;
  raw[1] = 0x59; raw[2] = 0x24;
  if (decodeMcp7940n(raw, result)) return false;
  raw[2] = 0x23; raw[4] = 0x31; raw[5] = 0x04;
  if (decodeMcp7940n(raw, result)) return false; // 31 April.
  raw[4] = 0x29; raw[5] = 0x22; raw[6] = 0x24;
  if (!decodeMcp7940n(raw, result) || result.month != 2) return false; // LPYR is not month BCD.
  raw[6] = 0x25;
  if (decodeMcp7940n(raw, result)) return false; // Non-leap year.
  raw[4] = 0x01; raw[5] = 0x01; raw[6] = 0x00;
  return !decodeMcp7940n(raw, result); // Factory reset year is not trusted.
}
static_assert(mcp7940nRegression(), "validate MCP7940N ST/OSCRUN/VBATEN, BCD, 24h and calendar");

constexpr bool mcp7940nWriteRegression() {
  const RtcFields dates[] = {{2024, 2, 29, 0, 0, 0}, {2026, 10, 21, 12, 34, 56}, {2099, 12, 31, 23, 59, 59}};
  for (const RtcFields &date : dates) {
    for (uint8_t weekday = 1; weekday <= 7; ++weekday) {
      uint8_t raw[7]{};
      if (!encodeMcp7940n(date, weekday, raw) || !(raw[0] & MCP7940N_ST) ||
          raw[3] != (weekday | MCP7940N_VBATEN) || (raw[2] & 0xc0)) return false;
      raw[3] |= MCP7940N_OSCRUN; // Read-only flag set by the chip after starting.
      RtcFields read{};
      if (!decodeMcp7940n(raw, read) || read.year != date.year || read.month != date.month ||
          read.day != date.day || read.hour != date.hour || read.minute != date.minute || read.second != date.second) return false;
    }
  }
  uint8_t raw[7]{};
  return !encodeMcp7940n(dates[0], 0, raw) && !encodeMcp7940n(dates[0], 8, raw) &&
         !encodeMcp7940n({2025, 2, 29, 0, 0, 0}, 1, raw);
}
static_assert(mcp7940nWriteRegression(), "MCP7940N writes set ST/VBATEN and round-trip all calendar fields");

constexpr bool ledPulseRegression() {
  for (int request = -5; request <= 260; ++request) {
    const int duty = ledDutyForRequest(request);
    if (duty < 0 || duty > PWM_MAX || (request <= 0 && duty != 0)) return false;
    // Every nonzero HIGH pulse and every LOW gap below full brightness lasts at least 16 us (5 counts on Lite).
    if (request > 0 && uint64_t(duty) * 1000000 < uint64_t(LED_MIN_PULSE_US) * LED_PWM_FREQ * PWM_COUNTS) return false;
    if (duty > 0 && duty < PWM_MAX &&
        uint64_t(PWM_COUNTS - duty) * 1000000 < uint64_t(LED_MIN_PULSE_US) * LED_PWM_FREQ * PWM_COUNTS) return false;
    if (request >= LED_MIN_DUTY && request <= PWM_MAX - LED_MIN_DUTY + 1 && duty != request) return false;
  }
  return ledDutyForRequest(255) == 255 && ledDutyForRequest(254) == 255 && ledDutyForRequest(253) == 255 && ledDutyForRequest(252) == 255 && ledDutyForRequest(251) == 251 &&
         ledDutyForRequest(1) == LED_MIN_DUTY;
}
static_assert(LED_PWM_FREQ >= 500 && LED_PWM_FREQ <= 1000 && ledPulseRegression(),
              "every LED pulse and every gap below full brightness must be at least 16 us (5 counts on Lite)");
static_assert(FAN_PWM_FREQ == 25000 && fanGpioDuty(255) == 0 && fanGpioDuty(0) == 255 &&
              fanGpioDuty(128) == 127 && fanGpioDuty(-1) == 255 && fanGpioDuty(256) == 0,
              "fan GPIO duty must invert logical airflow, including endpoint saturation");
// Core 3.3.x shares an LEDC timer between channels with the same frequency and resolution.
static_assert(LED_PWM_FREQ != FAN_PWM_FREQ, "LED nDIM and fans must never share an LEDC timer");

constexpr bool settingFloatRegression() {
  const float nan = __builtin_nanf(""), inf = __builtin_inff();
  return !finiteFloat(nan) && !finiteFloat(inf) && !finiteFloat(-inf) && finiteFloat(0.0f) && finiteFloat(-20.5f) &&
         clampSettingFloat(nan, 0, 50, 25) == 25 && clampSettingFloat(inf, 0, 50, 25) == 25 &&
         clampSettingFloat(-inf, 0, 50, 25) == 25 && clampSettingFloat(60, 0, 50, 25) == 50 &&
         clampSettingFloat(-1, 0, 50, 25) == 0 && clampSettingFloat(22.5f, 0, 50, 25) == 22.5f;
}
static_assert(settingFloatRegression(), "NaN/inf settings must fall back to the default, numbers are clamped");

constexpr bool heaterRegression() {
  const float nan = __builtin_nanf("");
  // Normal hysteresis: on at/below 23, off at/above 25, unchanged in between.
  if (!heaterNextState(false, true, 23.0f, 23.0f, 25.0f, HEATER_HARD_LIMIT_C)) return false;
  if (heaterNextState(false, true, 24.0f, 23.0f, 25.0f, HEATER_HARD_LIMIT_C)) return false;
  if (!heaterNextState(true, true, 24.9f, 23.0f, 25.0f, HEATER_HARD_LIMIT_C)) return false;
  if (heaterNextState(true, true, 25.0f, 23.0f, 25.0f, HEATER_HARD_LIMIT_C)) return false;
  if (heaterNextState(true, false, 10.0f, 23.0f, 25.0f, HEATER_HARD_LIMIT_C)) return false; // Schedule.
  // A NaN threshold or reading used to freeze the heater in its current state.
  if (heaterNextState(true, true, 30.0f, nan, 25.0f, HEATER_HARD_LIMIT_C)) return false;
  if (heaterNextState(true, true, 30.0f, 23.0f, nan, HEATER_HARD_LIMIT_C)) return false;
  if (heaterNextState(true, true, nan, 23.0f, 25.0f, HEATER_HARD_LIMIT_C)) return false;
  // Hard limit wins over any setting, e.g. a Drying off-threshold of 70 C.
  if (heaterNextState(true, true, 60.0f, 49.0f, 70.0f, HEATER_HARD_LIMIT_C)) return false;
  return heaterNextState(true, true, 59.9f, 49.0f, 70.0f, HEATER_HARD_LIMIT_C);
}
static_assert(HEATER_HARD_LIMIT_C <= 60.0f && heaterRegression(), "PTC thermostat: hysteresis, schedule, NaN and hard limit");

constexpr bool ina238Regression() {
  // 31.5 mV / 3.5 mOhm = 9 A; 24 V / 3.125 mV = 7680 counts.
  return ina238Amps(25200, 1.0f) > 8.999f && ina238Amps(25200, 1.0f) < 9.001f &&
         ina238Amps(-2800, 1.0f) < -0.999f && ina238Amps(-2800, 1.0f) > -1.001f &&
         ina238Amps(14000, 1.05f) > 5.249f && ina238Amps(14000, 1.05f) < 5.251f &&
         ina238Volts(7680) == 24.0f && ina238Volts(27200) == 85.0f;
}
static_assert(ina238Regression(), "INA238 raw voltage conversion, 3.5 mOhm low-side shunt");
static_assert(LED_MIN_DUTY == 5 && ledDutyForRequest(1) == 5 && ledDutyForRequest(4) == 5,
              "69 W LED startup requires at least 5 duty counts");
static_assert(!ledSupplyNext(false, false, 24) && !ledSupplyNext(true, false, 24) &&
              !ledSupplyNext(true, true, 21.59f) && ledSupplyNext(true, true, 21.6f) &&
              !ledSupplyNext(false, true, 22.49f) && ledSupplyNext(false, true, 22.5f) &&
              !ledSupplyNext(true, true, __builtin_nanf("")), "LED supply cutoff, hysteresis and invalid data");
// Решение о свете: измеренное напряжение главнее всего; одиночный сбой чтения держит прежнее решение;
// молчащий измеритель не гасит свет, пока есть время. Датчик SHT4x в решении не участвует: при его
// отказе свет идёт на минимальном уровне по аварийному режиму климата.
static_assert(!ledSupplyAllowed(true, true, 21.59f, true, true) &&             // измерено низкое: выключить, даже при часах
              !ledSupplyAllowed(true, true, 21.59f, false, true) &&
              ledSupplyAllowed(true, true, 21.6f, true, false) &&              // гистерезис и пороги те же, что у ledSupplyNext
              !ledSupplyAllowed(false, true, 22.49f, true, true) &&
              ledSupplyAllowed(false, true, 22.5f, true, false) &&
              !ledSupplyAllowed(true, true, __builtin_nanf(""), true, true),
              "a measured low supply must cut the LED whatever the clock says");
static_assert(ledSupplyAllowed(true, false, 0, true, false) &&                 // измеритель на связи, отсчёта нет: решение держится
              !ledSupplyAllowed(false, false, 0, true, true),                  // и холодный пуск ждёт первого измерения
              "a single missed INA238 sample must not change the LED decision");
static_assert(ledSupplyAllowed(false, false, 0, false, true) &&                // измерителя нет: светим, пока есть время
              ledSupplyAllowed(true, false, 0, false, true) &&
              !ledSupplyAllowed(true, false, 0, false, false) &&               // времени нет: день или ночь неизвестно
              !ledSupplyAllowed(false, false, 0, false, false),
              "without a power meter the LED runs whenever the clock is usable");
// Порядок приоритетов: датчик/часы, просадка шины, затем SD и данные питания.
static_assert(statusColor(false, true, true, false, false) == StatusColor::Red &&
              statusColor(true, false, false, true, true) == StatusColor::Red &&
              statusColor(true, true, true, false, false) == StatusColor::Purple,
              "Heavier status LED faults take priority over lighter ones");
// Жёлтый — пропала SD или данные INA238; зелёный — исправная плата.
static_assert(statusColor(true, true, false, false, true) == StatusColor::Yellow &&
              statusColor(true, true, false, true, false) == StatusColor::Yellow &&
              statusColor(true, true, false, true, true) == StatusColor::Green,
              "Yellow marks missing SD or power data, green marks a healthy board");

// --- Отправка телеметрии: выбор порции журнала и проверка настроек ---------------------------
constexpr bool logChunkRegression() {
  // Обычный случай: сервер принял всё до sent, дальше две целые строки и обрывок третьей.
  constexpr char text[] = "one\ntwo\nthr";
  LogChunk c = chooseLogChunk(text, 11, 0, 0, 100);
  if (c.from != 0 || c.len != 8) return false;                       // "one\ntwo\n", обрывок остаётся
  c = chooseLogChunk(text, 11, 0, 4, 100);
  if (c.from != 4 || c.len != 4) return false;                       // только "two\n"
  c = chooseLogChunk(text, 11, 0, 8, 100);
  if (c.len != 0) return false;                                      // целой строки пока нет
  c = chooseLogChunk(text, 11, 0, 11, 100);
  if (c.len != 0) return false;                                      // всё отправлено
  c = chooseLogChunk(text, 11, 0, 0, 6);
  if (c.from != 0 || c.len != 4) return false;                       // порция режется по концу строки
  // Кольцевой буфер затёр начало: окно с позиции 100 начинается с обрывка, берём первую целую строку.
  constexpr char lost[] = "ail\nnext\nmore";
  c = chooseLogChunk(lost, 13, 100, 0, 100);
  if (c.from != 104 || c.len != 5) return false;                     // "next\n"; "more" без перевода строки не уходит
  c = chooseLogChunk("no newline here", 15, 100, 0, 100);
  if (c.len != 0) return false;
  // Отправленное раньше начала окна и отправленное давно дают одну и ту же порцию после затирания.
  c = chooseLogChunk(lost, 13, 100, 50, 100);
  return c.from == 104 && c.len == 5;
}
static_assert(logChunkRegression(), "log chunk must start and end on line boundaries and survive buffer overwrite");

constexpr bool logChunkUtf8Regression() {
  // Строка из пяти букв «я» (по два байта) длиннее порции в 5 байт: режем между буквами.
  constexpr char text[] = {char(0xD1), char(0x8F), char(0xD1), char(0x8F), char(0xD1), char(0x8F),
                           char(0xD1), char(0x8F), char(0xD1), char(0x8F)};
  const LogChunk c = chooseLogChunk(text, 10, 0, 0, 5);
  if (c.from != 0 || c.len != 4) return false;
  // Окно, начавшееся посреди буквы после затирания, пропускает обрывок вместе с хвостом строки.
  constexpr char torn[] = {char(0x8F), 'a', '\n', char(0xD1), char(0x8F), '\n'};
  const LogChunk d = chooseLogChunk(torn, 6, 10, 0, 100);
  return d.from == 13 && d.len == 3 && uint8_t(torn[d.from - 10]) == 0xD1;
}
static_assert(logChunkUtf8Regression(), "log chunk must never split a UTF-8 character");

static_assert(deviceLabelValid("microgarden-v1") && deviceLabelValid("a") && deviceLabelValid("Box_1.2-b") &&
              !deviceLabelValid("") && !deviceLabelValid("two words") && !deviceLabelValid("box/1") &&
              !deviceLabelValid("\xD0\xBA\xD0\xB8\xD1\x80") && !deviceLabelValid("0123456789012345678901234567890123"),
              "device label must satisfy the server rule: 1-32 characters of [A-Za-z0-9_.-]");
static_assert(telemetryUrlValid("https://dash.dvinsk.lat/api/ingest") && !telemetryUrlValid("http://dash.dvinsk.lat/api/ingest") &&
              !telemetryUrlValid("https://") && !telemetryUrlValid("https://a b.lat/x") && !telemetryUrlValid(""),
              "telemetry URL must be https without spaces");
static_assert(telemetryTokenValid("0123456789abcdef0123456789abcdef") && !telemetryTokenValid("short") &&
              !telemetryTokenValid("0123456789abcdef01234567 9abcdef"),
              "telemetry token must be 24-128 printable characters without spaces");
static_assert(telemetryReportDue(0, 200, 5000, 0, TELEMETRY_REPORT_REPEAT_MS) &&      // первая успешная отправка
              !telemetryReportDue(200, 200, 999999, 0, TELEMETRY_REPORT_REPEAT_MS) &&  // успех не повторяется
              telemetryReportDue(200, -1, 5000, 0, TELEMETRY_REPORT_REPEAT_MS) &&      // отказ после успеха
              !telemetryReportDue(-1, -1, 60000, 0, TELEMETRY_REPORT_REPEAT_MS) &&     // тот же отказ молчит
              telemetryReportDue(-1, -1, TELEMETRY_REPORT_REPEAT_MS, 0, TELEMETRY_REPORT_REPEAT_MS) &&
              telemetryReportDue(-1, 401, 60000, 0, TELEMETRY_REPORT_REPEAT_MS),      // код изменился
              "telemetry results must be logged on change, not every minute");

// --- RGB-индикатор: яркость и ночной режим ---------------------------------------------------
static_assert(rgbDutyForPercent(RGB_BRIGHTNESS_DEFAULT_PCT) == RGB_DUTY,
              "default brightness must equal the previous fixed RGB_DUTY");
static_assert(rgbDutyForPercent(0) == 0 && rgbDutyForPercent(-5) == 0 && rgbDutyForPercent(1) == 3 &&
              rgbDutyForPercent(50) == 128 && rgbDutyForPercent(100) == 255 && rgbDutyForPercent(500) == 255,
              "brightness percent must map to 0..255 and saturate at both ends");
static_assert(rgbVisibleColor(StatusColor::Green, true, RGB_NIGHT_OFF) == StatusColor::Green &&        // днём ночной режим не действует
              rgbVisibleColor(StatusColor::Red, true, RGB_NIGHT_OFF) == StatusColor::Red &&
              rgbVisibleColor(StatusColor::Green, false, RGB_NIGHT_SAME) == StatusColor::Green &&      // «как днём»
              rgbVisibleColor(StatusColor::Red, false, RGB_NIGHT_SAME) == StatusColor::Red &&
              rgbVisibleColor(StatusColor::Green, false, 7) == StatusColor::Green,                     // неизвестный режим не гасит индикатор
              "the night mode must not change anything by day or in 'same as day' mode");
static_assert(rgbVisibleColor(StatusColor::Green, false, RGB_NIGHT_FAULTS_ONLY) == StatusColor::Off && // норма ночью не светит
              rgbVisibleColor(StatusColor::Red, false, RGB_NIGHT_FAULTS_ONLY) == StatusColor::Red &&   // неисправности видны
              rgbVisibleColor(StatusColor::Purple, false, RGB_NIGHT_FAULTS_ONLY) == StatusColor::Purple &&
              rgbVisibleColor(StatusColor::Yellow, false, RGB_NIGHT_FAULTS_ONLY) == StatusColor::Yellow,
              "'faults only' must hide the healthy colour at night and keep every fault visible");
static_assert(rgbVisibleColor(StatusColor::Green, false, RGB_NIGHT_OFF) == StatusColor::Off &&
              rgbVisibleColor(StatusColor::Red, false, RGB_NIGHT_OFF) == StatusColor::Off,
              "'off' must switch the indicator off at night");

// --- Суточная наработка нагревателя --------------------------------------------------------------
constexpr bool dailyRuntimeRegression() {
  DailyRuntime r{0, 0};
  r = dailyRuntimeStep(r, 20000, true, 1500);     // первый шаг: сутки начались, нагреватель включён
  r = dailyRuntimeStep(r, 20000, true, 500);
  r = dailyRuntimeStep(r, 20000, false, 9000);    // выключенный нагреватель время не копит
  if (r.day != 20000 || r.ms != 2000) return false;
  r = dailyRuntimeStep(r, 20001, true, 700);      // полночь платы: счёт заново, шаг уже новых суток
  if (r.day != 20001 || r.ms != 700) return false;
  r = dailyRuntimeStep(r, 20001, false, 100);
  r = dailyRuntimeStep(r, 19999, false, 100);     // часы переведены назад: тоже новые сутки, счёт заново
  return r.day == 19999 && r.ms == 0;
}
static_assert(dailyRuntimeRegression(), "heater run time must add up within a board day and restart on a new day");

