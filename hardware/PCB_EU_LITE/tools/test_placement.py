"""Regression checks for rebuilding and detecting actual placement defects.

Run using KiCad Python with Shapely 2; positional argument: fresh kicadxml netlist.
"""
import argparse
import tempfile
from pathlib import Path
import pcbnew as p
from build_board import build, HERE, SOURCE
from check_placement import check


def main(netlist):
    board_path = HERE/'PCB_EU_LITE.kicad_pcb'
    originals = {f:f.read_bytes() for f in [board_path, HERE/'PCB_EU_LITE.kicad_pro', SOURCE]}
    with tempfile.TemporaryDirectory(prefix='eu-lite-tests-') as folder:
        out = Path(folder)/board_path.name
        build(netlist,out)
        first = out.read_bytes()
        build(netlist,out)
        assert first == out.read_bytes(), 'Unrouted board generation is not byte-reproducible'
        check(out,netlist)
        def move(fp,x,y): fp.SetPosition(p.VECTOR2I(p.FromMM(x),p.FromMM(y)))
        def block_joint(b):
            fp=b.FindFootprintByReference('R901')
            if not fp.IsFlipped():
                fp.Flip(fp.GetPosition(),False)
            pad=next(iter(b.FindFootprintByReference('L501').Pads()))
            fp.SetPosition(pad.GetPosition())
        cases = [
            ('component outside', lambda b: move(b.FindFootprintByReference('C209'),115,20), 'outside board margin'),
            ('overlapping courtyards', lambda b: move(b.FindFootprintByReference('C209'),*tuple(p.ToMM(v) for v in [b.FindFootprintByReference('C208').GetPosition().x,b.FindFootprintByReference('C208').GetPosition().y])), 'courtyard gap'),
            ('high component on bottom', lambda b: b.FindFootprintByReference('L501').Flip(b.FindFootprintByReference('L501').GetPosition(),False), 'must be on top'),
            ('wrong pad net', lambda b: next(iter(b.FindFootprintByReference('R111').Pads())).SetNetCode(0), 'net differs'),
            ('blocked THT solder access', block_joint, 'THT solder access'),
            ('disabled RF rule', lambda b: next(iter(b.FindFootprintByReference('U202').Zones())).SetDoNotAllowTracks(False), 'antenna keepout'),
            ('UART moved', lambda b: move(b.FindFootprintByReference('J201'),7,10), 'UART must remain'),
            ('fan faces inward', lambda b: b.FindFootprintByReference('J411').SetOrientationDegrees(-90), 'wrong edge/mating orientation'),
            ('card far from UART', lambda b: move(b.FindFootprintByReference('J301'),60,80), 'microSD must remain near UART'),
            ('blocked connector access', lambda b: move(b.FindFootprintByReference('C209'),99,44), 'connector/card access obstructed'),
            ('bottom hot-air gap', lambda b: move(b.FindFootprintByReference('R902'),20,73), 'courtyard gap < 1.0'),
            ('SHT below fans', lambda b: move(b.FindFootprintByReference('J401'),90,75), 'SHT4x must be above'),
            ('LED undersized annulus', lambda b: next(iter(b.FindFootprintByReference('D901').Pads())).SetSize(p.VECTOR2I(p.FromMM(1.07),p.FromMM(1.8))), 'incorrect hole/annulus'),
        ]
        for name,mutate,expected in cases:
            b = p.LoadBoard(str(board_path));mutate(b);p.SaveBoard(str(out),b)
            try:
                check(out,netlist)
            except ValueError as exc:
                assert expected in str(exc), (name,str(exc))
            else:
                raise AssertionError(f'Accepted: {name}')
        b=p.LoadBoard(str(board_path))
        track=p.PCB_TRACK(b);track.SetStart(p.VECTOR2I(p.FromMM(70),p.FromMM(70)));track.SetEnd(p.VECTOR2I(p.FromMM(71),p.FromMM(70)));track.SetWidth(p.FromMM(.25));track.SetLayer(p.F_Cu);b.Add(track)
        p.SaveBoard(str(out),b)
        copper=out.read_bytes()
        try:
            build(netlist,out)
        except SystemExit as exc:
            assert 'Refusing' in str(exc)
        else:
            raise AssertionError('Builder overwrote routed copper')
        assert out.read_bytes()==copper
    assert all(f.read_bytes()==content for f,content in originals.items()), 'Source board/project modified by checks'
    print(f'PASS | identical regeneration; {len(cases)} placement mutations rejected; routed copper protected; source projects unchanged')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('netlist',type=Path)
    main(parser.parse_args().netlist.resolve())
