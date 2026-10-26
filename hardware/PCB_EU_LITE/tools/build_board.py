"""Rebuild unrouted EU-LITE from the netlist and placement.json using KiCad Python.

The outline and mounting holes come from the bundled mechanical template. Electrical
footprints and all fields are loaded afresh from repository libraries/netlist.
Rotations are front-view, after flipping. Existing routed copper is protected.
"""
import argparse
import json
import re
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
import pcbnew as p

HERE = Path(__file__).resolve().parents[1]
SOURCE = HERE / 'mechanical/outline.kicad_pcb'
OUT = HERE / 'PCB_EU_LITE.kicad_pcb'
HOLES = {'H1', 'H2', 'H3', 'H4'}
NS = uuid.UUID('bb4fc3ec-c871-45f5-8806-4a3c372068e2')
from stackup import apply_file as apply_stackup, BOARD_MM
from kicad_sexpr import spans


def identity(item, name):
    value = p.KIID(str(uuid.uuid5(NS, name)))
    if hasattr(item, 'SetUuidDirect'):
        item.SetUuidDirect(value)
    else:
        item.m_Uuid.Clone(value)


def build(netlist, output):
    if output.exists():
        old = p.LoadBoard(str(output))
        if len(old.GetTracks()) or any(not z.GetIsRuleArea() for z in old.Zones()):
            raise SystemExit('Refusing to overwrite routed tracks, vias or copper zones')
    root = ET.parse(netlist).getroot()
    comps = {c.get('ref'): c for c in root.findall('./components/comp')}
    layout = json.loads((HERE / 'placement.json').read_text(encoding='utf-8'))
    if set(layout['placements']) != set(comps):
        raise ValueError('placement.json reference set differs from exported netlist')
    root_uuid = re.search(r'\(uuid "([^"]+)"', (HERE / 'PCB_EU_LITE.kicad_sch').read_text(encoding='utf-8')).group(1)
    source = p.LoadBoard(str(SOURCE))
    # A new BOARD avoids carrying EU-0.1's project settings into SaveBoard.
    board = p.BOARD()
    identity(board, 'board')
    board.SetCopperLayerCount(4)
    board.GetDesignSettings().SetBoardThickness(p.FromMM(BOARD_MM))
    for item in [d for d in source.GetDrawings() if d.GetLayer() == p.Edge_Cuts] + [f for f in source.GetFootprints() if f.GetReference() in HOLES]:
        duplicate = item.Duplicate(False) if isinstance(item, p.FOOTPRINT) else item.Duplicate()
        identity(duplicate, 'mechanical/' + item.m_Uuid.AsString())
        if isinstance(item, p.FOOTPRINT):
            duplicate = p.Cast_to_FOOTPRINT(duplicate)
            for i, child in enumerate([*duplicate.Pads(), *duplicate.GraphicalItems(), *duplicate.GetFields()]):
                identity(child, f'{duplicate.GetReference()}/item/{i}')
        board.Add(duplicate)
    fps = {}
    for ref, comp in sorted(comps.items()):
        nick, name = comp.findtext('footprint').split(':', 1)
        libroot = HERE
        fp = p.FootprintLoad(str(libroot / 'libraries' / (nick + '.pretty')), name)
        if fp is None:
            raise ValueError(f'{ref}: missing repository footprint {nick}:{name}')
        fp.SetFPID(p.LIB_ID(nick, name))
        fp.SetReference(ref)
        fp.SetValue(comp.findtext('value'))
        for field in comp.findall('./fields/field'):
            key = field.get('name')
            if key != 'Footprint':
                fp.SetField(key, field.text or '')
                fp.GetField(key).SetVisible(False)
        fp.SetField('Datasheet', comp.findtext('datasheet', ''))
        fp.GetField('Datasheet').SetVisible(False)
        path = p.KIID_PATH()
        for u in [root_uuid] + [u for u in comp.find('sheetpath').get('tstamps').split('/') if u] + [comp.findtext('tstamps')]:
            path.push_back(p.KIID(u))
        fp.SetPath(path)
        identity(fp, ref)
        for i, item in enumerate([*fp.Pads(), *fp.GraphicalItems(), *fp.Zones(), *fp.GetFields()]):
            identity(item, f'{ref}/item/{i}')
        board.Add(fp)
        spec = layout['placements'][ref]
        if spec['side'] == 'B':
            fp.Flip(p.VECTOR2I(0, 0), False)
        elif spec['side'] != 'F':
            raise ValueError(f'{ref}: invalid side')
        fp.SetOrientationDegrees(spec['rotation'])
        fp.SetPosition(p.VECTOR2I(p.FromMM(spec['x']), p.FromMM(spec['y'])))
        fp.Reference().SetLayer(p.B_Fab if fp.IsFlipped() else p.F_Fab)
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        fps[ref] = fp
    for net in root.findall('./nets/net'):
        info = p.NETINFO_ITEM(board, net.get('name'))
        board.Add(info)
        for node in net.findall('node'):
            pads = [pad for pad in fps[node.get('ref')].Pads() if pad.GetNumber() == node.get('pin')]
            if not pads:
                raise ValueError(f'Missing pad: {node.attrib}')
            for pad in pads:
                pad.SetNet(info)
    title = board.GetTitleBlock()
    title.SetTitle('EU-LITE / unrouted placement')
    title.SetRevision('EU-LITE-0.1-PLACEMENT')
    title.SetDate(layout['date'])
    board.SetTitleBlock(title)
    note = p.PCB_TEXT(board)
    note.SetText('EU-LITE / UNROUTED / 100 x 100 mm / 4 Cu / NOT FOR FABRICATION')
    note.SetLayer(p.Dwgs_User)
    note.SetPosition(p.VECTOR2I(p.FromMM(50), p.FromMM(-18)))
    note.SetTextSize(p.VECTOR2I(p.FromMM(1), p.FromMM(1)))
    identity(note, 'stage')
    board.Add(note)
    # KiCad 10.0.5 SaveBoard can also write a .kicad_pro with empty/default
    # project settings. Serialize in isolation so the real project is untouched.
    with tempfile.TemporaryDirectory(prefix='eu-lite-board-') as temp:
        staged = Path(temp) / output.name
        p.SaveBoard(str(staged), board)
        text = staged.read_text(encoding='utf-8')
        # KiCad 10 exposes auxiliary footprint points as opaque SWIG objects.
        # Stabilize their IDs in the serialized file, retaining their geometry.
        for a,b in reversed(spans(text,'footprint')):
            fragment = text[a:b]
            ref = re.search(r'\(property "Reference" "([^"]+)"',fragment).group(1)
            for i,(start,end) in reversed(list(enumerate(spans(fragment,'point')))):
                point = fragment[start:end]
                value = str(uuid.uuid5(NS, f'{ref}/point/{i}'))
                point,count = re.subn(r'\(uuid "[^"]+"\)', f'(uuid "{value}")',point)
                if count != 1:
                    raise ValueError(f'{ref}: auxiliary point must have one UUID')
                fragment = fragment[:start]+point+fragment[end:]
            text = text[:a]+fragment+text[b:]
        output.write_text(text,encoding='utf-8',newline='\n')
    apply_stackup(output)
    output.write_text(output.read_text(encoding='utf-8'),encoding='utf-8',newline='\n')
    print(f'Built {len(fps)} netlisted footprints + 4 mounting holes; 0 tracks/vias/copper pours')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('netlist', type=Path)
    parser.add_argument('--output', type=Path, default=OUT)
    args = parser.parse_args()
    build(args.netlist, args.output)
