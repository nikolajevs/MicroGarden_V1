#pragma once
#include "Board.h"
#include "Safety.h"

constexpr bool ledSupplyNext(bool allowed, bool valid, float volts) {
  return valid && finiteFloat(volts) && volts >= (allowed ? PSU_LOW_VOLTAGE_V : PSU_RECOVER_VOLTAGE_V);
}

// Индикатор показывает только состояние платы. Каждый канал либо полностью
// включён, либо выключен: промежуточных яркостей и смешанных оттенков нет,
// поэтому цвета различимы при любом освещении.
// Вспышки процессов (помпа, нагреватель, отправка телеметрии) убраны — ход
// работы виден в веб-интерфейсе, а индикатор отвечает за неисправности.
enum class StatusColor : uint8_t { Off, Red, Purple, Blue, Yellow, Green };

constexpr StatusColor statusColor(uint32_t now, bool climateOk, bool clockOk, bool busLow,
                                  bool wifiOk, bool sdOk, bool powerOk) {
  // Более тяжёлая неисправность перекрывает более лёгкую. Мигание не блокирует loop().
  return !climateOk || !clockOk ? StatusColor::Red
       : busLow ? StatusColor::Purple
       : !wifiOk ? ((now / 500) % 2 ? StatusColor::Blue : StatusColor::Off)
       : !sdOk || !powerOk ? StatusColor::Yellow
       : StatusColor::Green;
}
