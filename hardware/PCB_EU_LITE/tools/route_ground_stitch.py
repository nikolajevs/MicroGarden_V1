#!/usr/bin/env python3
"""Connect two isolated component GND groups to the common inner ground plane.

The top copper around U201 and the top copper joining C501/C515 each contain
real component ground pads but have no connection to the main ground plane.
Two 0.5/0.3 mm through vias join those groups to In1.Cu. No keepout or design
rule is weakened. This is a one-time routing step; existing vias are refused.
SPDX-License-Identifier: MIT
"""
import argparse
import math
from pathlib import Path

import pcbnew as p

from build_board import HERE, identity

MM = p.FromMM
STITCHES = (
    ('C501-C515', 64.0, 34.0),
    ('U201', 42.0, 40.5),
)


def existing_ids(board):
    items = [board, *board.GetTracks(), *board.Zones(), *board.GetDrawings()]
    for footprint in board.GetFootprints():
        items.extend((footprint, *footprint.Pads(), *footprint.GraphicalItems(),
                      *footprint.GetFields(), *footprint.Zones()))
    return {item.m_Uuid.AsString() for item in items}


def apply(board):
    ground = board.FindNet('GND')
    if ground is None:
        raise RuntimeError('Missing GND net')
    zone_names = {zone.GetZoneName() for zone in board.Zones()}
    if not {'lite_power/gnd_top', 'lite_power/gnd_inner'} <= zone_names:
        raise RuntimeError('Expected the EU-LITE top and inner GND pours')

    known_ids = existing_ids(board)
    existing_vias = [item for item in board.GetTracks()
                     if isinstance(item, p.PCB_VIA)]
    pending = []
    for name, x, y in STITCHES:
        via = p.PCB_VIA(board)
        identity(via, f'lite_route/GND-stitch/{name}')
        identifier = via.m_Uuid.AsString()
        if identifier in known_ids:
            raise RuntimeError(f'{name}: GND stitch UUID already exists')
        for other in existing_vias:
            position = other.GetPosition()
            if math.dist((p.ToMM(position.x), p.ToMM(position.y)), (x, y)) < 0.01:
                raise RuntimeError(f'{name}: a via already exists at ({x}, {y}) mm')
        known_ids.add(identifier)
        via.SetPosition(p.VECTOR2I(MM(x), MM(y)))
        via.SetWidth(MM(0.5))
        via.SetDrill(MM(0.3))
        via.SetViaType(p.VIATYPE_THROUGH)
        via.SetLayerPair(p.F_Cu, p.B_Cu)
        via.SetNet(ground)
        pending.append(via)

    # Validate both sites before changing the board, so a duplicate at the
    # second site cannot leave the first via behind in the in-memory board.
    for via in pending:
        board.Add(via)
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
        # KiCad 10.0.5 may also rewrite the project while saving a board.
        # Preserve its original bytes, including the case of no project file.
        if original_project is None:
            if project.exists():
                project.unlink()
        elif not project.exists() or project.read_bytes() != original_project:
            project.write_bytes(original_project)
    print('Added GND stitches at (64, 34) and (42, 40.5) mm; refilled copper zones')


if __name__ == '__main__':
    main()
