#!/usr/bin/env python3
"""Encode the JLCPCB stack PCB_EU is ordered on: JLC04161H-7628, 1 oz outer / 0.5 oz inner.

Same factory and stack as PCB_V1 and cheap-version (hardware/PCB_V1/FAB.md, confirmed by the
user on 22.10.2026): copper 35 um outer / 15.2 um inner, prepreg 7628 0.2104 mm, core 1.065 mm,
10 um masks, 1.6062 mm in total, eps_r 4.6. The numbers are the JLCPCB schedule as recorded in the
PCB_V1 board, not a quote for this board: finished-copper tolerances, plating and the surface
finish are not checked here. Impedance is not controlled.

Raw targeted edits preserve routed copper, footprints, fills and other setup.
SPDX-License-Identifier: MIT
"""
import argparse
import copy
import json
from pathlib import Path

from kicad_sexpr import spans
from kicad_sexpr import parse, child, children

HERE = Path(__file__).resolve().parents[1]
COPPER_MM = {'F.Cu': .035, 'In1.Cu': .0152, 'In2.Cu': .0152, 'B.Cu': .035}
OUTER_UM = round(COPPER_MM['F.Cu'] * 1000, 3)    # 1 oz
INNER_UM = round(COPPER_MM['In1.Cu'] * 1000, 3)  # 0.5 oz as JLCPCB finishes it
# (type, thickness mm, material) of dielectric 1..3, JLC04161H-7628
DIELECTRICS = (('prepreg', .2104, 'FR4 7628'), ('core', 1.065, 'FR4'), ('prepreg', .2104, 'FR4 7628'))
EPSILON_R, LOSS_TANGENT = 4.6, .02
MASK_MM = .01
BOARD_MM = round(sum(COPPER_MM.values()) + sum(d[1] for d in DIELECTRICS) + 2 * MASK_MM, 4)
STACK_NAME = 'JLCPCB JLC04161H-7628, same as PCB_V1 and cheap-version (FAB.md)'
STATUS_KEY = 'STACKUP_STATUS'
NOMINAL_STATUS = STACK_NAME + '; finished-copper tolerances and plating not confirmed'


def number(value):
    return f'{value:g}'


def replace_form(text, key, value):
    found = spans(text, key)
    if len(found) != 1:
        raise RuntimeError(f'Expected one {key}, found {len(found)}')
    a, b = found[0]
    return text[:a] + value + text[b:]


def with_thickness(text, thickness):
    value = f'(thickness {number(thickness)})'
    found = spans(text, 'thickness')
    if found:
        return replace_form(text, 'thickness', value)
    at = text.rfind(')')
    return text[:at].rstrip() + '\n\t\t\t\t' + value + '\n\t\t\t)'


def layer_text(name, thickness):
    return (f'\t\t\t(layer "{name}"\n\t\t\t\t(type "copper")\n'
            f'\t\t\t\t(thickness {number(thickness)})\n\t\t\t)')


def dielectric_text(index):
    kind, thickness, material = DIELECTRICS[index]
    return (f'\t\t\t(layer "dielectric {index + 1}"\n\t\t\t\t(type "{kind}")\n'
            f'\t\t\t\t(thickness {number(thickness)})\n\t\t\t\t(material "{material}")\n'
            f'\t\t\t\t(epsilon_r {number(EPSILON_R)})\n\t\t\t\t(loss_tangent {number(LOSS_TANGENT)})\n\t\t\t)')


def dielectric_index(name):
    """0..2 for 'dielectric 1'..'dielectric 3', None for any other layer."""
    for index in range(len(DIELECTRICS)):
        if name == f'dielectric {index + 1}':
            return index
    return None


def nominal_stack():
    # KiCad's official BuildBoardThicknessFromStackup includes solder masks in the total.
    lines = ['(stackup',
             f'\t\t\t(layer "F.Mask" (type "Top Solder Mask") (thickness {number(MASK_MM)}))']
    for i, (name, copper) in enumerate(COPPER_MM.items()):
        lines.append(layer_text(name, copper))
        if i < len(DIELECTRICS):
            lines.append(dielectric_text(i))
    lines.extend([f'\t\t\t(layer "B.Mask" (type "Bottom Solder Mask") (thickness {number(MASK_MM)}))',
                  '\t\t\t(dielectric_constraints no)', '\t\t)'])
    return '\n'.join(lines)


