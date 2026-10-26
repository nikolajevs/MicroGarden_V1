"""Check EU-LITE placement, pad/net parity and assembly space (KiCad + Shapely 2).

This is a 2D placement check, not routing, native DRC, height or thermal sign-off.
Export a fresh kicadxml netlist and pass its path. No saved review is overwritten.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pcbnew as p
from shapely.geometry import Polygon, box
from shapely.affinity import rotate, translate

HERE = Path(__file__).resolve().parents[1]


def check(board_path, netlist, layout_path=HERE/'placement.json'):
    board = p.LoadBoard(str(board_path))
    layout = json.loads(layout_path.read_text(encoding='utf-8'))
    root = ET.parse(netlist).getroot()
    comps = {c.get('ref'): c for c in root.findall('./components/comp')}
    nets = {(n.get('ref'), n.get('pin')): net.get('name') for net in root.findall('./nets/net') for n in net.findall('node')}
    fps = {f.GetReference(): f for f in board.GetFootprints()}
    holes = {'H1': (61,3.5), 'H2': (96.5,3.5), 'H3': (3.5,85), 'H4': (96.5,96.5)}
    errors = []
    checks = 0

    def require(ok, why):
        nonlocal checks
        checks += 1
        if not ok:
            errors.append(why)

    require(len(fps) == len(board.GetFootprints()), 'Duplicate references')
    require(set(fps) == set(comps) | set(holes), 'PCB/netlist reference set differs')
    require(set(layout['placements']) == set(comps), 'Placement/netlist reference set differs')
    require(board.GetCopperLayerCount() == layout['copper_layers'] == 4, 'Expected 4 copper layers')
    require(abs(p.ToMM(board.GetDesignSettings().GetBoardThickness()) - 1.6062) < 1e-6, 'Nominal stack thickness differs')
    routed = bool(list(board.GetTracks()))
    if not routed:
        require(not any(not z.GetIsRuleArea() for z in board.Zones()), 'Unrouted placement must have no copper zones')
    require(layout['courtyard_gap_mm'] >= .15 and layout['opposite_side_tht_joint_guard_mm'] >= .5, 'Assembly clearances were weakened')
    require(layout['bottom_courtyard_gap_mm'] >= 1 and layout['bottom_battery_access_gap_mm'] >= 2, 'Bottom assembly clearances were weakened')
    bottom_smd = {ref for group in layout['bottom_smd_groups'].values() for ref in group['refs']}
    require(set(layout['bottom_side_allowed']) == bottom_smd | {'BT301'}, 'Bottom assembly list differs')
    edges = [d for d in board.GetDrawings() if d.GetLayer() == p.Edge_Cuts]
    def xy(v): return (round(p.ToMM(v.x),6),round(p.ToMM(v.y),6))
    expected = {frozenset(e) for e in [((0,0),(100,0)),((100,0),(100,100)),((100,100),(0,100)),((0,100),(0,0))]}
    require(len(edges) == 4 and {frozenset((xy(e.GetStart()),xy(e.GetEnd()))) for e in edges} == expected, 'Expected closed 100 x 100 mm outline')
    courts, joints = {}, {}
    root_uuid = re.search(r'\(uuid "([^"]+)"', (HERE/'PCB_EU_LITE.kicad_sch').read_text(encoding='utf-8')).group(1)
    for ref, comp in comps.items():
        if ref not in fps or ref not in layout['placements']:
            continue
        fp = fps[ref]
        spec = layout['placements'][ref]
        require(fp.GetFPID().GetUniStringLibId() == comp.findtext('footprint'), f'{ref}: footprint differs')
        require(fp.GetValue() == comp.findtext('value'), f'{ref}: value differs')
        require(fp.IsFlipped() == (spec['side'] == 'B') and xy(fp.GetPosition()) == (spec['x'],spec['y']) and abs((fp.GetOrientationDegrees()-spec['rotation']+180)%360-180) < 1e-6, f'{ref}: placement differs')
        path = '/' + '/'.join([root_uuid]+[u for u in comp.find('sheetpath').get('tstamps').split('/') if u]+[comp.findtext('tstamps')])
        require(fp.GetPath().AsString() == path, f'{ref}: schematic path differs')
        for field in comp.findall('./fields/field'):
            key = field.get('name')
            if key != 'Footprint':
                require(fp.HasField(key) and fp.GetField(key).GetText() == (field.text or ''), f'{ref}: field {key} differs')
        pads = list(fp.Pads())
        numbers = {pad.GetNumber() for pad in pads}
        for nref, pin in nets:
            if nref == ref:
                require(pin in numbers, f'{ref}.{pin}: missing pad')
        joints[ref] = []
        for pad in pads:
            require(pad.GetNetname() == nets.get((ref,pad.GetNumber()), ''), f'{ref}.{pad.GetNumber()}: net differs')
            if pad.GetAttribute() == p.PAD_ATTRIB_PTH:
                b = pad.GetBoundingBox()
                joints[ref].append(box(p.ToMM(b.GetLeft()),p.ToMM(b.GetTop()),p.ToMM(b.GetRight()),p.ToMM(b.GetBottom())).buffer(layout['opposite_side_tht_joint_guard_mm'],join_style=2))
                require(pad.IsOnLayer(p.F_Mask) and pad.IsOnLayer(p.B_Mask), f'{ref}: missing THT mask opening')
        tall = any(pad.GetAttribute() == p.PAD_ATTRIB_PTH for pad in pads) or ref.startswith(('J','L','BT','SW')) or ref in {'C110','C508'}
        if (tall and ref not in layout.get('bottom_side_allowed', [])) or ref in layout['top_side_required']:
            require(not fp.IsFlipped(), f'{ref}: tall/access component must be on top')
        require(not fp.IsFlipped() or ref in layout.get('bottom_side_allowed', []), f'{ref}: not approved for bottom assembly')
        if ref in bottom_smd:
            require(fp.IsFlipped() and all(pad.GetAttribute() == p.PAD_ATTRIB_SMD for pad in pads) and ref.startswith(('R','C','Q','U','Y')), f'{ref}: expected small bottom SMD')
        poly = fp.GetCourtyard(p.B_CrtYd if fp.IsFlipped() else p.F_CrtYd)
        require(poly.OutlineCount() == 1, f'{ref}: missing/ambiguous courtyard')
        if poly.OutlineCount() != 1:
            continue
        outline = poly.Outline(0)
        court = Polygon([xy(outline.CPoint(i)) for i in range(outline.PointCount())])
        courts[ref] = court
        require(court.is_valid, f'{ref}: invalid courtyard')
        if ref != 'U202':
            require(box(.3,.3,99.7,99.7).covers(court), f'{ref}: courtyard outside board margin')
    # The ESP RF reserve extends off-board by design; its physical body is inside.
    esp = fps['U202']
    x,y = xy(esp.GetPosition())
    require(not esp.IsFlipped() and esp.GetOrientationDegrees() == 0 and box(.3,.3,99.7,99.7).covers(box(x-9,y-12.75,x+9,y+12.75)), 'ESP physical body/side/orientation')
    rf = box(x-24,y-27.75,x+24,y-6.56)
    zones = list(esp.Zones())
    require(len(zones) == 1 and zones[0].GetIsRuleArea() and zones[0].GetDoNotAllowTracks() and zones[0].GetDoNotAllowVias() and zones[0].GetDoNotAllowPads() and zones[0].GetDoNotAllowZoneFills() and all(zones[0].IsOnLayer(layer) for layer in [p.F_Cu,p.In1_Cu,p.In2_Cu,p.B_Cu]), 'ESP all-copper antenna keepout missing/disabled')
    for ref,court in courts.items():
        if ref != 'U202':
            require(not rf.intersects(court), f'{ref}: RF reserve obstructed')
    refs = sorted(courts)
    for i,ref in enumerate(refs):
        for other in refs[i+1:]:
            if fps[ref].IsFlipped() == fps[other].IsFlipped():
                gap = layout['courtyard_gap_mm']
                if fps[ref].IsFlipped():
                    gap = layout['bottom_battery_access_gap_mm'] if 'BT301' in (ref,other) else layout['bottom_courtyard_gap_mm']
                require(courts[ref].distance(courts[other]) >= gap-1e-6, f'{ref}/{other}: courtyard gap < {gap} mm')
            else:
                require(not any(j.intersects(courts[other]) for j in joints[ref]) and not any(j.intersects(courts[ref]) for j in joints[other]), f'{ref}/{other}: THT solder access obstructed')
    for ref,(x,y) in holes.items():
        fp = fps[ref]
        pads = list(fp.Pads())
        require(xy(fp.GetPosition()) == (x,y) and len(pads) == 1 and pads[0].GetAttribute() == p.PAD_ATTRIB_NPTH and xy(pads[0].GetDrillSize()) == (3.2,3.2), f'{ref}: mounting hole changed')
        reserve = box(x-3.2,y-3.2,x+3.2,y+3.2)
        for other,court in courts.items():
            require(court.distance(reserve) >= .15-1e-6, f'{other}: {ref} mounting access obstructed')
    # Owner's connector-edge contract, including SHT above both fans.
    required_edges = {'J101':'left','J411':'right','J421':'right','J501':'right','J502':'right','J503':'right','J702':'bottom',
                      'J401':'right','J403':'bottom','J701':'bottom','J301':'left'}
    require(courts['J401'].bounds[3] < courts['J411'].bounds[1] and courts['J411'].bounds[3] < courts['J421'].bounds[1], 'SHT4x must be above both fans')
    ports = layout['edge_ports']
    require(set(ports) == set(required_edges) == {r for r in comps if r.startswith('J') and r!='J201'}, 'Edge connector list incomplete')
    uart=fps['J201']
    require(xy(uart.GetPosition()) == (4,9.75) and uart.GetOrientationDegrees() == 0 and not uart.IsFlipped(), 'UART must remain at its original placement')
    require(courts['J301'].distance(courts['J201']) <= 6, 'microSD must remain near UART')
    for ref,edge in required_edges.items():
        cfg=ports[ref];fp=fps[ref]
        angle={'right':90,'bottom':0,'left':-90}[edge]
        require(cfg['edge']==edge and abs(fp.GetOrientationDegrees()-angle)<1e-6 and not fp.IsFlipped(), f'{ref}: wrong edge/mating orientation')
        x,y=xy(fp.GetPosition());front=cfg['local_access_box'][1]
        gap={'right':100-x-front,'bottom':100-y-front,'left':x-front}[edge]
        require(0 <= gap <= cfg['max_edge_gap_mm'] <= 2, f'{ref}: mating face is not at the required board edge')
        access=translate(rotate(box(*cfg['local_access_box']),-fp.GetOrientationDegrees(),origin=(0,0)),x,y)
        require(cfg['local_access_box'][3]-front >= 20, f'{ref}: access corridor shorter than 20 mm')
        for other,court in courts.items():
            if other!=ref and not fps[other].IsFlipped():
                require(not access.intersects(court), f'{ref}/{other}: connector/card access obstructed')
        for hole,(hx,hy) in holes.items():
            require(not access.intersects(box(hx-3.2,hy-3.2,hx+3.2,hy+3.2)), f'{ref}: access obstructed by {hole} mounting hardware')
    for ref in ('J411','J421'):
        fp=fps[ref]
        require(fp.GetFPID().GetUniStringLibId() == 'LITE_Parts:KF2510_1x04_P2.54mm_RightAngle', f'{ref}: expected right-angle 2510 footprint')
        pads=sorted(fp.Pads(),key=lambda pad:int(pad.GetNumber()))
        require(len(pads)==4 and all(pad.GetAttribute()==p.PAD_ATTRIB_PTH and xy(pad.GetDrillSize())==(1,1) for pad in pads), f'{ref}: incorrect 2510 holes')
        require(all(abs(((xy(a.GetPosition())[0]-xy(b.GetPosition())[0])**2+(xy(a.GetPosition())[1]-xy(b.GetPosition())[1])**2)**.5-2.54)<1e-6 for a,b in zip(pads,pads[1:])), f'{ref}: incorrect 2.54 mm pitch')
    led = fps['D901']
    pads = sorted(led.Pads(), key=lambda pad:int(pad.GetNumber()))
    require(led.GetFPID().GetUniStringLibId() == 'LITE_Parts:LED_OSTAPA57E2A_Formed_P2.54mm' and len(pads)==4, 'D901: expected formed-lead footprint')
    require(all(xy(pad.GetDrillSize())==(1,1) and xy(pad.GetSize())==(1.8,1.8) for pad in pads), 'D901: incorrect hole/annulus')
    require([pad.GetNetname() for pad in pads] == ['LED_G_K','LED_B_K','+12V','LED_R_K'], 'D901: incorrect G/B/A/R pinout')
    if errors:
        raise ValueError('\n'.join(errors))
    return dict(checks_passed=checks, pcbnew=p.GetBuildVersion(), netlisted_references=len(comps), mapped_netlist_pins=len(nets), sides=dict(Counter('B' if fps[r].IsFlipped() else 'F' for r in comps)),
                stage='ROUTED_PLACEMENT' if routed else 'UNROUTED_PLACEMENT', physical_fit_verified=False, thermal_verified=False,
                sha256={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in [board_path,netlist,layout_path]})


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('netlist',type=Path)
    ap.add_argument('--board',type=Path,default=HERE/'PCB_EU_LITE.kicad_pcb')
    ap.add_argument('--output',type=Path)
    args=ap.parse_args()
    result=check(args.board,args.netlist)
    if args.output:
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))
