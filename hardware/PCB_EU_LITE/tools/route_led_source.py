#!/usr/bin/env python3
"""Widen the Q501 source-current path to the LED switch-current shunt.

Replace the 0.2 mm source daisy chain with three 0.8 mm branches and a
continuous 0.8 mm F.Cu trunk to R501.1. Keep the independent IS sense and
test-point routes at the shunt terminal. Reroute only the gate pull-down
branch that occupied the source-current corridor; the short gate-resistor
connection to Q501 is retained. This is a one-time step for the old routing.
SPDX-License-Identifier: MIT
"""
import argparse
from pathlib import Path
import sys

import pcbnew as p

from build_board import HERE, identity
sys.path.insert(0, str(HERE / 'tools'))
from route_complete import route_pair

MM = p.FromMM
OLD_SOURCE = {
    '1a64c1b0-3916-4dd9-a6d8-1fb26016920d',
    '7c415617-fe2b-4cad-a7d9-e9b19624397e',
    'bf33ba03-8446-4b5b-adfa-78694a40a53c',
    '42967fe2-8e24-494b-adbe-738ff61095c5',
    'cd532f7f-112f-4b1f-91a7-05cccc99c797',
    '1f9604f7-79c2-4ecf-a777-c61ba8dbdc95',
    'c27e5fd5-d16c-49e9-b571-e28e129753ee',
    '45cf00f2-a888-4b41-b306-dd29836c48d0',
}
OLD_PULLDOWN = {
    '05f648eb-b5c3-4e74-95a5-9dfa19dfd2c9',
    '160a2968-aced-4516-aaa5-5f127aa9aa57',
    '71831b7e-1018-48ad-a17f-f2db2610a232',
    '82f78761-24ea-4bbf-883a-d29387c4fe74',
    'b8c8cedf-12f1-43e6-a2d5-ceaea563309c',
    'bbdc85b7-4313-451c-8667-d9e40b0276ba',
    'd0f331df-f2fc-4981-a5da-7fa151754262',
    'dbaa2dda-fcd2-4149-8e29-32d794a81cae',
    'fff2210e-ecca-4b46-83a9-9b1cbca1b3d3',
}


def add_source_track(board, start, end, tag):
    track = p.PCB_TRACK(board)
    track.SetStart(p.VECTOR2I(MM(start[0]), MM(start[1])))
    track.SetEnd(p.VECTOR2I(MM(end[0]), MM(end[1])))
    track.SetWidth(MM(0.8))
    track.SetLayer(p.F_Cu)
    track.SetNet(board.FindNet('LED_IS'))
    identity(track, 'lite_route/LED-source/' + tag)
    board.Add(track)


def apply(board):
    footprints = {fp.GetReference(): fp for fp in board.GetFootprints()}
    for reference, expected in (('Q501', (81.96, 42.5)), ('R501', (82.0, 47.5))):
        fp = footprints[reference]
        position = (p.ToMM(fp.GetPosition().x), p.ToMM(fp.GetPosition().y))
        if position != expected or fp.IsFlipped():
            raise RuntimeError(f'Expected front-side {reference} at {expected}')
    identifiers = {item.m_Uuid.AsString() for item in board.GetTracks()}
    missing = (OLD_SOURCE | OLD_PULLDOWN) - identifiers
    if missing:
        raise RuntimeError(f'Expected the old LED source/gate routing: {sorted(missing)}')
    for track in list(board.GetTracks()):
        if track.m_Uuid.AsString() in OLD_SOURCE | OLD_PULLDOWN:
            board.Delete(track)

    for number, y in ((1, 40.595), (2, 41.865), (3, 43.135)):
        add_source_track(board, (79.06, y), (80.1, y), f'source-{number}')
    add_source_track(board, (80.1, 40.595), (80.1, 46.4375), 'trunk')
    add_source_track(board, (80.1, 46.4375), (79.0375, 47.5), 'shunt-entry')

    def pad(reference, number):
        return next(q for q in footprints[reference].Pads() if q.GetNumber() == str(number))

    board.BuildConnectivity()
    added = route_pair(board, 'LED_GATE', pad('R514', 1), pad('Q501', 4), 0.2,
                       'lite_route/LED-source/gate-pulldown', window=12,
                       allow_vias=True, preserve_escapes=True, avoid_same_net=False)
    print(f'Rerouted R514 gate pull-down using {len(added)} copper items', flush=True)
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', type=Path, default=HERE / 'PCB_EU_LITE.kicad_pcb')
    args = parser.parse_args()
    project = args.board.with_suffix('.kicad_pro')
    original_project = project.read_bytes() if project.exists() else None
    try:
        board = p.LoadBoard(str(args.board))
        apply(board)
        p.SaveBoard(str(args.board), board)
    finally:
        if original_project is None:
            if project.exists():
                project.unlink()
        else:
            project.write_bytes(original_project)
    print('Saved a 0.8 mm front-side Q501 source-current trunk to R501.1')


if __name__ == '__main__':
    main()
