#!/usr/bin/env python3
"""Shorten the ST1S10 switch loop on the pre-cleanup EU-LITE board.

Moves L201 and output capacitor C204 next to U201, replaces the three-via
0.2 mm switch-node detour with 0.6 mm front copper, and reconnects the 3V3 bus.
This is a one-time step for the layout with L201 at (37, 44) mm.
SPDX-License-Identifier: MIT
"""
import argparse
from pathlib import Path
import sys
from types import SimpleNamespace

import pcbnew as p

from build_board import HERE, identity
sys.path.insert(0, str(HERE / 'tools'))
from route_complete import Grid, WIDTH_BY_NET, complete, components, emit, route_pair, search

MM = p.FromMM
OLD_TRACKS = {
    '1630d447-043a-4e69-9f8d-9f3bfdca6169',
    '302c96d6-050a-4ff4-a5ca-5c07d4f151f3',
    'bf472d37-3872-45f5-9282-1e820b3eceec',
    'cde35e53-ccad-4763-8fef-bc6738814527',
    '69516c37-3411-46c2-bb56-d2c5d1a57099',
    'a61752c6-7a44-4bf5-9e8d-09cb0d792a35',
    'c400e694-5edb-4e34-acb1-9755943fd7aa',
    '3183a5d4-2819-43e4-888c-0a264a27da47',
    '4b9b61f1-bd93-45a5-be97-c15b917aa9f2',
    '4caffc91-b249-45f1-96b1-91b22b4b594d',
    '72702325-d5f6-417f-86b4-77eca549c277',
    '8f4fa73a-03cc-437f-92d2-078456eba3c6',
    'f25a874a-61dd-4e27-b6a2-9bc2d0774170',
    'd5cd772d-abef-4bfc-9047-db728cb5a424',
    '03aa1e3a-ce5e-48a6-a54c-bf0dd6895755',
    '0409ef6b-e9ee-4ebf-a7c6-97e191ba656f',
    'dcc7bad0-96ba-4975-a8f1-be7f61174240',
    'f4427e00-e545-42e1-b06e-8d210317a14b',
}


def add_track(board, net, start, end, width, tag):
    track = p.PCB_TRACK(board)
    track.SetStart(p.VECTOR2I(MM(start[0]), MM(start[1])))
    track.SetEnd(p.VECTOR2I(MM(end[0]), MM(end[1])))
    track.SetWidth(MM(width))
    track.SetLayer(p.F_Cu)
    track.SetNet(board.FindNet(net))
    identity(track, 'lite_route/switch_cleanup/' + tag)
    board.Add(track)


