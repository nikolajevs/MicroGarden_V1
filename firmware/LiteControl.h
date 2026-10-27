#pragma once
#include "Board.h"
#include "Safety.h"

constexpr bool ledSupplyNext(bool allowed, bool valid, float volts) {
  return valid && finiteFloat(volts) && volts >= (allowed ? PSU_LOW_VOLTAGE_V : PSU_RECOVER_VOLTAGE_V);
}

// Палитра ограничена шестью предельно контрастными цветами: каждый канал либо
// полностью включён, либо выключен. Оранжевый (зелёный на 1/3 яркости) и белый
// убраны — на глаз они путались с жёлтым и с ярким голубым.
// Правило чтения: ровный или мигающий цвет — состояние платы, короткая вспышка
// на зелёном фоне — идущий процесс. Неоднозначности нет: до вспышек выполнение
// доходит только тогда, когда ни одна авария не сработала.
enum class StatusColor : uint8_t { Off, Red, Purple, Blue, Yellow, Green, Cyan };

constexpr StatusColor statusColor(uint32_t now, bool climateOk, bool clockOk, bool busLow,
                                  bool wifiOk, bool sdOk, bool powerOk, bool heater, bool pump,
                                  bool activity) {
  // Faults always win over process flashes. Blink and flash timing never blocks loop().
  return !climateOk || !clockOk ? StatusColor::Red
       : busLow ? StatusColor::Purple
       : !wifiOk ? ((now / 500) % 2 ? StatusColor::Blue : StatusColor::Off)
       : !sdOk || !powerOk ? StatusColor::Yellow
       : activity ? StatusColor::Purple
       : now % 3000 < 300 && pump ? StatusColor::Cyan
       : now % 3000 < 300 && heater ? StatusColor::Yellow
       : StatusColor::Green;
}
