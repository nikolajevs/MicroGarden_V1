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
// гасится, пока плата знает время и датчик SHT4x показывает климат. Это решение владельца
// (27.10.2026): отказ измерителя не должен оставлять растения без света. Защиты от просадки
// питания в этом состоянии нет, поэтому плата пишет об этом в журнал.
constexpr bool ledSupplyAllowed(bool allowed, bool valid, float volts, bool meterOnline,
                                bool clockTrusted, bool climateOk) {
  return valid ? ledSupplyNext(allowed, true, volts)
       : meterOnline ? allowed
       : clockTrusted && climateOk;
}

// Индикатор показывает только состояние платы. Каждый канал либо полностью
// включён, либо выключен: промежуточных яркостей и смешанных оттенков нет,
// поэтому цвета различимы при любом освещении.
// Вспышки процессов (помпа, нагреватель, отправка телеметрии) и отсутствие связи
// с роутером индикатором не показываются — это видно в веб-интерфейсе, а
// индикатор отвечает за неисправности самой платы. Ровного состояния нет
// только у Blue: его зажигает setup() на время инициализации.
enum class StatusColor : uint8_t { Off, Red, Purple, Blue, Yellow, Green };

constexpr StatusColor statusColor(bool climateOk, bool clockOk, bool busLow,
                                  bool sdOk, bool powerOk) {
  // Более тяжёлая неисправность перекрывает более лёгкую.
  return !climateOk || !clockOk ? StatusColor::Red
       : busLow ? StatusColor::Purple
       : !sdOk || !powerOk ? StatusColor::Yellow
       : StatusColor::Green;
}
