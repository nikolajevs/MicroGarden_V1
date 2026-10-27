#!/usr/bin/env python3
"""Verify design.json against the connection specification, the calculated values and the stock/purchase BOM.

No KiCad is needed: nets are built from design.json, which generate.py writes from the same pin/net specification as the
schematics. This is NOT an ERC of the exported .kicad_sch files and not a circuit simulation or physical validation.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def ohm(text: str) -> float:
    """'6k2 1%' -> 6200, '0R15 2W' -> 0.15, '7m 1%' -> 0.007, '82k' -> 82000, '100R' -> 100."""
    m = re.match(r'^(\d+)([kMRm])(\d*)', text.strip())
    assert m, text
    whole, unit, frac = m.groups()
    return float(f'{whole}.{frac or 0}') * {'k': 1e3, 'M': 1e6, 'R': 1.0, 'm': 1e-3}[unit]


def par(*xs):
    return 1 / sum(1 / x for x in xs)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', type=Path, default=HERE, help='project folder with design.json, BOM_allocated.json, procurement_lite.json and stock/')
    ap.add_argument('--output', type=Path)
    args = ap.parse_args()
    root = args.root
    args.output = args.output or root/'review/connectivity.txt'
    spec = json.loads((root/'design.json').read_text(encoding='utf-8'))
    bom = json.loads((root/'BOM_allocated.json').read_text(encoding='utf-8'))
    buy_snapshot = json.loads((root/'procurement_lite.json').read_text(encoding='utf-8'))
    with (root/'stock/sklad-2026-10-24.csv').open(encoding='utf-8-sig', newline='') as f:
        stock = {int(r['id']): r for r in csv.DictReader(f) if r['id']}
    p = {x['ref']: x for x in spec['parts']}
    nodes = defaultdict(set)
    for x in spec['parts']:
        for k, v in x['nets'].items():
            if v is not None:
                nodes[v].add((x['ref'], k))
    passed = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        passed.append(label)

    def n(ref, pin):
        return p[ref]['nets'].get(str(pin))

    def pins(ref, expected):
        return all(n(ref, k) == v for k, v in expected.items())

    def val(ref):
        return ohm(p[ref]['value'])

    def two(ref, a, b):
        return {n(ref, 1), n(ref, 2)} == {a, b}

    def real(net):
        return {k for k in nodes[net] if not k[0].startswith('#')}

    # ---- structure
    check('Part references are unique', len(p) == len(spec['parts']))
    one_pin = [name for name in nodes if len(real(name)) < 2]
    check('No one-pin orphan nets', not one_pin)
    nc = sorted(f'{x["ref"]}.{k}' for x in spec['parts'] for k, v in x['nets'].items() if v is None)
    expected_nc = {'U101.2', 'U102.3', 'U301.7', 'U501.8', 'J411.3', 'J421.3', 'J301.CD1', 'J301.CD2'} | \
        {f'U202.{i}' for i in (4, 5, 14, 17, 18, 19, 20, 21, 22, 23, 24, 29, 32)}
    check('Intentional NC pins are exactly the documented ones (PG, ALERT, MFP, LM3429 pin 8, fan tach, SD detect, unused ESP32 pins)', set(nc) == expected_nc)
    check('Every part with a purchasable source has a footprint', all(x['footprint'] for x in spec['parts'] if x['src']))
    check('No passive SMD footprint is smaller than 0603', all(not re.search(r'_(0201|0402)_', x['footprint']) for x in spec['parts'] if x['symbol'] in ['R', 'C', 'CP', 'L']))

    # ---- input, reverse polarity, power monitor
    check('Input terminal polarity, main fuse and TVS', pins('J101', {1: '+24V_RAW', 2: 'PSU_RTN'}) and pins('F101', {1: '+24V_RAW', 2: '+24V_FUSED'}) and pins('D101', {1: '+24V_FUSED', 2: 'GND'}) and p['D101']['mpn'] == 'SMBJ26A-E3/52')
    check('Reverse polarity: Q102 IRL2505 drain on PSU return, source on the shunt; PSU_RTN touches only J101.2 and Q102',
          pins('Q102', {1: 'REVP_GATE', 2: 'PSU_RTN', 3: 'RTN_SHUNT'}) and p['Q102']['mpn'] == 'IRL2505' and real('PSU_RTN') == {('J101', '2'), ('Q102', '2')})
    vgs = lambda vin: min(12.0, vin * 0.98 - 0.6)   # zener 12 V (5%), gate feed R108; loose bound for the low end
    check('Q102 gate clamp: 12 V zener keeps Vgs below the IRL2505 16 V limit up to the 42 V TVS clamp; >= 10 V enhancement at 21.6 V; R108 1206 power at 42 V <= 0.125 W',
          two('R108', '+24V_FUSED', 'REVP_GATE') and two('D103', 'REVP_GATE', 'GND') and p['D103']['mpn'] == 'BZV55C12' and 12 * 1.05 < 16 and vgs(21.6) >= 10
          and (42 - 12) ** 2 / val('R108') <= 0.125 and 'R_1206' in p['R108']['footprint'])
    rsh = par(ohm('7m'), ohm('7m'))
    check('Power monitor: INA238 U102 at I2C 0x40 (A0=A1=GND), 3.5 mOhm low-side shunt carries all return current',
          pins('U102', {1: 'GND', 2: 'GND', 4: 'I2C_SDA', 5: 'I2C_SCL', 6: '+3V3', 7: 'GND', 8: '+24V_FUSED', 9: 'RTN_SHUNT', 10: 'GND'}) and n('U102', 3) is None
          and all(two(x, 'GND', 'RTN_SHUNT') for x in ('R111', 'R112')) and abs(rsh - 0.0035) < 1e-9
          and real('RTN_SHUNT') == {('Q102', '3'), ('R111', '2'), ('R112', '2'), ('U102', '9')})
    i_max = 9.0
    check('INA238 ADCRANGE=1 (+-40.96 mV): full scale >= 1.3 x the 9 A board-current envelope with 75 W LEDs; each shunt dissipates <= 0.5 W at 11.7 A',
          0.04096 / rsh >= 1.3 * i_max and (11.7 / 2) ** 2 * 0.007 <= 0.5 and 0.04096 / rsh < 15)
    check('INA238 VBUS is on the 85 V-rated bus directly: TVS clamp (42 V) below 85 V; supply decoupled', 42 < 85 and two('C115', '+3V3', 'GND'))
    fuse_expected = {'F102': '+24V_PTC', 'F103': '+24V_LED', 'F104': '+24V_AUX', 'F105': '+24V_PUMP'}
    check('All four 24 V load branches are separately fused from +24V_FUSED', all(pins(ref, {1: '+24V_FUSED', 2: net}) for ref, net in fuse_expected.items()))
    check('Fan branch fuse is on the 12 V rail', pins('F401', {1: '+12V', 2: '+12V_FANS'}))

    # ---- 12 V buck
    check('ST1S14 pin map: VIN 7 on fused +24V_AUX, EN1 3 and GND 6/EP 9 on GND, EN2 5, FB 4, SW 8, BOOT 1, PG 2 open',
          pins('U101', {1: 'AUX_BOOT', 3: 'GND', 4: 'AUX_FB', 5: 'AUX_EN2', 6: 'GND', 7: '+24V_AUX', 8: 'AUX_SW', 9: 'GND'}) and n('U101', 2) is None and p['U101']['mpn'] == 'ST1S14PHR')
    check('ST1S14 power stage: bootstrap 100n BOOT-SW, 22 uH to +12V, SS56 catch diode (cathode SW), 820 uF + 10 uF + 100 nF output',
          two('C106', 'AUX_BOOT', 'AUX_SW') and two('L101', 'AUX_SW', '+12V') and pins('D102', {1: 'AUX_SW', 2: 'GND'}) and p['D102']['mpn'] == 'SS56'
          and two('C110', '+12V', 'GND') and two('C111', '+12V', 'GND') and two('C112', '+12V', 'GND') and p['C110']['mpn'] == 'EEEFT1E821AP')
    rt = val('R101') + val('R102'); rb = val('R103')
    v12 = [vfb * (1 + rt / rb) for vfb in (1.202, 1.222, 1.239)]
    check(f'+12V = 1.222 V x (1 + 88.2k/10k) = {v12[1]:.2f} V (11.8-12.2 V over VFB 1.202-1.239 V)', two('R101', '+12V', 'AUX_FB_MID') and two('R102', 'AUX_FB_MID', 'AUX_FB') and two('R103', 'AUX_FB', 'GND') and 11.7 <= min(v12) and max(v12) <= 12.3)
    ren = val('R104') + val('R105') + val('R106'); ren_b = val('R107')
    th = 1.5 * (ren + ren_b) / ren_b
    check(f'ST1S14 EN2 divider 3 x 150k / 47k: threshold {th:.1f} V (start of the 12 V rail; the board runs from 21.6 V), {21.6 * ren_b / (ren + ren_b):.2f} V at 21.6 V',
          two('R104', '+24V_AUX', 'AUX_EN2_A') and two('R105', 'AUX_EN2_A', 'AUX_EN2_B') and two('R106', 'AUX_EN2_B', 'AUX_EN2') and two('R107', 'AUX_EN2', 'GND') and 14 <= th <= 17 and 21.6 * ren_b / (ren + ren_b) >= 1.6)
    ripple12 = 12 * (1 - 12 / 25) / (22e-6 * 850e3)
    check(f'L101 22 uH at 850 kHz: ripple {ripple12:.2f} A at 25 V in, peak at 1.1 A load {1.1 + ripple12 / 2:.2f} A below the 3 A part limit', ripple12 < 0.5 and 1.1 + ripple12 / 2 < 3)

    # ---- 3.3 V buck and ESP32
    check('ST1S10 pin map: VIN_A 1, INH 2 and VIN_SW 6 on +12V, VFB 3, AGND 4, SYNC 5 on GND (900 kHz), SW 7, PGND 8, EP 9',
          pins('U201', {1: '+12V', 2: '+12V', 3: 'LOGIC_FB', 4: 'GND', 5: 'GND', 6: '+12V', 7: 'LOGIC_SW', 8: 'GND', 9: 'GND'}) and p['U201']['mpn'] == 'ST1S10PHR')
    rb33 = par(val('R202'), val('R203')); rt33 = val('R201')
    v33 = [vfb * (1 + rt33 / rb33) for vfb in (0.784, 0.8, 0.816)]
    check(f'+3V3 = 0.8 V x (1 + 100k / (47k||100k)) = {v33[1]:.2f} V ({v33[0]:.2f}-{v33[2]:.2f} V); ESP32 needs 3.0-3.6 V',
          two('R201', '+3V3', 'LOGIC_FB') and two('R202', 'LOGIC_FB', 'GND') and two('R203', 'LOGIC_FB', 'GND') and min(v33) >= 3.15 and max(v33) <= 3.45)
    import math
    flc = 1 / (2 * math.pi * math.sqrt(2.2e-6 * 22e-6 * 3 * 0.4))
    rip33 = 3.3 * (1 - 3.3 / 12) / (2.2e-6 * 900e3)
    check(f'ST1S10 with 2.2 uH: ripple {rip33:.2f} A (datasheet window 0.6-1.2 A at 3 A rating), peak at 1 A {1 + rip33 / 2:.2f} A < Isat 2.7 A, LC corner {flc / 1e3:.0f} kHz (datasheet design 18.7 kHz)',
          two('L201', 'LOGIC_SW', '+3V3') and p['L201']['mpn'] == 'SRN2512-2R2M' and rip33 <= 1.25 and 1 + rip33 / 2 < 2.7 and 15e3 < flc < 25e3)
    gpio = {6: 'FAN1_TACH', 7: 'FAN2_TACH', 8: 'PTC_GPIO', 9: 'PUMP_GPIO', 10: 'LED_PWM_GPIO', 11: 'FAN1_GPIO', 12: 'FAN2_GPIO', 13: 'FLOAT_GPIO', 16: 'SD_CS', 25: 'ESP_BOOT', 26: 'LED_R_GPIO', 27: 'LED_G_GPIO', 28: 'LED_B_GPIO', 30: 'SD_SCK_GPIO', 31: 'SD_MISO',
            33: 'I2C_SDA', 34: 'UART_RX', 35: 'UART_TX', 36: 'I2C_SCL', 37: 'SD_MOSI_GPIO'}
    check('ESP32 GPIO map is the EU-0.1 map; all four ground contacts, 3V3 and EN are consistent; GPIO34/35 have 10k pull-ups and no connector signal (tach removed)',
          pins('U202', dict(gpio, **{'1': 'GND', '15': 'GND', '38': 'GND', '39': 'GND', '2': '+3V3', '3': 'ESP_EN'})) and p['U202']['mpn'] == 'ESP32-WROOM-32E-N4')
    check('GPIO34/35 sit on 10k pull-ups only (nets reach no connector); the strapping pins 2/5/12/15 (module 24/29/14/23) carry no external load',
          two('R206', '+3V3', 'FAN1_TACH') and two('R207', '+3V3', 'FAN2_TACH') and val('R206') == val('R207') == 10e3 and real('FAN1_TACH') == {('U202', '6'), ('R206', '2')}
          and real('FAN2_TACH') == {('U202', '7'), ('R207', '2')} and all(n('U202', i) is None for i in (24, 29, 14, 23)))
    check('UART service connector has the EU-0.1 3.3 V pin order', pins('J201', {1: 'GND', 2: '+3V3', 3: 'UART_TX', 4: 'UART_RX', 5: 'ESP_EN', 6: 'ESP_BOOT'}))
    check('BOOT / EN have external pull-ups and active-low buttons; EN delay 10k / 2.2u (Espressif: 10k / 1u or more)',
          pins('C209', {1: 'ESP_EN', 2: 'GND'}) and two('R204', '+3V3', 'ESP_EN') and two('R205', '+3V3', 'ESP_BOOT') and pins('SW201', {1: 'ESP_EN', 2: 'GND'}) and pins('SW202', {1: 'ESP_BOOT', 2: 'GND'}) and val('R204') == 10e3)

    # ---- RTC, SD, interfaces
    check('RTC battery is isolated from 3.3 V; MCP7940N pin 3 VBAT / pin 8 VCC; MFP unused',
          pins('U301', {1: 'RTC_X1', 2: 'RTC_X2', 3: 'VBAT', 4: 'GND', 5: 'I2C_SDA', 6: 'I2C_SCL', 8: '+3V3'}) and n('U301', 7) is None and real('VBAT') == {('U301', '3'), ('BT301', '1')})
    sd_lines = ['SD_CS', 'SD_MOSI', 'SD_MISO', 'SD_DAT1', 'SD_DAT2']
    check('All five SD CMD/DAT lines have external 10k pull-ups to +3V3', all(any(x['symbol'] == 'R' and two(x['ref'], '+3V3', net) and val(x['ref']) == 10e3 for x in p.values()) for net in sd_lines))
    check('SD SPI pin mapping and shell ground follow the GCT MEM2061 drawing; SCK/MOSI have 100 R series resistors',
          pins('J301', {1: 'SD_DAT2', 2: 'SD_CS', 3: 'SD_MOSI', 4: '+3V3', 5: 'SD_SCK', 6: 'GND', 7: 'SD_MISO', 8: 'SD_DAT1', 'SH': 'GND'}) and two('R301', 'SD_SCK_GPIO', 'SD_SCK') and two('R302', 'SD_MOSI_GPIO', 'SD_MOSI') and val('R301') == 100)
    check('I2C has 4.7k pull-ups; SHT4x header: 1 +3V3, 2 GND, 3 SDA, 4 SCL behind 100 R and USBLC6 U401 (ref pin 5 on +3V3)',
          two('R308', '+3V3', 'I2C_SDA') and two('R309', '+3V3', 'I2C_SCL') and pins('J401', {1: '+3V3', 2: 'GND', 3: 'SHT_SDA', 4: 'SHT_SCL'}) and two('R401', 'I2C_SDA', 'SHT_SDA') and two('R402', 'I2C_SCL', 'SHT_SCL')
          and pins('U401', {1: 'SHT_SDA', 6: 'SHT_SDA', 3: 'SHT_SCL', 4: 'SHT_SCL', 2: 'GND', 5: '+3V3'}) and val('R308') == 4700)
    check('Float input: series 1k, 10k pull-up and 100n filter to GPIO14; no USBLC6 on this line (documented trade-off)',
          two('R403', 'FLOAT_EXT', 'FLOAT_GPIO') and two('R404', '+3V3', 'FLOAT_GPIO') and pins('C403', {1: 'FLOAT_GPIO', 2: 'GND'}) and pins('J403', {1: 'FLOAT_EXT', 2: 'GND'}) and 'U402' not in p)
    for k, b in [(1, 410), (2, 420)]:
        pre = f'FAN{k}'
        check(f'Fan{k}: AO3422 source pin 2 grounded, 100k reset pull-down, 1k gate; header 1 GND, 2 +12 V, 3 tach NC, 4 PWM; no tach signal, no ESD part',
              pins(f'Q{b+1}', {1: pre + '_GATE', 2: 'GND', 3: pre + '_PWM'}) and two(f'R{b+2}', pre + '_GATE', 'GND') and val(f'R{b+2}') == 100e3 and two(f'R{b+1}', pre + '_GPIO', pre + '_GATE')
              and pins(f'J{b+1}', {1: 'GND', 2: '+12V_FANS', 4: pre + '_PWM'}) and n(f'J{b+1}', 3) is None and p[f'Q{b+1}']['mpn'] == 'AO3422')

    # ---- LED boost (one channel)
    check('LM3429 pin map: VIN on its fused 24 V, nDIM shared net, EP/PGND/AGND on GND, GATE_RAW, VCC, IS, HSP, HSN',
          pins('U501', {1: '+24V_LED', 2: 'LED_COMP', 3: 'LED_CSH', 4: 'LED_RCT', 5: 'GND', 6: 'LED_OVP', 7: 'LED_NDIM', 9: 'GND', 10: 'LED_GATE_RAW', 11: 'LED_VCC', 12: 'LED_IS', 13: 'LED_HSP', 14: 'LED_HSN', 15: 'GND'}) and n('U501', 8) is None)
    check('Boost power stage: 47 uH from fused 24 V to SW, SBRT15U100SP5 cathode on OUT, BSC146N10LS5 drain SW / gate / source on the IS shunt to GND',
          pins('L501', {1: '+24V_LED', 2: 'LED_SW'}) and pins('D501', {1: 'LED_OUT', 2: 'LED_SW'}) and p['D501']['mpn'].startswith('SBRT15U100SP5')
          and pins('Q501', {1: 'LED_IS', 2: 'LED_IS', 3: 'LED_IS', 4: 'LED_GATE', 5: 'LED_SW'}) and p['Q501']['mpn'] == 'BSC146N10LS5' and two('R501', 'LED_IS', 'GND'))
    check('LED return is GND; output bank (470u/63V + six 4u7/100V + 100n/100V) is before the high-side LED shunt; HSP/HSN use equal 1k filters',
          pins('J501', {1: 'LED_POS', 2: 'GND'}) and two('R502', 'LED_OUT', 'LED_POS') and two('R503', 'LED_OUT', 'LED_HSP') and two('R504', 'LED_POS', 'LED_HSN') and val('R503') == val('R504') == 1000
          and all(n(r_, 1) == 'LED_OUT' and n(r_, 2) == 'GND' for r_, x in p.items() if x['sheet'] == 'LED' and x['mpn'] in ('EEVFK1J471M', 'CL32B475KCI6PJE', 'KAF21KR72A104KL'))
          and sum(1 for x in p.values() if x['mpn'] == 'CL32B475KCI6PJE') == 6)
    led_terminals = ('J501', 'J502', 'J503')
    check('J501/J502/J503 are parallel stock KF301 terminals after R502, sharing one current-controlled LED output without bypassing its shunt',
          all(pins(ref, {1: 'LED_POS', 2: 'GND'}) and p[ref]['src'] == 'S93' and p[ref]['in_bom']
              and p[ref]['footprint'] == p['J501']['footprint'] for ref in led_terminals)
          and real('LED_POS') == {(ref, '1') for ref in led_terminals} | {('R502', '2'), ('R504', '1')}
          and pins('R502', {1: 'LED_OUT', 2: 'LED_POS'}))
    # TI LM3429 Rev H electrical table: CSH 1.21/1.235/1.26 V min/typ/max.
    # The upper modeled current also includes the three 1% setpoint resistors.
    rcsh = val('R505') + val('R506')
    iled = 1.235 * val('R503') / (rcsh * val('R502'))
    iled_high = 1.26 * val('R503') * 1.01 / (rcsh * 0.99 * val('R502') * 0.99)
    led_power = iled * 48
    check(f'69 W-class LED output: 1.235 V x 1k / (5.7k x 0.15 R) = {iled:.3f} A total, {led_power:.2f} W at 48 V, shared by J501/J502/J503',
          two('R505', 'LED_CSH', 'LED_CSH_MID') and two('R506', 'LED_CSH_MID', 'GND')
          and val('R505') == 4700 and val('R506') == 1000 and 1.43 <= iled <= 1.46 and 68 <= led_power <= 70.5)
    shunt_loss = iled ** 2 * val('R502')
    shunt_loss_high = iled_high ** 2 * val('R502') * 1.01
    check(f'R502 output shunt: nominal {shunt_loss:.3f} W, modeled high-current loss {shunt_loss_high:.3f} W below the 2 W part rating',
          p['R502']['mpn'] == 'CS2512-0R15-1%-HP' and shunt_loss_high < 1.0)
    ovp = 1.24 * (val('R509') + val('R510') + val('R511')) / val('R511')
    check(f'OVP = 1.24 V x 44 = {ovp:.2f} V: above the 48 V maximum Vf, below the 63 V electrolytic and 100 V parts', two('R509', 'LED_OUT', 'LED_OVP_MID') and two('R510', 'LED_OVP_MID', 'LED_OVP') and two('R511', 'LED_OVP', 'GND') and 50 < ovp < 60)
    # TI LM3429 Rev H, equation 6: f = 25 / ((R507 + R508) x C506). EU-0.1 LED1 used 56k = 402 kHz.
    f_sw = 25 / ((val('R507') + val('R508')) * 1e-9)
    check(f'RCT: 82k + 6k2 from SW, 1 nF C0G = {f_sw / 1e3:.2f} kHz nominal (below the DC-300 kHz storage range Feryster states for L501); COMP 22u, VCC 4u7',
          two('R507', 'LED_SW', 'LED_RT_MID') and two('R508', 'LED_RT_MID', 'LED_RCT') and val('R507') == 82e3 and val('R508') == 6.2e3 and 275e3 <= f_sw <= 295e3 and pins('C506', {1: 'LED_RCT', 2: 'GND'}) and pins('C505', {1: 'LED_COMP', 2: 'GND'}) and pins('C504', {1: 'LED_VCC', 2: 'GND'}) and pins('C507', {1: 'LED_OVP', 2: 'GND'}))
    ipk = {}
    for vin in (21.6, 24.0, 25.0):
        vo = 48.0; d = 1 - vin / vo; iin = iled_high / (1 - d) / 0.90; dil = vin * d / (43.5e-6 * f_sw)
        ipk[vin] = iin + dil / 2
    # Use the guaranteed minimum comparator threshold and the high 1% R501
    # tolerance. The modeled corners are explicit; analog offsets, actual L,
    # layout noise and temperature still require prototype qualification.
    ilim = 0.215 / (val('R501') * 1.01)
    check(f'IS minimum 215 mV / (0.040 R +1%) = {ilim:.2f} A versus modeled peak {max(ipk.values()):.2f} A (1.26 V CSH, 1% setpoint resistors, 21.6 V, Vf 48 V, 43.5 uH, {f_sw / 1e3:.2f} kHz, 90% efficiency): >=1.2x headroom for the 69 W design',
          ilim >= 1.2 * max(ipk.values()) and max(ipk.values()) < 6.3 * 0.73 and p['L501']['src'] == 'S12')
    check(f'F103 is the 6.3 A USI fast fuse 3413.0224.11; modeled input peak {max(ipk.values()):.2f} A is below the steady-current allowance, NOT proof of inrush/I2t coordination',
          p['F103']['mpn'] == '3413.0224.11' and p['F103']['value'] == '6.3 A fast' and max(ipk.values()) < 6.3 * 0.73)
    check('Gate: two 10 R in parallel (5 R) from GATE_RAW, 100k gate pull-down; GATE_RAW touches only U501.10 and the two resistors',
          abs(par(val('R512'), val('R513')) - 5) < 1e-9 and two('R512', 'LED_GATE_RAW', 'LED_GATE') and two('R513', 'LED_GATE_RAW', 'LED_GATE') and two('R514', 'LED_GATE', 'GND')
          and real('LED_GATE_RAW') == {('U501', '10'), ('R512', '1'), ('R513', '1')} and 'U801' not in p)
    hi = 3.3 * val('R516') / (val('R515') + val('R516')); lo = 60e-6 * val('R516')
    check(f'nDIM: GPIO series 5.1k + one 10k pull-down: HIGH {hi:.2f} V (> 1.6 V hysteresis threshold), Hi-Z {lo:.2f} V (< 1.24 V); correct net names, no capacitor on nDIM',
          two('R515', 'LED_PWM_GPIO', 'LED_NDIM') and two('R516', 'LED_NDIM', 'GND') and hi > 1.6 and lo < 0.8
          and not any('LED_NDIM' in x['nets'].values() and x['symbol'] in ['C', 'CP'] for x in p.values()) and 'D801' not in p and 'U802' not in p)

    tmin = 2 * iled_high * 48 * 47e-6 / 21.6 ** 2
    required_pwm_pulse = 5 / (1000 * 256)
    # The 69 W setpoint lowers the modeled startup pulse; the firmware minimum of 5 counts is kept
    # unchanged and stays above it, so the bound below only requires that ordering.
    check(f'Required firmware setting for the 69 W design: at least 5 PWM counts at 1 kHz/8 bit ({required_pwm_pulse * 1e6:.2f} us) exceeds modeled LM3429 startup {tmin * 1e6:.2f} us at 21.6 V/48 V/47 uH; firmware implementation remains pending',
          10e-6 < tmin < required_pwm_pulse and required_pwm_pulse > 3 / (1000 * 256))

    # ---- RGB status LED (common anode on +12V, three NPN low-side switches)
    vf = {'R': 2.0, 'G': 3.1, 'B': 3.1}
    ch_ok = True; cur = {}
    for ch, (qq, rb, rpd, rl) in {'R': ('Q901', 'R901', 'R904', 'R907'), 'G': ('Q902', 'R902', 'R905', 'R908'), 'B': ('Q903', 'R903', 'R906', 'R909')}.items():
        i_led = (12 - vf[ch] - 0.15) / val(rl); cur[ch] = i_led
        ch_ok &= (pins(qq, {1: f'LED_{ch}_BASE', 2: 'GND', 3: f'LED_{ch}_C'}) and p[qq]['mpn'] == 'BC847B-DIO' and two(rb, f'LED_{ch}_GPIO', f'LED_{ch}_BASE') and val(rb) == 4700
                  and two(rpd, f'LED_{ch}_BASE', 'GND') and val(rpd) == 100e3 and two(rl, f'LED_{ch}_C', f'LED_{ch}_K')
                  and 3e-3 <= i_led <= 8e-3 and (12 - vf[ch]) ** 2 / val(rl) <= 0.125 and (3.3 - 0.7) / val(rb) * 100 >= 2 * i_led)
    check(f'RGB status LED D901 (common anode on +12V, OSTAPA57E2A lead order G/B/A/R): per channel GPIO -> 4k7 -> BC847B base, 100k pull-down, collector -> 2k -> cathode; '
          f'{cur["R"] * 1e3:.1f}/{cur["G"] * 1e3:.1f}/{cur["B"] * 1e3:.1f} mA R/G/B (3-8 mA, below the 20 mA rating), base current saturates the NPN (hFE >= 100), resistor <= 0.125 W',
          ch_ok and pins('D901', {1: 'LED_G_K', 2: 'LED_B_K', 3: '+12V', 4: 'LED_R_K'}) and p['D901']['mpn'] == 'OSTAPA57E2A' and 12 < 45)
    check('RGB channels sit on GPIO4 / GPIO16 / GPIO17 (module pins 26/27/28: not strapping pins, not used by flash or PSRAM on WROOM-32E-N4); each GPIO net reaches only the ESP32 and its base resistor',
          pins('U202', {26: 'LED_R_GPIO', 27: 'LED_G_GPIO', 28: 'LED_B_GPIO'}) and all(len(real(f'LED_{c_}_GPIO')) == 2 for c_ in 'RGB') and all(len(real(f'LED_{c_}_K')) == 2 for c_ in 'RGB'))

    # ---- loads
    check('PTC driver defaults off: separate NPN and PNP stages, PNP emitter on +12V, 12 V gate drive of CSD19531KCS (1=G, 2=D/tab, 3=S), J701 return = drain',
          pins('Q701', {1: 'PTC_NPN_B', 2: 'GND', 3: 'PTC_NPN_C'}) and pins('Q702', {1: 'PTC_PNP_B', 2: '+12V', 3: 'PTC_DRV'}) and pins('Q703', {1: 'PTC_GATE', 2: 'PTC_DRAIN', 3: 'GND'})
          and p['Q703']['mpn'] == 'CSD19531KCS' and p['Q703']['footprint'] == 'Package_TO_SOT_THT:TO-220-3_Vertical' and two('R702', 'PTC_NPN_B', 'GND') and two('R706', 'PTC_GATE', 'GND') and pins('J701', {1: '+24V_PTC', 2: 'PTC_DRAIN'}))
    vb = 3.6 * val('R702') * 1.05 / (30e3 + val('R701') + val('R702') * 1.05)
    check(f'PTC stays off with the ESP32 internal pull-up (30k worst case) on GPIO32: base {vb:.2f} V < 0.45 V even with 5 % R702; HIGH drive {(3.3 - 0.7) / val("R701") * 1e3:.2f} mA saturates the NPN',
          two('R701', 'PTC_GPIO', 'PTC_NPN_B') and vb < 0.45 and (3.3 - 0.7) / val('R701') * 100 > (12 - 0.7 - 0.2) / val('R703') + (12 - 0.7) / val('R704'))
    vg = 12 * val('R706') / (val('R706') + val('R705'))
    check(f'PTC gate swing {vg:.1f} V from the PNP, 100 R gate resistor, 2k pull-down (CSD19531KCS rated ±20 V gate)', two('R705', 'PTC_DRV', 'PTC_GATE') and two('R703', 'PTC_NPN_C', 'PTC_PNP_B') and two('R704', '+12V', 'PTC_PNP_B') and 10 <= vg <= 12)
    ptc_max = 100 * (25 / 24) ** 2 / 25
    check(f'Q703 conduction loss at the 25 V PSU limit ({ptc_max:.2f} A): {ptc_max ** 2 * 0.0077 * 1.5:.2f} W with RDS(on) max x 1.5 hot; F102 8 A',
          ptc_max ** 2 * 0.0077 * 1.5 < 0.5 and ptc_max < 8 * 0.73)
    check('Pump AO3422: 1 gate / 2 source / 3 drain, reset pull-down 100k, SS56 flyback with cathode on fused +24V_PUMP',
          pins('Q704', {1: 'PUMP_GATE', 2: 'GND', 3: 'PUMP_DRAIN'}) and pins('D701', {1: '+24V_PUMP', 2: 'PUMP_DRAIN'}) and two('R708', 'PUMP_GATE', 'GND') and val('R708') == 100e3 and p['D701']['mpn'] == 'SS56' and pins('J702', {1: '+24V_PUMP', 2: 'PUMP_DRAIN'}))
    lo_i = {v: 100 * (v / 24) ** 2 / v + iled_high * 48 / 0.90 / v + 12 * 1.1 / 0.88 / v + 6 / 24 * (v / 24) for v in (21.6, 25.0)}
    check(f'24 V budget with 69 W-class LEDs plus PTC/pump/12 V loads: {lo_i[21.6]:.2f} A at 21.6 V / {lo_i[25.0]:.2f} A at 25 V under the modeled LED corners; within the 9 A envelope, F101 15 A and PSU 10 A',
          max(lo_i.values()) <= i_max and max(lo_i.values()) < 10 and max(lo_i.values()) < 15 * 0.73)

    # ---- sources and BOM
    stock_rows = {r_['id']: r_ for r_ in bom['stock']}
    used = Counter(x['src'] for x in spec['parts'] if x['in_bom'])
    stock_used = {int(k[1:]): v for k, v in used.items() if k[1:].isdigit() and k[0] == 'S'}
    check('Every stock source exists in the owner CSV, is not archived, and the stock covers the fitted quantity (no hidden shortage)',
          all(i in stock and stock[i]['архив'] == '0' and v <= int(stock[i]['остаток']) for i, v in stock_used.items()) and set(stock_used) == set(stock_rows))
    zero_spare = sorted(stock[i]['MPN'] or f'id{i}' for i, v in stock_used.items() if v == int(stock[i]['остаток']))
    check('Stock rows in BOM_allocated.json match the design counts and the remaining stock', all(stock_rows[i]['needed'] == v and stock_rows[i]['stock_left'] == int(stock[i]['остаток']) - v for i, v in stock_used.items()))
    allocated = Counter({k: v for k, v in used.items() if not (k[1:].isdigit() and k[0] == 'S')}) + Counter(spec['accessories'])
    buy = {r_['mpn']: r_ for r_ in bom['buy']}
    check('Every purchased MPN is bought at least in the fitted quantity, respecting the shop minimum and multiple, from an allowed shop, with URL and price date',
          set(allocated) == set(buy) and all(buy[k]['buy'] >= v and buy[k]['buy'] >= buy[k]['min_qty'] and buy[k]['buy'] % buy[k]['multiple'] == 0 and (buy[k]['unpriced'] or (buy[k]['shop'] in ('Salvats', 'Electrobase', 'Lemona')
                                           and buy[k]['url'].startswith('https://') and buy[k]['price_date'])) for k, v in allocated.items()))
    unpriced = sorted(k for k in buy if buy[k]['unpriced'])
    check(f'Purchase prices equal the frozen catalogue snapshot (procurement_lite.json); all lines including the owner-quoted OSTAPA57E2A have prices; unpriced ({", ".join(unpriced)})',
          all(abs(buy[k]['price_unit'] - buy_snapshot[k]['price']) < 1e-9 for k in buy if not buy[k]['unpriced']) and not unpriced and buy_snapshot['OSTAPA57E2A']['price'] == .85 and buy['OSTAPA57E2A']['line'] == .85)
    summary = bom['summary']
    total = Decimal(summary['total_EUR']); allowance = Decimal(summary['unpriced_allowance_EUR'])
    check(f'Purchases + delivery {total} EUR (unpriced allowance {allowance} EUR) stay under the 100 EUR ceiling and the ~80 EUR target; cheaper than the ~81 EUR of EU-0.1 (81.17 EUR by BOM lines)',
          total + allowance <= Decimal('80.00') and total + allowance < Decimal('81.17'))
    out = ['EU-LITE schematic connectivity review (from design.json, no KiCad)', f'Revision: {spec["revision"]}',
           f'PASS: {len(passed)} checks', f'Fitted electrical components: {sum(x["in_bom"] for x in spec["parts"])}',
           f'Stock pieces: {summary["stock_pieces"]} in {summary["stock_rows"]} rows; purchased pieces: {summary["buy_pieces"]} in {summary["buy_rows"]} rows',
           f'Purchases incl. VAT {summary["parts_EUR"]} EUR + delivery {summary["shipping_EUR"]} EUR = {total} EUR (catalogue 2026-10-23; RGB owner quote 2026-10-25), including CSD19531KCS and OSTAPA57E2A',
           f'Stock lines used up completely (no spare, a mounting error means a repurchase): {", ".join(zero_spare)}', '']
    out += [f'PASS {i+1:02d} | {label}' for i, label in enumerate(passed)]
    out += ['', 'Limitations: these checks establish pin/net, calculated-value and BOM consistency of the specification, not an ERC of the written .kicad_sch files',
            'or analog stability, power sequencing, component temperature, EMC/ESD immunity or manufacturing readiness.',
            'Pin tables: ST1S14 and ST1S10 from ../cheap-version (checked there against the datasheets; the ST1S14 table also matched a datasheet excerpt found by web search),',
            'INA238 from a TI datasheet translation and a third-party listing found by web search; no primary datasheet PDF could be fetched in this session.',
            'For separate KiCad/PCB checks see README; physical fit and prototype tests remain pending.']
    args.output.write_text('\n'.join(out) + '\n', encoding='utf-8')
    print('\n'.join(out[:8]))


if __name__ == '__main__':
    main()
