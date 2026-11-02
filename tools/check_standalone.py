"""Check local project dependencies without KiCad or access to the source repo."""
from pathlib import Path
import ast
import json
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
# V1 (released r3 prototype) and V2 (six 24 V lamp outputs) are separate, self-contained KiCad projects.
PROJECTS = {ROOT / 'hardware/PCB_EU_LITE': 'PCB_EU_LITE.kicad_pcb', ROOT / 'hardware/PCB_EU_LITE_V2': None}
sys.path.insert(0, str(ROOT / 'hardware/PCB_EU_LITE/tools'))
from kicad_sexpr import parse, children, child


def main():
    errors = []
    checked = 0

    def require(condition, message):
        nonlocal checked
        checked += 1
        if not condition:
            errors.append(message)

    for HW, board in PROJECTS.items():
        check_project(HW, board, require)

    for path in [*ROOT.glob('*.md'), *(ROOT / 'hardware').rglob('*.md')]:
        value = path.read_text(encoding='utf-8')
        for link in re.findall(r'\]\(([^\s)]+)\)', value):
            if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', link) or link.startswith('#'):
                continue
            target = unquote(link.split('#', 1)[0])
            require((path.parent / target).exists(), f'{path.relative_to(ROOT)}: broken link {link}')
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'PASS | {checked} standalone dependency, footprint, model, template and documentation checks ({len(PROJECTS)} projects)')


def check_project(HW, board, require):
    """Local libraries, footprints, models, template and tools of one project; nothing may point outside its folder."""
    for name, kind in [('fp-lib-table', 'fp_lib_table'), ('sym-lib-table', 'sym_lib_table')]:
        root = parse((HW / name).read_text(encoding='utf-8'))
        require(root[0] == kind, f'{name}: invalid table')
        names = []
        for lib in children(root, 'lib'):
            names.append(child(lib, 'name')[1])
            uri = child(lib, 'uri')[1]
            require(uri.startswith('${KIPRJMOD}/'), f'{name}: external library {uri}')
            resolved = (HW / uri.removeprefix('${KIPRJMOD}/')).resolve()
            require(resolved.is_relative_to(HW) and resolved.exists(), f'{name}: missing local library {uri}')
        require(len(names) == len(set(names)), f'{name}: duplicate libraries')

    spec = json.loads((HW / 'design.json').read_text(encoding='utf-8'))
    for item in spec['parts']:
        footprint = item['footprint']
        if not footprint:
            continue
        lib, name = footprint.split(':')
        require((HW / 'libraries' / (lib + '.pretty') / (name + '.kicad_mod')).exists(), f'{HW.name} {item["ref"]}: missing footprint {footprint}')

    for path in [*([HW/board] if board else []), *(HW/'libraries').rglob('*.kicad_mod')]:
        value = path.read_text(encoding='utf-8')
        for model in re.findall(r'\(model "\$\{KIPRJMOD\}/([^"\n]+)"', value):
            resolved = (HW / model).resolve()
            require(resolved.is_relative_to(HW) and resolved.exists(), f'{path.name}: missing local model {model}')
        require('../PCB_EU/' not in value, f'{path.name}: parent project dependency')

    outline = parse((HW/'mechanical/outline.kicad_pcb').read_text(encoding='utf-8'))
    holes = children(outline, 'footprint')
    refs = {next(p[2] for p in children(fp, 'property') if p[1] == 'Reference') for fp in holes}
    require(refs == {'H1', 'H2', 'H3', 'H4'} and len(holes) == 4, 'Mechanical template: wrong mounting holes')
    require(len(children(outline, 'gr_line')) == 4, 'Mechanical template: wrong contour')
    require(not any(children(outline, k) for k in ('segment', 'via', 'zone')), 'Mechanical template contains electrical copper')

    for path in (HW/'tools').glob('*.py'):
        value = path.read_text(encoding='utf-8')
        ast.parse(value, filename=str(path))
        require(not re.search(r"HERE\.parent\s*/\s*['\"]PCB_EU", value), f'{path.name}: external code dependency')
        require(not re.search(r"HERE\.parent|parents\[2\]|\.\./PCB_EU", value), f'{HW.name}/tools/{path.name}: depends on a sibling project folder')


if __name__ == '__main__':
    main()
