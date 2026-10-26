"""Keep EU-LITE load current on outer copper and vias outside solder paste.

Run after finalize_board.py. Reworks existing routing, not schematic or placement.
SPDX-License-Identifier: MIT
"""

import argparse
from pathlib import Path
import pcbnew as p
from build_board import HERE, identity
from finalize_board import add_track, add_via, xy, point
from route_power import add_zone, route_corridor
import route_complete as router


def route(board, net, a, b, width, name, layers=(p.F_Cu,), terminal_layers=None):
    ax, ay = xy(a.GetPosition())
    bx, by = xy(b.GetPosition())
    print("Routing", name, (ax, ay), "to", (bx, by), flush=True)
    old_layers, old_via = router.LAYERS, router.VIA
    router.LAYERS, router.VIA = list(layers), (
        (0.6, 0.3) if width <= 0.5 else (0.8, 0.4)
    )
    try:
        grid = router.Grid(
            board,
            board.FindNet(net).GetNetCode(),
            (min(ax, bx) - 10, min(ay, by) - 10, max(ax, bx) + 10, max(ay, by) + 10),
            width,
            preserve_escapes=False,
        )
        if len(layers) == 1:
            grid.via[:] = b"\x01" * len(grid.via)
        si, sj = grid.cell(ax, ay)
        gi, gj = grid.cell(bx, by)

        def lays(pad):
            if terminal_layers is not None:
                return (terminal_layers[0 if pad is a else 1],)
            return (
                layers
                if isinstance(pad, p.PCB_VIA) or pad.GetAttribute() == p.PAD_ATTRIB_PTH
                else (p.B_Cu if pad.GetParentFootprint().IsFlipped() else p.F_Cu,)
            )

        for q in (a, b):
            for lay in lays(q):
                grid.open_pad(q, lay)
        path = router.search(
            grid, [(l, si, sj) for l in lays(a)], [(l, gi, gj) for l in lays(b)]
        )
        if path is None:
            raise RuntimeError(f"No outer copper path: {name}, {width} mm")
        result = router.emit(
            board, board.FindNet(net), grid, path, a, b, width, "repair/" + name
        )
        print(name, "segments", len(result), flush=True)
        return result
    finally:
        router.LAYERS, router.VIA = old_layers, old_via


