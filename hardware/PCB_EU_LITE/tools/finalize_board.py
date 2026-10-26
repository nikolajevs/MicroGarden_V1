"""Final EU-LITE pre-fabrication corrections; run before silkscreen.py.

Moves C107 to the local VIN bypass position, adds ground/thermal vias and
direct high-current pad connections, and makes native silk printable.
No simulation or thermal qualification is implied. SPDX-License-Identifier: MIT
"""
import argparse
from pathlib import Path
import sys

import pcbnew as p

from build_board import HERE, identity
sys.path.insert(0, str(HERE / 'tools'))
from route_power import add_zone, add_rule_keepout, route_corridor
import silk_clean


def xy(v):
    return round(p.ToMM(v.x), 4), round(p.ToMM(v.y), 4)


def point(x, y):
    return p.VECTOR2I(p.FromMM(x), p.FromMM(y))


def add_track(board, name, net, layer, start, end, width):
    uid_name = 'finalize/' + name
    track = p.PCB_TRACK(board)
    identity(track, uid_name)
    for existing in list(board.GetTracks()):
        if existing.m_Uuid.AsString() == track.m_Uuid.AsString():
            board.Delete(existing)
    track.SetStart(point(*start))
    track.SetEnd(point(*end))
    track.SetLayer(layer)
    track.SetWidth(p.FromMM(width))
    track.SetNet(board.FindNet(net))
    board.Add(track)


def add_via(board, name, net, position):
    via = p.PCB_VIA(board)
    identity(via, 'finalize/' + name)
    for existing in list(board.GetTracks()):
        if existing.m_Uuid.AsString() == via.m_Uuid.AsString():
            board.Delete(existing)
    via.SetPosition(point(*position))
    via.SetWidth(p.FromMM(.6))
    via.SetDrill(p.FromMM(.3))
    via.SetViaType(p.VIATYPE_THROUGH)
    via.SetLayerPair(p.F_Cu, p.B_Cu)
    via.SetNet(board.FindNet(net))
    board.Add(via)


