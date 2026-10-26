#!/usr/bin/env python3
"""Connect the bottom battery holder's negative contact to the GND plane.

The BC-2001 footprint keeps copper out from underneath the holder, except for
its 4 x 4 mm negative contact. A narrow B.Cu corridor extends that contact to a
through via just outside the holder. The route carries only the RTC backup
current; it does not cross the battery's VBAT tracks on the inner layers.
SPDX-License-Identifier: MIT
"""
import argparse
import math
from pathlib import Path

import pcbnew as p

from build_board import HERE, identity

MM = p.FromMM
ROUTE_END = (67.2, 80.0)
CORRIDOR_HALF_HEIGHT = 0.35


def mm_box(item):
    box = item.GetBoundingBox()
    return tuple(p.ToMM(value) for value in
                 (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom()))


def find_pad(board, reference, number):
    footprint = next((fp for fp in board.GetFootprints()
                      if fp.GetReference() == reference), None)
    if footprint is None:
        raise RuntimeError(f'Missing footprint {reference}')
    pad = next((pad for pad in footprint.Pads()
                if pad.GetNumber() == str(number)), None)
    if pad is None:
        raise RuntimeError(f'Missing pad {reference}.{number}')
    return footprint, pad


def add_track_keepout(board, name, points):
    zone = p.ZONE(board)
    identity(zone, 'lite_power/' + name)
    zone.SetLayer(p.B_Cu)
    zone.SetIsRuleArea(True)
    zone.SetDoNotAllowTracks(True)
    zone.SetDoNotAllowVias(False)
    zone.SetDoNotAllowPads(False)
    zone.SetDoNotAllowFootprints(False)
    zone.SetDoNotAllowZoneFills(False)
    zone.SetZoneName('lite_power/' + name)
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in points:
        outline.Append(MM(x), MM(y))
    board.Add(zone)


def prepare_holder_keepout(board, footprint):
    matches = []
    for zone in footprint.Zones():
        if not zone.GetIsRuleArea():
            continue
        if not zone.IsOnLayer(p.B_Cu):
            continue
        left, top, right, bottom = mm_box(zone)
        if (math.isclose(left, 68.0, abs_tol=0.02) and
                math.isclose(top, 70.0, abs_tol=0.02) and
                math.isclose(right, 75.8, abs_tol=0.02) and
                math.isclose(bottom, 90.0, abs_tol=0.02)):
            matches.append(zone)
    if len(matches) != 1:
        raise RuntimeError(f'Expected one left-side BT301 B.Cu keepout; found {len(matches)}')

    # This LITE-specific library footprint allows the GND escape through a
    # narrow corridor; two board rule areas retain the no-track guard around it.
    matches[0].SetDoNotAllowTracks(False)
    x0, x1 = 68.0, 75.8
    y0, y1 = 70.0, 90.0
    gap0, gap1 = 80.0 - CORRIDOR_HALF_HEIGHT, 80.0 + CORRIDOR_HALF_HEIGHT
    add_track_keepout(board, 'bt301_ground_keepout/top',
                      [(x0, y0), (x1, y0), (x1, gap0), (x0, gap0)])
    add_track_keepout(board, 'bt301_ground_keepout/bottom',
                      [(x0, gap1), (x1, gap1), (x1, y1), (x0, y1)])


def remove_previous_route(board, pad):
    old_start = (p.ToMM(pad.GetPosition().x), p.ToMM(pad.GetPosition().y))
    old_segments = (
        (old_start, (76.2, 80.0)),
        ((76.2, 80.0), (76.2, 68.5)),
        ((76.2, 68.5), (67.2, 68.5)),
        (old_start, ROUTE_END),
    )
    for item in list(board.GetTracks()):
        if item.GetNetname() != 'GND':
            continue
        if isinstance(item, p.PCB_VIA):
            position = (p.ToMM(item.GetPosition().x), p.ToMM(item.GetPosition().y))
            if any(math.dist(position, point) < 0.01
                   for point in (ROUTE_END, (67.2, 68.5))):
                board.Delete(item)
            continue
        if item.GetLayer() != p.B_Cu:
            continue
        a, b = (p.ToMM(item.GetStart().x), p.ToMM(item.GetStart().y)), \
               (p.ToMM(item.GetEnd().x), p.ToMM(item.GetEnd().y))
        if any((math.dist(a, start) < 0.01 and math.dist(b, end) < 0.01) or
               (math.dist(b, start) < 0.01 and math.dist(a, end) < 0.01)
               for start, end in old_segments):
            board.Delete(item)

    for zone in list(board.Zones()):
        if (zone.GetIsRuleArea() and
                zone.GetZoneName().startswith('lite_power/bt301_ground_keepout/')):
            board.Delete(zone)


def add_track(board, start, end, index):
    track = p.PCB_TRACK(board)
    track.SetStart(p.VECTOR2I(MM(start[0]), MM(start[1])))
    track.SetEnd(p.VECTOR2I(MM(end[0]), MM(end[1])))
    track.SetWidth(MM(0.2))
    track.SetLayer(p.B_Cu)
    track.SetNet(board.FindNet('GND'))
    identity(track, f'lite_route/BT301/GND-track-{index}')
    board.Add(track)


def route_battery_ground(board):
    footprint, pad = find_pad(board, 'BT301', '2')
    position = (p.ToMM(pad.GetPosition().x), p.ToMM(pad.GetPosition().y))
    if not (math.isclose(position[0], 78.0, abs_tol=0.01) and
            math.isclose(position[1], 80.0, abs_tol=0.01)):
        raise RuntimeError(f'Unexpected BT301.2 position: {position}')

    remove_previous_route(board, pad)
    prepare_holder_keepout(board, footprint)
    add_track(board, position, ROUTE_END, 1)

    via = p.PCB_VIA(board)
    via.SetPosition(p.VECTOR2I(MM(ROUTE_END[0]), MM(ROUTE_END[1])))
    via.SetWidth(MM(0.5))
    via.SetDrill(MM(0.3))
    via.SetViaType(p.VIATYPE_THROUGH)
    via.SetLayerPair(p.F_Cu, p.B_Cu)
    via.SetNet(board.FindNet('GND'))
    identity(via, 'lite_route/BT301/GND-via')
    board.Add(via)

    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', type=Path, default=HERE / 'PCB_EU_LITE.kicad_pcb')
    args = parser.parse_args()
    board = p.LoadBoard(str(args.board))
    route_battery_ground(board)
    p.SaveBoard(str(args.board), board)
    print('Routed BT301.2 GND through the LITE holder footprint corridor to an external plane via')


if __name__ == '__main__':
    main()
