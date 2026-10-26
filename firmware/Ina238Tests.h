#pragma once
#include "Ina238.h"

// A fake register bus exercises the production driver at compile time, with no on-device test code.
namespace ina238_tests {
struct Bus {
  uint16_t registers[64] = {};
  int operations = 0;
  int failAt = -1;
  int writes = 0;
  int dataReads = 0;
  constexpr Bus() {
    registers[0x3E] = 0x5449;
    registers[0x3F] = 0x2381;
    registers[0x0B] = 1;
    registers[0x04] = 25200;
    registers[0x05] = 7680;
  }
  constexpr bool read(uint8_t reg, uint16_t &value) {
    if (operations++ == failAt || reg >= 64) return false;
    if (reg == 4 || reg == 5) ++dataReads;
    value = registers[reg];
    return true;
  }
  constexpr bool write(uint8_t reg, uint16_t value) {
    if (operations++ == failAt || reg >= 64) return false;
    registers[reg] = value;
    ++writes;
    return true;
  }
};

constexpr bool normalAndWrongDevice() {
  Bus bus;
  Ina238<Bus> driver(bus);
  uint16_t shunt = 0, volts = 0;
  if (!driver.begin() || bus.registers[0] != 0x0010 || bus.registers[1] != 0xBB6B ||
      !driver.sample(shunt, volts) || shunt != 25200 || volts != 7680 || bus.dataReads != 2) return false;
  Bus other;
  other.registers[0x3F] = 0x2260; // Same TI manufacturer ID is insufficient.
  Ina238<Bus> wrong(other);
  if (wrong.begin() || other.writes) return false;
  Bus laterRevision;
  laterRevision.registers[0x3F] = 0x238F;
  Ina238<Bus> revised(laterRevision);
  return revised.begin();
}

constexpr bool brownoutAndBadData() {
  Bus bus;
  Ina238<Bus> driver(bus);
  if (!driver.begin()) return false;
  bus.registers[0] = 0; // Reset reverted the shunt range: must not read data with the wrong scale.
  uint16_t shunt = 42, volts = 42;
  if (driver.sample(shunt, volts) || bus.dataReads || shunt != 42 || volts != 42 ||
      bus.registers[0] != 0x0010 || !driver.sample(shunt, volts)) return false;
  bus.registers[0x0B] = 0; // Trim memory checksum failure.
  if (driver.sample(shunt, volts)) return false;
  bus.registers[0x0B] = 1;
  bus.registers[4] = 0x7FFF;
  if (driver.sample(shunt, volts)) return false;
  bus.registers[4] = 0x8000;
  if (driver.sample(shunt, volts)) return false;
  bus.registers[4] = uint16_t(-2800);
  bus.registers[5] = 0xFFFF;
  if (driver.sample(shunt, volts)) return false;
  bus.registers[5] = 7680;
  return driver.sample(shunt, volts) && ina238Amps(int16_t(shunt), 1.0f) < 0;
}

constexpr bool transactionFailures() {
  // Every begin transaction must be checked, including write/readback failures.
  for (int fail = 0; fail < 6; ++fail) {
    Bus bus;
    bus.failAt = fail;
    Ina238<Bus> driver(bus);
    if (driver.begin()) return false;
  }
  // Every sample transaction must fail closed on I2C errors.
  for (int fail = 0; fail < 5; ++fail) {
    Bus bus;
    Ina238<Bus> driver(bus);
    if (!driver.begin()) return false;
    bus.failAt = bus.operations + fail;
    uint16_t shunt = 0, volts = 0;
    if (driver.sample(shunt, volts)) return false;
  }
  return true;
}
static_assert(normalAndWrongDevice(), "INA238 register map, scale, device/revision identification");
static_assert(brownoutAndBadData(), "INA238 reset, invalid memory, saturation and signed readings");
static_assert(transactionFailures(), "INA238 must check every I2C transaction");
} // namespace ina238_tests
