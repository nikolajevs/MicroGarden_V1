#pragma once
#include <stdint.h>

// Shared by firmware and host regression tests. All durations use modulo-2^32 ms.
constexpr uint32_t elapsedMs(uint32_t now, uint32_t start) { return now - start; }

constexpr void advanceBackupClock(uint32_t now, uint32_t &anchor, uint32_t &unixTime) {
  const uint32_t seconds = elapsedMs(now, anchor) / 1000;
  unixTime += seconds;
  anchor += seconds * 1000;
}

constexpr bool validCalendar(int year, int month, int day, int hour, int minute, int second) {
  if (year < 2024 || year > 2099 || month < 1 || month > 12 ||
      hour < 0 || hour > 23 || minute < 0 || minute > 59 || second < 0 || second > 59) return false;
  int maximum = month == 2 ? 28 : (month == 4 || month == 6 || month == 9 || month == 11 ? 30 : 31);
  if (month == 2 && year % 4 == 0) ++maximum;
  return day >= 1 && day <= maximum;
}

// NaN and infinity never compare as out of range, so an ordinary clamp lets them through,
// and String::toFloat() accepts "nan" and "inf". Comparisons only, so it works in constexpr.
constexpr bool finiteFloat(float v) { return v == v && v <= 3.402823466e+38f && v >= -3.402823466e+38f; }

constexpr float clampSettingFloat(float v, float lo, float hi, float fallback) {
  return !finiteFloat(v) ? fallback : (v < lo ? lo : (v > hi ? hi : v));
}

// One thermostat step for the 100 W PTC. Off when the schedule forbids heating, when any
// input is not a finite number, or at/above the settings-proof hard limit. Otherwise it
// switches on at/below onBelow and off at/above offAbove, with hysteresis in between.
constexpr bool heaterNextState(bool active, bool scheduleOk, float temp, float onBelow, float offAbove, float hardLimit) {
  if (!scheduleOk || !finiteFloat(temp) || !finiteFloat(onBelow) || !finiteFloat(offAbove) || !(temp < hardLimit)) return false;
  return active ? temp < offAbove : temp <= onBelow;
}

constexpr bool wateringDayAllowed(bool clockTrusted, uint32_t today, uint32_t lastDay) {
  // Also suppress duplicate watering after moving the date backwards.
  return clockTrusted && today > lastDay;
}

// A mark written by a wrong (future) clock must not block watering for months once the clock
// is corrected: pull it back to today. Today stays blocked, tomorrow waters as usual.
constexpr uint32_t reconcileWateringMark(bool clockTrusted, uint32_t today, uint32_t lastDay) {
  return clockTrusted && lastDay > today ? today : lastDay;
}

// A scheduled watering may start late (the board was off or rebooting during that minute),
// but only within windowSec and never for an occurrence older than armedFrom — the moment
// the user last changed the schedule, the mode or the clock. Otherwise saving settings in
// the afternoon would start the pump at once for a time that passed in the morning.
constexpr bool wateringDue(uint32_t now, uint32_t scheduled, uint32_t armedFrom, uint32_t windowSec) {
  return now >= scheduled && now - scheduled <= windowSec && scheduled >= armedFrom;
}

// Float switch debounce: the stable level follows the raw input only after it has held the
// new level for holdMs. Water sloshing while the pump runs must not look like an empty tank.
struct Debounce { bool stable; bool candidate; uint32_t sinceMs; };

constexpr void debounceUpdate(Debounce &d, bool raw, uint32_t nowMs, uint32_t holdMs) {
  if (raw != d.candidate) {
    d.candidate = raw;
    d.sinceMs = nowMs;
  } else if (d.stable != d.candidate && elapsedMs(nowMs, d.sinceMs) >= holdMs) {
    d.stable = d.candidate;
  }
}

// Sensirion CRC-8 (SHT4x datasheet: polynomial 0x31, init 0xFF).
constexpr uint8_t sensirionCrc8(const uint8_t *data, int len) {
  uint8_t crc = 0xFF;
  for (int i = 0; i < len; ++i) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; ++bit) crc = (crc & 0x80) ? uint8_t((crc << 1) ^ 0x31) : uint8_t(crc << 1);
  }
  return crc;
}

struct RtcFields { int year, month, day, hour, minute, second; };

constexpr uint8_t MCP7940N_ADDR = 0x6f;
constexpr uint8_t MCP7940N_ST = 0x80;
constexpr uint8_t MCP7940N_OSCRUN = 0x20;
constexpr uint8_t MCP7940N_VBATEN = 0x08;

// MCP7940N 00h..06h: seconds/ST, minutes, hours, weekday/status, date, month/LPYR, year.
// PWRFAIL is normal after running on a good battery; it is not an oscillator-stop flag.
// Accept only the board's 24-hour format and an enabled, running, battery-backed clock.
constexpr bool decodeMcp7940n(const uint8_t (&raw)[7], RtcFields &out) {
  if (!(raw[0] & MCP7940N_ST) || !(raw[3] & MCP7940N_OSCRUN) || !(raw[3] & MCP7940N_VBATEN)) return false;
  if ((raw[1] & 0x80) || (raw[2] & 0xc0) || (raw[3] & 0xc0) ||
      (raw[4] & 0xc0) || (raw[5] & 0xc0)) return false;
  const uint8_t mask[7] = {0x7f, 0x7f, 0x3f, 0x07, 0x3f, 0x1f, 0xff};
  int value[7] = {};
  for (int i = 0; i < 7; ++i) {
    const uint8_t bcd = raw[i] & mask[i];
    if ((bcd & 15) > 9 || (bcd >> 4) > 9) return false;
    value[i] = (bcd >> 4) * 10 + (bcd & 15);
  }
  if (value[3] < 1 || value[3] > 7 ||
      !validCalendar(2000 + value[6], value[5], value[4], value[2], value[1], value[0])) return false;
  out = {2000 + value[6], value[5], value[4], value[2], value[1], value[0]};
  return true;
}

constexpr uint8_t rtcBcd(int value) { return uint8_t((value / 10) * 16 + value % 10); }

// Final register image. The writer clears ST while loading the calendar, then sets it last.
constexpr bool encodeMcp7940n(const RtcFields &time, uint8_t weekday, uint8_t (&raw)[7]) {
  if (!validCalendar(time.year, time.month, time.day, time.hour, time.minute, time.second) ||
      weekday < 1 || weekday > 7) return false;
  raw[0] = rtcBcd(time.second) | MCP7940N_ST;
  raw[1] = rtcBcd(time.minute);
  raw[2] = rtcBcd(time.hour); // Bit 6 = 0: 24-hour mode.
  raw[3] = weekday | MCP7940N_VBATEN;
  raw[4] = rtcBcd(time.day);
  raw[5] = rtcBcd(time.month);
  raw[6] = rtcBcd(time.year - 2000);
  return true;
}

// Суточная наработка устройства (нагревателя): сколько миллисекунд оно было включено в текущие сутки
// платы. Сутки — код дня (unixtime / 86400) часов платы; при смене кода счёт начинается заново.
struct DailyRuntime { uint32_t day; uint32_t ms; };

constexpr DailyRuntime dailyRuntimeStep(DailyRuntime state, uint32_t today, bool active, uint32_t dtMs) {
  if (today != state.day) state = {today, 0};
  if (active) state.ms += dtMs;
  return state;
}
