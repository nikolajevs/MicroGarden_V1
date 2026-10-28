"""Stitch the INA238 shunt ground to the plane and tighten the LED boost loop.

Run after repair_power.py. Reworks existing routing; schematic, nets and BOM stay unchanged.
The shunt GND pads R111.1/R112.1 sit on F.Cu with no plane via nearby, so the whole
return current used to climb to F.Cu through SW201's pin. The LED part moves D501 onto
the Q501 drain and places the output capacitors between the cathode and R501's ground.
SPDX-License-Identifier: MIT
"""

import argparse
import math
from pathlib import Path
import pcbnew as p
from build_board import HERE, identity

# GND via sites next to the shunt pads and across the F.Cu ground island around them.
# Each site was checked against other-net copper on all layers, paste and Kelvin keepouts.
SHUNT_GND_VIAS = [
    (22.1, 57.96), (21.4, 58.76), (18.3, 53.06), (18.3, 55.86), (25.3, 58.56), (22.8, 60.66),
    (13.2, 57.3), (18.3, 59.1), (15.7, 58.7), (23.8, 64.1), (11.2, 55.3), (14.3, 63.0),
    (9.8, 56.8), (9.9, 58.4), (16.4, 64.7), (26.6, 59.8), (9.1, 55.3), (19.8, 67.0), (9.9, 60.7),
]


def point(x, y):
    return p.VECTOR2I(p.FromMM(x), p.FromMM(y))


def replace(board, item, name):
    identity(item, name)
    for existing in list(board.GetTracks()):
        if existing.m_Uuid.AsString() == item.m_Uuid.AsString():
            board.Delete(existing)


def add_via(board, name, net, xy, size=0.6, drill=0.3):
    via = p.PCB_VIA(board)
    replace(board, via, "repair/" + name)
    via.SetPosition(point(*xy))
    via.SetWidth(p.FromMM(size))
    via.SetDrill(p.FromMM(drill))
    via.SetViaType(p.VIATYPE_THROUGH)
    via.SetLayerPair(p.F_Cu, p.B_Cu)
    via.SetNet(board.FindNet(net))
    board.Add(via)
    return via


def add_track(board, name, net, layer, a, b, width):
    track = p.PCB_TRACK(board)
    replace(board, track, "repair/" + name)
    track.SetStart(point(*a))
    track.SetEnd(point(*b))
    track.SetLayer(layer)
    track.SetWidth(p.FromMM(width))
    track.SetNet(board.FindNet(net))
    board.Add(track)
    return track


def shunt_ground(board):
    for i, xy in enumerate(SHUNT_GND_VIAS):
        add_via(board, f"shunt_gnd_{i}", "GND", xy)
    print("Shunt ground vias", len(SHUNT_GND_VIAS), flush=True)