def repair(board):
    pads = {
        (f.GetReference(), q.GetNumber()): q
        for f in board.GetFootprints()
        for q in f.Pads()
        if q.GetNumber()
    }
    # Free the top-side inductor feed from three low-current crossings.
    crossings = [
        ("+3V3", (87.3038, 20.6962), (42.2837, 20.6962), 0.5),
        ("SHT_SDA", (83.1147, 19.8853), (56.3558, 19.8853), 0.2),
        ("FAN1_PWM", (22.1356, 29.2626), (63.0909, 29.2626), 0.2),
    ]
    generated = set()
    marker = p.PCB_TRACK(board)
    for i in range(3):
        for side in ("a", "c"):
            for j in range(200):
                identity(marker, f"repair/crossing_top_{i}_{side}/track/{j}")
                generated.add(marker.m_Uuid.AsString())
        for side in ("a", "c"):
            identity(marker, f"finalize/repair_crossing_top_{i}_{side}")
            generated.add(marker.m_Uuid.AsString())
        for j in range(2):
            identity(marker, f"finalize/repair_crossing_{i}_{j}")
            generated.add(marker.m_Uuid.AsString())
        for kind in ("track", "via"):
            for j in range(200):
                identity(marker, f"repair/crossing_{i}/{kind}/{j}")
                generated.add(marker.m_Uuid.AsString())
    for t in list(board.GetTracks()):
        if t.m_Uuid.AsString() in generated:
            board.Delete(t)
            continue
        if isinstance(t, p.PCB_VIA):
            continue
        if any(
            t.GetNetname() == net
            and t.GetLayer() == p.F_Cu
            and {xy(t.GetStart()), xy(t.GetEnd())} == {a, c}
            for net, a, c, w in crossings
        ):
            board.Delete(t)
    # These ground jumpers crossed the boost power path; the outer pours
    # and local stitching vias provide the ground connection instead.
    for t in list(board.GetTracks()):
        if (
            isinstance(t, p.PCB_VIA)
            or t.GetLayer() != p.F_Cu
            or t.GetNetname() != "GND"
        ):
            continue
        a, c = xy(t.GetStart()), xy(t.GetEnd())
        if (
            min(a[0], c[0]) > 62
            and max(a[0], c[0]) < 80
            and min(a[1], c[1]) > 33
            and max(a[1], c[1]) < 38
        ):
            board.Delete(t)
    # Replace the old trunks and reconnect the controller/bypass taps separately.
    for t in list(board.GetTracks()):
        if t.GetNetname() in {"LED_SW", "LED_POS", "LED_OUT", "+24V_LED"}:
            board.Delete(t)
    for z in list(board.Zones()):
        if not z.GetIsRuleArea() and z.GetNetname() in {
            "LED_SW",
            "LED_POS",
            "LED_OUT",
            "+24V_LED",
        }:
            board.Delete(z)
    add_track(
        board,
        "repair_d501_cathodes",
        "LED_SW",
        p.F_Cu,
        (66.58, 41.356),
        (68.42, 41.356),
        1.2,
    )
    # A thick F.Cu route supplies the input capacitor and inductor. The
    # controller and bypass taps carry only their local operating current.
    for net, a, b, w, name, layers in (
        (
            "+24V_LED",
            ("F103", "2"),
            ("L501", "1"),
            2.0,
            "led_input_inductor",
            (p.F_Cu,),
        ),
        ("+24V_LED", ("F103", "2"), ("C501", "1"), 2.0, "led_input_cap", (p.F_Cu,)),
        ("LED_OUT", ("D501", "1"), ("R502", "1"), 0.8, "led_diode_shunt", (p.F_Cu,)),
        (
            "LED_POS",
            ("R502", "2"),
            ("J501", "1"),
            1.2,
            "led_positive",
            (p.F_Cu, p.B_Cu),
        ),
        ("LED_SW", ("L501", "2"), ("Q501", "5"), 2.0, "led_switch_fet", (p.F_Cu,)),
        ("LED_SW", ("L501", "2"), ("D501", "2"), 1.2, "led_switch_diode", (p.F_Cu,)),
        ("LED_SW", ("D501", "2"), ("R507", "1"), 0.25, "led_snubber", (p.F_Cu,)),
        (
            "+24V_LED",
            ("C501", "1"),
            ("C502", "1"),
            0.8,
            "led_input_cap2",
            (p.F_Cu, p.B_Cu),
        ),
    ):
        route(board, net, pads[a], pads[b], w, name, layers)
    for i, (net, original_a, original_c, width) in enumerate(crossings):
        left, right = sorted((original_a, original_c))
        a = (max(left[0], 56.5), left[1])
        c = (min(right[0], 62.0), right[1])
        if net == "SHT_SDA":
            a = left
        # A through via must clear copper on every layer, including B.Cu
        # when only a top-side signal is being moved to In2.Cu.
        selected = []
        for preferred in (a, c):
            existing = next(
                (
                    t
                    for t in board.GetTracks()
                    if isinstance(t, p.PCB_VIA)
                    and t.GetNetname() == net
                    and xy(t.GetPosition()) == preferred
                ),
                None,
            )
            if existing is not None:
                selected.append(preferred)
                continue
            options = [
                (round(preferred[0] + dx * 0.1, 4), round(preferred[1] + dy * 0.1, 4))
                for dx in range(-15, 16)
                for dy in range(-15, 16)
            ]
            options.sort(
                key=lambda q: (q[0] - preferred[0]) ** 2 + (q[1] - preferred[1]) ** 2
            )
            selected.append(next(q for q in options if free_via(board, net, q)))
        a, c = selected
        for side, start, end in (("a", left, a), ("c", c, right)):
            if start == end:
                continue
            anchors = []
            for pos in (start, end):
                q = p.PCB_VIA(board)
                q.SetPosition(point(*pos))
                q.SetNet(board.FindNet(net))
                anchors.append(q)
            route(board, net, *anchors, width, f"crossing_top_{i}_{side}", (p.F_Cu,))
        ends = []
        for j, pos in enumerate((a, c)):
            via = next(
                (
                    t
                    for t in board.GetTracks()
                    if isinstance(t, p.PCB_VIA)
                    and t.GetNetname() == net
                    and xy(t.GetPosition()) == pos
                ),
                None,
            )
            if via is None:
                add_via(board, f"repair_crossing_{i}_{j}", net, pos)
                via = next(
                    t
                    for t in board.GetTracks()
                    if isinstance(t, p.PCB_VIA)
                    and t.GetNetname() == net
                    and xy(t.GetPosition()) == pos
                )
            ends.append(via)
        route(board, net, *ends, width, f"crossing_{i}", (p.In2_Cu,))
    board.BuildConnectivity()
    # Output DC stays on external layers. The controller and its bypass may
    # use In2.Cu; their 0.25 mm branches never feed the inductor or load.
    for net in ("+24V_LED", "LED_OUT", "LED_POS"):
        for i in range(30):
            groups = router.components(board, board.FindNet(net).GetNetCode())
            if len(groups) <= 1:
                break
            anchor = pads[
                (
                    ("C501", "1")
                    if net == "+24V_LED"
                    else ("D501", "1") if net == "LED_OUT" else ("R502", "2")
                )
            ]
            groups.sort(
                key=lambda g: (
                    0
                    if any(q.m_Uuid.AsString() == anchor.m_Uuid.AsString() for q in g)
                    else 1
                )
            )
            sources = (
                [anchor]
                if net == "+24V_LED"
                else [
                    q
                    for q in groups[0]
                    if q.GetParentFootprint().GetReference()
                    not in {"R503", "R513", "C515"}
                ]
            )
            pairs = [(a, b) for a in sources for g in groups[1:] for b in g]
            a, b = min(
                pairs,
                key=lambda ab: (
                    ab[0].GetPosition() - ab[1].GetPosition()
                ).EuclideanNorm(),
            )
            width = (
                0.25
                if net == "+24V_LED"
                or any(
                    q.GetParentFootprint().GetReference() in {"R503", "R513", "C515"}
                    for q in (a, b)
                )
                else 0.8
            )
            if any(
                q.GetParentFootprint().GetReference().startswith("R")
                and q.GetParentFootprint().GetReference() != "R502"
                for q in (a, b)
            ):
                width = 0.25
                if net in {"LED_OUT", "LED_POS"}:
                    # Pick up the current-sense voltage at the shunt terminal,
                    # rather than at a load connector or output capacitor.
                    a = pads["R502", "1" if net == "LED_OUT" else "2"]
            if any(q.GetParentFootprint().GetReference() == "C511" for q in (a, b)):
                width = 0.8
            route(
                board,
                net,
                a,
                b,
                width,
                f"{net}_tap_{i}",
                (p.F_Cu, p.In2_Cu, p.B_Cu) if net == "+24V_LED" else (p.F_Cu, p.B_Cu),
            )
            board.BuildConnectivity()
        else:
            raise RuntimeError(f"Cannot join {net}")
    repair_vias(board)
    stitch(board, "GND", "c515_ground", (76, 34))
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


