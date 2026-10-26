#!/usr/bin/env python3
"""Add two parallel right-edge LED terminals from the regenerated netlist.

J501/J502/J503 all share LED_POS after R502 and GND. The extra terminals
do not bypass the current shunt or add independent LED channels.
SPDX-License-Identifier: MIT
"""
import argparse
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pcbnew as p

from build_board import HERE, identity

MM = p.FromMM


def apply(board, netlist):
    root = ET.parse(netlist).getroot()
    components = {c.get('ref'): c for c in root.findall('./components/comp')}
    footprints = {f.GetReference(): f for f in board.GetFootprints()}
    if any(ref in footprints for ref in ('J502', 'J503')):
        raise RuntimeError('The additional LED terminals are already on the board')
    layout = json.loads((HERE / 'placement.json').read_text(encoding='utf-8'))
    root_uuid = re.search(r'\(uuid "([^"]+)"',
                          (HERE / 'PCB_EU_LITE.kicad_sch').read_text(encoding='utf-8')).group(1)
    terminal_nets = {(node.get('ref'), node.get('pin')): net.get('name')
                     for net in root.findall('./nets/net') for node in net.findall('node')
                     if node.get('ref') in ('J502', 'J503')}
    for reference in ('J502', 'J503'):
        comp = components[reference]
        nick, name = comp.findtext('footprint').split(':', 1)
        if terminal_nets[(reference, '1')] != 'LED_POS' or terminal_nets[(reference, '2')] != 'GND':
            raise RuntimeError(f'{reference}: expected the shared LED_POS/GND output')
        fp = p.FootprintLoad(str(HERE / 'libraries' / (nick + '.pretty')), name)
        if fp is None:
            raise RuntimeError(f'Missing repository footprint {nick}:{name}')
        fp.SetFPID(p.LIB_ID(nick, name))
        fp.SetReference(reference)
        fp.SetValue(comp.findtext('value'))
        for field in comp.findall('./fields/field'):
            key = field.get('name')
            if key != 'Footprint':
                fp.SetField(key, field.text or '')
                fp.GetField(key).SetVisible(False)
        fp.SetField('Datasheet', comp.findtext('datasheet', ''))
        fp.GetField('Datasheet').SetVisible(False)
        path = p.KIID_PATH()
        for identifier in [root_uuid] + [u for u in comp.find('sheetpath').get('tstamps').split('/') if u] + [comp.findtext('tstamps')]:
            path.push_back(p.KIID(identifier))
        fp.SetPath(path)
        identity(fp, reference)
        for index, item in enumerate([*fp.Pads(), *fp.GraphicalItems(), *fp.Zones(), *fp.GetFields()]):
            identity(item, f'{reference}/item/{index}')
        board.Add(fp)
        spec = layout['placements'][reference]
        if spec['side'] != 'F' or spec['rotation'] != 90:
            raise RuntimeError(f'{reference}: expected a top right-facing terminal')
        fp.SetOrientationDegrees(spec['rotation'])
        fp.SetPosition(p.VECTOR2I(MM(spec['x']), MM(spec['y'])))
        fp.Reference().SetLayer(p.F_Fab)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        for pad in fp.Pads():
            pad.SetNet(board.FindNet(terminal_nets[(reference, pad.GetNumber())]))

    # Keep existing pad geometry and routing while updating the specification
    # fields for the current-set resistors and any revised assembly notes.
    for reference, fp in footprints.items():
        comp = components.get(reference)
        if comp is None:
            continue
        if fp.GetFPID().GetUniStringLibId() != comp.findtext('footprint'):
            raise RuntimeError(f'{reference}: footprint changed; a deliberate placement update is needed')
        fp.SetValue(comp.findtext('value'))
        for field in comp.findall('./fields/field'):
            key = field.get('name')
            if key != 'Footprint':
                fp.SetField(key, field.text or '')
                fp.GetField(key).SetVisible(False)

    paths = (
        ((94.0, 61.0), (96.5, 63.5)),
        ((96.5, 63.5), (96.5, 74.0)),
        ((96.5, 74.0), (94.0, 74.0)),
        ((96.5, 74.0), (96.5, 87.0)),
        ((96.5, 87.0), (94.0, 87.0)),
    )
    for index, (start, end) in enumerate(paths):
        track = p.PCB_TRACK(board)
        track.SetStart(p.VECTOR2I(MM(start[0]), MM(start[1])))
        track.SetEnd(p.VECTOR2I(MM(end[0]), MM(end[1])))
        track.SetWidth(MM(1.0))
        track.SetLayer(p.F_Cu)
        track.SetNet(board.FindNet('LED_POS'))
        identity(track, f'lite_route/LED-terminals/positive/{index}')
        board.Add(track)
    # Connector names and polarity legends are generated after all routing by
    # tools/silkscreen.py.  Keeping them out of this one-time routing pass
    # lets the silk generator remove collisions and apply one consistent rule.
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('netlist', type=Path)
    parser.add_argument('--board', type=Path, default=HERE / 'PCB_EU_LITE.kicad_pcb')
    args = parser.parse_args()
    project = args.board.with_suffix('.kicad_pro')
    original_project = project.read_bytes()
    try:
        board = p.LoadBoard(str(args.board))
        apply(board, args.netlist)
        p.SaveBoard(str(args.board), board)
    finally:
        project.write_bytes(original_project)
    print('Added J502/J503 at the right edge and routed the shared LED output with 1 mm F.Cu')


if __name__ == '__main__':
    main()
