#!/usr/bin/env python3
"""Mutation test of verify.py: every deliberate defect in a temporary copy of the design must be rejected.

Run: python hardware/PCB_EU_LITE/tools/test_verify.py. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def part(spec, ref):
    return next(x for x in spec['parts'] if x['ref'] == ref)


def swap(ref, a, b):
    def f(spec, bom):
        x = part(spec, ref)['nets']; x[a], x[b] = x[b], x[a]
    return f


def net(ref, pin, value):
    def f(spec, bom):
        part(spec, ref)['nets'][str(pin)] = value
    return f


def value(ref, text):
    def f(spec, bom):
        part(spec, ref)['value'] = text
    return f


def mpn(ref, text):
    def f(spec, bom):
        part(spec, ref)['mpn'] = text
    return f


def move_red_to_strapping_pin(spec, bom):
    nets = part(spec, 'U202')['nets']
    nets['26'] = None; nets['14'] = 'LED_R_GPIO'


def stock_short(spec, bom):
    next(r for r in bom['stock'] if r['id'] == 112)['needed'] = 3


def price(spec, bom):
    next(r for r in bom['buy'] if r['mpn'] == 'LM3429MH/NOPB')['price_unit'] = 1.0


def shop(spec, bom):
    next(r for r in bom['buy'] if r['mpn'] == 'LM3429MH/NOPB')['shop'] = 'Farnell'


MUTATIONS = {
    'ST1S14 VIN and SW pins swapped': swap('U101', '7', '8'),
    'ST1S14 EN1 left floating instead of GND': net('U101', 3, 'AUX_EN2'),
    '12 V divider top 82k -> 100k (13 V)': value('R101', '100k 1%'),
    '3.3 V divider bottom resistor dropped to 100k only': value('R202', '100k 1%'),
    'INA238 IN+ and IN- swapped': swap('U102', '9', '10'),
    'Q102 drain and source swapped': swap('Q102', '2', '3'),
    'F103 fast fuse mislabeled slow': value('F103', '6.3 A slow'),
    'Reverse-polarity gate without zener (12 V -> 24 V)': net('D103', 2, 'REVP_GATE'),
    'LM3429 OVP divider top 33k -> 47k (59.5 V)': value('R509', '47k 1%'),
    'LED sense resistor 0R15 -> 0R10 (about 2.3 A)': value('R502', '0R10 2W'),
    'Added LED terminal J502 has reversed polarity': swap('J502', '1', '2'),
    'Added LED terminal J503 bypasses the current shunt': net('J503', 1, 'LED_OUT'),
    'Old 6k2 setpoint resistor cannot provide the 75 W LED load': value('R505', '6k2 1%'),
    'IS resistor 0R040 -> 0R15 (current limit too low)': value('R501', '0R15 2W'),
    'BSC146N10LS5 gate and drain swapped (Q501)': swap('Q501', '4', '5'),
    'nDIM series resistor shorted to HIGH-only (no pull-down)': net('R516', 2, '+3V3'),
    'Fan 1 PWM FET gate pull-down removed': net('R412', 2, None),
    'Heater R702 raised to 47k (internal pull-up would turn it on)': value('R702', '47k'),
    'SD pull-up for DAT1 missing': net('R306', 2, 'SD_DAT2'),
    'GPIO34 tach pull-up removed (floating input)': net('R206', 1, None),
    'Fan tach signal wired from the header to GPIO34': net('J411', 3, 'FAN1_TACH'),
    'Diode D501 reversed': swap('D501', '1', '2'),
    'RGB LED anode wired to +3V3 (blue/green Vf cannot be driven)': net('D901', 3, '+3V3'),
    'RGB green resistor 2k -> 100 R (about 90 mA)': value('R908', '100R'),
    'RGB blue base pull-down missing (glows during reset)': net('R906', 2, None),
    'RGB red channel moved to strapping pin GPIO12': move_red_to_strapping_pin,
    'RGB base resistor 4k7 -> 470k (NPN not saturated)': value('R901', '470k'),
    'Stock overdrawn (3 x BSC146N10LS5, 2 in stock)': stock_short,
    'Purchase price differs from the frozen snapshot': price,
    'Purchase from a shop outside the allowed list': shop,
}


def main():
    ok = True
    for name, mutate in MUTATIONS.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for f in ('design.json', 'BOM_allocated.json', 'procurement_lite.json'):
                shutil.copy(HERE/f, root/f)
            shutil.copytree(HERE/'stock', root/'stock')
            (root/'review').mkdir()
            spec = json.loads((root/'design.json').read_text(encoding='utf-8')); bom = json.loads((root/'BOM_allocated.json').read_text(encoding='utf-8'))
            mutate(spec, bom)
            (root/'design.json').write_text(json.dumps(spec), encoding='utf-8'); (root/'BOM_allocated.json').write_text(json.dumps(bom), encoding='utf-8')
            res = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(HERE/'tools/verify.py'), '--root', str(root)], capture_output=True, text=True, encoding='utf-8')
            rejected = res.returncode != 0 and 'AssertionError' in res.stderr
            ok &= rejected
            why = res.stderr.strip().splitlines()[-1].replace('AssertionError: ', '')[:90] if rejected else ''
            print(('REJECTED ' if rejected else 'MISSED   ') + name + (f'  <- {why}' if why else ''))
    base = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(HERE/'tools/verify.py'), '--output', str(Path(tempfile.gettempdir())/'lite_conn.txt')], capture_output=True, text=True, encoding='utf-8')
    ok &= base.returncode == 0
    print('Unmodified design:', 'PASS' if base.returncode == 0 else 'FAIL')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
