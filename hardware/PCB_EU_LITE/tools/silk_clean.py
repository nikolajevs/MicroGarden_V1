#!/usr/bin/env python3
"""Keep the silkscreen of library footprints off pads and holes, with strokes the factory can print.

JLCPCB (JLCDFM): silkscreen at least 0.15 mm from every pad and hole, lines at least 0.15 mm wide.
Library outlines (round electrolytics, SOT/TO bodies, connector frames) touch or overlap pads of the
placed board, so the board's own copy of those lines is cut where it comes closer than SILK_GAP to a
pad, a drill or a via, and strokes (lines and text) thinner than SILK_LINE_MIN are widened and texts lower than SILK_TEXT_MIN are enlarged. The library files and every
other item of the footprints stay as they are. Cut pieces shorter than SILK_PIECE_MIN are dropped;
polarity marks (filled shapes) are never cut or removed; a pin-number text on a pad moves by up to 1 mm. Only used by silkscreen.py, which splices the
changed footprints into the board text; running it again changes nothing.

SPDX-License-Identifier: MIT
"""
import math

import pcbnew as p

from build_board import identity

FMM = p.FromMM
SILK = (p.F_SilkS, p.B_SilkS)
SILK_GAP = 0.15         # silkscreen to pad and to hole, mm
SILK_LINE_MIN = 0.15    # JLCPCB minimum line width, mm
SILK_TEXT_MIN = 1.0     # JLCPCB minimum character height, mm
SILK_PIECE_MIN = 0.3    # cut pieces shorter than this are dropped, mm
SAMPLE = 0.02           # step along a stroke, mm
graveyard = []          # removed items stay referenced, or pcbnew frees them under the footprint


