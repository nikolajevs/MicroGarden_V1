"""OSTAPA57E2A formed-lead footprint; source: OptoSupply Ver.a.1.1.

https://www.optosupply.com/uppic/2020811188683.pdf
Native pitch 1.25 mm, lead width 0.5 +/-0.10 mm, flange diameter 5.8 mm.
Design choice: form leads to 2.54 mm pitch with 1.0 mm finished holes and
1.8 mm pads (0.4 mm nominal annulus). Raise the body 5 mm above the board;
support each lead while forming, leaving 3 mm straight below the body.
Pads 1..4 = G cathode, B cathode, common anode, R cathode. Verify the
manufacturer's drawing/lead lengths before trimming. The 3D is an envelope,
not a supplier model or proof of the formed fit.
"""
from pathlib import Path
import math

HERE = Path(__file__).resolve().parents[1]
NAME = 'LED_OSTAPA57E2A_Formed_P2.54mm'


def main():
    forms = [f'(footprint "{NAME}" (version 20241229) (generator "pcbnew") (layer "F.Cu")',
             '(descr "OSTAPA57E2A; form 1.25mm leads to 2.54mm pitch; raise body 5mm; pins 1 G, 2 B, 3 A, 4 R")',
             '(attr through_hole)',
             '(property "Reference" "REF**" (at 3.81 -4) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
             f'(property "Value" "{NAME}" (at 3.81 4) (layer "F.Fab") (hide yes) (effects (font (size 1 1) (thickness 0.15))))',
             '(fp_circle (center 0 0) (end 2.9 0) (stroke (width 0.1) (type solid)) (fill none) (layer "F.Fab"))',
             '(fp_rect (start -5.21 -3.4) (end 5.21 3.4) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))',
             '(fp_text user "FORM LEADS" (at 0 1.8) (layer "F.Fab") (effects (font (size 0.7 0.7) (thickness 0.1))))']
    for i,label in enumerate(('G','B','A','R')):
        shape='rect' if i==0 else 'circle'
        forms.append(f'(pad "{i+1}" thru_hole {shape} (at {(i-1.5)*2.54:g} 0) (size 1.8 1.8) (drill 1) (layers "*.Cu" "*.Mask"))')
        forms.append(f'(fp_text user "{label}" (at {(i-1.5)*2.54:g} -1.7) (layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.12))))')
    forms.append(f'(model "${{KIPRJMOD}}/libraries/LITE.3dshapes/{NAME}.wrl" (offset (xyz 0 0 0)) (scale (xyz 0.3937007874 0.3937007874 0.3937007874)) (rotate (xyz 0 0 0)))')
    forms.append(')')
    (HERE/'libraries/LITE_Parts.pretty'/f'{NAME}.kicad_mod').write_text('\n'.join(forms)+'\n',encoding='utf-8',newline='\n')
    vrml=['#VRML V2.0 utf8', '# Formed-lead schematic envelope only; dimensions in mm.']
    def cylinder(x,z,r,h):
        vrml.append(f'Transform {{ translation {x} 0 {z} rotation 1 0 0 1.570796327 children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.85 0.85 0.85 transparency 0.15 }} }} geometry Cylinder {{ radius {r} height {h} }} }} ] }}')
    cylinder(0,5.5,2.9,1)
    cylinder(0,8.15,2.5,4.3)
    for i in range(4):
        hole=(i-1.5)*2.54; native=(i-1.5)*1.25
        for x1,z1,x2,z2 in [(hole,-2,hole,1),(hole,1,native,2),(native,2,native,5)]:
            length=math.hypot(x2-x1,z2-z1)
            angle=math.atan2(x2-x1,z2-z1)
            vrml.append(f'Transform {{ translation {(x1+x2)/2} 0 {(z1+z2)/2} rotation 0 1 0 {angle} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.7 0.7 0.72 }} }} geometry Box {{ size 0.5 0.5 {length} }} }} ] }}')
    (HERE/'libraries/LITE.3dshapes'/f'{NAME}.wrl').write_text('\n'.join(vrml)+'\n',encoding='utf-8',newline='\n')
    print('Generated OSTAPA57E2A formed-lead footprint and envelope')


if __name__ == '__main__':
    main()
