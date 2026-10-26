#!/usr/bin/env python3
"""Remove obsolete dangling track segments and vias from a fresh KiCad DRC.

Only track_dangling/via_dangling UUIDs in the supplied report are considered.
Pad-group connectivity is compared before and after as an early guard. Native
DRC must follow each pass: it checks the physical zone polygons individually
and finds newly exposed dead ends that the pad-group helper cannot detect.
SPDX-License-Identifier: MIT
"""
import argparse
import json
from pathlib import Path
import sys

import pcbnew as p

from build_board import HERE
sys.path.insert(0, str(HERE / 'tools'))
from route_complete import components


def pad_groups(board, code):
    board.BuildConnectivity()
    return [frozenset(pad.m_Uuid.AsString() for pad in group)
            for group in components(board, code)]


def apply(board, report):
    identifiers = {item['uuid'] for violation in report.get('violations', [])
                   if violation.get('type') in {'track_dangling', 'via_dangling'}
                   for item in violation.get('items', [])}
    tracks = [track for track in board.GetTracks()
              if track.m_Uuid.AsString() in identifiers]
    codes = {track.GetNetCode() for track in tracks}
    if any(track.GetNetname() == 'GND' for track in tracks):
        raise RuntimeError('GND stubs require a separate physical copper-group audit')
    before = {code: pad_groups(board, code) for code in codes}
    removed = []
    for track in tracks:
        removed.append((track.GetNetname(), track.m_Uuid.AsString(),
                        'via' if isinstance(track, p.PCB_VIA) else 'track'))
        board.Delete(track)
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    for code, groups in before.items():
        after = pad_groups(board, code)
        if any(not any(group <= other for other in after) for group in groups):
            raise RuntimeError(f'Pruning would split pad connections on net code {code}')
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--board', type=Path, default=HERE / 'PCB_EU_LITE.kicad_pcb')
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding='utf-8'))
    project = args.board.with_suffix('.kicad_pro')
    original_project = project.read_bytes()
    try:
        board = p.LoadBoard(str(args.board))
        removed = apply(board, report)
        if removed:
            p.SaveBoard(str(args.board), board)
    finally:
        project.write_bytes(original_project)
    for net, identifier, kind in removed:
        print(f'Removed {net} dead-end {kind} {identifier}')
    print(f'Pruned {len(removed)} copper items without splitting pad groups')


if __name__ == '__main__':
    main()