# Commutation cell: D501 anodes sit on the Q501 drain, C515/C514/C513 run from the
# D501 cathode to a local ground that also carries R501's ground pad. The inductor feed
# reaches the switch node from outside the loop. (x, y, rotation) in mm/degrees.
CELL = {
    "D501": (83.06, 37.3, 0),
    "Q501": (81.96, 44.35, 0),
    "R501": (76.21, 42.02, 90),
    "C515": (79.5, 35.65, 270),
    "C514": (76.69, 35.65, 270),
    "C513": (73.26, 35.65, 270),
    "R512": (81.0, 48.65, 0),
    "R513": (81.0, 51.15, 0),
    "R514": (84.9, 49.0, 90),
    "R507": (67.35, 34.6, 0),
}
# Nets whose routing in the cell is replaced completely.
RIPPED_NETS = ("LED_SW", "LED_GATE", "LED_GATE_RAW", "LED_IS", "LED_RT_MID")
# Single segments (net, layer, start, end) and vias (net, position) that cross the new cell
# or only served the old positions of D501, C513, C514, C515, R501 and R514.
RIPPED_SEGMENTS = [
    ("LED_OUT", "F.Cu", (72.2, 38.7625), (71.6, 38.7625)),
    ("LED_OUT", "F.Cu", (71.6, 38.7625), (71.2, 39.1625)),
    ("LED_OUT", "B.Cu", (64.4, 40.4625), (66.1, 38.7625)),
    ("LED_OUT", "F.Cu", (63.0, 41.8625), (64.4, 40.4625)),
    ("LED_OUT", "B.Cu", (66.1, 38.7625), (72.2, 38.7625)),
    ("LED_OUT", "F.Cu", (73.9, 34.4), (75.0, 35.5)),
    ("LED_OUT", "F.Cu", (75.0, 35.5), (75.05, 35.5)),
    ("LED_OUT", "B.Cu", (68.1, 34.4), (73.9, 34.4)),
    ("LED_OUT", "F.Cu", (68.1, 36.8), (68.1, 34.4)),
    ("LED_OUT", "F.Cu", (67.5, 37.4), (68.1, 36.8)),
    ("LED_OUT", "F.Cu", (53.525, 56.0), (55.525, 58.0)),
    ("LED_OUT", "F.Cu", (55.525, 58.0), (55.525, 62.0)),
    ("LED_OUT", "F.Cu", (55.525, 62.0), (55.525, 64.5)),
    ("LED_OUT", "F.Cu", (55.525, 64.5), (56.525, 65.5)),
    ("GND", "F.Cu", (58.475, 62.0), (58.6962, 61.7788)),
    ("GND", "F.Cu", (58.6962, 61.7788), (58.6962, 59.283)),
    ("GND", "F.Cu", (58.475, 62.0), (59.475, 63.0)),
    ("GND", "F.Cu", (59.475, 63.0), (59.475, 65.5)),
    ("GND", "F.Cu", (76.95, 51.5), (80.9625, 51.5)),
    ("GND", "F.Cu", (80.9625, 51.5), (84.9625, 47.5)),
    ("GND", "F.Cu", (84.9625, 47.5), (85.4983, 47.5)),
    ("GND", "F.Cu", (85.4983, 47.5), (86.9983, 46.0)),
    ("GND", "F.Cu", (88.2, 46.0), (86.9983, 46.0)),
    ("GND", "F.Cu", (88.2, 34.475), (85.5, 37.175)),
]
RIPPED_VIAS = [("LED_OUT", (72.2, 38.7625)), ("LED_OUT", (73.9, 34.4)), ("LED_OUT", (64.4, 40.4625)),
               ("LED_OUT", (68.1, 34.4)), ("GND", (76.0, 34.0))]
# Solid F.Cu copper of the cell (net, name, outline).
CELL_ZONES = [
    ("LED_OUT", "led_out_cell", [(70.0, 33.45), (84.9, 33.45), (84.9, 35.05), (71.5, 35.05),
                                 (71.5, 39.95), (73.9, 39.95), (73.9, 46.9), (71.5, 46.9),
                                 (71.5, 42.6), (65.3, 42.6), (65.3, 36.0), (70.0, 36.0)]),
    ("GND", "led_loop_ground", [(71.9, 35.85), (80.55, 35.85), (80.55, 40.3), (74.3, 40.3),
                                (74.3, 39.75), (71.9, 39.75)]),
    ("LED_IS", "led_source_bus", [(74.4, 44.2), (77.1, 44.2), (77.1, 41.95), (79.65, 41.95),
                                  (79.65, 45.6), (74.4, 45.6)]),
    ("LED_SW", "led_switch_node", [(81.25, 39.25), (87.0, 39.25), (87.0, 42.45), (81.25, 42.45)]),
]
CELL_GND_VIAS = [(72.4, 38.85), (73.5, 38.85), (80.1, 39.45), (78.3, 38.7), (79.2, 38.7),
                 (78.4, 39.6), (75.0, 40.1), (75.9, 40.1)]