def free_via(board, net, pos, diameter=0.6, drill=0.3, ignore=None):
    center = point(*pos)
    shape = p.SHAPE_CIRCLE(center, p.FromMM(diameter / 2))
    for f in board.GetFootprints():
        for q in f.Pads():
            for lay in (p.F_Paste, p.B_Paste):
                if q.IsOnLayer(lay) and q.GetEffectiveShape(lay).Collide(
                    shape, p.FromMM(0.15)
                ):
                    return False
            for lay in (p.F_Cu, p.In1_Cu, p.In2_Cu, p.B_Cu):
                if not q.IsOnLayer(lay):
                    continue
                paste = q.IsOnLayer(p.F_Paste) or q.IsOnLayer(p.B_Paste)
                if q.GetNetname() != net or paste:
                    if q.GetEffectiveShape(lay).Collide(
                        shape, p.FromMM(0.2 if q.GetNetname() != net else 0.15)
                    ):
                        return False
            if q.HasHole():
                if (q.GetPosition() - center).EuclideanNorm() < p.FromMM(
                    max(p.ToMM(q.GetDrillSize().x), p.ToMM(q.GetDrillSize().y)) / 2
                    + drill / 2
                    + 0.3
                ):
                    return False
    for t in board.GetTracks():
        if t is ignore or (
            ignore is not None and t.m_Uuid.AsString() == ignore.m_Uuid.AsString()
        ):
            continue
        if isinstance(t, p.PCB_VIA):
            if (t.GetPosition() - center).EuclideanNorm() < p.FromMM(
                p.ToMM(t.GetDrillValue()) / 2 + drill / 2 + 0.3
            ):
                return False
            for lay in (p.F_Cu, p.In1_Cu, p.In2_Cu, p.B_Cu):
                if t.GetNetname() != net and t.GetEffectiveShape(lay).Collide(
                    shape, p.FromMM(0.2)
                ):
                    return False
        elif t.GetNetname() != net and t.GetEffectiveShape().Collide(
            shape, p.FromMM(0.2)
        ):
            return False
    for z in [*board.Zones(), *(z for f in board.GetFootprints() for z in f.Zones())]:
        if (
            z.GetIsRuleArea()
            and z.GetDoNotAllowVias()
            and z.Outline().Collide(center, p.FromMM(diameter / 2 + 0.1))
        ):
            return False
    return True


