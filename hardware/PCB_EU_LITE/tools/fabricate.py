"""Validate EU-LITE and export a hashed four-layer Gerber/Excellon package.

Use KiCad Python, Shapely 2 and gerbonara. This is a prototype fabrication
package; physical fit and temperature still require first-article checks.
SPDX-License-Identifier: MIT
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

HERE = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--kicad-cli', required=True)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--dependencies', type=Path)
    ap.add_argument('--allow-other-version', action='store_true')
    args = ap.parse_args()
    if args.dependencies:
        sys.path.insert(0, str(args.dependencies.resolve()))
    sys.path.insert(0, str(HERE/'tools'))
    from check_placement import check as placement_check
    from fabrication_checks import check as board_check
    from check_power_paths import check as power_check
    from gerbonara import LayerStack

    version = subprocess.check_output([args.kicad_cli,'version'],text=True).strip()
    if version != '10.0.7' and not args.allow_other_version:
        ap.error(f'KiCad {version}; target is 10.0.7. Explicitly allow the local version.')
    out = args.output.resolve()
    if out.exists():
        ap.error('Output directory already exists; choose a fresh directory')
    out.mkdir(parents=True)
    checks = out/'checks'
    checks.mkdir()
    board = HERE/'PCB_EU_LITE.kicad_pcb'
    schematic = board.with_suffix('.kicad_sch')
    before = digest(board)

    def run(name, command):
        result = subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                text=True,encoding='utf-8',errors='replace')
        (checks/(name+'.log')).write_text(result.stdout,encoding='utf-8')
        if result.returncode:
            raise RuntimeError(f'{name} failed: see {checks/(name+".log")}')
        print('PASS |', name, flush=True)

    netlist = checks/'netlist.xml'
    run('netlist',[args.kicad_cli,'sch','export','netlist','--format','kicadxml','-o',str(netlist),str(schematic)])
    run('erc',[args.kicad_cli,'sch','erc','--exit-code-violations','--format','json','-o',str(checks/'erc.json'),str(schematic)])
    run('schematic_connections',[sys.executable,'-B',str(HERE/'tools/check_schematics.py')])
    run('design_and_bom',[sys.executable,'-B',str(HERE/'tools/verify.py'),'--output',str(checks/'connectivity.txt')])
    run('design_mutations',[sys.executable,'-B',str(HERE/'tools/test_verify.py')])
    run('silkscreen',[sys.executable,'-B',str(HERE/'tools/silkscreen.py'),'--check','--output',str(checks/'silkscreen.json')])
    placement = placement_check(board,netlist)
    (checks/'placement.json').write_text(json.dumps(placement,indent=2)+'\n',encoding='utf-8')
    physical = board_check(board)
    (checks/'board.json').write_text(json.dumps(physical,indent=2)+'\n',encoding='utf-8')
    (checks/'power_paths.json').write_text(json.dumps(power_check(board),indent=2)+'\n',encoding='utf-8')
    print('PASS | placement, manufacturing geometry, power paths and same-net Kelvin isolation',flush=True)
    run('drc',[args.kicad_cli,'pcb','drc','--schematic-parity','--format','json','-o',str(checks/'drc.json'),str(board)])
    drc = json.loads((checks/'drc.json').read_text(encoding='utf-8'))
    import pcbnew as p
    loaded = p.LoadBoard(str(board))
    refs = {fp.m_Uuid.AsString():fp.GetReference() for fp in loaded.GetFootprints()}
    allowed = set(physical['verified_silk_only_library_differences'])
    accepted, unexpected = [], []
    for violation in drc.get('violations',[]):
        affected = {refs.get(item['uuid']) for item in violation.get('items',[])}
        if (violation['type']=='lib_footprint_mismatch' and affected
                and None not in affected and affected <= allowed):
            accepted.append(violation)
        else:
            unexpected.append(violation)
    if unexpected or drc.get('unconnected_items') or drc.get('schematic_parity'):
        raise RuntimeError(f'DRC release gate failed: {len(unexpected)} unexpected violations')
    print(f'PASS | DRC: no electrical/geometric issues; {len(accepted)} proven silk-only library warnings',flush=True)

    gerber = out/'gerber'
    run('gerber',[args.kicad_cli,'pcb','export','gerbers','--layers',
        'F.Cu,In1.Cu,In2.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,Edge.Cuts',
        '--subtract-soldermask','--precision','6','-o',str(gerber),str(board)])
    run('drill',[args.kicad_cli,'pcb','export','drill','--format','excellon',
        '--drill-origin','absolute','--excellon-units','mm','--excellon-oval-format','route',
        '--excellon-separate-th','--generate-report','-o',str(gerber),str(board)])

    # Independent CAM re-import, using the exported files instead of PCB data.
    stack = LayerStack.open(gerber)
    width,height = stack.outline.size('mm')
    if abs(width-100)>.12 or abs(height-100)>.12:
        raise RuntimeError(f'Gerber outline size differs: {width} x {height}')
    copper = list(stack.copper_layers)
    if len(copper)!=4 or stack.drill_pth is None or stack.drill_npth is None:
        raise RuntimeError('Incomplete copper layer stack or separated drill files')
    for side in ('top','bottom'):
        (out/('CAM_'+side+'.svg')).write_text(str(stack.to_pretty_svg(side=side)),encoding='utf-8')
    cam = dict(board_width_mm=width,board_height_mm=height,copper_layers=len(copper),
               pth_objects=len(stack.drill_pth.objects),npth_objects=len(stack.drill_npth.objects),
               reader='gerbonara: independent Gerber and Excellon import')
    (checks/'cam.json').write_text(json.dumps(cam,indent=2)+'\n',encoding='utf-8')
    if digest(board)!=before:
        raise RuntimeError('Source board changed while exporting')
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=HERE,text=True).strip()
    source = [board,board.with_suffix('.kicad_pro'),HERE/'placement.json',HERE/'design.json',
              *sorted(HERE.glob('*.kicad_sch'))]
    manifest = dict(source_revision=revision,board_sha256=before,kicad_version=version,
                    target_version='10.0.7',target_version_verified=version=='10.0.7',
                    stage='PROTOTYPE_FABRICATION',electrical_drc_issues=0,unconnected=0,schematic_parity_issues=0,
                    verified_silk_only_library_warnings=len(accepted),cam=cam,
                    source_hash_encoding='UTF-8 text, LF-normalized as in .gitattributes',
                    source_sha256={str(path.relative_to(HERE)):hashlib.sha256(
                        path.read_text(encoding='utf-8').encode('utf-8')).hexdigest() for path in source},
                    fabrication_files={path.name:digest(path) for path in sorted(gerber.iterdir()) if path.is_file()},
                    physical_fit_verified=False,thermal_qualified=False)
    archive=out/f'PCB_EU_LITE_Gerber_{before[:8]}.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for path in sorted(gerber.iterdir()):
            if path.is_file():
                info=zipfile.ZipInfo(path.name,date_time=(1980,1,1,0,0,0))
                info.compress_type=zipfile.ZIP_DEFLATED
                z.writestr(info,path.read_bytes())
    manifest['archive']=dict(filename=archive.name,sha256=digest(archive))
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    (out/'README.md').write_text(f'''# EU-LITE — Gerber для изготовления прототипа

Архив: [{archive.name}]({archive.name}). SHA-256: `{digest(archive)}`.
Исходная плата: `{before}`; исходная ревизия: `{revision}`.

100 × 100 мм, четыре медных слоя: F.Cu → In1.Cu → In2.Cu → B.Cu.
FR-4, номинально 1,6 мм; внешняя медь 1 oz, внутренняя 0,5 oz;
зелёная маска, белая шелкография. Сквозные переходы, маска на обеих сторонах.
В архиве отдельные PTH/NPTH Excellon в мм и абсолютные координаты;
платы не панелизированы, контролируемый импеданс не заявлен.

Проверки: свежие ERC, DRC с паритетом схемы, netlist, размещение,
съёмы INA238 Kelvin и производственная геометрия. Электрических и
геометрических DRC-проблем, неподключённых цепей и расхождений со схемой: 0.
Сохранено {len(accepted)} предупреждений о библиотеке: независимо проверено,
что они относятся исключительно к печатным контурам, обрезанным возле площадок.
Правила DRC не отключались. Полные исходные отчёты находятся в `checks/`.

Gerber и Excellon повторно прочитаны независимым CAM-парсером: 4 слоя,
контур {width:.2f} × {height:.2f} мм. Обзоры CAM_top.svg/CAM_bottom.svg получены
из производственных файлов. Контрольные суммы всех файлов — в manifest.json.

Часть позиционных надписей имеет высоту 0,8 мм при штрихе 0,15 мм — абсолютный
минимум из рекомендаций JLCPCB; рекомендуемая высота 1 мм. При заказе сверить
предпросмотр шелкографии. [Источник](https://jlcpcb.com/blog/character-design-specifications).

Это первый прототип. Корпус, формовка выводов RGB, фактические разъёмы,
нагрев, минимальный PWM и прошивка INA238 требуют стендовых проверок.
Плату подключать сначала к лабораторному БП с ограничением тока;
см. ../../review/Bench_checklist.md. Это не заказ и не оплата производства.
''',encoding='utf-8')
    print('PASS | independent CAM import and Gerber archive:',archive,flush=True)


if __name__=='__main__':
    main()