def apply_text(text):
    """Return text with selected nominal thicknesses, preserving other data."""
    old = parse(text)
    enabled = [v[1] for v in child(old, 'layers')[1:]
               if isinstance(v, list) and len(v) > 1 and str(v[1]).endswith('.Cu')]
    if enabled != list(COPPER_MM):
        raise RuntimeError(f'Expected F.Cu/In1.Cu/In2.Cu/B.Cu, found {enabled}')
    expected = copy.deepcopy(old)
    child(child(expected, 'general'), 'thickness')[1] = number(BOARD_MM)
    setup = child(old, 'setup')
    old_stacks = children(setup, 'stackup')
    if len(old_stacks) > 1:
        raise RuntimeError('Multiple stackup specifications')
    if old_stacks:
        expected_stack = child(child(expected, 'setup'), 'stackup')
        for name, thickness in COPPER_MM.items():
            matches = [v for v in children(expected_stack, 'layer') if v[1] == name]
            if len(matches) > 1:
                raise RuntimeError(f'Duplicate stackup copper layer {name}')
            if matches:
                layer = matches[0]
                if child(layer, 'type')[1] != 'copper':
                    raise RuntimeError(f'{name} is not declared as copper')
                if children(layer, 'thickness'):
                    child(layer, 'thickness')[1] = number(thickness)
                else:
                    layer.append(['thickness', number(thickness)])
            else:
                expected_stack.append(parse(layer_text(name, thickness)))
        for layer in children(expected_stack, 'layer'):
            if (str(layer[1]).endswith('.Cu') or child(layer, 'type')[1] == 'copper') and layer[1] not in COPPER_MM:
                raise RuntimeError(f'Unknown copper layer in stackup: {layer[1]}')
        found = sorted(dielectric_index(layer[1]) for layer in children(expected_stack, 'layer')
                       if dielectric_index(layer[1]) is not None)
        if found != list(range(len(DIELECTRICS))):
            raise RuntimeError(f'Expected dielectric 1..{len(DIELECTRICS)} in the stackup, found indexes {found}')
        for layer in children(expected_stack, 'layer'):
            if dielectric_index(layer[1]) is not None:
                layer[:] = parse(dielectric_text(dielectric_index(layer[1])).strip())
        a, b = spans(text, 'stackup')[0]
        stack = text[a:b]
        edits = []
        names = set()
        for start, end in spans(stack, 'layer'):
            node = parse(stack[start:end])
            if node[1] in COPPER_MM:
                names.add(node[1])
                edits.append((start, end, with_thickness(stack[start:end], COPPER_MM[node[1]])))
            elif dielectric_index(node[1]) is not None:
                edits.append((start, end, dielectric_text(dielectric_index(node[1])).strip()))
        for start, end, value in sorted(edits, reverse=True):
            stack = stack[:start] + value + stack[end:]
        missing = [layer_text(name, thickness) for name, thickness in COPPER_MM.items() if name not in names]
        if missing:
            at = stack.rfind(')')
            stack = stack[:at].rstrip() + '\n' + '\n'.join(missing) + '\n\t\t)'
        result = text[:a] + stack + text[b:]
    else:
        stack = nominal_stack()
        child(expected, 'setup').insert(1, parse(stack))
        a, b = spans(text, 'setup')[0]
        insert = a + len('(setup')
        result = text[:insert] + '\n\t\t' + stack + text[insert:]
        properties = [v for v in children(old, 'property') if v[1] == STATUS_KEY]
        if properties and (len(properties) != 1 or properties[0][2] != NOMINAL_STATUS):
            raise RuntimeError(f'Existing {STATUS_KEY} property conflicts with provisional scaffold')
        if not properties:
            status = ['property', STATUS_KEY, NOMINAL_STATUS]
            expected.append(status)
            at = result.rfind(')')
            result = result[:at] + '\t(property ' + json.dumps(STATUS_KEY) + ' ' + json.dumps(NOMINAL_STATUS) + ')\n' + result[at:]
    a, b = spans(result, 'general')[0]
    result = result[:a] + with_thickness(result[a:b], BOARD_MM) + result[b:]
    if parse(result) != expected:
        raise RuntimeError('Unexpected non-stackup change; refusing to write PCB')
    return result