# Board fields left behind by the 283 kHz change (values from LED.kicad_sch).
FIELDS = {
    ("R505", "Design_note"): "With R506=1k: 5.7k total, ~1.444 A / 69.3 W-class at 48 V shared by all LED outputs",
    ("R506", "MPN"): "SMD1206-1K-1%",
    ("R507", "MPN"): "CRCW120682K0FKTABC",
}
CELL_TRACKS = [
    # Inductor feed along the F.Cu edge of the cell, outside the commutation loop.
    ("LED_SW", (65.0, 30.75), (86.0, 30.75), 2.0),
    ("LED_SW", (86.0, 30.75), (86.0, 40.4), 2.0),
    # RT resistor taken from the inductor pad on F.Cu.
    ("LED_SW", (65.89, 33.73), (65.89, 32.4), 0.5),
    # Gate drive below Q501.
    ("LED_GATE", (79.06, 46.26), (79.06, 47.15), 0.3),
    ("LED_GATE", (79.06, 47.15), (82.46, 47.15), 0.3),
    ("LED_GATE", (82.46, 47.15), (82.46, 51.15), 0.3),
    ("LED_GATE", (82.46, 49.83), (84.9, 49.83), 0.3),
    ("LED_GATE_RAW", (79.54, 48.65), (79.54, 51.15), 0.3),
    # U501.10 escapes between the VCC and PGND traces as before, then runs on In2
    # around the top of the HSN sense trace to the gate resistors.
    ("LED_GATE_RAW", (72.9, 50.65), (74.0, 50.65), 0.2),
    ("LED_GATE_RAW", (74.0, 50.65), (74.15, 50.6), 0.2),
    ("LED_GATE_RAW", (74.15, 50.6), (72.1, 48.55), 0.2, "In2.Cu"),
    ("LED_GATE_RAW", (72.1, 48.55), (72.1, 45.9), 0.2, "In2.Cu"),
    ("LED_GATE_RAW", (72.1, 45.9), (78.2, 45.9), 0.2, "In2.Cu"),
    ("LED_GATE_RAW", (78.2, 45.9), (78.2, 48.6), 0.2, "In2.Cu"),
    ("LED_GATE_RAW", (78.2, 48.6), (79.54, 48.65), 0.25),
]
CELL_VIAS = [("LED_GATE_RAW", (74.15, 50.6)), ("LED_GATE_RAW", (78.2, 48.6))]


def same(a, b):
    return abs(a[0] - b[0]) < 0.002 and abs(a[1] - b[1]) < 0.002


def mm_xy(v):
    return p.ToMM(v.x), p.ToMM(v.y)


def add_zone(board, net, name, points, priority=6):
    zone = p.ZONE(board)
    identity(zone, "lite_power/zone/" + name)
    for existing in list(board.Zones()):
        if existing.m_Uuid.AsString() == zone.m_Uuid.AsString():
            board.Remove(existing)
    zone.SetLayer(p.F_Cu)
    zone.SetNet(board.FindNet(net))
    zone.SetLocalClearance(p.FromMM(0.2))
    zone.SetMinThickness(p.FromMM(0.25))
    zone.SetPadConnection(p.ZONE_CONNECTION_FULL)
    zone.SetIslandRemovalMode(p.ISLAND_REMOVAL_MODE_ALWAYS)
    zone.SetAssignedPriority(priority)
    zone.SetZoneName("lite_power/" + name)
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in points:
        outline.Append(p.FromMM(x), p.FromMM(y))
    board.Add(zone)


def led_cell(board):
    removed = 0
    for item in list(board.GetTracks()):
        net = item.GetNetname()
        if net in RIPPED_NETS:
            board.Delete(item)
            removed += 1
            continue
        if isinstance(item, p.PCB_VIA):
            if any(net == n and same(mm_xy(item.GetPosition()), xy) for n, xy in RIPPED_VIAS):
                board.Delete(item)
                removed += 1
            continue
        ends = (mm_xy(item.GetStart()), mm_xy(item.GetEnd()))
        layer = board.GetLayerName(item.GetLayer())
        for n, lay, a, b in RIPPED_SEGMENTS:
            if net == n and layer == lay and (
                (same(ends[0], a) and same(ends[1], b)) or (same(ends[0], b) and same(ends[1], a))
            ):
                board.Delete(item)
                removed += 1
                break
    fps = {f.GetReference(): f for f in board.GetFootprints()}
    for ref, (x, y, rot) in CELL.items():
        fps[ref].SetPosition(point(x, y))
        fps[ref].SetOrientationDegrees(rot)
    for net, name, outline in CELL_ZONES:
        add_zone(board, net, name, outline)
    for i, xy in enumerate(CELL_GND_VIAS):
        add_via(board, f"led_loop_gnd_{i}", "GND", xy)
    for i, (net, a, b, width, *layer) in enumerate(CELL_TRACKS):
        add_track(board, f"led_cell_{i}", net, board.GetLayerID(layer[0] if layer else "F.Cu"), a, b, width)
    for i, (net, xy) in enumerate(CELL_VIAS):
        add_via(board, f"led_cell_via_{i}", net, xy)
    print("LED cell: removed", removed, "items; moved", len(CELL), "footprints", flush=True)


def route_cell_signals(board):
    import route_complete as router
    for net in ("LED_IS", "LED_RT_MID"):
        routed, failed = router.complete(board, only={net}, attempts=1)
        print("Routed", routed, "failed", failed, flush=True)
        if failed:
            raise RuntimeError(f"Unrouted cell net: {net}")


