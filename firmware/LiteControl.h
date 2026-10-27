#pragma once
#include "Board.h"
#include "Safety.h"

constexpr bool ledSupplyNext(bool allowed, bool valid, float volts) {
  return valid && finiteFloat(volts) && volts >= (allowed ? PSU_LOW_VOLTAGE_V : PSU_RECOVER_VOLTAGE_V);
}

// Разрешено ли светить. Пока измеритель INA238 даёт достоверное напряжение, действуют пороги
// с гистерезисом: ниже 21,6 В свет запрещён, снова разрешён от 22,5 В.
// Одиночный сбой чтения (измеритель на связи, отсчёта нет) прежнее решение не меняет: свет не мигает.
// Если измеритель не отвечает совсем (не впаян, отказал, три чтения подряд неудачны), свет не
// гасится, пока плата знает время (иначе неизвестно, день сейчас или ночь). Это решение владельца
// (27.10.2026): отказ измерителя не должен оставлять растения без света. Датчик SHT4x условием не
// является: если он тоже молчит, свет идёт на минимальном уровне по аварийному режиму климата
// (решение от 28.10.2026). Защиты от просадки питания в этом состоянии нет, поэтому плата пишет
// об этом в журнал.
constexpr bool ledSupplyAllowed(bool allowed, bool valid, float volts, bool meterOnline,
                                bool clockUsable) {
  return valid ? ledSupplyNext(allowed, true, volts)
       : meterOnline ? allowed
       : clockUsable;
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