def check_text(text):
    board = parse(text)
    enabled = [v[1] for v in child(board, 'layers')[1:]
               if isinstance(v, list) and len(v) > 1 and str(v[1]).endswith('.Cu')]
    if enabled != list(COPPER_MM):
        raise RuntimeError(f'Unexpected copper layers: {enabled}')
    if abs(float(child(child(board, 'general'), 'thickness')[1]) - BOARD_MM) > 1e-9:
        raise RuntimeError(f'Board thickness must be {BOARD_MM:g} mm (JLC04161H-7628 stack)')
    stack = child(child(board, 'setup'), 'stackup')
    actual = {}
    layers = children(stack, 'layer')
    for layer in layers:
        if (str(layer[1]).endswith('.Cu') or child(layer, 'type')[1] == 'copper') and layer[1] not in COPPER_MM:
            raise RuntimeError(f'Unknown copper layer in stackup: {layer[1]}')
        if layer[1] in COPPER_MM:
            if layer[1] in actual:
                raise RuntimeError(f'Duplicate stackup copper layer {layer[1]}')
            if child(layer, 'type')[1] != 'copper':
                raise RuntimeError(f'{layer[1]} is not copper')
            actual[layer[1]] = float(child(layer, 'thickness')[1])
    if actual != COPPER_MM:
        raise RuntimeError(f'Copper thicknesses differ from JLC04161H-7628 (1 oz / 0.5 oz): {actual}')
    dielectrics = [v for v in layers if str(v[1]).startswith('dielectric ')]
    if len(dielectrics) != len(DIELECTRICS):
        raise RuntimeError('Four-layer stackup needs three dielectric layers; partial copper-only stacks are not a physical model')
    for layer in dielectrics:
        index = dielectric_index(layer[1])
        if index is None:
            raise RuntimeError(f'Unknown dielectric layer {layer[1]}')
        kind, thickness, material = DIELECTRICS[index]
        got = (child(layer, 'type')[1], float(child(layer, 'thickness')[1]), child(layer, 'material')[1],
               float(child(layer, 'epsilon_r')[1]), float(child(layer, 'loss_tangent')[1]))
        if got != (kind, thickness, material, EPSILON_R, LOSS_TANGENT):
            raise RuntimeError(f'{layer[1]} differs from JLC04161H-7628: {got}')
    physical = [v for v in layers if v[1] in COPPER_MM or str(v[1]).startswith('dielectric ') or str(v[1]).endswith('.Mask')]
    stack_mm = sum(float(t[1]) for layer in physical for t in children(layer, 'thickness'))
    if abs(stack_mm - BOARD_MM) > 1e-6:
        raise RuntimeError(f'Existing stackup totals {stack_mm:g} mm, expected {BOARD_MM:g} mm; do not silently rescale factory dielectrics')
    return dict(board_mm=BOARD_MM, copper_layers=4,
                copper_um={name: round(v * 1000, 3) for name, v in actual.items()},
                stack_total_including_masks_mm=round(stack_mm, 6),
                dielectric_schedule=STACK_NAME,
                factory_stackup_approved=False,
                impedance_control_verified=False,
                plating_and_finished_copper_tolerances_verified=False)


def apply_file(path):
    path = Path(path)
    original = path.read_text()
    result = apply_text(original)
    report = check_text(result)
    if result != original:
        path.write_text(result)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--board', type=Path, default=HERE / 'PCB_EU_LITE.kicad_pcb')
    ap.add_argument('--check', action='store_true', help='Validate selected copper/board thickness without writing')
    ap.add_argument('--output', type=Path, help='Optional JSON report')
    args = ap.parse_args()
    report = check_text(args.board.read_text()) if args.check else apply_file(args.board)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'PASS | stackup | 4 Cu, stack total {BOARD_MM:g} mm, outer {OUTER_UM:g} um / inner {INNER_UM:g} um; {STACK_NAME}; finished-copper tolerances not confirmed')


if __name__ == '__main__':
    main()