def blockers(board):
    """Per silkscreen layer: (shape, bounding box) of pad copper, drills and vias to keep SILK_GAP away from."""
    out = {p.F_SilkS: [], p.B_SilkS: []}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            for silk, cu in ((p.F_SilkS, p.F_Cu), (p.B_SilkS, p.B_Cu)):
                if pad.IsOnLayer(cu):
                    out[silk].append((pad.GetEffectiveShape(cu), pad.GetBoundingBox()))
            if pad.GetDrillSizeX() > 0:
                for silk in out:
                    out[silk].append((pad.GetEffectiveHoleShape(), pad.GetBoundingBox()))
    for t in board.GetTracks():
        if isinstance(t, p.PCB_VIA):
            for silk in out:
                out[silk].append((p.SHAPE_CIRCLE(t.GetPosition(), t.GetDrill() // 2), t.GetBoundingBox()))
    return out


def blocked_at(items, x, y, half_width):
    reach = FMM(SILK_GAP + half_width + 0.02)
    probe = p.SHAPE_CIRCLE(p.VECTOR2I(int(x), int(y)), FMM(half_width))
    for shape, bb in items:
        if x < bb.GetLeft() - reach or x > bb.GetRight() + reach or y < bb.GetTop() - reach or y > bb.GetBottom() + reach:
            continue
        if p.SHAPE.Collide(shape, probe, FMM(SILK_GAP)):
            return True
    return False


def stroke_points(g):
    """Centre line of a silkscreen shape as [(x, y, is_vertex)] in nm, closed shapes with the first point repeated; None otherwise."""
    kind = g.GetShape()
    s, e = g.GetStart(), g.GetEnd()
    if kind == p.SHAPE_T_SEGMENT:
        return [(s.x, s.y, True), (e.x, e.y, True)]
    if kind == p.SHAPE_T_RECT:
        corners = [(s.x, s.y), (e.x, s.y), (e.x, e.y), (s.x, e.y), (s.x, s.y)]
        return [(x, y, True) for x, y in corners]
    if kind == p.SHAPE_T_CIRCLE:
        r = math.hypot(e.x - s.x, e.y - s.y)
        return [(s.x + r * math.cos(2 * math.pi * k / 72), s.y + r * math.sin(2 * math.pi * k / 72), False) for k in range(73)]
    if kind == p.SHAPE_T_ARC:
        c, m = g.GetCenter(), g.GetArcMid()
        r = math.hypot(s.x - c.x, s.y - c.y)
        a0 = math.atan2(s.y - c.y, s.x - c.x)
        am = math.atan2(m.y - c.y, m.x - c.x)
        a1 = math.atan2(e.y - c.y, e.x - c.x)
        sweep = (a1 - a0) % (2 * math.pi)
        if ((am - a0) % (2 * math.pi)) > sweep:
            sweep -= 2 * math.pi
        n = max(2, int(abs(sweep) * r / FMM(0.1)))
        return [(c.x + r * math.cos(a0 + sweep * k / n), c.y + r * math.sin(a0 + sweep * k / n), False) for k in range(n + 1)]
    return None


def densify(points):
    out = []
    for (x0, y0, v0), (x1, y1, _) in zip(points, points[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / FMM(SAMPLE)))
        for k in range(n):
            out.append((x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n, v0 if k == 0 else False))
    out.append(points[-1])
    return out


def new_line(owner, name, layer, width, a, b):
    s = p.PCB_SHAPE(owner)
    s.SetShape(p.SHAPE_T_SEGMENT)
    s.SetStart(p.VECTOR2I(int(a[0]), int(a[1])))
    s.SetEnd(p.VECTOR2I(int(b[0]), int(b[1])))
    s.SetWidth(width)
    s.SetLayer(layer)
    identity(s, name)
    owner.Add(s)


def clean_board(board):
    """Widen and cut the silkscreen shapes of the footprints in place; returns {reference: [pieces kept, shapes cut]}."""
    items = blockers(board)
    changed = {}
    for fp in board.GetFootprints():
        shapes = [g for g in fp.GraphicalItems() if g.GetLayer() in SILK and isinstance(g, p.PCB_SHAPE)]
        area = fp.GetBoundingBox(False)
        area.Inflate(FMM(1))
        near = {side: [(shape, bb) for shape, bb in items[side] if area.Intersects(bb)] for side in items}
        for g in shapes:
            if 0 < g.GetWidth() < FMM(SILK_LINE_MIN) - 1:
                g.SetWidth(FMM(SILK_LINE_MIN))
                changed.setdefault(fp.GetReference(), [0, 0])
        for g in fp.GraphicalItems():
            if g.GetLayer() in SILK and isinstance(g, p.PCB_TEXT) and g.IsVisible():
                if g.GetTextThickness() < FMM(SILK_LINE_MIN) - 1:
                    g.SetTextThickness(FMM(SILK_LINE_MIN))
                    changed.setdefault(fp.GetReference(), [0, 0])
                if g.GetTextHeight() < FMM(SILK_TEXT_MIN) - 1:
                    g.SetTextSize(p.VECTOR2I(FMM(SILK_TEXT_MIN), FMM(SILK_TEXT_MIN)))
                    changed.setdefault(fp.GetReference(), [0, 0])
        for g in shapes:
            side, half = g.GetLayer(), p.ToMM(g.GetWidth()) / 2
            pts = stroke_points(g)
            if pts is None:                      # filled polygons are polarity marks: left alone
                continue
            dense = densify(pts)
            flags = [blocked_at(near[side], x, y, half) for x, y, _ in dense]
            if not any(flags):
                continue
            width, keep_all = g.GetWidth(), g.GetShape() in (p.SHAPE_T_ARC, p.SHAPE_T_CIRCLE)
            uuid_text = g.m_Uuid.AsString()
            fp.Remove(g)
            graveyard.append(g)
            entry = changed.setdefault(fp.GetReference(), [0, 0])
            entry[1] += 1
            run = []
            for q, bad in list(zip(dense, flags)) + [(None, True)]:
                if not bad:
                    run.append(q)
                    continue
                if len(run) > 1:
                    length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(run, run[1:]))
                    if length >= FMM(SILK_PIECE_MIN):
                        pick = [run[0]]
                        for q2 in run[1:-1]:
                            if (keep_all and math.hypot(q2[0] - pick[-1][0], q2[1] - pick[-1][1]) >= FMM(0.1)) or (not keep_all and q2[2]):
                                pick.append(q2)
                        pick.append(run[-1])
                        for a, b in zip(pick, pick[1:]):
                            new_line(fp, f'{fp.GetReference()}/silk/{uuid_text}/{entry[0]}', side, width, a, b)
                            entry[0] += 1
                run = []
    nudge_texts(board, items, changed)
    return changed


def nudge_texts(board, items, changed):
    """Move a visible silkscreen text of a footprint (a pin number) off pads and holes, at most 1 mm."""
    strokes = {side: [g.GetEffectiveShape() for fp in board.GetFootprints() for g in fp.GraphicalItems()
                      if g.GetLayer() == side and isinstance(g, p.PCB_SHAPE)] +
                     [g.GetEffectiveShape() for g in board.GetDrawings() if g.GetLayer() == side and isinstance(g, p.PCB_SHAPE)]
               for side in SILK}

    def conflict(g, near):
        shape = g.GetEffectiveTextShape()
        return any(p.SHAPE.Collide(b, shape, FMM(SILK_GAP)) for b, _ in near) or \
            any(p.SHAPE.Collide(o, shape, FMM(SILK_GAP)) for o in strokes[g.GetLayer()])

    for fp in board.GetFootprints():
        area = fp.GetBoundingBox(False)
        area.Inflate(FMM(1.5))
        near = {side: [(shape, bb) for shape, bb in items[side] if area.Intersects(bb)] for side in items}
        for g in fp.GraphicalItems():
            if g.GetLayer() not in SILK or not isinstance(g, p.PCB_TEXT) or not g.IsVisible() or not conflict(g, near[g.GetLayer()]):
                continue
            home = g.GetPosition()
            for r in (0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0):
                for angle in range(0, 360, 45):
                    g.SetPosition(p.VECTOR2I(int(home.x + FMM(r) * math.cos(math.radians(angle))),
                                             int(home.y + FMM(r) * math.sin(math.radians(angle)))))
                    if not conflict(g, near[g.GetLayer()]):
                        changed.setdefault(fp.GetReference(), [0, 0])
                        break
                else:
                    continue
                break
            else:
                g.SetPosition(home)
