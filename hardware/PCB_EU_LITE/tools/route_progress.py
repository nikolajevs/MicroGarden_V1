#!/usr/bin/env python3
"""Apply the 25 Oct EU-LITE routing step to the partial-routing board.

Moves C203 to free the U201 switch-node breakout, joins U201.7 to L201.1,
connects the moved bypass cap to GND, relieves U102.2 directly to the ground
zone, and refills copper zones. This is a one-time step; it refuses to run on
a board whose starting geometry differs.
SPDX-License-Identifier: MIT
"""
import argparse
import math
from pathlib import Path
import sys

import pcbnew as p

from build_board import HERE, identity
sys.path.insert(0, str(HERE / 'tools'))
from route_complete import route_pair

MM = p.FromMM
TRACKS_TO_REMOVE = (
    ('+12V', (36.6888, 32.6388), (37.0500, 33.0000)),
    ('GND', (36.5950, 35.1572), (38.7522, 33.0000)),
    ('GND', (36.5950, 35.5250), (36.5950, 35.1572)),
    ('GND', (38.7522, 33.0000), (38.9500, 33.0000)),
    ('GND', (44.7750, 33.0000), (38.9500, 33.0000)),
)


def mm_point(point):
    return p.ToMM(point.x), p.ToMM(point.y)


def same_point(a, b, tolerance=0.006):
    return math.dist(a, b) <= tolerance


def remove_track(board, net, start, end):
    matches = []
    for item in board.GetTracks():
        if isinstance(item, p.PCB_VIA) or item.GetLayer() != p.F_Cu or item.GetNetname() != net:
            continue
        a, b = mm_point(item.GetStart()), mm_point(item.GetEnd())
        if ((same_point(a, start) and same_point(b, end)) or
                (same_point(a, end) and same_point(b, start))):
            matches.append(item)
    if len(matches) != 1:
        raise RuntimeError(f'Expected one {net} segment {start} -> {end}; found {len(matches)}')
    board.Delete(matches[0])


def get_pad(board, ref, number):
    fp = next((f for f in board.GetFootprints() if f.GetReference() == ref), None)
    if fp is None:
        raise RuntimeError(f'Missing footprint {ref}')
    pad = next((q for q in fp.Pads() if q.GetNumber() == str(number)), None)
    if pad is None:
        raise RuntimeError(f'Missing pad {ref}.{number}')
    return pad


def apply(board):
    c203 = next((f for f in board.GetFootprints() if f.GetReference() == 'C203'), None)
    if c203 is None or mm_point(c203.GetPosition()) != (38.0, 33.0):
        raise RuntimeError('Expected C203 at its pre-routing position (38, 33) mm')

    for net, start, end in TRACKS_TO_REMOVE:
        remove_track(board, net, start, end)

    c203.SetPosition(p.VECTOR2I(MM(40.7), MM(31.5)))
    get_pad(board, 'C203', '2').SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)
    get_pad(board, 'U102', '2').SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)

    route_pair(board, 'LOGIC_SW', get_pad(board, 'U201', '7'), get_pad(board, 'L201', '1'),
               0.2, 'lite_route/LOGIC_SW', window=20, allow_vias=True,
               preserve_escapes=True, avoid_same_net=True)

    ground_via = p.PCB_VIA(board)
    ground_via.SetPosition(p.VECTOR2I(MM(42.7), MM(31.5)))
    ground_via.SetWidth(MM(0.5))
    ground_via.SetDrill(MM(0.3))
    ground_via.SetViaType(p.VIATYPE_THROUGH)
    ground_via.SetLayerPair(p.F_Cu, p.B_Cu)
    ground_via.SetNet(board.FindNet('GND'))
    identity(ground_via, 'lite_route/C203/GND-via')
    board.Add(ground_via)

    title = board.GetTitleBlock()
    title.SetTitle('EU-LITE / routing in progress')
    title.SetRevision('EU-LITE-0.1-ROUTING')
    board.SetTitleBlock(title)
    for item in board.GetDrawings():
        if isinstance(item, p.PCB_TEXT) and 'UNROUTED' in item.GetText():
            item.SetText('EU-LITE / PARTIALLY ROUTED / 100 x 100 mm / 4 Cu / NOT FOR FABRICATION')
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--board', type=Path, default=HERE / 'PCB_EU_LITE.kicad_pcb')
    args = ap.parse_args()
    board = p.LoadBoard(str(args.board))
    apply(board)
    p.SaveBoard(str(args.board), board)
    print(f'Routed U201.7 to L201.1; unconnected={board.GetConnectivity().GetUnconnectedCount(False)}')


if __name__ == '__main__':
    main()
