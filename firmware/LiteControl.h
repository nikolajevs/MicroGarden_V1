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

// День ли сейчас по расписанию света (минуты от полуночи; включение == выключению — весь день).
// Без времени, которому можно верить (RTC или программный таймер, выставленный из RTC или владельцем),
// день не наступает: плата стартует с выдуманной даты, и свет горел бы в случайные часы. Решение
// владельца от 28.10.2026: свету достаточно хотя бы введённого вручную времени, но не выдуманного.
constexpr bool lightScheduleDay(bool clockUsable, int nowMin, int onMin, int offMin) {
  return clockUsable && (onMin == offMin || (onMin < offMin ? nowMin >= onMin && nowMin < offMin
                                                            : nowMin >= onMin || nowMin < offMin));
}

// Линейный выход между двумя порогами: до low включительно — atLow, от high — atHigh, между ними —
// по прямой. Нечисловое значение даёт atLow; при high <= low получается ступенька без деления на ноль.
constexpr int rampPwm(float value, float low, float high, int atLow, int atHigh) {
  return !(value > low) ? atLow
       : !(value < high) ? atHigh
       : int(atLow + (value - low) * float(atHigh - atLow) / (high - low));
}

// Лампа днём в Growing. Ниже цели она всегда на максимуме: недобор тепла покрывает нагреватель,
// а не приглушение света (решение владельца от 28.10.2026). Выше цели лампа плавно уходит
// к минимуму и достигает его при «цель + дельта», чтобы не перегреть растения.
constexpr int growLampPwm(float temp, float target, float delta, int pwmMin, int pwmMax) {
  return rampPwm(temp, target, target + delta, pwmMax, pwmMin);
}

// Вентилятор в Growing. По температуре: до цели — минимум, к «цель + дельта» — максимум. Нагреватель
// выключается на цели, поэтому вентиляторы не выдувают его тепло ни во время нагрева, ни сразу после.
// Ночью (watchHumidity) — ещё и по влажности: выше humTarget разгон, к humTarget + humBand (не выше
// 100 %) максимум, в том числе пока греет нагреватель: сырой воздух всё равно нужно сменить, а нагрев
// сам снижает относительную влажность. humTarget = 100 — влажность не учитывается. Из двух требований
// берётся большее. Днём влажность не учитывается: при работающей лампе проблем с ней нет.
constexpr int growFanPwm(float temp, float tempTarget, float tempDelta, bool watchHumidity,
                         float hum, float humTarget, float humBand, int pwmMin, int pwmMax) {
  const int byTemp = rampPwm(temp, tempTarget, tempTarget + tempDelta, pwmMin, pwmMax);
  const float humTop = humTarget + humBand < 100.0f ? humTarget + humBand : 100.0f;
  const int byHum = watchHumidity ? rampPwm(hum, humTarget, humTop, pwmMin, pwmMax) : pwmMin;
  return byTemp > byHum ? byTemp : byHum;
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

// Состояние подключения к WiFi роутера для страницы настроек. Приоритет: сеть не задана, подключено,
// идёт попытка, нет связи. Номера используются как индексы в таблице имён в firmware.ino.
enum class WifiState : uint8_t { NotConfigured = 0, Connecting = 1, Connected = 2, Disconnected = 3 };

constexpr WifiState wifiState(bool configured, bool connected, bool connecting) {
  return !configured ? WifiState::NotConfigured
       : connected ? WifiState::Connected
       : connecting ? WifiState::Connecting
       : WifiState::Disconnected;
}
