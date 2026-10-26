#!/usr/bin/env python3
"""Move the RTC cluster to B.Cu near the battery and reconnect its nets.

The oscillator, its load capacitors, and the RTC decoupler stay close to
MCP7940N. Shared I2C pull-ups remain at the existing bus branch. This is a
one-time routing step for the pre-move board.
SPDX-License-Identifier: MIT
"""
import argparse
import math
from pathlib import Path
import sys

import pcbnew as p

from build_board import HERE
sys.path.insert(0, str(HERE / 'tools'))
from route_complete import route_pair

MM = p.FromMM
SOURCE_POSITIONS = {
    'U301': (28.0, 44.0),
    'Y301': (24.0, 39.5),
    'C301': (19.5, 40.0),
    'C302': (19.5, 37.5),
    'C303': (28.0, 36.0),
}
PLACEMENTS = {
    'U301': (61.0, 61.5, 270.0),
    'Y301': (57.2, 49.5, 0.0),
    'C301': (56.2, 54.2, 90.0),
    'C302': (59.7, 54.2, 90.0),
    'C303': (58.5, 67.5, 0.0),
}
OLD_I2C_STUBS = (
    ('I2C_SDA', (27.8975, 45.5786), (30.1486, 45.5786)),
    ('I2C_SDA', (30.1486, 45.5786), (30.4750, 45.9050)),
    ('I2C_SCL', (30.4750, 44.6350), (31.7898, 44.6350)),
    ('I2C_SCL', (31.7898, 44.6350), (33.7917, 42.6331)),
)


def item_mm(position):
    return p.ToMM(position.x), p.ToMM(position.y)


def same_point(a, b, tolerance=0.008):
    return math.dist(a, b) <= tolerance


def find_footprint(board, reference):
    footprint = next((fp for fp in board.GetFootprints()
                      if fp.GetReference() == reference), None)
    if footprint is None:
        raise RuntimeError(f'Missing footprint {reference}')
    return footprint


def find_pad(board, reference, number, index=0):
    footprint = find_footprint(board, reference)
    pads = [pad for pad in footprint.Pads() if pad.GetNumber() == str(number)]
    if index >= len(pads):
        raise RuntimeError(f'Missing {reference}.{number} occurrence {index}')
    return pads[index]


def remove_net_tracks(board, net_names):
    for item in list(board.GetTracks()):
        if item.GetNetname() in net_names:
            board.Delete(item)


def remove_old_track(board, net, start, end):
    matches = []
    for item in board.GetTracks():
        if isinstance(item, p.PCB_VIA) or item.GetLayer() != p.F_Cu or item.GetNetname() != net:
            continue
        a, b = item_mm(item.GetStart()), item_mm(item.GetEnd())
        if ((same_point(a, start) and same_point(b, end)) or
                (same_point(a, end) and same_point(b, start))):
            matches.append(item)
    if len(matches) != 1:
        raise RuntimeError(f'Expected one obsolete {net} segment {start} -> {end}; found {len(matches)}')
    board.Delete(matches[0])


def route(board, net, pad_a, pad_b, name, allow_vias=True, window=20.0):
    added = route_pair(board, net, pad_a, pad_b, 0.2, f'lite_route/RTC/{name}',
                       window=window, allow_vias=allow_vias,
                       preserve_escapes=True, avoid_same_net=True)
    print(f'{name}: {len(added)} track/via items')


def apply(board):
    for reference, expected in SOURCE_POSITIONS.items():
        footprint = find_footprint(board, reference)
        if footprint.IsFlipped() or not same_point(item_mm(footprint.GetPosition()), expected):
            raise RuntimeError(f'{reference} is not at its pre-move front-side position {expected}')

    remove_net_tracks(board, {'RTC_X1', 'RTC_X2', 'VBAT'})

    for reference, (x, y, rotation) in PLACEMENTS.items():
        footprint = find_footprint(board, reference)
        footprint.Flip(p.VECTOR2I(0, 0), False)
        footprint.SetOrientationDegrees(rotation)
        footprint.SetPosition(p.VECTOR2I(MM(x), MM(y)))

    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()

    route(board, 'RTC_X1', find_pad(board, 'U301', 1), find_pad(board, 'C301', 1),
          'X1-U-C', allow_vias=False, window=12.0)
    route(board, 'RTC_X1', find_pad(board, 'C301', 1), find_pad(board, 'Y301', 1),
          'X1-C-Y', allow_vias=False, window=12.0)
    route(board, 'RTC_X2', find_pad(board, 'U301', 2), find_pad(board, 'C302', 1),
          'X2-U-C', allow_vias=False, window=12.0)
    route(board, 'RTC_X2', find_pad(board, 'C302', 1), find_pad(board, 'Y301', 2),
          'X2-C-Y', allow_vias=False, window=12.0)
    route(board, 'VBAT', find_pad(board, 'U301', 3), find_pad(board, 'BT301', 1, 0), 'VBAT-upper')
    route(board, 'VBAT', find_pad(board, 'U301', 3), find_pad(board, 'BT301', 1, 1), 'VBAT-lower')
    route(board, 'I2C_SDA', find_pad(board, 'U301', 5), find_pad(board, 'R308', 2), 'SDA')
    route(board, 'I2C_SCL', find_pad(board, 'U301', 6), find_pad(board, 'R309', 2), 'SCL')
    route(board, '+3V3', find_pad(board, 'U301', 8), find_pad(board, 'C303', 1), 'VDD-decoupling')
    route(board, '+3V3', find_pad(board, 'U301', 8), find_pad(board, 'R308', 1),
          'VDD-bus', window=25.0)

    # Remove the former U301 stubs only after the new B.Cu branches have
    # connected into the shared pull-up/bus routes.
    for net, start, end in OLD_I2C_STUBS:
        remove_old_track(board, net, start, end)

    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', type=Path, default=HERE / 'PCB_EU_LITE.kicad_pcb')
    args = parser.parse_args()
    board = p.LoadBoard(str(args.board))
    apply(board)
    p.SaveBoard(str(args.board), board)
    print('Moved U301 and its RTC components to B.Cu; connected RTC, VBAT, I2C, and +3V3 nets')


if __name__ == '__main__':
    main()
