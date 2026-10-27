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

constexpr StatusColor statusColor(bool climateOk, bool clockOk, bool busLow,
                                  bool sdOk, bool powerOk) {
  // Более тяжёлая неисправность перекрывает более лёгкую.
  return !climateOk || !clockOk ? StatusColor::Red
       : busLow ? StatusColor::Purple
       : !sdOk || !powerOk ? StatusColor::Yellow
       : StatusColor::Green;
}
