#!/usr/bin/env python3
"""Freeze the purchase lines of PCB_EU_LITE from the shop catalogue database into procurement_lite.json.

Usage: python hardware/PCB_EU_LITE/tools/snapshot_prices.py CATALOG.db
Only Salvats, Electrobase and Lemona rows are read (owner's decision of 23.10.2026). The database is a scraped snapshot,
not an offer: prices and stock are copied with the scrape date. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ALLOWED = ('Salvats', 'Electrobase', 'Lemona')
REFERENCE_QUOTES = HERE / 'stock/reference-quotes.json'

# key (used by generate.py) -> (shop, DB sku or DB url). Electrobase rows carry no MPN: the URL comes from the EU-0.1 BOM,
# where the owner checked the product page.
LINES = {
    'ESP32-WROOM-32E-N4': ('Electrobase', 'url:https://electrobase.lv/katalogs/moduli-iot-wifi-bluetooth-lora/module-iot-bluetooth-low-energywifi-smd-0'),
    'LM3429MH/NOPB': ('Electrobase', 'url:https://electrobase.lv/katalogs/led-draiveri/ic-driver-boostbuck-boost-led-driver-htssop14-5a-372v-ch-1'),
    'MCP7940N-I/SN': ('Electrobase', 'sku:EB2031421749'),
    'ABS07-32.768KHZ-7-T': ('Electrobase', 'sku:EB297444889'),
    'MEM2061-01-188-00-A': ('Electrobase', 'sku:MEM2061-01-188-00-A'),
    'SMBJ26A-E3/52': ('Electrobase', 'sku:SMBJ26A-E3/52'),
    'AO3422': ('Electrobase', 'sku:EB4428624'),
    'JNRH1207-220M-A': ('Electrobase', 'sku:JNRH1207-220M-A'),
    '3413.0330.11': ('Salvats', 'sku:3413.0330.11'),
    '3413.0327.11': ('Salvats', 'sku:3413.0327.11'),
    '3413.0224.11': ('Salvats', 'sku:3413.0224.11'),
    '3403.0169.11': ('Salvats', 'sku:3403.0169.11'),
    '3403.0166.11': ('Salvats', 'sku:3403.0166.11'),
    'USBLC6-2SC6': ('Salvats', 'sku:USBLC6-2SC6'),
    'ZL201-10G': ('Salvats', 'sku:ZL201-10G'),
    'UPW1H221MPD': ('Salvats', 'sku:UPW1H221MPD'),
    'LRP2512-R040-1%': ('Salvats', 'sku:LRP2512-R040-1%'),
    'CS2512-0R15-1%-HP': ('Salvats', 'sku:CS2512-0R15-1%-HP'),
    'BC847B-DIO': ('Salvats', 'sku:BC847B-DIO'),
    'BC857B': ('Salvats', 'sku:BC857B'),
    'BZV55C12': ('Salvats', 'sku:BZV55C12'),
    'BC-2001': ('Salvats', 'sku:BC-2001'),
    'BAT-CR2032/P': ('Salvats', 'sku:BAT-CR2032/P'),
    'C1206C102JCGACTU': ('Salvats', 'sku:C1206C102JCGACTU'),
    'CL31C101JBCNNNC': ('Salvats', 'sku:CL31C101JBCNNNC'),
    'SMD1206-10K-1%': ('Salvats', 'sku:SMD1206-10K-1%'),
    'CRCW120682K0FKTABC': ('Salvats', 'sku:CRCW120682K0FKTABC'),
    'SMD1206-6K2-1%': ('Salvats', 'sku:SMD1206-6K2-1%'),
    'CRCW12061K20FKTABC': ('Salvats', 'sku:CRCW12061K20FKTABC'),
    'SMD1206 56K 1%': ('Salvats', 'descr:%Artikuls: SMD1206 56K 1%'),
    'SMD1206-33K-1%': ('Salvats', 'sku:SMD1206-33K-1%'),
    'CSD19531KCS': ('Salvats', 'sku:CSD19531KCS'),
    'SMD1206 4K7 1%': ('Salvats', 'url:https://www.salvats.lv/lv/rezistori-smd-1206-025w/4k7-1206-1-smd-rezistors-1206'),
    'CRCW1206620RFKTABC': ('Salvats', 'sku:CRCW1206620RFKTABC'),
}
# Not present in the scraped database: the price and URL are taken from the EU-0.1 BOM (owner checked, 23.10.2026).
FROM_EU01 = {'C1206C100J5GACTU': ('Electrobase', 'C1206C100J5GACTU')}
# Owner-provided Electrobase quote, retained when refreshing the scraped catalogue snapshot.
OWNER_QUOTES = {'OSTAPA57E2A': dict(shop='Electrobase', sku='EB1854986288',
    title='OPTOSUPPLY OSTAPA57E2A RGB 5mm, diffused, common anode; pins 1 G, 2 B, 3 A, 4 R',
    price=0.85, stock='99 (supplier warehouse, owner quote)',
    url='https://electrobase.lv/katalogs/diodes-led-tht-18mm/led-rgb-5mm-140deg-front-convex-231vdc-noof-term-4-round',
    price_date='2026-10-25', min_qty=1, multiple=1, pack=1,
    source='Owner-provided Electrobase product card 2026-10-25; VAT included; no live checkout verification')}
# Read from the shop's own product page, not from the scraped database: kept when the snapshot is refreshed.
SITE_QUOTES = {'SMD1206-1K-1%': dict(shop='Salvats', sku='SMD1206-1K-1%',
    title='1K-1206-1% SMD Rezistors, 1206',
    price=0.03, stock='490',
    url='https://www.salvats.lv/lv/rezistori-smd-1206-025w/1k-1206-1-smd-rezistors-1206',
    price_date='2026-10-27', min_qty=1, multiple=1, pack=1,
    source='Salvats product page read on 2026-10-27 (VAT as shown, no checkout verification); not from the 2026-10-23 catalog.db snapshot')}


def main():
    con = sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True)
    out = {}
    for key, (shop, sel) in LINES.items():
        assert shop in ALLOWED, shop
        kind, val = sel.split(':', 1)
        op, col = ('like', 'description') if kind == 'descr' else ('=', kind)
        rows = con.execute(f'select shop,sku,title,price_eur,in_stock,url,description,scraped_at from components where shop=? and {col} {op} ?', (shop, val)).fetchall()
        assert len(rows) == 1, (key, sel, len(rows))
        shop_, sku, title, price, stock, url, desc, scraped = rows[0]
        m = re.search(r'Мин\. количество: (\d+); Кратность: (\d+)', desc)
        out[key] = dict(shop=shop_, sku=sku, title=title, price=price, stock=stock, url=url, price_date=scraped[:10],
                        min_qty=int(m.group(1)) if m else 1, multiple=int(m.group(2)) if m else 1,
                        pack=10 if '(10gab)' in title else 1, source='catalog.db')
    eu01 = {r['mpn']: r for r in json.loads(REFERENCE_QUOTES.read_text(encoding='utf-8'))}
    for key, (shop, mpn) in FROM_EU01.items():
        r = eu01[mpn]
        out[key] = dict(shop=shop, sku=mpn, title=r['usage'][:90], price=r['price_unit'], stock=str(r['stock']), url=r['url'],
                        price_date=r.get('price_date', '2026-10-23'), min_qty=1, multiple=1, pack=1, source='EU-0.1 BOM')
    out.update(OWNER_QUOTES)
    out.update(SITE_QUOTES)
    # Refuse an incomplete refresh before replacing the working price snapshot.
    required = {r['mpn'] for r in json.loads((HERE / 'BOM_allocated.json').read_text(encoding='utf-8'))['buy']}
    missing = required - out.keys()
    if missing:
        raise ValueError(f'Price refresh would omit current BOM lines: {sorted(missing)}')
    (HERE / 'procurement_lite.json').write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    for k, v in out.items():
        print(f"{v['shop'][:4]} {k:22} {str(v['price']):>5} min={v['min_qty']}x{v['multiple']} pack={v['pack']} {v['price_date']} {v['stock'][:12]}")


if __name__ == '__main__':
    main()
