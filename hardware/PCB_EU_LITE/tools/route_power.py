"""Add the EU-LITE ground plane and strengthen local power copper.

The pours are deliberately local around the PTC, pump, 24 V input, LED driver,
and 3.3 V converter. They supplement the routed tracks; they do not replace
current, thermal, or prototype measurements.
SPDX-License-Identifier: MIT
"""
import argparse
import math
from pathlib import Path

import pcbnew as p

from build_board import HERE, identity

MM = p.FromMM
CLEARANCE = 0.2
MIN_THICKNESS = 0.25


def route_corridor(points, half_width):
    """Return a mitered copper corridor around a non-reversing route polyline."""
    directions = []
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length == 0:
            raise ValueError('Route corridor has duplicate consecutive points')
        directions.append((dx / length, dy / length))

    def offset_side(sign):
        normals = [(sign * -dy, sign * dx) for dx, dy in directions]
        result = [(points[0][0] + half_width * normals[0][0],
                   points[0][1] + half_width * normals[0][1])]
        for i in range(1, len(points) - 1):
            previous, following = normals[i - 1], normals[i]
            denominator = 1 + previous[0] * following[0] + previous[1] * following[1]
            if denominator < 0.1:
                raise ValueError('Route corridor has an almost reversing corner')
            scale = half_width / denominator
            result.append((points[i][0] + scale * (previous[0] + following[0]),
                           points[i][1] + scale * (previous[1] + following[1])))
        result.append((points[-1][0] + half_width * normals[-1][0],
                       points[-1][1] + half_width * normals[-1][1]))
        return result

    return offset_side(1) + list(reversed(offset_side(-1)))


def add_zone(board, net, layer, name, points, priority=0):
    zone = p.ZONE(board)
    identity(zone, 'lite_power/zone/' + name)
    zone.SetLayer(board.GetLayerID(layer))
    zone.SetNet(board.FindNet(net))
    zone.SetLocalClearance(MM(CLEARANCE))
    zone.SetMinThickness(MM(MIN_THICKNESS))
    solid_power = {'led_feed', 'led_input_inner', 'led_terminal_bus', 'input_raw', 'input_raw_bottom', 'input_return_inner', 'q102_drain_thermal', 'pump_return', 'fused_led_feed', 'fused_pump_feed', 'fused_ptc_feed',
                   'fused_main_bus', 'fused_main_bus_source', 'fused_main_bus_wide', 'return_q102_bus'}
    connection = p.ZONE_CONNECTION_FULL if name in solid_power else p.ZONE_CONNECTION_THERMAL
    zone.SetPadConnection(connection)
    zone.SetThermalReliefGap(MM(0.3))
    zone.SetThermalReliefSpokeWidth(MM(0.5 if net == 'GND' else 0.35))
    zone.SetIslandRemovalMode(p.ISLAND_REMOVAL_MODE_ALWAYS)
    zone.SetAssignedPriority(priority)
    zone.SetZoneName('lite_power/' + name)
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in points:
        outline.Append(MM(x), MM(y))
    board.Add(zone)
    return zone


def add_ground_outline(board, layer, name):
    edges = board.GetBoardEdgesBoundingBox()
    left, top, right, bottom = [p.ToMM(v) for v in
                                (edges.GetLeft(), edges.GetTop(), edges.GetRight(), edges.GetBottom())]
    inset = 0.5
    return add_zone(board, 'GND', layer, name,
                    [(left+inset,top+inset),(right-inset,top+inset),
                     (right-inset,bottom-inset),(left+inset,bottom-inset)], 0)


def add_rule_keepout(board, name, layer, points):
    zone = p.ZONE(board)
    identity(zone, 'lite_power/keepout/' + name)
    zone.SetLayer(board.GetLayerID(layer))
    zone.SetIsRuleArea(True)
    zone.SetDoNotAllowTracks(False)
    zone.SetDoNotAllowVias(False)
    zone.SetDoNotAllowPads(False)
    zone.SetDoNotAllowFootprints(False)
    zone.SetDoNotAllowZoneFills(True)
    zone.SetZoneName('lite_power/keepout/' + name)
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in points:
        outline.Append(MM(x), MM(y))
    board.Add(zone)
    return zone


def set_full_pad_connection(board, ref, number):
    fp = next((f for f in board.GetFootprints() if f.GetReference() == ref), None)
    if fp is None:
        raise RuntimeError(f'Cannot find footprint {ref}')
    pad = next((q for q in fp.Pads() if q.GetNumber() == number), None)
    if pad is None:
        raise RuntimeError(f'Cannot find pad {ref}.{number}')
    pad.SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)


