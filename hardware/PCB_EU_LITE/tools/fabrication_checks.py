"""Independent physical pre-export checks for EU-LITE (pcbnew + Shapely 2).

Includes same-net Kelvin isolation, which normal DRC cannot enforce.
SPDX-License-Identifier: MIT
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import pcbnew as p
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from build_board import HERE, identity

LAYERS = (p.F_Cu, p.In1_Cu, p.In2_Cu, p.B_Cu)
SILK = (p.F_SilkS, p.B_SilkS)
KEEP = []


def xy(v):
    return p.ToMM(v.x), p.ToMM(v.y)


def bbox(item):
    b = item.GetBoundingBox()
    return box(p.ToMM(b.GetLeft()), p.ToMM(b.GetTop()),
               p.ToMM(b.GetRight()), p.ToMM(b.GetBottom()))


def conductor(item):
    if isinstance(item, p.PCB_VIA):
        return Point(*xy(item.GetPosition())).buffer(p.ToMM(item.GetWidth(p.F_Cu))/2)
    return LineString([xy(item.GetStart()), xy(item.GetEnd())]).buffer(p.ToMM(item.GetWidth())/2)


def filled(zone):
    polys = zone.GetFilledPolysList(zone.GetLayer())
    def ring(chain):
        return [xy(chain.CPoint(i)) for i in range(chain.PointCount())]
    return unary_union([
        Polygon(ring(polys.Outline(i)),
                [ring(polys.Hole(i,j)) for j in range(polys.HoleCount(i))])
        for i in range(polys.OutlineCount())
    ])


def marker(board, name):
    item = p.PCB_TRACK(board)
    identity(item, 'finalize/' + name)
    return item.m_Uuid.AsString()


def kelvin_check(board):
    tracks = list(board.GetTracks())
    pads = {(f.GetReference(), q.GetNumber()): q for f in board.GetFootprints() for q in f.Pads()}
    plus = {marker(board, 'kelvin_plus_' + name) for name in (
        'escape', 'escape2', 'escape3', 'back', 'back2', 'entry', 'pad', 'start', 'end',
    )}
    groups = {
        'IN+': ('GND', [t for t in tracks if t.m_Uuid.AsString() in plus], '10', '1'),
        'IN-': ('RTN_SHUNT', [t for t in tracks if t.GetNetname() == 'RTN_SHUNT'
                and (isinstance(t,p.PCB_VIA) or p.ToMM(t.GetWidth()) <= .20001)], '9', '2'),
    }
    evidence = {}
    for name, (net, sense, ic_pin, shunt_pin) in groups.items():
        if not sense:
            raise RuntimeError(f'{name}: missing isolated sense route')
        ids = {t.m_Uuid.AsString() for t in sense}
        allowed = bbox(pads[('R111',shunt_pin)]).buffer(.05)
        for layer in LAYERS:
            # Include the whole IC pin, not merely its centre: the old keepout
            # missed the upper half after U102 had been moved upwards.
            pieces = [conductor(t) for t in sense if t.IsOnLayer(layer)]
            pin = pads[('U102',ic_pin)]
            if pin.IsOnLayer(layer):
                pieces.append(bbox(pin))
            if not pieces:
                continue
            shape = unary_union(pieces).difference(allowed)
            for zone in board.Zones():
                if zone.GetIsRuleArea() or zone.GetNetname() != net or not zone.IsOnLayer(layer):
                    continue
                contact = filled(zone).intersection(shape)
                if contact.area > .00001:
                    raise RuntimeError(f'{name}: premature pour contact on {board.GetLayerName(layer)} at {contact.bounds}')
            for (ref,number), pad in pads.items():
                if (ref,number) in {('U102',ic_pin),('R111',shunt_pin)}:
                    continue
                if pad.GetNetname() == net and pad.IsOnLayer(layer) and bbox(pad).intersection(shape).area > .00001:
                    raise RuntimeError(f'{name}: unintended pad contact {ref}.{number}')
            for item in tracks:
                if item.m_Uuid.AsString() in ids or item.GetNetname() != net or not item.IsOnLayer(layer):
                    continue
                if conductor(item).intersection(shape).area > .00001:
                    raise RuntimeError(f'{name}: premature same-net track/via contact {item.m_Uuid.AsString()}')
        evidence[name] = dict(pin='U102.'+ic_pin, target='R111.'+shunt_pin,
                              isolated_from_same_net_pours_pads_and_tracks=True, items=len(sense))
    return evidence


def library_check(board):
    differences, other = [], []
    for fp in board.GetFootprints():
        lib = str(fp.GetFPID().GetLibNickname())
        if not lib:
            continue
        root = HERE
        source = p.FootprintLoad(str(root/'libraries'/(lib+'.pretty')), str(fp.GetFPID().GetLibItemName()))
        if source is None:
            raise RuntimeError(f'Cannot load library footprint for {fp.GetReference()}')
        if not fp.FootprintNeedsUpdate(source):
            continue
        copy = p.FOOTPRINT(fp)
        KEEP.extend((copy,source))
        for footprint in (copy, source):
            for graphic in list(footprint.GraphicalItems()):
                if graphic.GetLayer() in SILK:
                    footprint.Remove(graphic)
                    graphic.thisown = False
                    KEEP.append(graphic)
        if copy.FootprintNeedsUpdate(source):
            other.append(fp.GetReference())
        else:
            differences.append(fp.GetReference())
    if other:
        raise RuntimeError(f'Electrical/mechanical library mismatch: {other}')
    return sorted(differences)


def check(board_path):
    board = p.LoadBoard(str(board_path))
    board.BuildConnectivity()
    if board.GetConnectivity().GetUnconnectedCount(False):
        raise RuntimeError('Unconnected copper')
    tracks = [t for t in board.GetTracks() if not isinstance(t,p.PCB_VIA)]
    vias = [t for t in board.GetTracks() if isinstance(t,p.PCB_VIA)]
    if min(p.ToMM(t.GetWidth()) for t in tracks) < .19999:
        raise RuntimeError('Track below project 0.2 mm rule')
    if any(p.ToMM(v.GetDrillValue()) < .29999 or
           p.ToMM(v.GetWidth(p.F_Cu)-v.GetDrillValue()) < .19999 for v in vias):
        raise RuntimeError('Via drill/ring below approved geometry')
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetAttribute() == p.PAD_ATTRIB_PTH:
                ring = min(pad.GetSize().x-pad.GetDrillSize().x,
                           pad.GetSize().y-pad.GetDrillSize().y)/2
                if p.ToMM(ring) < .14999:
                    raise RuntimeError(f'{fp.GetReference()}: PTH annulus below 0.15 mm')
    texts = []
    shapes = []
    for item in list(board.GetDrawings()) + [g for fp in board.GetFootprints() for g in fp.GraphicalItems()]:
        if item.GetLayer() not in SILK:
            continue
        if isinstance(item,p.PCB_TEXT) and item.IsVisible():
            texts.append(item)
        elif isinstance(item,p.PCB_SHAPE) and item.GetWidth() > 0:
            shapes.append(item)
    if any(p.ToMM(t.GetTextThickness()) < .14999 or p.ToMM(t.GetTextHeight()) < .79999 for t in texts):
        raise RuntimeError('Silkscreen text below 0.15 mm stroke / 0.8 mm absolute height')
    if any(p.ToMM(s.GetWidth()) < .14999 for s in shapes):
        raise RuntimeError('Native silk line below 0.15 mm')
    return dict(
        source_sha256=hashlib.sha256(board_path.read_bytes()).hexdigest(),
        kicad_version=p.Version(), tracks=len(tracks), vias=len(vias),
        copper_layers=board.GetCopperLayerCount(),
        size_mm=[100,100], nominal_thickness_mm=p.ToMM(board.GetDesignSettings().GetBoardThickness()),
        visible_silk_texts=len(texts), silk_texts_below_recommended_1mm=sum(p.ToMM(t.GetTextHeight())<.99999 for t in texts),
        min_silk_stroke_mm=min([p.ToMM(t.GetTextThickness()) for t in texts]+[p.ToMM(s.GetWidth()) for s in shapes]),
        min_track_mm=min(p.ToMM(t.GetWidth()) for t in tracks),
        min_via_drill_mm=min(p.ToMM(v.GetDrillValue()) for v in vias),
        via_drill_counts=dict(Counter(str(p.ToMM(v.GetDrillValue())) for v in vias)),
        verified_silk_only_library_differences=library_check(board),
        kelvin=kelvin_check(board),
        physical_fit_verified=False, thermal_qualified=False,
    )


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--board',type=Path,default=HERE/'PCB_EU_LITE.kicad_pcb')
    ap.add_argument('--output',type=Path)
    args=ap.parse_args()
    result=check(args.board)
    if args.output:
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))