def stitch(board, net, name, pos, diameter=0.6, drill=0.3):
    via = p.PCB_VIA(board)
    identity(via, "repair/" + name)
    for t in list(board.GetTracks()):
        if t.m_Uuid.AsString() == via.m_Uuid.AsString():
            board.Delete(t)
    if not free_via(board, net, pos, diameter, drill):
        raise RuntimeError(f"Blocked stitching via {name}: {pos}")
    via.SetPosition(point(*pos))
    via.SetWidth(p.FromMM(diameter))
    via.SetDrill(p.FromMM(drill))
    via.SetLayerPair(p.F_Cu, p.B_Cu)
    via.SetViaType(p.VIATYPE_THROUGH)
    via.SetNet(board.FindNet(net))
    board.Add(via)
    return via


def relocate_via(board, via):
    old = xy(via.GetPosition())
    net = via.GetNetname()
    diameter = p.ToMM(via.GetWidth(p.F_Cu))
    drill = p.ToMM(via.GetDrillValue())
    candidates = [
        (round(old[0] + i * 0.1, 4), round(old[1] + j * 0.1, 4))
        for i in range(-30, 31)
        for j in range(-30, 31)
    ]
    candidates.sort(key=lambda q: (q[0] - old[0]) ** 2 + (q[1] - old[1]) ** 2)
    for new in candidates:
        if not free_via(board, net, new, diameter, drill, via):
            continue
        if new == old:
            print("Via already outside paste", net, old, flush=True)
            return
        connected = []
        for t in list(board.GetTracks()):
            if isinstance(t, p.PCB_VIA) or t.GetNetname() != net:
                continue
            a, c = xy(t.GetStart()), xy(t.GetEnd())
            reach = (diameter + p.ToMM(t.GetWidth())) / 2 + 0.001
            for side, pos in (("start", a), ("end", c)):
                if (pos[0] - old[0]) ** 2 + (pos[1] - old[1]) ** 2 < reach**2:
                    connected.append((t, side))
        via.SetPosition(point(*new))
        removed = set()
        for t, side in connected:
            if net == "GND":
                # A short escape can have both endpoints inside the old via.
                if id(t) not in removed:
                    removed.add(id(t))
                    board.Delete(t)
            else:
                (t.SetStart if side == "start" else t.SetEnd)(point(*new))
        print("Moved via", net, old, "->", new, flush=True)
        return
    raise RuntimeError(f"Cannot move paste via {net} at {old}")


RAW_OBSTACLES = [
    [
        "169c1405-0f38-4ac0-9873-74e5bb9eae29",
        "+24V_FUSED",
        "B.Cu",
        [25.1439, 52.0695],
        [25.5683, 52.0695],
        1.2,
    ],
    [
        "890f0cb8-8db1-409d-9dd6-cd143739af82",
        "+24V_FUSED",
        "B.Cu",
        [25.1439, 52.0695],
        [25.1439, 51.7584],
        1.2,
    ],
    [
        "a04546b6-59b8-425e-a5c7-eb6df1efde64",
        "+24V_FUSED",
        "B.Cu",
        [25.5683, 52.0695],
        [27.4988, 54.0],
        1.2,
    ],
    [
        "b566cd86-e3e6-44dd-a635-5a891886cf9c",
        "+24V_FUSED",
        "B.Cu",
        [25.1439, 51.7584],
        [22.2375, 48.852],
        1.2,
    ],
    [
        "ba392bbc-004c-42e6-82dc-ca28599be2d7",
        "+24V_FUSED",
        "B.Cu",
        [15.3136, 61.8998],
        [25.1439, 52.0695],
        1.2,
    ],
    [
        "1b292ea2-ed0f-466a-9680-669ca2e591d5",
        "FLOAT_GPIO",
        "In2.Cu",
        [27.2739, 51.8228],
        [25.9978, 53.0989],
        0.2,
    ],
    [
        "7abcad3f-870a-4456-8439-4c77d0bef444",
        "FLOAT_GPIO",
        "In2.Cu",
        [25.9978, 53.0989],
        [25.9978, 68.2317],
        0.2,
    ],
    [
        "b8614948-d26f-4e74-8ab1-308dfb2e5293",
        "LED_R_GPIO",
        "In2.Cu",
        [24.433, 73.8942],
        [24.433, 48.9527],
        0.2,
    ],
    [
        "995c9858-2a1f-4039-bf83-70308d0d2886",
        "LED_G_GPIO",
        "In2.Cu",
        [25.5346, 77.0529],
        [25.5346, 48.4212],
        0.2,
    ],
]