def add_power_zones(board):
    # Keep all GND pours clear of the INA238 IN+ Kelvin branch. It uses two
    # through vias; the plane must not join either via or the trace before the
    # R111.1 shunt pad, where the high-current return is sensed.
    for layer in ('F.Cu','In1.Cu','B.Cu'):
        add_rule_keepout(board, f'ina238_inplus_kelvin_vertical_{layer}', layer,
                         [(17.7,42.9),(18.9,42.9),(18.9,51.9),(17.7,51.9)])
        add_rule_keepout(board, f'ina238_inplus_kelvin_entry_{layer}', layer,
                         [(18.618,51.182),(19.718,52.282),(19.082,52.918),(17.982,51.818)])
    for layer, name in (('In1.Cu', 'gnd_inner'), ('F.Cu', 'gnd_top'), ('B.Cu', 'gnd_bottom')):
        add_ground_outline(board, layer, name)

    # Use direct copper on these compact SMD pins where the thermal relief was
    # starved to one spoke by the nearby zones.
    for ref, number in (('U102','2'), ('U102','7'), ('U102','10'), ('U201','2'), ('U201','4'),
                        ('U201','6'), ('U501','1'), ('U501','9'), ('D701','1'), ('Q102','2'),
                        ('Q703','2'), ('Q703','3'), ('J701','1'), ('J701','2'), ('Q102','3')):
        set_full_pad_connection(board, ref, number)

    specs = [
        ('+24V_RAW', 'F.Cu', 'input_raw',
         [(5.4,44.5),(24.0,48.8),(24.0,51.2),(5.4,47.0)], 4),
        # Do not force the clearance-limited B.Cu trace itself wider.  A local
        # parallel pour follows it instead, so the fill retracts around the
        # REVP_GATE and +24V_FUSED copper while adding width wherever space
        # permits between the input terminal and the F101 via.
        ('+24V_RAW', 'B.Cu', 'input_raw_bottom',
         route_corridor([(6.0,46.0),(6.0,46.625),(14.129,54.754),
                         (20.472,54.754),(23.135,52.091)], 1.6), 4),
        ('PSU_RTN', 'F.Cu', 'input_return',
         [(5.4,49.4),(17.2,48.4),(17.2,53.0),(5.4,52.7)], 3),
        # J101.2 and Q102.2 are through-hole pads, so a local In1.Cu strip
        # creates a continuous parallel return path without adding vias.  It
        # intentionally ends before the INA238 Kelvin keepout at x=17.7.
        ('PSU_RTN', 'In1.Cu', 'input_return_inner',
         route_corridor([(6.0,51.0),(6.54,50.46),(15.5,50.46)], 1.2), 4),
        # The IRL2505 drain/tab is Q102.2.  This deliberately small B.Cu
        # island adds local thermal copper through its plated pad without
        # approaching the +24V_RAW diagonal or the RTN_SHUNT Kelvin area.
        ('PSU_RTN', 'B.Cu', 'q102_drain_thermal',
         route_corridor([(13.0,50.46),(15.5,50.46)], 1.0), 4),
        # Strengthen only the Q102 side of RTN_SHUNT; the existing INA238
        # Kelvin keepout separates this pour from the sense branch.
        ('RTN_SHUNT', 'F.Cu', 'return_q102_bus',
         route_corridor([(15.5,47.92),(16.817,47.92),(16.817,48.952),(17.591,49.725)], 3.0), 4),
        ('+24V_FUSED', 'F.Cu', 'input_24v',
         [(22.3,46.5),(44.5,46.5),(46.8,49.5),(45.2,56.6),(25.0,57.4),(22.3,52.2)], 2),
        # Add a 4 mm copper corridor to F103. The modeled average LED-driver
        # input current reaches about 3.9 A at 75 W, 21.6 V, and 90% efficiency.
        # The pour clips itself around the nearby D101 and LED nets.
        ('+24V_FUSED', 'F.Cu', 'fused_led_feed',
         route_corridor([(44.0,46.5),(53.35,42.15),(55.0,42.15),
                         (56.99,40.16),(56.99,35.29),(56.2,34.5)], 2.0), 3),
        # Widen the source-side segment feeding the pump fuse F105.
        ('+24V_FUSED', 'F.Cu', 'fused_pump_feed',
         route_corridor([(61.5,93.51),(70.01,85.0),(70.75,85.0)], 1.5), 3),
        # Widen the fused source-side branch from F102 toward the lower power rail.
        ('+24V_FUSED', 'F.Cu', 'fused_ptc_feed',
         route_corridor([(48.0,87.3),(48.0,88.65),(48.1,88.76),
                         (48.1,94.08),(50.13,96.11),(60.49,96.11)], 2.0), 4),
        # Keep a 3 mm corridor by R706 so its GND pad retains two thermal spokes.
        ('+24V_FUSED', 'F.Cu', 'fused_main_bus',
         route_corridor([(42.93,59.06),(42.93,67.68),(43.25,68.0),
                         (45.31,70.19),(50.06,70.19),(51.36,71.5),(54.5,71.5),
                         (60.55,77.55),(75.72,77.55),(81.06,82.89),(81.06,87.12),
                         (80.46,87.72),(73.47,87.72),(70.75,85.0)], 1.5), 4),
        # Widen the source-side run, leaving a 3 mm gap around R706's GND pad.
        ('+24V_FUSED', 'F.Cu', 'fused_main_bus_source',
         route_corridor([(42.93,59.06),(42.93,67.68),(43.25,68.0),
                         (45.31,70.19),(50.06,70.19),(51.36,71.5),(54.5,71.5),
                         (60.55,77.55)], 2.0), 5),
        # Widen the load-side run to 4 mm after R706.
        ('+24V_FUSED', 'F.Cu', 'fused_main_bus_wide',
         route_corridor([(66.0,77.55),(75.72,77.55),(81.06,82.89),(81.06,87.12),
                         (80.46,87.72),(73.47,87.72),(70.75,85.0)], 2.0), 5),
        ('+12V', 'F.Cu', 'logic_12v',
         [(31.0,31.0),(46.8,31.0),(47.0,36.5),(42.0,36.5),(42.0,41.5),(32.0,41.5)], 2),
        ('+24V_LED', 'F.Cu', 'led_input',
         [(57.5,32.5),(62.5,32.5),(65.7,41.7),(68.0,45.5),(67.7,51.9),
          (63.8,52.6),(61.0,45.0),(57.5,39.5)], 2),
        # The LED driver can draw up to about 4.36 A peak at the low input
        # voltage corner.  Its long In2.Cu trunk remains a routed 0.8 mm
        # conductor for determinism, with this same-net copper beside it
        # wherever local signal clearances permit.
        ('+24V_LED', 'In2.Cu', 'led_input_inner',
         route_corridor([(65.0,13.25),(62.277,15.973),(62.277,36.666),
                         (67.127,41.516)], 1.2), 3),
        ('+24V_LED', 'F.Cu', 'led_feed',
         [(57.4,17.8),(64.2,11.1),(66.6,13.5),(60.0,20.2),
          (60.0,34.5),(57.4,34.5)], 3),
        # This is the quiet DC side after R502.  Reinforce the common LED+
        # collector to the three parallel terminals without growing the
        # preceding high-dv/dt LED_OUT switch node.
        ('LED_POS', 'F.Cu', 'led_terminal_bus',
         route_corridor([(94.0,61.0),(96.5,63.5),(96.5,87.0),(94.0,87.0)], 1.0), 3),
        ('+24V_PTC', 'F.Cu', 'ptc_supply',
         [(43.0,82.0),(49.1,82.0),(49.1,96.5),(43.0,96.5)], 3),
        ('PTC_DRAIN', 'F.Cu', 'ptc_return',
         [(49.6,80.0),(56.4,80.0),(56.4,95.2),(49.6,95.2)], 3),
        ('+24V_PUMP', 'F.Cu', 'pump_supply',
         [(56.5,82.5),(80.5,82.5),(80.5,95.2),(56.5,95.2)], 3),
        ('PUMP_DRAIN', 'F.Cu', 'pump_return',
         [(58.0,81.0),(61.0,78.2),(66.5,77.5),(66.5,95.2),
          (62.5,95.2),(62.5,83.5),(58.0,84.5)], 4),
    ]
    for net, layer, name, points, priority in specs:
        add_zone(board, net, layer, name, points, priority)