C508_GND_GAP = 0.5  # mm minimum from the exposed C508.2 GND pad (up to ~55 V on LED_OUT)
# Thin F.Cu GND ties inside the ground pour above C508.1; the pour and the In1 plane already join
# R506.2, C505.2, C508.2 and the via at (69.5; 62.0). They block the direct feed and one of them
# ran under the capacitor body to C508.2.
C508_GND_TIES = [
    ((73.4625, 58.0), (70.7063, 55.2437)),
    ((70.9489, 60.0732), (73.0221, 58.0)),
    ((73.0221, 58.0), (73.4625, 58.0)),
    ((80.2183, 70.0), (70.9489, 60.7306)),
    ((70.9489, 60.7306), (70.9489, 60.0732)),
    ((70.95, 60.73), (69.5, 62.0)),
]
# 1 mm F.Cu feed: from C511.1 left above R506.2, down between the R506 pads and into C508.1
# from its own (upper) side, about 6 mm from the C508.2 GND pad.
C508_FEED = [((79.0, 56.3), (72.0, 56.3)), ((72.0, 56.3), (72.0, 66.0))]


def c508_feed(board):
    """Feed the 470 uF output capacitor with 1 mm F.Cu instead of the 0.25 mm trace alone."""
    removed = 0
    for item in list(board.GetTracks()):
        if isinstance(item, p.PCB_VIA) or item.GetNetname() != "GND" or item.GetLayer() != p.F_Cu:
            continue
        a, b = mm_xy(item.GetStart()), mm_xy(item.GetEnd())
        if any((same(a, x) and same(b, y)) or (same(a, y) and same(b, x)) for x, y in C508_GND_TIES):
            board.Delete(item)
            removed += 1
    if removed != len(C508_GND_TIES):
        raise RuntimeError(f"C508 feed: expected {len(C508_GND_TIES)} GND ties, found {removed}")
    tracks = [add_track(board, f"c508_feed_{i}", "LED_OUT", p.F_Cu, a, b, 1.0)
              for i, (a, b) in enumerate(C508_FEED)]
    gnd = next(q for f in board.GetFootprints() if f.GetReference() == "C508"
               for q in f.Pads() if q.GetNumber() == "2")
    box = gnd.GetBoundingBox()
    x0, y0, x1, y1 = (p.ToMM(v) for v in (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom()))
    gap = min(
        math.hypot(max(x0 - x, 0, x - x1), max(y0 - y, 0, y - y1)) - 0.5
        for a, b in C508_FEED for k in range(201)
        for x, y in [(a[0] + (b[0] - a[0]) * k / 200, a[1] + (b[1] - a[1]) * k / 200)]
    )
    if gap < C508_GND_GAP:
        raise RuntimeError(f"C508 feed only {gap:.2f} mm from the GND pad")
    # The feed passes on two sides of R506.2 (CSH set resistor ground): connect the pad
    # solidly to the pour and give it its own plane via instead of the removed ties.
    r506 = next(f for f in board.GetFootprints() if f.GetReference() == "R506")
    next(q for q in r506.Pads() if q.GetNumber() == "2").SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)
    add_via(board, "r506_gnd", "GND", (74.8, 58.0))
    print(f"C508 feed: 1.0 mm F.Cu, {gap:.2f} mm from C508.2; GND ties removed {removed}", flush=True)


def sync_fields(board):
    fps = {f.GetReference(): f for f in board.GetFootprints()}
    for (ref, key), value in FIELDS.items():
        fps[ref].GetField(key).SetText(value)


def repair(board):
    shunt_ground(board)
    led_cell(board)
    route_cell_signals(board)
    c508_feed(board)
    sync_fields(board)
    p.ZONE_FILLER(board).Fill(board.Zones())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", type=Path, default=HERE / "PCB_EU_LITE.kicad_pcb")
    ap.add_argument("--output", type=Path, help="write here instead of overwriting --board")
    args = ap.parse_args()
    out = args.output or args.board
    project = out.with_suffix(".kicad_pro")
    original = project.read_bytes() if project.exists() else None
    try:
        board = p.LoadBoard(str(args.board))
        repair(board)
        p.SaveBoard(str(out), board)
    finally:
        if original is not None:
            project.write_bytes(original)


if __name__ == "__main__":
    main()
