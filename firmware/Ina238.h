#pragma once
#include "Board.h"

// Register access is injected so the exact I2C protocol can be tested on the host.
// Bus provides bool read(uint8_t, uint16_t&) and bool write(uint8_t, uint16_t).
template<class Bus> class Ina238 {
 public:
  explicit constexpr Ina238(Bus &bus) : bus_(bus) {}

  constexpr bool begin() {
    uint16_t manufacturer = 0, device = 0;
    return bus_.read(0x3E, manufacturer) && manufacturer == INA238_MANUFACTURER_ID &&
           bus_.read(0x3F, device) && (device & 0xFFF0) == INA238_DEVICE_ID &&
           bus_.write(0x00, INA238_CONFIG) && bus_.write(0x01, INA238_ADC_CONFIG) && configured();
  }

  constexpr bool sample(uint16_t &shunt, uint16_t &voltage) {
    // Check range BEFORE using raw data: a brownout resets ADCRANGE to 0 (4x LSB).
    // Reconfigure, then discard this sample and allow a complete conversion before retrying.
    if (!configured()) { begin(); return false; }
    uint16_t diagnostic = 0;
    return bus_.read(0x0B, diagnostic) && (diagnostic & 1) && // MEMSTAT: trim checksum valid.
           bus_.read(0x04, shunt) && bus_.read(0x05, voltage) &&
           shunt != 0x7FFF && shunt != 0x8000 && voltage <= 27200; // Reject clipped shunt / >85 V.
  }

 private:
  constexpr bool configured() {
    uint16_t config = 0, adc = 0;
    return bus_.read(0x00, config) && config == INA238_CONFIG &&
           bus_.read(0x01, adc) && adc == INA238_ADC_CONFIG;
  }
  Bus &bus_;
};