def prepare_return_path(board):
    """Move INA238 away from the return trunk and widen the three freed segments."""
    fp = next((f for f in board.GetFootprints() if f.GetReference() == 'U102'), None)
    if fp is None:
        raise RuntimeError('Cannot find footprint U102')
    target_y = MM(45.2)
    delta_y = target_y - fp.GetPosition().y
    if delta_y:
        if abs(p.ToMM(delta_y) + 0.8) > 0.005:
            raise RuntimeError(f'Unexpected U102 Y position {p.ToMM(fp.GetPosition().y):.3f} mm')
        moved = {}
        for pad in fp.Pads():
            old = pad.GetPosition()
            moved[(old.x, old.y)] = p.VECTOR2I(old.x, old.y + delta_y)
        for item in board.GetTracks():
            if isinstance(item, p.PCB_VIA):
                continue
            start, end = item.GetStart(), item.GetEnd()
            if (start.x, start.y) in moved:
                item.SetStart(moved[(start.x, start.y)])
            if (end.x, end.y) in moved:
                item.SetEnd(moved[(end.x, end.y)])
        fp.SetPosition(p.VECTOR2I(fp.GetPosition().x, target_y))

    def pair(item):
        start, end = item.GetStart(), item.GetEnd()
        return frozenset(((round(p.ToMM(start.x), 3), round(p.ToMM(start.y), 3)),
                          (round(p.ToMM(end.x), 3), round(p.ToMM(end.y), 3))))

    # These three short segments only routed GND between the old U102.7 escape
    # and a GND trunk. The full GND plane now connects both ends directly.
    redundant_gnd = {
        frozenset(((20.0, 43.0), (20.0, 43.017))),
        frozenset(((20.0, 43.017), (19.696, 42.713))),
        frozenset(((19.696, 42.713), (16.975, 42.713))),
    }
    for item in list(board.GetTracks()):
        if (not isinstance(item, p.PCB_VIA) and item.GetNetname() == 'GND'
                and item.GetLayer() == p.F_Cu and pair(item) in redundant_gnd):
            board.Delete(item)

    widened_segments = {
        frozenset(((17.591, 49.725), (16.817, 48.952))),
        frozenset(((17.591, 49.725), (19.488, 49.725))),
        frozenset(((19.488, 49.725), (20.0, 50.237))),
    }
    found = set()
    for item in board.GetTracks():
        if (isinstance(item, p.PCB_VIA) or item.GetNetname() != 'RTN_SHUNT'
                or item.GetLayer() != p.F_Cu or pair(item) not in widened_segments):
            continue
        item.SetWidth(MM(1.4))
        found.add(pair(item))
    if found != widened_segments:
        raise RuntimeError(f'Expected three return segments to widen; found {len(found)}')

    medium_segments = {
        frozenset(((15.5, 47.92), (16.817, 47.92))),
        frozenset(((16.817, 48.952), (16.817, 47.92))),
        frozenset(((21.733, 53.370), (22.4, 54.038))),
    }
    found_medium = set()
    for item in board.GetTracks():
        if (isinstance(item, p.PCB_VIA) or item.GetNetname() != 'RTN_SHUNT'
                or item.GetLayer() != p.F_Cu or pair(item) not in medium_segments):
            continue
        item.SetWidth(MM(1.3))
        found_medium.add(pair(item))
    if found_medium != medium_segments:
        raise RuntimeError(f'Expected three return segments to widen; found {len(found_medium)}')

    clearance_limited_segments = {
        frozenset(((20.0, 50.237), (21.733, 51.970))),
        frozenset(((21.733, 51.970), (21.733, 53.370))),
    }
    found_limited = set()
    for item in board.GetTracks():
        if (isinstance(item, p.PCB_VIA) or item.GetNetname() != 'RTN_SHUNT'
                or item.GetLayer() != p.F_Cu or pair(item) not in clearance_limited_segments):
            continue
        item.SetWidth(MM(1.2))
        found_limited.add(pair(item))
    if found_limited != clearance_limited_segments:
        raise RuntimeError(f'Expected two clearance-limited return segments; found {len(found_limited)}')


