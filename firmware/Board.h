#pragma once
#include <stdint.h>

// MicroGarden_V1 / EU-LITE, ESP32-WROOM-32E-N4 (4 MB, no PSRAM).
// Source: hardware/PCB_EU_LITE/design.json and DESIGN.md.
constexpr uint8_t LED_PWM_PIN = 25; // Active-high nDIM of the single LM3429 channel.
constexpr uint8_t FAN1_PWM_PIN = 26;
constexpr uint8_t FAN2_PWM_PIN = 27;
constexpr uint8_t RGB_RED_PIN = 4;   // NPN drivers: HIGH turns each common-anode LED channel on.
constexpr uint8_t RGB_GREEN_PIN = 16;
constexpr uint8_t RGB_BLUE_PIN = 17;
constexpr uint8_t HEATER_PIN = 32; // 100 W PTC: OUTPUT only, LOW = off. Never pull up/hold.
constexpr uint8_t PUMP_PIN = 33;
constexpr uint8_t WATER_LEVEL_PIN = 14; // External 10k pull-up; closed to GND = water available.
constexpr uint8_t SD_CS_PIN = 13;
constexpr uint8_t SD_SCK_PIN = 18;
constexpr uint8_t SD_MOSI_PIN = 23;
constexpr uint8_t SD_MISO_PIN = 19;
constexpr uint8_t I2C_SDA_PIN = 21;
constexpr uint8_t I2C_SCL_PIN = 22;

constexpr uint32_t LED_PWM_FREQ = 1000;
constexpr uint32_t FAN_PWM_FREQ = 25000;
constexpr uint8_t PWM_RES = 8;
// Core 3.3.x assigns LEDC timers by frequency/resolution, not by channel number:
// the LED gets its own timer only because LED_PWM_FREQ != FAN_PWM_FREQ (see SafetyTests.h).
constexpr uint8_t LED_PWM_CHANNEL = 0;  // nDIM, 1 kHz, attached first.
constexpr uint8_t FAN1_PWM_CHANNEL = 2; // Fans, 25 kHz, share one timer.
constexpr uint8_t FAN2_PWM_CHANNEL = 3;
constexpr uint8_t RGB_RED_CHANNEL = 4;
constexpr uint8_t RGB_GREEN_CHANNEL = 5;
constexpr uint8_t RGB_BLUE_CHANNEL = 6;
constexpr uint32_t RGB_PWM_FREQ = 1000; // Same frequency/resolution as nDIM, so sharing its timer is safe.
constexpr uint8_t RGB_DUTY = 48;         // Limited brightness; physical colour balance requires a bench check.
constexpr uint32_t LED_MIN_PULSE_US = 16; // 15.74 us calculated startup at the 75 W corner; rounds to 5 counts.
constexpr uint32_t PWM_COUNTS = 1UL << PWM_RES;
constexpr int PWM_MAX = PWM_COUNTS - 1;
constexpr int LED_MIN_DUTY = (LED_MIN_PULSE_US * LED_PWM_FREQ * PWM_COUNTS + 999999UL) / 1000000UL;

// Software ceiling for the 100 W PTC, independent of every setting. The external thermal
// cutoff stays mandatory; this only adds a second, settings-proof limit.
constexpr float HEATER_HARD_LIMIT_C = 60.0f;

// INA238, TI SLYS025B Rev. B, sections 6.6 and 7.1.
// Two 7 mOhm shunts in parallel = 3.5 mOhm. IN+ is on board GND; VBUS is directly on +24V_FUSED.
constexpr uint8_t INA238_ADDR = 0x40; // A0 = A1 = GND.
constexpr uint16_t INA238_MANUFACTURER_ID = 0x5449;
constexpr uint16_t INA238_DEVICE_ID = 0x2380; // High 12 bits; low nibble is silicon revision.
constexpr uint16_t INA238_CONFIG = 0x0010;   // ADCRANGE=1, +/-40.96 mV, no conversion delay.
constexpr uint16_t INA238_ADC_CONFIG = (0xB << 12) | (5 << 9) | (5 << 6) | (5 << 3) | 3;
// Continuous shunt+bus, 1052 us per input, AVG=64: about 134.7 ms per result.
// Use raw VSHUNT and VBUS; CURRENT/POWER and SHUNT_CAL are not used.
constexpr float INA238_SHUNT_LSB_V = 1.25e-6f;
constexpr float INA238_BUS_LSB_V = 3.125e-3f;
constexpr float INA238_SHUNT_OHM = 0.0035f;
constexpr float PSU_LOW_VOLTAGE_V = 21.6f; // Software LED cutoff, lower end of the design input range.
constexpr float PSU_RECOVER_VOLTAGE_V = 22.5f; // Hysteresis, prevents repeated LED restarts near cutoff.
constexpr uint32_t POWER_FRESH_MS = 2500;

constexpr int clampPwm(int duty) { return duty < 0 ? 0 : (duty > PWM_MAX ? PWM_MAX : duty); }

// Keep zero off; 1..4 round up to 5 counts (19.53125 us). 255 is continuous HIGH.
// LOW gaps shorter than 5 counts also snap to continuous HIGH (requests 252..254).
constexpr int ledDutyForRequest(int duty) {
  return duty <= 0 ? 0
       : duty < LED_MIN_DUTY ? LED_MIN_DUTY
       : duty >= PWM_MAX ? PWM_MAX
       : int(PWM_COUNTS) - duty < LED_MIN_DUTY ? PWM_MAX
       : duty;
}

// AO3422 inverts the fan's PWM wire. Keep speed/status/CSV in logical 0..255;
// only the GPIO duty is complemented. GPIO LOW (duty 0) = full airflow, including boot.
constexpr int fanGpioDuty(int speed) { return PWM_MAX - clampPwm(speed); }

// Signed VSHUNT register; user correction applies only to current. No old R113 divider correction.
constexpr float ina238Amps(int16_t shuntRaw, float correction) {
  return float(shuntRaw) * (INA238_SHUNT_LSB_V / INA238_SHUNT_OHM) * correction;
}
constexpr float ina238Volts(uint16_t busRaw) { return float(busRaw) * INA238_BUS_LSB_V; }
