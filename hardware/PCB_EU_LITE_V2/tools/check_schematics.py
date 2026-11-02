#!/usr/bin/env python3
"""Read the written .kicad_sch files back and compare their connectivity with design.json.

Independent of generate.py's in-memory data: the sheets are parsed as S-expressions, every symbol pin is traced through its
wire stub to the global label (or no-connect flag) at the stub end, and the resulting pin->net map must equal design.json.
It also checks balanced syntax, unique UUIDs, lib_symbol pin lists and Reference/Footprint/MPN properties. This is NOT KiCad's
ERC. Run: python hardware/PCB_EU_LITE_V2/tools/check_schematics.py. SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def parse(text: str):
    stack, cur, pos = [], [], 0
    stack.append(cur)
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            if text[pos:].strip():
                raise ValueError(f'syntax error near {text[pos:pos+40]!r}')
            break
        pos = m.end()
        if m.group(1):
            new = []; stack[-1].append(new); stack.append(new)
        elif m.group(2):
            stack.pop()
            if not stack:
                raise ValueError('unbalanced )')
        elif m.group(3) is not None:
            stack[-1].append(json.loads('"' + m.group(3) + '"'))
        else:
            tok = m.group(4)
            try:
                stack[-1].append(float(tok))
            except ValueError:
                stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError('unbalanced (')
    return cur


def children(node, head):
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def walk(node):
    yield node
    for c in node:
        if isinstance(c, list):
            yield from walk(c)


def key(x, y):
    return (round(float(x), 3), round(float(y), 3))


def main():
    spec = json.loads((HERE/'design.json').read_text(encoding='utf-8'))
    by_sheet = defaultdict(list)
    for p in spec['parts']:
        by_sheet[p['sheet']].append(p)
    uuids = Counter(); problems = []; pins_checked = 0
    lib = {}
    for sh, plist in by_sheet.items():
        tree = parse((HERE/f'{sh}.kicad_sch').read_text(encoding='utf-8'))[0]
        for n in walk(tree):
            if n and n[0] == 'uuid' and len(n) > 1 and isinstance(n[1], str):
                uuids[n[1]] += 1
        libsyms = {}
        for ls in children(children(tree, 'lib_symbols')[0], 'symbol'):
            pl = {}
            for sub in walk(ls):
                if sub and sub[0] == 'pin' and len(sub) > 2 and isinstance(sub[1], str) and isinstance(sub[2], str):
                    at = children(sub, 'at')[0]
                    pl[children(sub, 'number')[0][1]] = (float(at[1]), float(at[2]))
            libsyms[ls[1]] = pl
        wires = {}
        for w in children(tree, 'wire'):
            a, b = children(children(w, 'pts')[0], 'xy')
            wires[key(a[1], a[2])] = key(b[1], b[2])
        labels = {key(*children(g, 'at')[0][1:3]): g[1] for g in children(tree, 'global_label')}
        ncs = {key(*children(g, 'at')[0][1:3]) for g in children(tree, 'no_connect')}
        inst = {}
        for s in children(tree, 'symbol'):
            if len(s) > 1 and isinstance(s[1], list) and s[1][0] == 'lib_id':
                props = {pr[1]: pr[2] for pr in children(s, 'property')}
                inst[props['Reference']] = (s, props)
        for p in plist:
            if p['ref'] not in inst:
                problems.append(f'{sh}: {p["ref"]} missing from the written sheet'); continue
            s, props = inst[p['ref']]
            lid = s[1][1]; at = children(s, 'at')[0]; x, y = float(at[1]), float(at[2])
            if lid != 'EU_LITE:' + p['symbol']:
                problems.append(f'{p["ref"]}: lib_id {lid}')
            if props.get('Footprint') != p['footprint'] or props.get('MPN') != p['mpn'] or props.get('Value') != p['value']:
                problems.append(f'{p["ref"]}: Footprint/MPN/Value property differs from design.json')
            pl = libsyms[lid]
            written = {c[1] for c in children(s, 'pin')}
            if written != set(pl) or set(pl) != set(p['nets']):
                problems.append(f'{p["ref"]}: pin list differs (symbol {sorted(pl)}, instance {sorted(written)}, design {sorted(p["nets"])})')
                continue
            for number, (px, py) in pl.items():
                pos = key(x + px, y - py)
                if pos in ncs:
                    got = None
                elif pos in wires and wires[pos] in labels:
                    got = labels[wires[pos]]
                else:
                    got = '<unconnected stub>'
                pins_checked += 1
                if got != p['nets'][number]:
                    problems.append(f'{p["ref"]}.{number}: written {got!r}, design {p["nets"][number]!r}')
    dup = [u for u, k in uuids.items() if k > 1]
    if dup:
        problems.append(f'{len(dup)} duplicated UUIDs, e.g. {dup[0]}')
    root_files = {pr[2] for sh in children(parse((HERE/'PCB_EU_LITE_V2.kicad_sch').read_text(encoding='utf-8'))[0], 'sheet') for pr in children(sh, 'property') if pr[1] == 'Sheetfile'}
    if root_files != {f'{s}.kicad_sch' for s in by_sheet}:
        problems.append(f'root sheet lists {sorted(root_files)}')
    for pr in problems:
        print('FAIL', pr)
    print(f'{len(spec["parts"])} parts, {pins_checked} pins traced through the written sheets, {len(uuids)} UUIDs, {len(problems)} problems')
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
