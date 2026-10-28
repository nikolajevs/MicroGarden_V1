"""Generate the local 4-pin 2510 right-angle footprint and envelope model.

Reference: LHE A2543-WR04 / 2510-4AW, drawing page 14:
https://www.lhecn.com/wp-content/uploads/2019/01/a25432510.pdf
Pitch 2.54, span 7.62, body width 10.16, depth 11.5, height 5.7,
square contact 0.64 mm. Pin row is 0.7 mm from the rear of the envelope.
The manufacturer's PCB drawing specifies 0.9 mm drills; our 1.0 mm drills,
1.8 mm pads and 0.5 mm courtyard allowance are design choices.
Stock KF2510/KF2540 manufacturer, variant and pin numbering need physical
confirmation. The generated 3D is a schematic envelope, not a supplier STEP.
Local +Y is the mating direction; pad 1 is the left pin viewed from above.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
NAME = 'KF2510_1x04_P2.54mm_RightAngle'


def main():
    forms = [f'(footprint "{NAME}" (version 20241229) (generator "pcbnew") (layer "F.Cu")',
             '(descr "2510 4P right-angle; LHE A2543-WR04 reference envelope; verify actual stock part; mating +Y")',
             '(tags "KF2510 KF2540 2510-4AW right angle horizontal 4P 2.54")',
             '(attr through_hole)',
             '(property "Reference" "REF**" (at 3.81 -2.2) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
             f'(property "Value" "{NAME}" (at 3.81 13) (layer "F.Fab") (hide yes) (effects (font (size 1 1) (thickness 0.15))))',
             '(fp_rect (start -1.27 -0.7) (end 8.89 10.8) (stroke (width 0.1) (type solid)) (fill none) (layer "F.Fab"))',
             '(fp_rect (start -1.77 -1.4) (end 9.39 11.3) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))',
             '(fp_text user "${REFERENCE}" (at 3.81 5.2) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))']
    # Leave the rear silk open around the pads; retain a pin-1 marker outside copper.
    for start,end in [((-1.5,-.7),(-1.5,11.05)),((-1.5,11.05),(9.12,11.05)),((9.12,11.05),(9.12,-.7)),((-1.5,-1.2),(-.5,-1.2))]:
        forms.append(f'(fp_line (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) (stroke (width 0.15) (type solid)) (layer "F.SilkS"))')
    for i in range(4):
        shape='rect' if i==0 else 'circle'
        forms.append(f'(pad "{i+1}" thru_hole {shape} (at {i*2.54:g} 0) (size 1.8 1.8) (drill 1) (layers "*.Cu" "*.Mask"))')
    # VRML uses 0.1-inch units in KiCad; scaling converts our millimetre model.
    forms.append(f'(model "${{KIPRJMOD}}/libraries/LITE.3dshapes/{NAME}.wrl" (offset (xyz 0 0 0)) (scale (xyz 0.3937007874 0.3937007874 0.3937007874)) (rotate (xyz 0 0 0)))')
    forms.append(')')
    (HERE/'libraries/LITE_Parts.pretty'/f'{NAME}.kicad_mod').write_text('\n'.join(forms)+'\n',encoding='utf-8',newline='\n')
    vrml=['#VRML V2.0 utf8', '# Schematic envelope from LHE 2510-4AW drawing; not a qualified mechanical model.']
    def block(x,y,z,sx,sy,sz,color):
        # VRML/STEP Y is opposite to KiCad footprint Y.
        vrml.append(f'Transform {{ translation {x:g} {-y:g} {z:g} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {color} }} }} geometry Box {{ size {sx:g} {sy:g} {sz:g} }} }} ] }}')
    block(3.81,.9,2.85,10.16,3.2,5.7,'0.87 0.84 0.72')
    block(3.81,6.65,5.2,10.16,8.3,1,'0.87 0.84 0.72')
    for i in range(4):
        block(i*2.54,0,-.45,.64,.64,5.9,'0.7 0.7 0.72')
        block(i*2.54,5.15,2.18,.64,10.3,.64,'0.7 0.7 0.72')
    (HERE/'libraries/LITE.3dshapes'/f'{NAME}.wrl').write_text('\n'.join(vrml)+'\n',encoding='utf-8',newline='\n')
    print('Generated 2510 right-angle footprint and schematic 3D envelope')


if __name__ == '__main__':
    main()
