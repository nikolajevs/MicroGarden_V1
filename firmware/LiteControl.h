#pragma once
#include "Board.h"
#include "Safety.h"

constexpr bool ledSupplyNext(bool allowed, bool valid, float volts) {
  return valid && finiteFloat(volts) && volts >= (allowed ? PSU_LOW_VOLTAGE_V : PSU_RECOVER_VOLTAGE_V);
}

// Индикатор показывает только состояние платы. Каждый канал либо полностью
// включён, либо выключен: промежуточных яркостей и смешанных оттенков нет,
// поэтому цвета различимы при любом освещении.
// Вспышки процессов (помпа, нагреватель, отправка телеметрии) и отсутствие связи
// с роутером индикатором не показываются — это видно в веб-интерфейсе, а
// индикатор отвечает за неисправности самой платы. Ровного состояния нет
// только у Blue: его зажигает setup() на время инициализации.
enum class StatusColor : uint8_t { Off, Red, Purple, Blue, Yellow, Green };

// Яркость индикатора задаётся в процентах (0 — выключен). 19 % = 48 из 255: прежняя постоянная
// RGB_DUTY, поэтому до первой настройки индикатор светит так же, как раньше.
constexpr int RGB_BRIGHTNESS_DEFAULT_PCT = 19;

constexpr uint8_t rgbDutyForPercent(int pct) {
  return pct <= 0 ? 0 : pct >= 100 ? 255 : uint8_t((pct * 255 + 50) / 100);
}

// Что делает индикатор в тёмный период растений (вне расписания света).
enum RgbNightMode : int { RGB_NIGHT_SAME = 0, RGB_NIGHT_FAULTS_ONLY = 1, RGB_NIGHT_OFF = 2 };

constexpr bool statusIsFault(StatusColor color) {
  return color == StatusColor::Red || color == StatusColor::Purple || color == StatusColor::Yellow;
}

// Цвет с учётом ночного режима. Днём и при неизвестном режиме цвет не меняется: индикатор,
// который почему-то молчит, хуже лишнего огонька. Режим «только неисправности» гасит зелёный
// (норма), а красный, пурпурный и жёлтый оставляет.
constexpr StatusColor rgbVisibleColor(StatusColor color, bool isDay, int nightMode) {
  return (!isDay && (nightMode == RGB_NIGHT_OFF || (nightMode == RGB_NIGHT_FAULTS_ONLY && !statusIsFault(color))))
       ? StatusColor::Off : color;
}

constexpr StatusColor statusColor(bool climateOk, bool clockOk, bool busLow,
                                  bool sdOk, bool powerOk) {
  // Более тяжёлая неисправность перекрывает более лёгкую.
  return !climateOk || !clockOk ? StatusColor::Red
       : busLow ? StatusColor::Purple
       : !sdOk || !powerOk ? StatusColor::Yellow
       : StatusColor::Green;
}