def repair_vias(board):
    obstacle_ids = {spec[0] for spec in RAW_OBSTACLES}
    marker = p.PCB_TRACK(board)
    for name in [
        *(f"raw_array_{i}" for i in range(10)),
        *(f"u501_ground_{i}" for i in range(6)),
        *(f"esp_ground_{i}" for i in range(7)),
    ]:
        identity(marker, "repair/" + name)
        obstacle_ids.add(marker.m_Uuid.AsString())
    for i in range(len(RAW_OBSTACLES)):
        for kind in ("track", "via"):
            for j in range(200):
                identity(marker, f"repair/raw_clearance_{i}/{kind}/{j}")
                obstacle_ids.add(marker.m_Uuid.AsString())
    for t in list(board.GetTracks()):
        if t.m_Uuid.AsString() in obstacle_ids | {
            "0b9038f1-ca0f-4583-a9fa-7ae370f4a041"
        }:
            board.Delete(t)

    # Remove the four open barrels in the LM3429 paste window.
    for t in list(board.GetTracks()):
        if (
            isinstance(t, p.PCB_VIA)
            and t.GetNetname() == "GND"
            and xy(t.GetPosition())
            in {(69.2, 49.2), (70.8, 49.2), (69.2, 50.8), (70.8, 50.8)}
        ):
            board.Delete(t)
    for t in list(board.GetTracks()):
        if isinstance(t, p.PCB_VIA) and xy(t.GetPosition()) in {
            (26.65, 67.95),
            (25.984, 80.95),
        }:
            relocate_via(board, t)
    for i, pos in enumerate(
        ((68.9, 46.7), (70, 46.7), (71.1, 46.7), (69, 53.2), (70.1, 53.2), (71.2, 53.2))
    ):
        if free_via(board, "GND", pos):
            stitch(board, "GND", f"u501_ground_{i}", pos)
    for i, pos in enumerate(
        (
            (18.3, 8.7),
            (39, 8.7),
            (22.8, 29.9),
            (29.7, 28.3),
            (32.7, 28.3),
            (18.5, 23.6),
            (39.6, 23.9),
        )
    ):
        if free_via(board, "GND", pos):
            stitch(board, "GND", f"esp_ground_{i}", pos)
    pads = {
        (f.GetReference(), q.GetNumber()): q
        for f in board.GetFootprints()
        for q in f.Pads()
    }
    pads["U501", "5"].SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)
    add_track(
        board,
        "repair_u101_ground_pad6",
        "GND",
        p.F_Cu,
        (26.475, 67.935),
        (24, 67.3),
        0.35,
    )
    # Leave the Kelvin keepouts untouched. Strengthen the actual outer return.
    for net, name, points, half in (
        ("PSU_RTN", "repair_return", [(6, 51), (7, 50.46), (12.8, 50.46)], 2.5),
        (
            "+24V_RAW",
            "repair_raw_top",
            [(23.5, 50.3), (23.5, 52.1), (24.4, 53), (24.4, 56.2), (20, 56.2)],
            1.6,
        ),
        (
            "+24V_RAW",
            "repair_raw_bottom",
            [(20, 54.75), (23.5, 52.1), (24.4, 53), (24.4, 56.2), (20, 56.2)],
            1.6,
        ),
    ):
        layer = "B.Cu" if name.endswith("bottom") else "F.Cu"
        for z in list(board.Zones()):
            if z.GetZoneName() == "lite_power/" + name:
                board.Delete(z)
        z = add_zone(board, net, layer, name, route_corridor(points, half), 6)
        z.SetPadConnection(p.ZONE_CONNECTION_FULL)
    for t in list(board.GetTracks()):
        if t.m_Uuid.AsString() == "0cc98e7d-39fe-425d-90a6-49dcc6dc3a83":
            board.Delete(t)
    # A 1.1 mm lattice adjacent to the fuse; select only collision-free sites.
    raw = []
    for y in (52.1, 53.2, 54.3, 55.4, 56.5):
        for x in (22.4, 23.5, 24.6, 25.7):
            if free_via(board, "+24V_RAW", (x, y), 0.8, 0.4):
                raw.append(
                    stitch(board, "+24V_RAW", f"raw_array_{len(raw)}", (x, y), 0.8, 0.4)
                )
                if len(raw) == 10:
                    break
        if len(raw) == 10:
            break
    if len(raw) < 8:
        raise RuntimeError(f"Only {len(raw)} input array vias fit")
    moved_nodes = {}
    for layer, net, nodes, width in (
        (
            "B.Cu",
            "+24V_FUSED",
            ((25.1439, 52.0695), (25.1439, 51.7584), (25.5683, 52.0695)),
            1.2,
        ),
        ("In2.Cu", "FLOAT_GPIO", ((25.9978, 53.0989),), 0.2),
    ):
        for old in nodes:
            candidates = [
                (round(old[0] + i * 0.1, 4), round(old[1] + j * 0.1, 4))
                for i in range(-40, 41)
                for j in range(-40, 41)
            ]
            candidates.sort(key=lambda q: (q[0] - old[0]) ** 2 + (q[1] - old[1]) ** 2)
            for pos in candidates:
                shape = p.SHAPE_CIRCLE(point(*pos), p.FromMM(width / 2 + 0.03))
                lay = board.GetLayerID(layer)
                items = [
                    t
                    for t in board.GetTracks()
                    if t.GetNetname() != net and t.IsOnLayer(lay)
                ]
                items += [
                    q
                    for f in board.GetFootprints()
                    for q in f.Pads()
                    if q.IsOnLayer(lay) and q.GetNetname() != net
                ]
                if all(
                    not item.GetEffectiveShape(lay).Collide(shape, p.FromMM(0.21))
                    for item in items
                ):
                    moved_nodes[old] = pos
                    break
            else:
                raise RuntimeError(f"No clearance for displaced junction {old}")
    print("Input-area junctions", moved_nodes, flush=True)
    for i, (uid, net, layer, a, c, width) in sorted(
        enumerate(RAW_OBSTACLES),
        key=lambda item: {
            "FLOAT_GPIO": 0,
            "LED_G_GPIO": 1,
            "LED_R_GPIO": 2,
            "+24V_FUSED": 3,
        }[item[1][1]],
    ):
        ends = []
        for pos in (a, c):
            pos = moved_nodes.get(tuple(pos), pos)
            anchor = p.PCB_VIA(board)
            anchor.SetNet(board.FindNet(net))
            anchor.SetPosition(point(*pos))
            ends.append(anchor)
        route(
            board,
            net,
            *ends,
            width,
            f"raw_clearance_{i}",
            (p.F_Cu, p.B_Cu) if net == "+24V_FUSED" else (p.F_Cu, p.In2_Cu, p.B_Cu),
            terminal_layers=(board.GetLayerID(layer),) * 2,
        )
    # Existing small rings get a 0.15 mm radial annulus where clearance permits.
    changed = 0
    for t in board.GetTracks():
        if isinstance(t, p.PCB_VIA) and p.ToMM(t.GetWidth(p.F_Cu)) < 0.599:
            if free_via(
                board,
                t.GetNetname(),
                xy(t.GetPosition()),
                0.6,
                p.ToMM(t.GetDrillValue()),
                t,
            ):
                t.SetWidth(p.FromMM(0.6))
                changed += 1
    print("Input array", len(raw), "vias; enlarged rings", changed, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", type=Path, default=HERE / "PCB_EU_LITE.kicad_pcb")
    args = ap.parse_args()
    project = args.board.with_suffix(".kicad_pro")
    original = project.read_bytes() if project.exists() else None
    try:
        b = p.LoadBoard(str(args.board))
        repair(b)
        p.SaveBoard(str(args.board), b)
    finally:
        if original is not None:
            project.write_bytes(original)


if __name__ == "__main__":
    main()