def widen_led_supply_trunk(board):
    """Widen selected 24 V LED supply tracks within their clearance limits."""
    targets = {
        (board.GetLayerID('F.Cu'), frozenset(((58.75, 38.275), (58.75, 46.914)))): 0.85,
        (board.GetLayerID('F.Cu'), frozenset(((61.025, 36.0), (58.75, 38.275)))): 1.0,
        (board.GetLayerID('F.Cu'), frozenset(((61.893, 49.062), (64.106, 51.275)))): 0.8,
        (board.GetLayerID('In2.Cu'), frozenset(((62.277, 36.666), (67.127, 41.516)))): 1.0,
        (board.GetLayerID('In2.Cu'), frozenset(((65.0, 13.25), (62.277, 15.973)))): 1.0,
        (board.GetLayerID('In2.Cu'), frozenset(((62.277, 15.973), (62.277, 36.666)))): 0.8,
    }
    found = set()
    for item in board.GetTracks():
        if isinstance(item, p.PCB_VIA) or item.GetNetname() != '+24V_LED':
            continue
        start, end = item.GetStart(), item.GetEnd()
        pair = frozenset(((round(p.ToMM(start.x), 3), round(p.ToMM(start.y), 3)),
                          (round(p.ToMM(end.x), 3), round(p.ToMM(end.y), 3))))
        key = (item.GetLayer(), pair)
        if key in targets:
            item.SetWidth(MM(targets[key]))
            found.add(key)
    if found != set(targets):
        raise RuntimeError(f'Expected six LED supply trunk segments; found {len(found)}')


