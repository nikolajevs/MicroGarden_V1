"""Regression tests for refreshing a standalone project's frozen price data."""
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]


class PriceRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for rel in ('tools/snapshot_prices.py', 'stock/reference-quotes.json', 'BOM_allocated.json', 'procurement_lite.json'):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / rel, target)
        self.snapshot = self.root / 'procurement_lite.json'
        self.before = self.snapshot.read_bytes()
        self.data = json.loads(self.before)
        self.db = self.root / 'catalog.db'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE components (shop, sku, title, price_eur, in_stock, url, description, scraped_at)')
            for key, item in self.data.items():
                if item['source'] != 'catalog.db':
                    continue
                conn.execute('INSERT INTO components VALUES (?,?,?,?,?,?,?,?)',
                             (item['shop'], item['sku'], item['title'], item['price'], item['stock'], item['url'],
                              f'Мин. количество: {item["min_qty"]}; Кратность: {item["multiple"]}; Artikuls: {key}', item['price_date']))

    def refresh(self):
        return subprocess.run([sys.executable, '-X', 'utf8', self.root/'tools/snapshot_prices.py', self.db],
                              capture_output=True, encoding='utf-8')

    def test_current_bom_and_distinct_resistors_survive_refresh(self):
        result = self.refresh()
        self.assertEqual(result.returncode, 0, result.stderr)
        refreshed = json.loads(self.snapshot.read_text(encoding='utf-8'))
        required = {row['mpn'] for row in json.loads((self.root/'BOM_allocated.json').read_text(encoding='utf-8'))['buy']}
        self.assertFalse(required - refreshed.keys())
        for key in required:
            self.assertEqual(refreshed[key]['price'], self.data[key]['price'], key)
        self.assertNotEqual(refreshed['SMD1206 4K7 1%']['url'], refreshed['SMD1206 56K 1%']['url'])

    def test_missing_catalog_row_preserves_working_snapshot(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM components WHERE sku=?', ('CSD19531KCS',))
        result = self.refresh()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot.read_bytes(), self.before)


if __name__ == '__main__':
    unittest.main()
