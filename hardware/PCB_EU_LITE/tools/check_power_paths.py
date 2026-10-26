"""Check outer-layer boost power paths, input via bank and paste clearance.

These geometric checks supplement KiCad DRC; they do not qualify temperature.
SPDX-License-Identifier: MIT
"""

import argparse
import json
import math
from pathlib import Path
import pcbnew as p
from shapely.geometry import Point
from shapely.ops import unary_union
from build_board import HERE, identity
from fabrication_checks import conductor, filled, bbox


def check(path):
    board = p.LoadBoard(str(path))
    pads = {
        (f.GetReference(), q.GetNumber()): q
        for f in board.GetFootprints()
        for q in f.Pads()
        if q.GetNumber()
    }
    tracks = list(board.GetTracks())

    def pos(q):
        return p.ToMM(q.GetPosition().x), p.ToMM(q.GetPosition().y)

    def copper(net, layer):
        pieces = [
            conductor(t) for t in tracks if t.GetNetname() == net and t.IsOnLayer(layer)
        ]
        pieces += [
            bbox(q)
            for f in board.GetFootprints()
            for q in f.Pads()
            if q.GetNetname() == net and q.IsOnLayer(layer)
        ]
        pieces += [
            filled(z)
            for z in board.Zones()
            if not z.GetIsRuleArea() and z.GetNetname() == net and z.IsOnLayer(layer)
        ]
        return unary_union(pieces).buffer(0.00001)

    def component(shape, anchor):
        matches = [
            g for g in getattr(shape, "geoms", [shape]) if g.covers(Point(*pos(anchor)))
        ]
        if len(matches) != 1:
            raise RuntimeError("Power anchor is not in a unique copper island")
        return matches[0]

    evidence = {}
    for net, start, ends in (
        ("+24V_LED", ("F103", "2"), (("L501", "1"), ("C501", "1"))),
        ("LED_SW", ("L501", "2"), (("Q501", "5"), ("D501", "2"), ("R507", "1"))),
    ):
        island = component(copper(net, p.F_Cu), pads[start])
        for end in ends:
            if not island.covers(Point(*pos(pads[end]))):
                raise RuntimeError(f"{net}: no continuous F.Cu path to {end}")
        evidence[net] = {
            "power_path_layer": "F.Cu",
            "from": ".".join(start),
            "to": [".".join(e) for e in ends],
        }
    for net in ("LED_SW", "LED_OUT", "LED_POS"):
        inner = [
            t
            for t in tracks
            if t.GetNetname() == net
            and not isinstance(t, p.PCB_VIA)
            and t.GetLayer() in (p.In1_Cu, p.In2_Cu)
        ]
        zones = [
            z
            for z in board.Zones()
            if not z.GetIsRuleArea()
            and z.GetNetname() == net
            and z.GetLayer() in (p.In1_Cu, p.In2_Cu)
        ]
        if inner or zones:
            raise RuntimeError(f"{net}: inner-layer power copper remains")
    marker = p.PCB_VIA(board)
    bank = []
    for i in range(10):
        identity(marker, f"repair/raw_array_{i}")
        bank += [
            t
            for t in tracks
            if isinstance(t, p.PCB_VIA)
            and t.m_Uuid.AsString() == marker.m_Uuid.AsString()
        ]
    if not 8 <= len(bank) <= 10:
        raise RuntimeError(f"Input via bank: {len(bank)} vias")
    for v in bank:
        if (
            abs(p.ToMM(v.GetWidth(p.F_Cu)) - 0.8) > 0.0001
            or abs(p.ToMM(v.GetDrillValue()) - 0.4) > 0.0001
        ):
            raise RuntimeError("Input via bank geometry differs")
    spacing = min(
        math.dist(pos(a), pos(b)) for i, a in enumerate(bank) for b in bank[i + 1 :]
    )
    if spacing < 1.0999:
        raise RuntimeError("Input via bank pitch below 1.1 mm")
    for layer, key in ((p.F_Cu, ("F101", "1")), (p.B_Cu, ("J101", "1"))):
        island = component(copper("+24V_RAW", layer), pads[key])
        for v in bank:
            if not island.covers(Point(*pos(v))):
                raise RuntimeError(
                    f"Input via {pos(v)} does not reach {key} on {board.GetLayerName(layer)}"
                )
    overlaps = []
    for via in (t for t in tracks if isinstance(t, p.PCB_VIA)):
        shape = p.SHAPE_CIRCLE(via.GetPosition(), via.GetWidth(p.F_Cu) // 2)
        for f in board.GetFootprints():
            for q in f.Pads():
                for layer in (p.F_Paste, p.B_Paste):
                    if q.IsOnLayer(layer) and q.GetEffectiveShape(layer).Collide(
                        shape, 0
                    ):
                        overlaps.append(
                            {
                                "via": via.m_Uuid.AsString(),
                                "position_mm": pos(via),
                                "footprint": f.GetReference(),
                                "pad": q.GetNumber(),
                                "layer": board.GetLayerName(layer),
                            }
                        )
    if overlaps:
        raise RuntimeError("Via barrels overlap paste: " + json.dumps(overlaps))
    esp_vias = []
    for i in range(7):
        identity(marker, f"repair/esp_ground_{i}")
        esp_vias += [
            t
            for t in tracks
            if isinstance(t, p.PCB_VIA)
            and t.m_Uuid.AsString() == marker.m_Uuid.AsString()
        ]
    if len(esp_vias) < 4:
        raise RuntimeError("Insufficient local ESP32 ground stitching")
    small = [
        t
        for t in tracks
        if isinstance(t, p.PCB_VIA)
        and p.ToMM(t.GetWidth(p.F_Cu) - t.GetDrillValue()) / 2 < 0.14999
    ]
    return dict(
        kicad_version=p.Version(),
        power_paths=evidence,
        inner_power_copper={"LED_SW": 0, "LED_OUT": 0, "LED_POS": 0},
        raw_vias=len(bank),
        raw_via_size_drill_mm=[0.8, 0.4],
        raw_via_min_pitch_mm=round(spacing, 4),
        all_raw_vias_connected_to_fuse_on_F_Cu_and_input_on_B_Cu=True,
        paste_overlaps=0,
        esp_ground_vias=len(esp_vias),
        remaining_small_annulus_vias=len(small),
        thermal_qualified=False,
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", type=Path, default=HERE / "PCB_EU_LITE.kicad_pcb")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    result = check(args.board)
    if args.output:
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
