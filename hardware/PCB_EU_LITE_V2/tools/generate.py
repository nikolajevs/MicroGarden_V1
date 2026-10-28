#!/usr/bin/env python3
"""Generate the EU-LITE V2 schematic, design.json and the stock+purchase BOM (Python >=3.10, no KiCad needed).

EU-LITE V2 keeps the EU-LITE (V1) board and replaces its LM3429 boost LED channel by six 24 V constant-voltage lamp
outputs behind one PWM low-side switch (owner decision 2026-10-28). Parts come from the owner's stock
(stock/sklad-*.csv) and, for the rest, from the shop catalogue snapshot (procurement_lite.json: Salvats / Electrobase).
The schematic is the output of this explicit pin/net specification, derived from the V1 generator. Do not rerun after
editing in KiCad without first incorporating the manual changes here. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import csv
import json
import math
import uuid
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
NS = uuid.UUID('0d6c5bb2-8a0d-4a3e-a1c1-0b6f2f3c9e11')
ROOT = 'PCB_EU_LITE_V2'
REV = 'EU-LITE-0.2-DRAFT'
DATE = '2026-10-28'
STOCK_CSV = HERE / 'stock/sklad-2026-10-24.csv'
SHIPPING = {'Electrobase': Decimal('2.99'), 'Salvats': Decimal('2.88')}   # EU-0.1 README (24.10.2026), not rechecked
parts = []
sheets = {}
symbols = {}
counts = Counter()


def uid(*args):
    return str(uuid.uuid5(NS, '/'.join(map(str, args))))


def q(x):
    return json.dumps(str(x), ensure_ascii=False)


def num(x):
    return f'{x:.4f}'.rstrip('0').rstrip('.') if x else '0'


def effects(size=1, extra=''):
    return f'(effects (font (size {size} {size})) {extra})'


def prop(key, val, x=0, y=0, hide=True, size=1):
    return f'(property {q(key)} {q(val)} (at {num(x)} {num(y)} 0) {effects(size, "(hide yes)" if hide else "")})'


def sheet(name, title, notes, chip_area=0):
    sheets[name] = dict(title=title, notes=notes, chip_area=chip_area, parts=[])


def pin(n, name, side='L', typ='passive'):
    return dict(number=str(n), name=name, side=side, typ=typ)


def ic(name, left, right, width=25.4):
    """Each side uses 2.54 mm pitch; explicit physical pin numbers are mandatory."""
    pp = []
    n = max(len(left), len(right))
    height = (n + 1) * 2.54
    for side, seq in [('L', left), ('R', right)]:
        for k, item in enumerate(seq):
            p = pin(*item, side=side) if len(item) == 2 else pin(item[0], item[1], side, item[2])
            p.update(x=-(width/2+5.08) if side == 'L' else width/2+5.08,
                     y=height/2 - 2.54*(k+1), angle=0 if side == 'L' else 180)
            pp.append(p)
    symbols[name] = dict(name=name, pins=pp, width=width, height=height, kind='ic')
    return name


def primitive(name, kind, ps):
    symbols[name] = dict(name=name, kind=kind, width=10.16, height=5.08, pins=[])
    for no, nm, x, y, angle, typ in ps:
        symbols[name]['pins'].append(dict(number=str(no), name=nm, x=x, y=y, angle=angle, typ=typ))


for name, kind in [('R','r'),('C','c'),('CP','cp'),('L','l'),('F','f'),('D','d'),('TVS','tvs'),('SW','sw'),('Y','y'),('BAT','bat')]:
    primitive(name, kind, [(1,'K' if name in ['D','TVS'] else '~',-7.62,0,0,'passive'),
                           (2,'A' if name in ['D','TVS'] else '~',7.62,0,180,'passive')])
primitive('NMOS', 'nmos', [(1,'G',-5.08,0,0,'input'),(2,'D',2.54,5.08,270,'passive'),(3,'S',2.54,-5.08,90,'passive')])
primitive('NMOS_GSD', 'nmos', [(1,'G',-5.08,0,0,'input'),(2,'S',2.54,-5.08,90,'passive'),(3,'D',2.54,5.08,270,'passive')])
primitive('NPN', 'npn', [(1,'B',-7.62,0,0,'input'),(2,'E',5.08,-5.08,90,'passive'),(3,'C',5.08,5.08,270,'passive')])
primitive('PNP', 'pnp', [(1,'B',-7.62,0,0,'input'),(2,'E',5.08,5.08,270,'passive'),(3,'C',5.08,-5.08,90,'passive')])
primitive('FLAG', 'flag', [(1,'PWR',0,0,90,'power_out')])
primitive('TP', 'tp', [(1,'PAD',0,0,90,'passive')])

# ST1S14PHR (HSOP8-EP): pin table as used and checked against the datasheet in ../cheap-version of nikolajevs/ai
# (1 BOOT, 2 PG, 3 EN1 active low, 4 FB, 5 EN2 active high, 6 GND, 7 VIN, 8 SW, EP GND); VFB 1.222 V.
ic('ST1S14', [(7,'VIN','power_in'),(5,'EN2','input'),(3,'EN1','input'),(4,'FB','input')],
             [(8,'SW','output'),(1,'BOOT','passive'),(2,'PG','open_collector'),(6,'GND','power_in'),(9,'EP_GND','power_in')])
# ST1S10PHR (DocID13844 Rev 7, as in ../cheap-version): 1 VIN_A, 2 INH, 3 VFB, 4 AGND, 5 SYNC, 6 VIN_SW, 7 SW, 8 PGND, EP GND.
ic('ST1S10', [(1,'VIN_A','power_in'),(6,'VIN_SW','power_in'),(2,'INH','input'),(3,'VFB','input'),(5,'SYNC','input')],
             [(7,'SW','output'),(4,'AGND','power_in'),(8,'PGND','power_in'),(9,'EP_GND','power_in')])
ic('MCP7940N', [(1,'X1','passive'),(2,'X2','passive'),(3,'VBAT','power_in'),(4,'VSS','power_in')],
               [(8,'VCC','power_in'),(7,'MFP','open_collector'),(6,'SCL','input'),(5,'SDA','bidirectional')])
ic('USBLC6', [(1,'IO1','passive'),(3,'IO2','passive'),(2,'GND','power_in')], [(6,'IO1','passive'),(4,'IO2','passive'),(5,'VREF','power_in')])
# INA238 VSSOP-10 (DGS) as TI's pin table: 1 A1, 2 A0, 3 ALERT, 4 SDA, 5 SCL, 6 VS, 7 GND, 8 VBUS, 9 IN-, 10 IN+.
ic('INA238', [(10,'IN+','input'),(9,'IN-','input'),(8,'VBUS','input'),(1,'A1','input'),(2,'A0','input')],
             [(6,'VS','power_in'),(4,'SDA','bidirectional'),(5,'SCL','input'),(3,'ALERT','open_collector'),(7,'GND','power_in')])
# OptoSupply OSTAPA57E2A, datasheet Ver.a.1.1: 1 G cathode, 2 B cathode, 3 common anode, 4 R cathode.
ic('LED_RGB_CA', [(3,'A','passive')], [(4,'R_K','passive'),(1,'G_K','passive'),(2,'B_K','passive')], 15.24)
ic('SD', [(1,'DAT2','passive'),(2,'DAT3_CS','input'),(3,'CMD_MOSI','input'),(4,'VDD','power_in'),(5,'CLK','input')],
         [(8,'DAT1','passive'),(7,'DAT0_MISO','tri_state'),(6,'VSS','power_in'),('SH','SHELL','passive'),('CD1','CARD_DETECT_1','passive'),('CD2','CARD_DETECT_2','passive')], 30.48)
for n in [2, 4, 6]:
    ic(f'J{n}', [(i, str(i), 'passive') for i in range(1, n+1)], [], 10.16)
esp = [(1,'GND','power_in'),(2,'3V3','power_in'),(3,'EN','input'),(4,'GPIO36','input'),(5,'GPIO39','input'),
       (6,'GPIO34','input'),(7,'GPIO35','input'),(8,'GPIO32','bidirectional'),(9,'GPIO33','bidirectional'),
       (10,'GPIO25','bidirectional'),(11,'GPIO26','bidirectional'),(12,'GPIO27','bidirectional'),(13,'GPIO14','bidirectional'),
       (14,'GPIO12','bidirectional'),(15,'GND','power_in'),(16,'GPIO13','bidirectional'),(17,'NC','no_connect'),(18,'NC','no_connect'),(19,'NC','no_connect'),(39,'GND_EP','power_in')]
er = [(38,'GND','power_in'),(37,'GPIO23','bidirectional'),(36,'GPIO22','bidirectional'),(35,'GPIO1_TXD0','bidirectional'),(34,'GPIO3_RXD0','bidirectional'),
      (33,'GPIO21','bidirectional'),(32,'NC','no_connect'),(31,'GPIO19','bidirectional'),(30,'GPIO18','bidirectional'),(29,'GPIO5','bidirectional'),
      (28,'GPIO17','bidirectional'),(27,'GPIO16','bidirectional'),(26,'GPIO4','bidirectional'),(25,'GPIO0_BOOT','bidirectional'),(24,'GPIO2','bidirectional'),
      (23,'GPIO15','bidirectional'),(22,'NC','no_connect'),(21,'NC','no_connect'),(20,'NC','no_connect')]
ic('ESP32', esp, er, 35.56)

# ------------------------------------------------------------------ sources: stock rows and purchase lines
STOCK = {}
with STOCK_CSV.open(encoding='utf-8-sig', newline='') as f:
    for row in csv.DictReader(f):
        if row['id']:
            STOCK[int(row['id'])] = row
BUY = json.loads((HERE/'procurement_lite.json').read_text(encoding='utf-8'))

R0603 = 'Resistor_SMD:R_0603_1608Metric'; R0805 = 'Resistor_SMD:R_0805_2012Metric'; R1206 = 'Resistor_SMD:R_1206_3216Metric'
C0603 = 'Capacitor_SMD:C_0603_1608Metric'; C0805 = 'Capacitor_SMD:C_0805_2012Metric'; C1206 = 'Capacitor_SMD:C_1206_3216Metric'
C1210 = 'Capacitor_SMD:C_1210_3225Metric'
KF301 = 'TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal'
SOIC8EP = 'Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.3mm'
SRC_FP = {   # footprint per source (stock id 'S<n>' or purchase key)
    'S70': R0805, 'S59': R0805, 'S71': R0805, 'S72': R0805, 'S73': R0805, 'S74': R0805,
    'S25': R0603, 'S55': R0603, 'S62': R0603,
    'S41': R1206, 'S51': R1206, 'S61': R1206, 'S39': R1206, 'S52': R1206,
    'S54': C0603, 'S57': C0805, 'S7': C0805, 'S21': C0805, 'S5': C0805,
    'S3': C1206, 'S6': C1210, 'S47': C1210,
    'S14': 'Capacitor_SMD:CP_Elec_10x10.5',
    'S26': SOIC8EP, 'S27': SOIC8EP, 'S114': 'LITE_Parts:VSSOP-10_3x3mm_P0.5mm',
    'CSD19531KCS': 'Package_TO_SOT_THT:TO-220-3_Vertical',
    'S20': 'Diode_SMD:D_SMC',
    'S31': 'Inductor_SMD:L_1008_2520Metric', 'S93': KF301,
    'S90': 'Connector_JST:JST_XH_S4B-XH-A_1x04_P2.50mm_Horizontal',
    'S87': 'LITE_Parts:KF2510_1x04_P2.54mm_RightAngle',
    'S95': 'Button_Switch_THT:SW_PUSH_6mm_H9.5mm',
    'ESP32-WROOM-32E-N4': 'EU_Parts:ESP32-WROOM-32E_NoVias',
    'MCP7940N-I/SN': 'LITE_Parts:SOIC-8_3.9x4.9mm_P1.27mm', 'ABS07-32.768KHZ-7-T': 'EU_Mechanical:Crystal_Abracon_ABS07',
    'MEM2061-01-188-00-A': 'EU_Mechanical:microSD_GCT_MEM2061-01-188-00-A', 'SMBJ26A-E3/52': 'Diode_SMD:D_SMB',
    'AO3422': 'Package_TO_SOT_SMD:SOT-23', 'JNRH1207-220M-A': 'EU_Mechanical:L_Jantek_JNRH1207',
    '3413.0331.11': 'EU_Mechanical:Fuse_Schurter_UST_USI1206_Reflow', '3413.0327.11': 'EU_Mechanical:Fuse_Schurter_UST_USI1206_Reflow',
    '3403.0169.11': 'EU_Mechanical:Fuse_Schurter_UMT250',
    '3403.0166.11': 'EU_Mechanical:Fuse_Schurter_UMT250', 'USBLC6-2SC6': 'Package_TO_SOT_SMD:SOT-23-6',
    'ZL201-10G': 'Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical', 'UPW1H221MPD': 'Capacitor_THT:CP_Radial_D10.0mm_P5.00mm',
    'BC847B-DIO': 'Package_TO_SOT_SMD:SOT-23', 'BC857B': 'Package_TO_SOT_SMD:SOT-23', 'BZV55C12': 'Diode_SMD:D_MiniMELF',
    'BC-2001': 'LITE_Mechanical:Battery_COMF_BC-2001',
    'C1206C100J5GACTU': C1206, 'OSTAPA57E2A': 'LITE_Parts:LED_OSTAPA57E2A_Formed_P2.54mm', 'SMD1206-10K-1%': R1206, 'CRCW120682K0FKTABC': R1206, 'SMD1206-6K2-1%': R1206,
}
# Footprints already drawn from manufacturer drawings in EU-0.1 / cheap-version (physical fit still pending).
# 3413.0331.11 (UST 1206, 20 A) uses the same SCHURTER 1206 reflow land as the V1 15 A fuse 3413.0330.11.
CHECKED_FP = {'MEM2061-01-188-00-A', 'BC-2001', 'ABS07-32.768KHZ-7-T', 'JNRH1207-220M-A', '3413.0331.11',
              '3413.0327.11', '3403.0169.11', '3403.0166.11', 'ESP32-WROOM-32E-N4'}


def is_stock(src):
    return src.startswith('S') and src[1:].isdigit()


def src_label(src):
    if is_stock(src):
        r = STOCK[int(src[1:])]
        return r['MPN'] or f"{r['название']} {r['номинал']}"
    return src


def add(sh, ref, sym, value, src, nets, fp='', note='', pos=None, bom=True):
    """Place one part. src = 'S<id>' (owner's stock row) or a key of procurement_lite.json; '' for non-purchased symbols."""
    if src:
        assert (int(src[1:]) in STOCK) if is_stock(src) else (src in BUY), src
    if not fp and src:
        fp = SRC_FP[src]
    pd = {str(k): v for k, v in (nets.items() if isinstance(nets, dict) else enumerate(nets, 1))}
    expected = {x['number'] for x in symbols[sym]['pins']}
    if set(pd) != expected:
        raise ValueError((ref, 'pin map differs', set(pd) ^ expected))
    assert ref not in {c['ref'] for c in parts}, ref
    c = dict(sheet=sh, ref=ref, symbol=sym, value=value, mpn=src_label(src) if src else '', src=src, nets=pd, footprint=fp,
             note=note, position=pos, in_bom=bom)
    parts.append(c); sheets[sh]['parts'].append(c)
    if src and bom:
        counts[src] += 1
    return c


def r(sh, ref, val, src, a, b, note='', pos=None):
    return add(sh, ref, 'R', val, src, [a, b], note=note, pos=pos)


def c(sh, ref, val, src, a, b='GND', pos=None, note=''):
    polar = src in ('S13', 'S14', 'UPW1H221MPD')
    return add(sh, ref, 'CP' if polar else 'C', val, src, [a, b], pos=pos, note=note)


GND = 'GND'; V3 = '+3V3'; V12 = '+12V'; V24 = '+24V_FUSED'

# ------------------------------------------------------------------ 01 | input
sheet('InputPower', '01 | 24 V input, reverse polarity, fuses and power monitor', [
 'External PSU 24.0 V, max 25 V, at least 360 W / 15 A (V2: six 25 W lamps plus the 100 W PTC; board envelope 13 A). J101: 1 = +24 V, 2 = PSU return.',
 'Reverse polarity: Q102 || Q103 CSD19531KCS (TO-220, 1=G, 2=D/tab, 3=S) in the return path; drains = J101.2, sources = RTN_SHUNT. Gates from +24V_FUSED',
 'through R108 10k and R109/R110 100 R, clamped by D103 12 V (VGS max +-20 V). At 13 A with 60/40 sharing: <=0.7 W per FET (7.7 mOhm x 1.5 hot).',
 'Power monitor: R111||R112||R113 = 2.33 mOhm (three 7 mOhm metal strip, stock) between board GND and the Q102/Q103 sources; INA238 U102 at I2C 0x40.',
 'INA238 VBUS takes +24V_FUSED directly (85 V limit). IN+ (pin 10) is a Kelvin tap on the GND pad of R111, IN- (pin 9) on its RTN_SHUNT pad.',
 'ADCRANGE=1 (+-40.96 mV): full scale 17.6 A for the 13 A envelope. Firmware: the shunt constant changes from 3.5 mOhm (V1) to 2.333 mOhm.',
 'Fuses: F101 main 20 A UST; F102 PTC 8 A; F104 12 V buck 2 A; F105 pump 1 A; F501-F506 2 A per lamp on sheet 06. All time-lag; coordination NOT qualified.',
 'Programmer/USB ground must never become the power return. D101 clamps +24V_FUSED at ~42 V.',
], 95)
add('InputPower', 'J101', 'J2', '24 V IN', 'S93', ['+24V_RAW', 'PSU_RTN'], note='KF301-5.0-2P: pin 1 +24 V, pin 2 return. Design envelope 13 A: confirm the terminal batch rating (cheap-version cites 17 A from LCSC) and use >=1.5 mm2 wire')
for ref, val, src, a, b in [('F101', '20 A slow', '3413.0331.11', '+24V_RAW', V24), ('F102', '8 A slow', '3413.0327.11', V24, '+24V_PTC'),
                            ('F104', '2 A slow', '3403.0169.11', V24, '+24V_AUX'), ('F105', '1 A slow', '3403.0166.11', V24, '+24V_PUMP')]:
    add('InputPower', ref, 'F', val, src, [a, b], note='SCHURTER reflow lands from EU-0.1; fuse clearing I2t/coordination remain unqualified')
add('InputPower', 'D101', 'TVS', 'SMBJ26A', 'SMBJ26A-E3/52', [V24, GND])
for ref, rg, pos in [('Q102', 'REVP_G1', (265, 200)), ('Q103', 'REVP_G2', (330, 200))]:
    add('InputPower', ref, 'NMOS', 'CSD19531KCS', 'CSD19531KCS', {1: rg, 2: 'PSU_RTN', 3: 'RTN_SHUNT'},
        note='Parallel reverse-polarity FETs, TO-220 vertical without heatsink: ~6.5 A each at the 13 A envelope. Drain tab = PSU_RTN (live). Gate clamped by D103', pos=pos)
r('InputPower', 'R108', '10k 1206', 'SMD1206-10K-1%', V24, 'REVP_GATE', 'Gate feed; 90 mW at the 42 V TVS clamp (1206 is required)')
r('InputPower', 'R109', '100R', 'S71', 'REVP_GATE', 'REVP_G1', 'Separate gate resistors keep the two parallel FETs from ringing together')
r('InputPower', 'R110', '100R', 'S71', 'REVP_GATE', 'REVP_G2')
add('InputPower', 'D103', 'D', 'BZV55C12 12 V', 'BZV55C12', ['REVP_GATE', GND], note='Clamps Q102/Q103 gate-source to 12 V (CSD19531KCS VGS max +-20 V)')
for ref in ('C101', 'C102'):
    c('InputPower', ref, '220u 50V', 'UPW1H221MPD', V24)
r('InputPower', 'R111', '7m 1% 1W', 'S52', GND, 'RTN_SHUNT', 'Shunt for U102; pad 1 = board GND side (Kelvin IN+), pad 2 = Q102/Q103 source side (IN-)', pos=(165, 95))
r('InputPower', 'R112', '7m 1% 1W', 'S52', GND, 'RTN_SHUNT', 'Parallel with R111 and R113: 2.333 mOhm, ADCRANGE=1 full scale 17.6 A', pos=(165, 112))
r('InputPower', 'R113', '7m 1% 1W', 'S52', GND, 'RTN_SHUNT', 'Third parallel shunt (V2): keep the three shunt paths symmetric around the R111 Kelvin taps', pos=(165, 78))
add('InputPower', 'U102', 'INA238', 'INA238', 'S114', {1: GND, 2: GND, 3: None, 4: 'I2C_SDA', 5: 'I2C_SCL', 6: V3, 7: GND, 8: V24, 9: 'RTN_SHUNT', 10: GND},
    note='I2C 0x40 (A0=A1=GND). IN+ Kelvin to R111.1, IN- Kelvin to R111.2; VBUS direct from +24V_FUSED. Single piece in stock, shared with V1: no spare', pos=(240, 75))
c('InputPower', 'C115', '100n VS', 'S54', V3, pos=(310, 95))

# ------------------------------------------------------------------ 02 | 12 V buck
sheet('Aux12V', '02 | 24 V to 12 V buck (ST1S14)', [
 'U101 ST1S14PHR (stock): 5.5-48 V in, 3 A, 850 kHz, VFB 1.222 V. Pins: 1 BOOT, 2 PG (unused), 3 EN1 (active low, GND), 4 FB, 5 EN2 (active high), 6 GND, 7 VIN, 8 SW, EP GND.',
 'Vout = 1.222 V x (1 + (82k + 6k2) / 10k) = 12.0 V (11.8-12.2 V over VFB and 1% resistors). Load: fans <= 10 W, 3.3 V buck, PTC and lamp drivers: ~1.1 A.',
 'EN2 divider 3 x 150k / 47k from +24V_AUX: 1.5 V threshold at 15.9 V input, 2.04 V at 21.6 V, 3.97 V at the 42 V TVS clamp. This is the only input UVLO of EU-LITE:',
 'below ~14 V the 12 V and 3.3 V rails drop, the ESP32 resets and the lamp gate driver loses +12V: R507 holds Q501 off. EU-0.1 LM2903/BLOCK_OC is not fitted.',
 'L101 22 uH (ripple 0.33 A at 25 V in, as in cheap-version), D102 SS56 catch diode (the ST1S14 is not synchronous), C110 820 uF/25 V (ESR ~60 mOhm).',
 'ST1S14 internal compensation with an 820 uF electrolytic: stability is NOT calculated; bench check in review/Bench_checklist.md. C107 100n sits on the VIN pin.',
], 85)
add('Aux12V', 'U101', 'ST1S14', 'ST1S14PHR', 'S26', {1: 'AUX_BOOT', 2: None, 3: GND, 4: 'AUX_FB', 5: 'AUX_EN2', 6: GND, 7: '+24V_AUX', 8: 'AUX_SW', 9: GND},
    note='Exposed pad (9) = GND. Stock: 3 pcs', pos=(90, 60))
add('Aux12V', 'L101', 'L', '22u', 'JNRH1207-220M-A', ['AUX_SW', V12], note='Jantek JNRH1207 22 uH (EU-0.1 L601). Shop card (URL slug): Ioper 5.4 A, Isat 5.6 A, 52.5 mOhm +-30%: above the ST1S14 current limit 3.7-5.2 A')
add('Aux12V', 'D102', 'D', 'SS56 60V 5A', 'S20', ['AUX_SW', GND])
c('Aux12V', 'C103', '10u 50V', 'S47', '+24V_AUX'); c('Aux12V', 'C104', '10u 50V', 'S47', '+24V_AUX')
c('Aux12V', 'C105', '2u2 50V', 'S3', '+24V_AUX'); c('Aux12V', 'C107', '100n HF', 'S54', '+24V_AUX')
c('Aux12V', 'C106', '100n BOOT', 'S54', 'AUX_BOOT', 'AUX_SW')
c('Aux12V', 'C110', '820u 25V', 'S14', V12); c('Aux12V', 'C111', '10u 35V', 'S6', V12); c('Aux12V', 'C112', '100n', 'S54', V12)
r('Aux12V', 'R101', '82k 1%', 'CRCW120682K0FKTABC', V12, 'AUX_FB_MID'); r('Aux12V', 'R102', '6k2 1%', 'SMD1206-6K2-1%', 'AUX_FB_MID', 'AUX_FB')
r('Aux12V', 'R103', '10k 1%', 'S59', 'AUX_FB', GND)
r('Aux12V', 'R104', '150k', 'S74', '+24V_AUX', 'AUX_EN2_A'); r('Aux12V', 'R105', '150k', 'S74', 'AUX_EN2_A', 'AUX_EN2_B')
r('Aux12V', 'R106', '150k', 'S74', 'AUX_EN2_B', 'AUX_EN2'); r('Aux12V', 'R107', '47k 1%', 'S61', 'AUX_EN2', GND)

# ------------------------------------------------------------------ 03 | 3.3 V and ESP32
sheet('MCU', '03 | 12 V to 3.3 V buck (ST1S10) and ESP32', [
 'U201 ST1S10PHR (stock): 2.5-18 V in, 3 A, synchronous, 900 kHz with SYNC on GND. Pins: 1 VIN_A, 2 INH (to VIN: on), 3 VFB, 4 AGND, 5 SYNC, 6 VIN_SW, 7 SW, 8 PGND, EP GND.',
 'Vout = 0.8 V x (1 + 100k / (47k || 100k)) = 3.30 V (3.24-3.37 V over VFB 0.784-0.816 V). Divider parts are 1% stock resistors.',
 'L201 2.2 uH (stock SRN2512): ripple 1.2 A, peak 1.6 A at 1 A; LC corner ~19-22 kHz, close to the datasheet design (3.3 uH / 22 uF = 18.7 kHz).',
 'GPIO34/35 (module pins 6/7): fan tachometers are not fitted in EU-LITE; the inputs only have 10k pull-ups (R206/R207) so EU-0.1 firmware reads 0 rpm. Boot strapping pins 2, 5, 12, 15 have no external loads.',
 'ESP32 pins 17-22 and 32 are NC on this module. J201 UART is 3.3 V logic: 1 GND, 2 3V3_REF, 3 TX, 4 RX, 5 EN, 6 BOOT.',
 'Programmer power is not a board supply. Keep its VCC disconnected while the PSU powers the board.',
], 110)
add('MCU', 'U201', 'ST1S10', 'ST1S10PHR', 'S27', {1: V12, 2: V12, 3: 'LOGIC_FB', 4: GND, 5: GND, 6: V12, 7: 'LOGIC_SW', 8: GND, 9: GND}, note='Stock: 4 pcs', pos=(80, 45))
add('MCU', 'L201', 'L', '2u2', 'S31', ['LOGIC_SW', V3], note='Bourns SRN2512-2R2M, Isat 2.7 A, DCR <= 102 mOhm; package 2.5 x 2.0 mm. Stock: 2 pcs')
c('MCU', 'C201', '10u 35V', 'S6', V12); c('MCU', 'C202', '100n', 'S54', V12); c('MCU', 'C203', '2u2 VIN_A', 'S57', V12)
for ref in ('C204', 'C205', 'C206'):
    c('MCU', ref, '22u 10V', 'S7', V3)
r('MCU', 'R201', '100k 1%', 'S55', V3, 'LOGIC_FB'); r('MCU', 'R202', '47k 1%', 'S61', 'LOGIC_FB', GND); r('MCU', 'R203', '100k 1%', 'S55', 'LOGIC_FB', GND)
en = {str(i): None for i in range(1, 40)}
en.update({'1': GND, '2': V3, '3': 'ESP_EN', '6': 'FAN1_TACH', '7': 'FAN2_TACH', '8': 'PTC_GPIO', '9': 'PUMP_GPIO', '10': 'LED_PWM_GPIO', '11': 'FAN1_GPIO', '12': 'FAN2_GPIO',
           '13': 'FLOAT_GPIO', '15': GND, '16': 'SD_CS', '25': 'ESP_BOOT', '26': 'LED_R_GPIO', '27': 'LED_G_GPIO', '28': 'LED_B_GPIO', '30': 'SD_SCK_GPIO', '31': 'SD_MISO', '33': 'I2C_SDA',
           '34': 'UART_RX', '35': 'UART_TX', '36': 'I2C_SCL', '37': 'SD_MOSI_GPIO', '38': GND, '39': GND})
add('MCU', 'U202', 'ESP32', 'ESP32-WROOM-32E-N4', 'ESP32-WROOM-32E-N4', en, note='Antenna keepout and GND pad 39 mandatory; pins 6/7 (GPIO34/35, former tachometers) only pulled up; pins 26/27/28 (GPIO4/16/17) drive the RGB status LED', pos=(285, 69))
c('MCU', 'C207', '22u 10V', 'S7', V3); c('MCU', 'C208', '100n', 'S54', V3); c('MCU', 'C209', '2u2 RESET', 'S57', 'ESP_EN')
r('MCU', 'R204', '10k', 'S70', V3, 'ESP_EN'); r('MCU', 'R205', '10k', 'S70', V3, 'ESP_BOOT')
r('MCU', 'R206', '10k', 'S70', V3, 'FAN1_TACH', 'GPIO34 is input-only without internal pull-up: held high so EU-0.1 firmware reads 0 rpm instead of noise')
r('MCU', 'R207', '10k', 'S70', V3, 'FAN2_TACH', 'Same for GPIO35; the fan headers carry no tach signal')
add('MCU', 'SW201', 'SW', 'RESET', 'S95', ['ESP_EN', GND], note='6x6x10 THT tact switch (stock); 4 leads form two common pairs')
add('MCU', 'SW202', 'SW', 'BOOT', 'S95', ['ESP_BOOT', GND])
add('MCU', 'J201', 'J6', 'UART 3V3', 'ZL201-10G', [GND, V3, 'UART_TX', 'UART_RX', 'ESP_EN', 'ESP_BOOT'], note='Cut 6 of the 10 header positions; 4 spare')

# ------------------------------------------------------------------ 04 | RTC and microSD
sheet('Peripherals', '04 | RTC, battery and microSD', [
 'MCP7940N: I2C 0x6F; firmware must set ST and VBATEN and validate the time before schedules (as in EU-0.1).',
 'CR2032 is not rechargeable. No path from +3V3 to VBAT; MFP is unused.',
 'SD uses SPI; CMD and DAT0..DAT3 all have 10k pull-ups, including unused DAT1/2. SCK and MOSI series resistors are 100 R (stock) instead of 220 R.',
 'GCT MEM2061 drawing Rev A1: shell is ground; normally closed detect contact is unused. CD1/CD2 pads unused; both SH tabs grounded.',
], 85)
add('Peripherals', 'U301', 'MCP7940N', 'MCP7940N', 'MCP7940N-I/SN', {1: 'RTC_X1', 2: 'RTC_X2', 3: 'VBAT', 4: GND, 5: 'I2C_SDA', 6: 'I2C_SCL', 7: None, 8: V3}, pos=(75, 60))
add('Peripherals', 'Y301', 'Y', '32.768k CL7p', 'ABS07-32.768KHZ-7-T', ['RTC_X1', 'RTC_X2'], note='Crystal ESR/load/drive to verify on the prototype')
c('Peripherals', 'C301', '10p C0G', 'C1206C100J5GACTU', 'RTC_X1'); c('Peripherals', 'C302', '10p C0G', 'C1206C100J5GACTU', 'RTC_X2')
c('Peripherals', 'C303', '100n', 'S54', V3)
add('Peripherals', 'BT301', 'BAT', 'CR2032 + BC-2001', 'BC-2001', ['VBAT', GND], note='COMF BC-2001: side pads 1 positive; centre pad 2 negative')
add('Peripherals', 'J301', 'SD', 'MEM2061 SPI', 'MEM2061-01-188-00-A', {1: 'SD_DAT2', 2: 'SD_CS', 3: 'SD_MOSI', 4: V3, 5: 'SD_SCK', 6: GND, 7: 'SD_MISO', 8: 'SD_DAT1', 'SH': GND, 'CD1': None, 'CD2': None}, pos=(280, 60))
c('Peripherals', 'C304', '22u 10V', 'S7', V3); c('Peripherals', 'C305', '100n', 'S54', V3)
r('Peripherals', 'R301', '100R', 'S71', 'SD_SCK_GPIO', 'SD_SCK'); r('Peripherals', 'R302', '100R', 'S71', 'SD_MOSI_GPIO', 'SD_MOSI')
for n, net in enumerate(['SD_CS', 'SD_MOSI', 'SD_MISO', 'SD_DAT1', 'SD_DAT2'], 303):
    r('Peripherals', f'R{n}', '10k', 'S70', V3, net)
r('Peripherals', 'R308', '4k7', 'S72', V3, 'I2C_SDA'); r('Peripherals', 'R309', '4k7', 'S72', V3, 'I2C_SCL')

# ------------------------------------------------------------------ 05 | interfaces and fans
sheet('Interfaces', '05 | SHT4x, float and two 4-wire fans (no tachometers)', [
 'Both fans combined <= 10 W at 12 V; combined fuse F401 2 A on the 12 V side. Start currents not measured.',
 'Fan PWM = open drain, 25 kHz. MCU HIGH pulls fan PWM LOW: invert firmware duty. Reset / Hi-Z opens the PWM MOSFET: full speed while +12V is present.',
 'Fan tachometer outputs are NOT connected (owner decision 24.10.2026): header pin 3 is NC, GPIO34/35 only have the 10k pull-ups R206/R207 on the MCU sheet, no ESDA6V1L.',
 'J411/J421 fan headers: 1 GND, 2 +12 V, 3 tach (NC), 4 PWM. J401 SHT4x: 1 +3V3, 2 GND, 3 SDA, 4 SCL; U401 USBLC6 on SDA/SCL.',
 'The float input has no USBLC6 (EU-0.1 U402 is dropped to save 0.67 EUR): 1k series + 100n + 10k pull-up only. J403 closed -> GPIO14 LOW.',
], 110)
add('Interfaces', 'J401', 'J4', 'SHT4x XH2.54-4P-90', 'S90', [V3, GND, 'SHT_SDA', 'SHT_SCL'],
    note='1 +3V3, 2 GND, 3 SDA, 4 SCL. XH clone, right angle; real pitch is the JST XH 2.50 mm: check pitch and wire order')
add('Interfaces', 'J403', 'J2', 'FLOAT / GND', 'S93', ['FLOAT_EXT', GND])
r('Interfaces', 'R401', '100R', 'S71', 'I2C_SDA', 'SHT_SDA'); r('Interfaces', 'R402', '100R', 'S71', 'I2C_SCL', 'SHT_SCL')
add('Interfaces', 'U401', 'USBLC6', 'USBLC6-2SC6', 'USBLC6-2SC6', {1: 'SHT_SDA', 6: 'SHT_SDA', 3: 'SHT_SCL', 4: 'SHT_SCL', 2: GND, 5: V3}, pos=(65, 45))
c('Interfaces', 'C401', '100n', 'S54', V3)
r('Interfaces', 'R403', '1k', 'S62', 'FLOAT_EXT', 'FLOAT_GPIO'); r('Interfaces', 'R404', '10k', 'S70', V3, 'FLOAT_GPIO')
c('Interfaces', 'C403', '100n FILTER', 'S54', 'FLOAT_GPIO')
add('Interfaces', 'F401', 'F', '2 A slow', '3403.0169.11', [V12, '+12V_FANS'])
for k in (1, 2):
    i = 410 if k == 1 else 420
    sh = 'Interfaces'; prefix = f'FAN{k}'
    add(sh, f'J{i+1}', 'J4', f'FAN{k} 2510-4P 90deg', 'S87', [GND, '+12V_FANS', None, prefix + '_PWM'],
        note='Right-angle 2510-4AW footprint; stock KF2510/KF2540 variant needs physical confirmation. Pinout unchanged: 1 GND, 2 +12 V, 3 tach (NC), 4 PWM. Reference envelope: LHE A2543-WR04; check latch and pin 1 on actual part')
    add(sh, f'Q{i+1}', 'NMOS_GSD', f'FAN{k} AO3422', 'AO3422', {1: prefix + '_GATE', 2: GND, 3: prefix + '_PWM'}, pos=(65 if k == 1 else 195, 100))
    r(sh, f'R{i+1}', '1k GATE', 'S62', prefix + '_GPIO', prefix + '_GATE', '25 kHz edge timing must be checked')
    r(sh, f'R{i+2}', '100k', 'S55', prefix + '_GATE', GND)

# ------------------------------------------------------------------ 06 | six 24 V lamp outputs, one PWM low-side switch
# Lamps (owner, 2026-10-28): KQO QL63, 24 V DC constant-voltage, 25 W, Samsung LM281B+, 1.6 mm aluminium board ~550 x 30 mm.
LAMPS = 6
LAMP_W = 25.0; LAMP_V = 24.0
LAMP_ENVELOPE_A = 1.25   # per lamp at the 25 V PSU limit: resistor-ballasted strings draw more than 25 W / 24 V; measure
sheet('LED', '06 | Six 24 V lamp outputs, one PWM low-side switch', [
 'Lamps (owner 2026-10-28): KQO QL63, 24 V DC constant-voltage, 25 W, Samsung LM281B+, 1.6 mm aluminium board ~550 x 30 mm, no driver inside.',
 'Each lamp ~1.04 A at 24.0 V. Design envelope 1.25 A per lamp at the 25 V PSU limit (series resistors: current rises with voltage), 7.5 A for six. Measure one lamp first.',
 'J501..J506: pin 1 = +24V_LEDn behind its own 2 A time-lag fuse F501..F506, pin 2 = LED_NEG, the common SWITCHED return. LED_NEG is NOT ground:',
 'no lamp negative or aluminium body may touch board GND or a grounded enclosure, otherwise that lamp bypasses Q501 and stays on.',
 'Q501 CSD19531KCS (TO-220, 1=G, 2=D/tab, 3=S): drain LED_NEG, source GND. 7.5 A: ~0.65 W with 7.7 mOhm x 1.5 hot, no heatsink; the tab is live (LED_NEG).',
 'Gate driver from +12V, default off: GPIO25 -> 10k -> Q502 BC847B (4k7 to GND) -> 4k7 -> Q503 BC857B (10k to +12V) -> LED_DRV (4k7 to GND) ->',
 'totem pole Q504 BC847B / Q505 BC857B -> 100 R -> gate, 10k gate pull-down. GPIO HIGH = lamps on (same polarity as the V1 nDIM); reset, Hi-Z or no +12V = off.',
 'PWM 1 kHz / 8 bit as in the EU-LITE firmware; the LM3429 minimum pulse no longer applies. Gate current <= 0.12 A: sub-microsecond edges, ~0.03 W switching loss.',
 'D501 SS56 freewheel (cathode +24V_FUSED, anode LED_NEG) clamps the lamp-cable inductance at turn-off. C501/C502 10u + C503 100n hold +24V_FUSED at the stage.',
 'The board does not regulate lamp current: light = PWM duty x lamp current at the PSU voltage. Lamp current at 24/25 V and PWM behaviour need bench checks.',
], 85)
for k in range(1, LAMPS + 1):
    add('LED', f'F50{k}', 'F', '2 A slow', '3403.0169.11', [V24, f'+24V_LED{k}'],
        note='SCHURTER UMT 250 2 A time-lag (same part as F104/F401): protects one lamp lead; envelope 1.25 A = 62.5 % of rating')
    add('LED', f'J50{k}', 'J2', f'LAMP{k} + / -', 'S93', [f'+24V_LED{k}', 'LED_NEG'],
        note='KF301-5.0-2P: pin 1 +24 V (fused), pin 2 switched lamp negative LED_NEG (not GND). One 25 W lamp per terminal; wire >=0.5 mm2')
add('LED', 'Q501', 'NMOS', 'CSD19531KCS', 'CSD19531KCS', {1: 'LED_GATE', 2: 'LED_NEG', 3: GND},
    note='TI SLPS407D: 100 V, VGS +-20 V, RDS(on) 6.4/7.7 mOhm typ/max at 10 V, Qg 38 nC, RthJA 62 C/W. Tab = drain (LED_NEG): insulate from any grounded heatsink', pos=(320, 65))
add('LED', 'D501', 'D', 'SS56 60V 5A', 'S20', [V24, 'LED_NEG'], note='Freewheel for the lamp cables: cathode +24V_FUSED, anode LED_NEG; carries current only for microseconds after each turn-off')
add('LED', 'Q502', 'NPN', 'BC847B-DIO', 'BC847B-DIO', {1: 'LED_NPN_B', 2: GND, 3: 'LED_NPN_C'}, pos=(55, 60))
add('LED', 'Q503', 'PNP', 'BC857B', 'BC857B', {1: 'LED_PNP_B', 2: V12, 3: 'LED_DRV'}, pos=(130, 60))
add('LED', 'Q504', 'NPN', 'BC847B-DIO', 'BC847B-DIO', {1: 'LED_DRV', 2: 'LED_GDRV', 3: V12}, pos=(205, 50),
    note='Totem pole source side: emitter follower from +12V; gate high ~11 V')
add('LED', 'Q505', 'PNP', 'BC857B', 'BC857B', {1: 'LED_DRV', 2: 'LED_GDRV', 3: GND}, pos=(205, 85),
    note='Totem pole sink side: emitter follower to GND discharges the gate quickly at turn-off')
r('LED', 'R501', '10k', 'S70', 'LED_PWM_GPIO', 'LED_NPN_B')
r('LED', 'R502', '4k7', 'S72', 'LED_NPN_B', GND, 'Keeps Q502 off (<0.45 V) even with the ESP32 internal pull-up on GPIO25, as R702 on the PTC driver')
r('LED', 'R503', '4k7', 'S72', 'LED_NPN_C', 'LED_PNP_B', '2.3 mA base current saturates Q503 (load ~3 mA)')
r('LED', 'R504', '10k', 'S70', V12, 'LED_PNP_B')
r('LED', 'R505', '4k7', 'S72', 'LED_DRV', GND, 'Pulls the totem-pole bases low when Q503 is off')
r('LED', 'R506', '100R', 'S71', 'LED_GDRV', 'LED_GATE', 'Gate resistor: peak gate current <= 0.12 A, inside the BC847B/BC857B pulse rating')
r('LED', 'R507', '10k', 'S70', 'LED_GATE', GND, 'Holds Q501 off when +12V is absent or the driver is unpowered')
c('LED', 'C501', '10u 50V', 'S47', V24); c('LED', 'C502', '10u 50V', 'S47', V24); c('LED', 'C503', '100n', 'S54', V24)
c('LED', 'C504', '100n DRV', 'S54', V12)

# ------------------------------------------------------------------ 07 | PTC heater and pump
sheet('Loads', '07 | PTC heater and 24 V pump', [
 'PTC is ON/OFF only, no fast PWM. External independent thermal cut-out is required in series with the heater.',
 'GPIO32 HIGH turns on Q701 NPN, Q702 PNP, then Q703 power MOSFET CSD19531KCS (TO-220, 12 V gate drive); LOW / Hi-Z turns it off.',
 'Q703 CSD19531KCS: 1=G, 2=D/tab, 3=S. RDS(on) max 7.7 mOhm at VGS=10 V; ~0.22 W at 4.34 A using 1.5x hot resistance. Verify temperature under load; TO-220 tab is live at PTC_DRAIN.',
 'The stock IRS4427S is NOT used as driver: its input threshold left 0.14 V of margin at 3.3 V (owner decision in ../cheap-version, 04.10.2026).',
 'Pump GPIO33 HIGH turns on Q704 AO3422. D701 SS56 flyback: cathode to fused +24V_PUMP, anode to PUMP_DRAIN.',
 'R702 4k7 keeps Q701 off (<0.4 V) even with the ESP32 internal pull-up on GPIO32; HIGH still gives ~0.13 mA base.',
 '100k GPIO/gate pulls stay active during reset. Pump stall current and fuse coordination still unmeasured.',
], 70)
add('Loads', 'J701', 'J2', 'PTC + / RETURN', 'S93', ['+24V_PTC', 'PTC_DRAIN'])
add('Loads', 'Q701', 'NPN', 'BC847B-DIO', 'BC847B-DIO', {1: 'PTC_NPN_B', 2: GND, 3: 'PTC_NPN_C'}, pos=(65, 55))
add('Loads', 'Q702', 'PNP', 'BC857B', 'BC857B', {1: 'PTC_PNP_B', 2: V12, 3: 'PTC_DRV'}, pos=(165, 55))
add('Loads', 'Q703', 'NMOS', 'CSD19531KCS', 'CSD19531KCS', {1: 'PTC_GATE', 2: 'PTC_DRAIN', 3: GND},
    note='Salvats catalog snapshot 2026-10-23; TI datasheet: 100 V, TO-220, RDS(on) max 7.7 mOhm at VGS=10 V. Tab = drain; ~0.22 W at 4.34 A with 1.5x hot-resistance estimate. Check physical temperature; insulate live tab from grounded heatsink.', pos=(265, 55))
r('Loads', 'R701', '10k', 'S70', 'PTC_GPIO', 'PTC_NPN_B'); r('Loads', 'R702', '4k7', 'S72', 'PTC_NPN_B', GND)
r('Loads', 'R703', '2k', 'S51', 'PTC_NPN_C', 'PTC_PNP_B'); r('Loads', 'R704', '10k', 'S70', V12, 'PTC_PNP_B')
r('Loads', 'R705', '100R', 'S71', 'PTC_DRV', 'PTC_GATE'); r('Loads', 'R706', '2k', 'S51', 'PTC_GATE', GND)
add('Loads', 'J702', 'J2', 'PUMP + / RETURN', 'S93', ['+24V_PUMP', 'PUMP_DRAIN'])
add('Loads', 'Q704', 'NMOS_GSD', 'AO3422', 'AO3422', {1: 'PUMP_GATE', 2: GND, 3: 'PUMP_DRAIN'}, pos=(365, 55))
r('Loads', 'R707', '1k GATE', 'S62', 'PUMP_GPIO', 'PUMP_GATE'); r('Loads', 'R708', '100k', 'S55', 'PUMP_GATE', GND)
add('Loads', 'D701', 'D', 'SS56 60V 5A', 'S20', ['+24V_PUMP', 'PUMP_DRAIN'])

# ------------------------------------------------------------------ 08 | RGB status LED
sheet('Status', '08 | RGB status LED', [
 'D901: OptoSupply OSTAPA57E2A, diffused RGB, COMMON ANODE on +12V. Pins 1 G, 2 B, 3 A, 4 R. Form leads from 1.25 to 2.54 mm pitch; body raised 5 mm above PCB.',
 'Each channel: GPIO -> 4k7 -> BC847B base (100k to GND), collector -> 2k -> LED cathode. The 12 V supply makes the current independent of the Vf of the green and blue dies',
 '(a 3.3 V supply leaves only 0.1-0.3 V across a resistor for Vf 3.0-3.2 V). ~4.9 mA red, ~4.4 mA green/blue; resistor 49 mW; 100k keeps the LED dark during reset.',
 'GPIO4 = red, GPIO16 = green, GPIO17 = blue (module pins 26/27/28; no strapping function, free on WROOM-32E-N4). Drive with LEDC PWM; HIGH = channel on.',
 'The 12 V rail is off below ~14 V input, so the indicator is dark exactly when the ESP32 is not running.',
], 70)
add('Status', 'D901', 'LED_RGB_CA', 'OSTAPA57E2A', 'OSTAPA57E2A', {1: 'LED_G_K', 2: 'LED_B_K', 3: V12, 4: 'LED_R_K'},
    note='Electrobase EB1854986288, owner quote 2026-10-25: EUR 0.85 incl. VAT. Manufacturer Ver.a.1.1: flat top, pins G/B/A/R; formed-lead footprint, 1.0 mm holes, 1.8 mm pads. Check formed fit before soldering.', pos=(120, 70))
for k, ch in enumerate(('R', 'G', 'B')):
    q_ = f'Q9{k+1:02d}'
    add('Status', q_, 'NPN', f'BC847B ({ch})', 'BC847B-DIO', {1: f'LED_{ch}_BASE', 2: GND, 3: f'LED_{ch}_C'}, pos=(60 + 70*k, 130))
    r('Status', f'R9{k+1:02d}', '4k7', 'S72', f'LED_{ch}_GPIO', f'LED_{ch}_BASE')
    r('Status', f'R9{k+4:02d}', '100k', 'S73', f'LED_{ch}_BASE', GND)
    r('Status', f'R9{k+7:02d}', '2k', 'S51', f'LED_{ch}_C', f'LED_{ch}_K', f'{ch} channel current limit: 12 V supply')

# A power flag documents the external/regulator source on each independently fused power net.
for k, net in enumerate([GND, '+24V_RAW', V24, '+24V_PTC', '+24V_PUMP', '+24V_AUX', V12, V3, 'VBAT'], 1):
    add('InputPower', f'#FLG{k:02}', 'FLAG', 'PWR_FLAG', '', {1: net}, bom=False, pos=(20+33*((k-1) % 11), 240))
# Free PCB probe pads, deliberately not purchased or included in the parts count.
for n, (sh, net) in enumerate([('MCU', V3), ('Aux12V', V12), ('LED', 'LED_GATE'), ('LED', 'LED_NEG'), ('InputPower', V24)], 1):
    add(sh, f'TP{n:02}', 'TP', net, '', {1: net}, 'TestPoint:TestPoint_Pad_D1.0mm', bom=False)

# Accessories bought for assembly but not drawn as symbols
accessories = {'BAT-CR2032/P': 1}


def geometry(s):
    kind = s['kind']; line = lambda pts: f'(polyline (pts {" ".join(f"(xy {num(x)} {num(y)})" for x, y in pts)}) (stroke (width 0.254) (type default)) (fill (type none)))'
    rect = lambda x1, y1, x2, y2: f'(rectangle (start {num(x1)} {num(y1)}) (end {num(x2)} {num(y2)}) (stroke (width 0.254) (type default)) (fill (type background)))'
    if kind == 'ic': return rect(-s['width']/2, s['height']/2, s['width']/2, -s['height']/2)
    if kind in ['r', 'f']: return rect(-2.54, 1.27, 2.54, -1.27) + (line([(-2.54, 0), (2.54, 0)]) if kind == 'f' else '')
    if kind in ['c', 'cp']:
        out = line([(-.635, 2.54), (-.635, -2.54)]) + line([(.635, 2.54), (.635, -2.54)])
        if kind == 'cp': out += line([(-3.5, 3.3), (-2, 3.3)]) + line([(-2.75, 2.55), (-2.75, 4.05)])
        return out
    if kind == 'l': return line([(-2.54, 0), (-1.9, 1.27), (-1.27, 0), (-.635, 1.27), (0, 0), (.635, 1.27), (1.27, 0), (1.9, 1.27), (2.54, 0)])
    if kind in ['d', 'tvs']:
        out = line([(-1.27, 2.54), (-1.27, -2.54)]) + line([(-1.27, 0), (1.27, 2.54), (1.27, -2.54), (-1.27, 0)])
        if kind == 'tvs': out += line([(-1.9, 3.17), (-1.27, 2.54)]) + line([(-1.27, -2.54), (-.635, -3.17)])
        return out
    if kind == 'sw': return line([(-2.54, 0), (2.54, 2.54)])
    if kind == 'y': return rect(-1.27, 2.54, 1.27, -2.54) + line([(-2.54, 2.54), (-2.54, -2.54)]) + line([(2.54, 2.54), (2.54, -2.54)])
    if kind == 'bat': return line([(-.635, 2.54), (-.635, -2.54)]) + line([(.635, 1.27), (.635, -1.27)]) + line([(-3, 3), (-1.5, 3)]) + line([(-2.25, 2.25), (-2.25, 3.75)])
    if kind == 'nmos': return (HERE/'libraries/NMOS_graphics.sexpr').read_text(encoding='utf-8').rstrip()
    if kind in ['npn', 'pnp']:
        body = line([(-2.54, 2.54), (-2.54, -2.54)]) + line([(-2.54, 1.27), (5.08, 3.81), (5.08, 5.08)]) + line([(-2.54, -1.27), (5.08, -3.81), (5.08, -5.08)])
        arrow = [(3.81, -3.386), (2.05, -3.45), (2.75, -1.78), (3.81, -3.386)] if kind == 'npn' else [(0.635, 2.328), (2.8, 2.07), (2.1, 3.74), (0.635, 2.328)]
        return body + line(arrow)
    if kind in ['tp', 'flag']: return f'(circle (center 0 1.27) (radius 0.635) (stroke (width 0.254) (type default)) (fill (type none)))'
    raise ValueError(kind)


def pin_length(s, p):
    if s['kind'] == 'ic': return 5.08
    if s['kind'] in ['tp', 'flag']: return 0
    if s['kind'] in ['c', 'cp', 'bat']: return 6.985
    if s['kind'] in ['d', 'tvs']: return 6.35
    if s['kind'] == 'y': return 5.08
    if s['kind'] == 'nmos': return 2.54
    if s['kind'] in ['npn', 'pnp']: return 5.08 if p['number'] == '1' else 1.27
    return 5.08


def libsymbol(name, qualified=False):
    s = symbols[name]; shown = s['kind'] == 'ic'; ref = 'U' if shown else name
    n = f'EU_LITE:{name}' if qualified else name
    a = [f'(symbol {q(n)} (pin_names (offset 0.762){"" if shown else " (hide yes)"})',
         '(in_bom yes) (on_board yes)', prop('Reference', ref, 0, 5, False), prop('Value', name, 0, -5, False), prop('Footprint', ''), prop('Datasheet', ''),
         f'(symbol {q(name+"_0_1")} {geometry(s)})', f'(symbol {q(name+"_1_1")}']
    for p in s['pins']:
        a.append(f'(pin {p["typ"]} line (at {num(p["x"])} {num(p["y"])} {p["angle"]}) (length {num(pin_length(s, p))}) (name {q(p["name"])} {effects(.95)}) (number {q(p["number"])} {effects(.95)}))')
    return '\n'.join(a) + '))'


def header(name, title):
    return (f'(kicad_sch (version 20260306) (generator "eeschema") (generator_version "10.0") (uuid {q(uid(name))}) (paper "A3") '
            f'(title_block (title {q(title)}) (date {q(DATE)}) (rev "EU-LITE-0.2") (company "GrowBox | EU-LITE V2 from stock") '
            f'(comment 1 {q("SCHEMATIC DRAFT | no PCB / manufacturing release")}))')


def textblk(name, key, t, x, y, size=1.1):
    return f'(text {q(t)} (at {num(x)} {num(y)} 0) {effects(size, "(justify left top)")} (uuid {q(uid(name, key))}))'


source_urls = {
 'CSD19531KCS': 'https://www.ti.com/lit/ds/symlink/csd19531kcs.pdf',
 'OSTAPA57E2A': 'https://www.optosupply.com/uppic/2020811188683.pdf',
 'S26': 'https://www.st.com/resource/en/datasheet/st1s14.pdf',
 'S27': 'https://www.st.com/resource/en/datasheet/st1s10.pdf',
 'S114': 'https://www.ti.com/lit/ds/symlink/ina238.pdf',
 'USBLC6-2SC6': 'https://www.st.com/resource/en/datasheet/usblc6-2.pdf',
 'MEM2061-01-188-00-A': 'https://gct.co/Files/Drawings/MEM2061.pdf',
 'ESP32-WROOM-32E-N4': 'https://www.espressif.com/sites/default/files/documentation/esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.pdf',
 'AO3422': 'https://www.aosmd.com/sites/default/files/res/datasheets/AO3422.pdf',
 'MCP7940N-I/SN': 'https://ww1.microchip.com/downloads/aemDocuments/documents/MPD/ProductDocuments/DataSheets/MCP7940N-Battery-Backed-I2C-RTCC-with-SRAM-20005010J.pdf',
 '3413.0331.11': 'https://www.salvats.lv/lv/drosinataji-smd-1206-32x16x06mm/drosinatajs-smd-1206-20a-63v-fast',
}


def schematic(sh, meta):
    used = sorted({p['symbol'] for p in meta['parts']})
    a = [header(sh, meta['title']), '(lib_symbols ' + '\n'.join(libsymbol(n, True) for n in used) + ')']
    root = uid(ROOT); su = uid('sheet', sh)
    a.append(textblk(sh, 'heading', meta['title'], 12.7, 12.7, 2))
    a.append(textblk(sh, 'note', '\n'.join(meta['notes']), 12.7, 251, 1.02))
    grid = []
    for y in range((45 if meta['chip_area'] == 0 else 35 + meta['chip_area']), 236, 20):
        for x in [46, 107, 174, 241, 308, 375]: grid.append((x, y))
    g = iter(grid)
    for p in meta['parts']:
        p['position'] = p['position'] or next(g)
        x, y = p['position']; x = round(x/1.27)*1.27; y = round(y/1.27)*1.27
        p['position'] = (round(x, 4), round(y, 4)); s = symbols[p['symbol']]; h = s['height']/2
        pi = uid('component', p['ref'])
        a.append(f'(symbol (lib_id {q("EU_LITE:"+p["symbol"])}) (at {num(x)} {num(y)} 0) (unit 1) (in_bom {"yes" if p["in_bom"] else "no"}) (on_board {"no" if p["symbol"]=="FLAG" else "yes"}) (dnp no) (uuid {q(pi)})')
        tx = x-7.62 if s['kind'] in ['nmos', 'npn', 'pnp'] else x
        if s['kind'] == 'tp': tx = x-8.89
        status = ('DRAWING CHECKED in EU-0.1/cheap-version - physical fit pending' if p['src'] in CHECKED_FP
                  else 'PACKAGE ONLY - verify before PCB' if p['footprint'] else 'PENDING - no approved footprint')
        a.extend([prop('Reference', p['ref'], tx, y-h-3.1, s['kind'] == 'flag', 1.15), prop('Value', p['value'], tx, y+h+3.1, s['kind'] in ['flag', 'tp'], .98),
                  prop('Footprint', p['footprint'], x, y), prop('Datasheet', source_urls.get(p['src'], ''), x, y), prop('MPN', p['mpn'], x, y),
                  prop('Source', ('stock:' + p['src'][1:]) if is_stock(p['src']) else ('buy' if p['src'] else ''), x, y),
                  prop('Design_note', p['note'], x, y), prop('FootprintStatus', status, x, y)])
        for pin_ in s['pins']: a.append(f'(pin {q(pin_["number"])} (uuid {q(uid(pi, pin_["number"]))}))')
        a.append(f'(instances (project {q(ROOT)} (path {q("/"+root+"/"+su)} (reference {q(p["ref"])}) (unit 1)))) )')
        for pin_ in s['pins']:
            pn = pin_['number']; xx = x+pin_['x']; yy = y-pin_['y']; net = p['nets'][pn]
            if net is None:
                a.append(f'(no_connect (at {num(xx)} {num(yy)}) (uuid {q(uid(pi, pn, "NC"))}))'); continue
            th = math.radians(pin_['angle']); dx = -math.cos(th)*5.08; dy = math.sin(th)*5.08
            if s['kind'] in ['flag', 'tp']: dx = -5.08; dy = 0
            ex = round(xx+dx, 4); ey = round(yy+dy, 4)
            a.append(f'(wire (pts (xy {num(xx)} {num(yy)}) (xy {num(ex)} {num(ey)})) (stroke (width 0) (type default)) (uuid {q(uid(pi, pn, "wire"))}))')
            la = (pin_['angle']+180) % 360
            if s['kind'] in ['flag', 'tp']: la = 180
            just = 'left' if la in [0, 90] else 'right'
            a.append(f'(global_label {q(net)} (shape bidirectional) (at {num(ex)} {num(ey)} {la}) {effects(.9, f"(justify {just})")} (uuid {q(uid(pi, pn, "label"))}) {prop("Intersheetrefs", "${INTERSHEET_REFS}", ex, ey)})')
    a.append(f'(sheet_instances (path "/" (page {q(str(list(sheets).index(sh)+2))})))')
    a.append('(embedded_fonts no))')
    return '\n'.join(a) + '\n'


def buy_units(key, need):
    """Pieces to buy for `need` fitted pieces: respect the shop's minimum quantity and multiple."""
    b = BUY[key]
    n = max(need, b['min_qty'])
    return -(-n // b['multiple']) * b['multiple']


def build_bom():
    stock_rows, buy_rows = [], []
    for src, need in sorted(counts.items(), key=lambda kv: (kv[0][0] != 'S', kv[0])):
        refs = [p['ref'] for p in parts if p['src'] == src and p['in_bom']]
        if is_stock(src):
            row = STOCK[int(src[1:])]
            have = int(row['остаток'])
            assert row['архив'] == '0' and need <= have, (src, need, have)
            stock_rows.append(dict(shop='Склад', id=int(src[1:]), mpn=row['MPN'] or f"{row['название']} {row['номинал']}", name=row['название'],
                                   value=row['номинал'], package=row['корпус'], needed=need, buy=0, on_hand=need, stock_total=have,
                                   stock_left=have-need, refs=refs, line=0.0))
        else:
            b = BUY[src]; n = buy_units(src, need)
            buy_rows.append(dict(shop=b['shop'], mpn=src, sku=b['sku'], needed=need, buy=n, unit='шт.',
                                 price_unit=b['price'], line=0.0 if b['price'] is None else float(Decimal(str(b['price'])) * n), stock=b['stock'], url=b['url'],
                                 price_date=b['price_date'], refs=refs, accessory_count=0, min_qty=b['min_qty'], multiple=b['multiple'],
                                 unpriced=b['price'] is None))
    for key, n in accessories.items():
        b = BUY[key]; buy_rows.append(dict(shop=b['shop'], mpn=key, sku=b['sku'], needed=n, buy=n, unit='шт.', price_unit=b['price'],
                                           line=float(Decimal(str(b['price'])) * n), stock=b['stock'], url=b['url'], price_date=b['price_date'],
                                           refs=[], accessory_count=n, min_qty=1, multiple=1, unpriced=False))
    buy_rows.sort(key=lambda x: (x['shop'], x['mpn']))
    return stock_rows, buy_rows


def main():
    for d in ['libraries', 'review']: (HERE/d).mkdir(exist_ok=True)
    lib = '(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor")\n' + '\n'.join(libsymbol(n) for n in sorted(symbols)) + ')\n'
    (HERE/'libraries/EU_LITE.kicad_sym').write_text(lib, encoding='utf-8')
    (HERE/'sym-lib-table').write_text('(sym_lib_table (version 7) (lib (name "EU_LITE") (type "KiCad") (uri "${KIPRJMOD}/libraries/EU_LITE.kicad_sym") (options "") (descr "Project pin-verified symbols")))\n', encoding='utf-8')
    fp = [('EU_Mechanical', '${KIPRJMOD}/libraries/EU_Mechanical.pretty'), ('EU_Parts', '${KIPRJMOD}/libraries/EU_Parts.pretty'),
          ('LITE_Parts', '${KIPRJMOD}/libraries/LITE_Parts.pretty')]
    fp.extend((lib.stem, '${KIPRJMOD}/libraries/' + lib.name)
              for lib in sorted((HERE/'libraries').glob('*.pretty')) if lib.stem not in ('LITE_Parts', 'EU_Mechanical', 'EU_Parts'))
    (HERE/'fp-lib-table').write_text('(fp_lib_table (version 7)\n' + '\n'.join(
        f'(lib (name {q(n)}) (type "KiCad") (uri "{u}") (options "") (descr "Project footprints; see README.md and libraries/SOURCES.md"))' for n, u in fp) + ')\n', encoding='utf-8')
    for sh, meta in sheets.items(): (HERE/f'{sh}.kicad_sch').write_text(schematic(sh, meta), encoding='utf-8')
    root = [header(ROOT, 'GrowBox | EU-LITE V2 | schematic review'), '(lib_symbols)']
    root.append(textblk(ROOT, 'title', 'GrowBox controller EU-LITE V2\nSix 24 V constant-voltage lamp outputs / 24 V 15 A PSU', 20, 17, 2.5))
    root.append(textblk(ROOT, 'scope', 'SCHEMATIC DRAFT EU-LITE-0.2 (V2) | 28 October 2026\nConnection/BOM draft. No LED boost: six fused 24 V lamp outputs, one PWM switch.\n'
                        'No PCB yet: placement and routing follow in a separate step.\nSee README.md for the checks that were run.', 20, 40, 1.6))
    for k, (sh, meta) in enumerate(sheets.items()):
        x = 25+185*(k % 2); y = 75+42*(k//2); sid = uid('sheet', sh)
        root.append(f'(sheet (at {x} {y}) (size 155 28) (stroke (width 0.254) (type default)) (fill (color 0 0 0 0)) (uuid {q(sid)}) {prop("Sheetname", meta["title"], x+77.5, y-2, False, 1.4)} {prop("Sheetfile", sh+".kicad_sch", x+77.5, y+30, False, 1)} (instances (project {q(ROOT)} (path {q("/"+uid(ROOT))} (page {q(k+2)})))))')
    root.append('(sheet_instances (path "/" (page "1"))) (embedded_fonts no))')
    (HERE/f'{ROOT}.kicad_sch').write_text('\n'.join(root) + '\n', encoding='utf-8')
    project_path = HERE/f'{ROOT}.kicad_pro'
    project = json.loads(project_path.read_text(encoding='utf-8')) if project_path.exists() else dict(meta=dict(filename=f'{ROOT}.kicad_pro', version=3), erc=dict(erc_exclusions=[], meta=dict(version=0)), schematic=dict(meta=dict(version=1)))
    settings = project.setdefault('board', {}).setdefault('design_settings', {})
    settings.setdefault('drc_exclusions', [])
    settings.setdefault('rules', {}).update({
        'min_clearance': 0.2, 'min_copper_edge_clearance': 0.3,
        'min_hole_clearance': 0.2, 'min_hole_to_hole': 0.25,
        'min_track_width': 0.2, 'min_via_diameter': 0.5,
        'min_via_annular_width': 0.1, 'min_through_hole_diameter': 0.3,
        'min_text_height': 0.8, 'min_text_thickness': 0.15,
        'min_silk_clearance': 0.15,
    })
    project.setdefault('text_variables', {})['DESIGN_STAGE'] = 'SCHEMATIC_DRAFT'
    project_path.write_text(json.dumps(project, indent=2) + '\n', encoding='utf-8')
    spec = dict(revision=REV, date=DATE, base='EU-LITE V1 (hardware/PCB_EU_LITE, r3) with the LM3429 boost replaced by six 24 V lamp outputs', parts=parts, accessories=accessories, source_urls=source_urls)
    (HERE/'design.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    stock_rows, buy_rows = build_bom()
    totals = defaultdict(Decimal)
    for row in buy_rows:
        if not row['unpriced']: totals[row['shop']] += Decimal(str(row['line']))
    shipping = sum(SHIPPING[s] for s in totals)
    unpriced = [x['mpn'] for x in buy_rows if x['unpriced']]
    summary = dict(electrical_components=sum(counts.values()), testpads=sum(1 for p in parts if p['symbol'] == 'TP'), accessories=accessories,
                   stock_rows=len(stock_rows), stock_pieces=sum(x['needed'] for x in stock_rows), buy_rows=len(buy_rows),
                   buy_pieces=sum(x['buy'] for x in buy_rows), shop_totals={k: str(v) for k, v in totals.items()},
                   parts_EUR=str(sum(totals.values())), shipping_EUR=str(shipping), total_EUR=str(sum(totals.values()) + shipping),
                   unpriced_items=unpriced, unpriced_allowance_EUR='0.00',
                   price_basis='V1 catalogue snapshot 2026-10-23, including CSD19531KCS from catalog.db; OSTAPA57E2A owner Electrobase quote 2026-10-25; 3413.0331.11 read from the Salvats product page on 2026-10-28 (procurement_lite.json); delivery as in the EU-0.1 README of 24.10.2026, not rechecked. No reservation or order.')
    (HERE/'BOM_allocated.json').write_text(json.dumps(dict(stock=stock_rows, buy=buy_rows, summary=summary), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with (HERE/'BOM_allocated.csv').open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(['Source', 'Shop', 'MPN_or_shop_SKU', 'Fitted', 'Buy_pieces', 'Price_per_piece_EUR_VAT', 'Line_EUR_VAT', 'References', 'Stock_id', 'Stock_left', 'URL', 'Price_date', 'Notes'])
        for x in buy_rows:
            w.writerow(['buy', x['shop'], x['mpn'], x['needed'], x['buy'], '' if x['unpriced'] else x['price_unit'], '' if x['unpriced'] else f"{x['line']:.2f}", ' '.join(x['refs']), '', '', x['url'], x['price_date'],
                        ('нет в снимке каталога и на складе: цена не известна, ориентир не выше 0,50 € (оценка); магазин выбрать из разрешённых' if x['unpriced'] else f"минимум {x['min_qty']}, кратность {x['multiple']}; наличие в каталоге: {str(x['stock'])[:24]}")])
        for x in stock_rows:
            w.writerow(['stock', 'Склад', x['mpn'], x['needed'], 0, 0, '0.00', ' '.join(x['refs']), x['id'], x['stock_left'], '', '2026-10-24', f"{x['name']}; {x['value']}; {x['package']}"])
    (HERE/'review/BOM_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