def finalize(board):
    fp = next(f for f in board.GetFootprints() if f.GetReference() == 'C107')
    if xy(fp.GetPosition()) == (10.0, 55.0):
        old = {xy(pad.GetPosition()) for pad in fp.Pads()}
        for track in list(board.GetTracks()):
            if not isinstance(track, p.PCB_VIA) and (
                xy(track.GetStart()) in old or xy(track.GetEnd()) in old
            ):
                board.Delete(track)
        fp.Flip(fp.GetPosition(), False)
    elif xy(fp.GetPosition()) not in ((28.25, 68.9), (28.25, 70.5)) or not fp.IsFlipped():
        raise RuntimeError('Unexpected C107 position; inspect before changing it')
    fp.SetPosition(point(28.25, 70.5))
    fp.SetOrientationDegrees(270)
    pads = {pad.GetNumber(): pad for pad in fp.Pads()}
    add_via(board, 'c107_vin', '+24V_AUX', (27.8, 67.1))
    add_via(board, 'c107_ground', 'GND', (26.65, 67.95))
    add_track(board, 'c107_vin_bottom', '+24V_AUX', p.B_Cu,
              xy(pads['1'].GetPosition()), (27.4, 69.725), .35)
    add_track(board, 'c107_vin_bottom_2', '+24V_AUX', p.B_Cu,
              (27.4, 69.725), (27.4, 67.1), .35)
    add_track(board, 'c107_vin_bottom_3', '+24V_AUX', p.B_Cu,
              (27.4, 67.1), (27.8, 67.1), .35)
    add_track(board, 'c107_vin_top', '+24V_AUX', p.F_Cu,
              (27.8, 67.1), (26.475, 66.665), .35)
    add_track(board, 'c107_ground_top', 'GND', p.F_Cu,
              (26.65, 67.95), (26.475, 67.935), .35)
    obsolete = p.PCB_TRACK(board)
    identity(obsolete, 'finalize/c107_ground_bottom')
    for item in list(board.GetTracks()):
        if item.m_Uuid.AsString() in {
            obsolete.m_Uuid.AsString(), '6aca11c6-ffb4-43d6-967a-a37065e9d93a',
            '734bd5fb-4318-4674-abf9-e60f0f321fa5',
        }:
            board.Delete(item)
    for i, pos in enumerate(((69.2, 49.2), (70.8, 49.2), (69.2, 50.8), (70.8, 50.8))):
        add_via(board, f'u501_ep_ground_{i}', 'GND', pos)
    high_current = {'F101', 'F102', 'F103', 'F104', 'F105',
                    'J101', 'J501', 'J502', 'J503', 'J701', 'J702',
                    'Q102', 'Q703', 'R111', 'R112', 'R501', 'R502', 'D501'}
    for footprint in board.GetFootprints():
        if footprint.GetReference() in high_current:
            for pad in footprint.Pads():
                if pad.GetNetname():
                    pad.SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)
    for via in board.GetTracks():
        if isinstance(via, p.PCB_VIA) and via.GetNetname() in {'LED_POS', 'LED_OUT', 'LED_SW', '+24V_LED'}:
            constrained = via.m_Uuid.AsString() in {
                'cd8a03f8-3b0d-4cd6-bce9-2414869db9c7',
                'd64815c7-6ebb-4034-b851-7934ef323b2b',
                '39596837-5d43-4f30-9b3b-47bc5d2e0f14',
            }
            via.SetWidth(p.FromMM(.6 if constrained else .8))
            via.SetDrill(p.FromMM(.3 if constrained else .4))
    add_via(board, 'led_sw_parallel', 'LED_SW', (69.694, 39.9))
    for layer in (p.F_Cu, p.In2_Cu):
        add_track(board, f'led_sw_parallel_{layer}', 'LED_SW', layer,
                  (69.694,39.9), (69.6936,40.5493), .6)
    obsolete_parallel = p.PCB_TRACK(board)
    identity(obsolete_parallel, 'finalize/led_out_parallel')
    for item in list(board.GetTracks()):
        if item.m_Uuid.AsString() == obsolete_parallel.m_Uuid.AsString():
            board.Delete(item)
    for zone in list(board.Zones()):
        if zone.GetZoneName().startswith(('lite_power/fab_', 'lite_power/keepout/fab_kelvin_', 'lite_power/keepout/ina238_inplus_')):
            board.Delete(zone)
    # Moving U102 upwards exposed the IN+ pad above the old keepout and the
    # old sense via at (18.3,47) touched its A1/GND pad. Route IN+ independently
    # to R111.1, and keep pours off both actual sense branches up to the shunt.
    old_sense = {
        '41121803-0225-4973-8de2-0e7c9b5b66ae',
        '706db217-8262-417f-aaa1-4ee675685e57',
        'fc72dab0-fe1e-4757-93d9-2856dae6a903',
        'ffa4753e-7507-45a9-9ad5-3b46b5a1248c',
        '58f5e29c-8c6f-49df-874d-55767b45b078',
        '58a8f4d8-3172-4770-a434-f0962a7ca32d',
        'c0f1fc45-2c24-47ec-98da-61411010b8aa',
    }
    for item in list(board.GetTracks()):
        if item.m_Uuid.AsString() in old_sense:
            board.Delete(item)
    add_via(board, 'kelvin_plus_start', 'GND', (18.05,46.2))
    add_via(board, 'kelvin_plus_end', 'GND', (18.3,51.5))
    for name, layer, a, b in (
        ('escape', p.F_Cu, (18.5,43), (18.3,44)),
        ('escape2', p.F_Cu, (18.3,44), (18.05,44.4)),
        ('escape3', p.F_Cu, (18.05,44.4), (18.05,46.2)),
        ('back', p.B_Cu, (18.05,46.2), (18.3,47)),
        ('back2', p.B_Cu, (18.3,47), (18.3,51.5)),
        ('entry', p.F_Cu, (18.3,51.5), (19.9625,53.1625)),
        ('pad', p.F_Cu, (19.9625,53.1625), (20,53.1625)),
    ):
        add_track(board, 'kelvin_plus_' + name, 'GND', layer, a, b, .2)
    keepouts = [
        ('plus_pin', 'F.Cu', [(18.1,42.0),(18.9,42.0),(18.9,44.0),(18.1,44.0)]),
        ('plus_escape', 'F.Cu', route_corridor([(18.5,43),(18.3,44),(18.05,44.4),(18.05,46.2)], .45)),
        ('plus_back', 'B.Cu', route_corridor([(18.05,46.2),(18.3,47),(18.3,51.5)], .45)),
        ('plus_entry', 'F.Cu', route_corridor([(18.3,51.5),(19.9625,53.1625),(20,53.1625)], .45)),
        ('minus_pin', 'F.Cu', [(18.65,42.0),(19.35,42.0),(19.35,43.95),(18.65,43.95)]),
        ('minus_escape', 'F.Cu', route_corridor([(19,43),(19,45),(18.9,45.1),(18.9,46.1)], .45)),
        ('minus_back', 'B.Cu', route_corridor([(18.9,46.1),(18.9,47.1),(21,49.2)], .45)),
        ('minus_entry', 'F.Cu', route_corridor([(21,49.2),(21,49.4),(20.5,49.9)], .45)),
    ]
    for side in ('F.Cu','In1.Cu','In2.Cu','B.Cu'):
        for name, position in (
            ('p0',(18.05,46.2)), ('p1',(18.3,51.5)),
            ('m0',(18.9,46.1)), ('m1',(21,49.2)),
        ):
            x, y = position
            keepouts.append((name + side, side, [(x-.55,y-.55),(x+.55,y-.55),
                                                (x+.55,y+.55),(x-.55,y+.55)]))
    for name, side, polygon in keepouts:
        add_rule_keepout(board, 'fab_kelvin_' + name, side, polygon)
    for net, name, points in (
        ('LED_POS', 'fab_led_pos_inner', [(63.505,40.169),(63.505,40.507),
                                        (68.169,45.172),(68.169,56.899),(71.652,60.382)]),
        ('LED_OUT', 'fab_led_out_inner', [(75.487,48.083),(77.037,49.633),
                                        (77.037,63.079),(74.213,65.903)]),
    ):
        zone = add_zone(board, net, 'In2.Cu', name, route_corridor(points, 1.0), 4)
        zone.SetPadConnection(p.ZONE_CONNECTION_FULL)
    zone = add_zone(board, 'RTN_SHUNT', 'F.Cu', 'fab_parallel_shunts',
                    route_corridor([(20,50.2375),(21.733,51.97),
                                    (21.733,53.37),(22.4,54.0375)], 1.1), 5)
    zone.SetPadConnection(p.ZONE_CONNECTION_FULL)
    # This established helper changes only native silk. Electrical library
    # parity is checked separately with the silk removed from both copies.
    silk_clean.clean_board(board)
    p.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', type=Path, default=HERE/'PCB_EU_LITE.kicad_pcb')
    args = parser.parse_args()
    project = args.board.with_suffix('.kicad_pro')
    original_project = project.read_bytes()
    try:
        board = p.LoadBoard(str(args.board))
        finalize(board)
        p.SaveBoard(str(args.board), board)
    finally:
        project.write_bytes(original_project)
    print('Finalized bypass, direct power pads, thermal vias and native silk; unconnected=',
          board.GetConnectivity().GetUnconnectedCount(False))


if __name__ == '__main__':
    main()