def apply(board):
    footprints = {fp.GetReference(): fp for fp in board.GetFootprints()}
    l201 = footprints['L201']
    position = (p.ToMM(l201.GetPosition().x), p.ToMM(l201.GetPosition().y))
    if position != (37.0, 44.0) or l201.IsFlipped():
        raise RuntimeError('Expected front-side L201 at (37, 44) mm')
    c204 = footprints['C204']
    if (p.ToMM(c204.GetPosition().x), p.ToMM(c204.GetPosition().y)) != (37.0, 50.0) or c204.IsFlipped():
        raise RuntimeError('Expected front-side C204 at (37, 50) mm')
    identifiers = {item.m_Uuid.AsString() for item in board.GetTracks()}
    missing = OLD_TRACKS - identifiers
    if missing:
        raise RuntimeError(f'The starting board is missing expected old routes: {sorted(missing)}')

    def pad(reference, number):
        return next(q for q in footprints[reference].Pads() if q.GetNumber() == str(number))

    for item in list(board.GetTracks()):
        if item.GetNetname() == 'LOGIC_SW' or item.m_Uuid.AsString() in OLD_TRACKS:
            board.Delete(item)
    for reference, x, y, rotation in (
            ('L201', 37.3, 32.0, 90), ('C204', 33.8, 31.5, 180)):
        fp = footprints[reference]
        fp.SetOrientationDegrees(rotation)
        fp.SetPosition(p.VECTOR2I(MM(x), MM(y)))

    add_track(board, 'LOGIC_SW', (37.865, 35.525), (37.865, 33.64), 0.6, 'sw1')
    add_track(board, 'LOGIC_SW', (37.865, 33.64), (37.3, 33.075), 0.6, 'sw2')
    add_track(board, '+3V3', (37.3, 30.925), (36.725, 31.5), 0.6, 'out1')
    add_track(board, '+3V3', (36.725, 31.5), (34.75, 31.5), 0.6, 'out2')
    add_track(board, 'GND', (32.85, 31.5), (32.85, 32.45), 0.3, 'cap-ground1')
    add_track(board, 'GND', (32.85, 32.45), (32.6, 32.7), 0.3, 'cap-ground2')
    via = p.PCB_VIA(board)
    via.SetPosition(p.VECTOR2I(MM(32.6), MM(32.7)))
    via.SetWidth(MM(0.5))
    via.SetDrill(MM(0.3))
    via.SetViaType(p.VIATYPE_THROUGH)
    via.SetLayerPair(p.F_Cu, p.B_Cu)
    via.SetNet(board.FindNet('GND'))
    identity(via, 'lite_route/switch_cleanup/C204-gnd-via')
    board.Add(via)

    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    print('Routing the 3V3 bus from the relocated output capacitor', flush=True)
    added = route_pair(board, '+3V3', pad('C204', 1), pad('C205', 1), 0.5,
                       'lite_route/switch_cleanup/3v3-bus', window=20,
                       allow_vias=True, preserve_escapes=True, avoid_same_net=False)
    print(f'Added {len(added)} bus track/via items', flush=True)
    added = route_pair(board, '+12V', pad('C203', 1), pad('C201', 1), 0.6,
                       'lite_route/switch_cleanup/12v-bus', window=20,
                       allow_vias=True, preserve_escapes=True, avoid_same_net=False)
    print(f'Added {len(added)} input bus track/via items', flush=True)
    # Retain the rest of the pump signal route. Replace only the local F.Cu
    # and In2.Cu runs and the via that collided with the relocated inductor.
    start, end = (24.1434, 30.4336), (37.0533, 41.8932)
    net = board.FindNet('PUMP_GPIO')
    grid = Grid(board, net.GetNetCode(),
                (start[0]-4, start[1]-4, end[0]+4, end[1]+4), 0.25)
    si, sj = grid.cell(*start)
    gi, gj = grid.cell(*end)
    path = search(grid, [(p.F_Cu, si, sj)], [(p.In2_Cu, gi, gj)])
    if path is None:
        raise RuntimeError('No local PUMP_GPIO bypass found')
    a = SimpleNamespace(GetPosition=lambda: p.VECTOR2I(MM(start[0]), MM(start[1])))
    c = SimpleNamespace(GetPosition=lambda: p.VECTOR2I(MM(end[0]), MM(end[1])))
    added = emit(board, net, grid, path, a, c, 0.25,
                 'lite_route/switch_cleanup/pump-control')
    print(f'Added {len(added)} pump control track/via items', flush=True)
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    # The former L201/C204 junctions also joined branches of both supply buses.
    # Reconnect those branches after removing the old escape traces.
    WIDTH_BY_NET['+3V3'] = 0.5
    routes, failed = complete(board, only={'+3V3', '+12V'}, attempts=1)
    print(f'Reconnected supply branches: {routes}; failed: {failed}', flush=True)
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    disconnected = [net.GetNetname() for code, net in board.GetNetInfo().NetsByNetcode().items()
                    if code and len(components(board, code)) > 1]
    if disconnected:
        raise RuntimeError(f'Pad connections split after switch routing: {disconnected}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', type=Path, default=HERE / 'PCB_EU_LITE.kicad_pcb')
    args = parser.parse_args()
    board = p.LoadBoard(str(args.board))
    apply(board)
    project = args.board.with_suffix('.kicad_pro')
    project_data = project.read_bytes()
    try:
        p.SaveBoard(str(args.board), board)
    finally:
        project.write_bytes(project_data)
    print('Saved the shortened switch stage', flush=True)


if __name__ == '__main__':
    main()