def widen_ptc_trunk(board):
    """Widen the PTC feed and drain tracks inside their high-current pours."""
    targets = {
        ('+24V_PTC', frozenset(((46.0, 94.0), (46.0, 84.898)))),
        ('+24V_PTC', frozenset(((46.0, 84.898), (46.198, 84.7)))),
        ('+24V_PTC', frozenset(((48.0, 84.7), (46.198, 84.7)))),
        ('PTC_DRAIN', frozenset(((51.0, 94.0), (51.0, 84.95)))),
    }
    found = set()
    for item in board.GetTracks():
        if isinstance(item, p.PCB_VIA) or item.GetNetname() not in {'+24V_PTC', 'PTC_DRAIN'}:
            continue
        start, end = item.GetStart(), item.GetEnd()
        pair = frozenset(((round(p.ToMM(start.x), 3), round(p.ToMM(start.y), 3)),
                          (round(p.ToMM(end.x), 3), round(p.ToMM(end.y), 3))))
        key = (item.GetNetname(), pair)
        if key in targets:
            item.SetWidth(MM(1.5))
            found.add(key)
    if found != targets:
        raise RuntimeError(f'Expected four PTC trunk segments; found {len(found)}')


def widen_input_trunks(board):
    """Reinforce the input return and the short +24V_RAW fuse-side escape.

    The longer +24V_RAW B.Cu run stays at 1.2 mm: trial widths caused
    clearances/shorts to REVP_GATE and +24V_FUSED. This deliberately targets
    only the F.Cu escape and the wider return path that pass native DRC.
    """
    targets = {
        ('+24V_RAW', board.GetLayerID('F.Cu'), frozenset(((23.500, 50.300), (23.500, 51.652)))): 1.6,
        ('+24V_RAW', board.GetLayerID('F.Cu'), frozenset(((23.500, 51.726), (23.500, 51.652)))): 1.6,
        ('PSU_RTN', board.GetLayerID('F.Cu'), frozenset(((6.540, 50.460), (6.000, 51.000)))): 2.0,
        ('PSU_RTN', board.GetLayerID('F.Cu'), frozenset(((15.500, 50.460), (6.540, 50.460)))): 2.0,
    }
    found = set()
    for item in board.GetTracks():
        if isinstance(item, p.PCB_VIA):
            continue
        start, end = item.GetStart(), item.GetEnd()
        pair = frozenset(((round(p.ToMM(start.x), 3), round(p.ToMM(start.y), 3)),
                          (round(p.ToMM(end.x), 3), round(p.ToMM(end.y), 3))))
        key = (item.GetNetname(), item.GetLayer(), pair)
        if key in targets:
            item.SetWidth(MM(targets[key]))
            found.add(key)
    if found != set(targets):
        raise RuntimeError(f'Expected four input power tracks to widen; found {len(found)}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--board', type=Path, default=HERE/'PCB_EU_LITE.kicad_pcb')
    args = ap.parse_args()
    project_path = args.board.with_suffix('.kicad_pro')
    if not project_path.exists():
        raise RuntimeError('Copy the matching .kicad_pro beside the board before zone filling')
    original_project = project_path.read_bytes()
    try:
        board = p.LoadBoard(str(args.board))
        for zone in list(board.Zones()):
            if zone.GetZoneName().startswith('lite_power/'):
                board.Delete(zone)
        prepare_return_path(board)
        widen_led_supply_trunk(board)
        widen_ptc_trunk(board)
        widen_input_trunks(board)
        add_power_zones(board)
        p.ZONE_FILLER(board).Fill(board.Zones())
        board.BuildConnectivity()
        p.SaveBoard(str(args.board), board)
    finally:
        project_path.write_bytes(original_project)
    print(f'Added three ground pours and 22 local power pours; moved U102 and widened power routes; '
          f'tracks/vias={len(board.GetTracks())}; '
          f'unconnected={board.GetConnectivity().GetUnconnectedCount(False)}')


if __name__ == '__main__':
    main()
