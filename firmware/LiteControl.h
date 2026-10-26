#pragma once
#include "Board.h"
#include "Safety.h"

constexpr bool ledSupplyNext(bool allowed, bool valid, float volts) {
  return valid && finiteFloat(volts) && volts >= (allowed ? PSU_LOW_VOLTAGE_V : PSU_RECOVER_VOLTAGE_V);
}

enum class StatusColor : uint8_t { Off, Red, Purple, Blue, Yellow, Green, Orange, Cyan, White };

constexpr StatusColor statusColor(uint32_t now, bool climateOk, bool clockOk, bool busLow,
                                  bool wifiOk, bool sdOk, bool powerOk, bool heater, bool pump,
                                  bool activity) {
  // Faults always win over process flashes. Blink and flash timing never blocks loop().
  return !climateOk || !clockOk ? StatusColor::Red
       : busLow ? StatusColor::Purple
       : !wifiOk ? ((now / 500) % 2 ? StatusColor::Blue : StatusColor::Off)
       : !sdOk || !powerOk ? StatusColor::Yellow
       : activity ? StatusColor::White
       : now % 3000 < 300 && pump ? StatusColor::Cyan
       : now % 3000 < 300 && heater ? StatusColor::Orange
       : StatusColor::Green;
}
