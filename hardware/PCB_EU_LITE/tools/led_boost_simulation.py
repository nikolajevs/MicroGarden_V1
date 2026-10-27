#!/usr/bin/env python3
"""Periodic switched-power-stage simulation of the EU-LITE LED boost.

Solve the CCM periodic orbit of L501/C508 for an ideal PWM source, using
RK4 integration and a two-state affine cycle map. Duty is fitted to the
specified load current; no LM3429 COMP or protection behaviour is inferred.
Only the Python standard library is required. SPDX-License-Identifier: MIT
"""
import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
DESIGN = HERE / 'design.json'
OUTPUT = HERE / 'review'
TARGET_A = 1.235 * 1000 / ((4700 + 1000) * .150)
CORNER_A = 1.26 * 1000 * 1.01 / ((4700 + 1000) * .99 * .150 * .99)
IS_LIMIT_MIN_A = .215 / (.040 * 1.01)
OVP_V = 1.24 * (33_000 + 10_000 + 1_000) / 1_000
SOURCE = 'https://www.ti.com/lit/ds/symlink/lm3429.pdf'


@dataclass(frozen=True)
class Model:
    frequency_hz: float = 25 / ((82_000 + 6_200) * 1e-9)  # LM3429 Rev H eq. 6: 283.45 kHz nominal
    inductance_h: float = 43.5e-6
    inductor_dcr_ohm: float = .030  # assumed, not measured
    switch_ron_ohm: float = .0208  # cold 4.5 V table value; no thermal prediction
    sense_ohm: float = .040 * 1.01
    diode_vf_v: float = .60  # assumed constant drop
    diode_rd_ohm: float = .020  # assumed slope resistance
    output_cap_f: float = 470e-6 + 6 * 4.7e-6 + .1e-6
    output_esr_ohm: float = .060  # lumped assumption; real bank is not characterised
    output_shunt_ohm: float = .150
    led_voltage_v: float = 48.0


CASES = (
    ('low_line', 21.6, TARGET_A, .150),
    ('nominal', 24.0, TARGET_A, .150),
    ('high_line', 25.0, TARGET_A, .150),
    ('low_line_tolerance', 21.6, CORNER_A, .150 * .99),
)


def require_design_values():
    parts = {part['ref']: part for part in json.loads(DESIGN.read_text(encoding='utf-8'))['parts']}
    expected = {
        'L501': '47u 15A', 'Q501': 'BSC146N10LS5',
        'D501': 'SBRT15U100SP5', 'R501': '0R040 3W', 'R502': '0R15 2W',
        'R503': '1k 1%', 'R504': '1k 1%', 'R505': '4k7 1%', 'R506': '1k 1%',
        'R509': '33k 1%', 'R510': '10k 1%', 'R511': '1k 1%',
        'C508': '470u 63V', 'C515': '100n 100V',
        **{f'C{number}': '4u7 100V' for number in range(509, 515)},
    }
    nets = {
        'L501': {'1': '+24V_LED', '2': 'LED_SW'},
        'D501': {'1': 'LED_OUT', '2': 'LED_SW'},
        'Q501': {'1': 'LED_IS', '2': 'LED_IS', '3': 'LED_IS',
                 '4': 'LED_GATE', '5': 'LED_SW'},
        'R501': {'1': 'LED_IS', '2': 'GND'},
        'R502': {'1': 'LED_OUT', '2': 'LED_POS'},
        **{f'J{number}': {'1': 'LED_POS', '2': 'GND'} for number in (501, 502, 503)},
        **{f'C{number}': {'1': 'LED_OUT', '2': 'GND'} for number in range(508, 516)},
    }
    for ref, value in expected.items():
        if parts[ref]['value'] != value:
            raise RuntimeError(f'{ref} changed; update simulation assumptions')
    for ref, expected_nets in nets.items():
        if parts[ref]['nets'] != expected_nets:
            raise RuntimeError(f'{ref} connectivity changed; update simulation topology')


def terminal_voltage(state, on, model, load_ohm):
    current, capacitor_v = state
    diode_a = 0.0 if on else current
    return (capacitor_v + diode_a * model.output_esr_ohm) / (
        1 + model.output_esr_ohm / load_ohm
    )


def derivative(state, on, vin, model, load_ohm):
    current, _ = state
    vout = terminal_voltage(state, on, model, load_ohm)
    if on:
        voltage_l = vin - current * (
            model.inductor_dcr_ohm + model.switch_ron_ohm + model.sense_ohm
        )
        capacitor_a = -vout / load_ohm
    else:
        voltage_l = vin - model.diode_vf_v - vout - current * (
            model.inductor_dcr_ohm + model.diode_rd_ohm
        )
        capacitor_a = current - vout / load_ohm
    return voltage_l / model.inductance_h, capacitor_a / model.output_cap_f


def advance(state, on, vin, model, load_ohm, dt):
    def shifted(slope, scale):
        return tuple(x + dt * scale * dx for x, dx in zip(state, slope))
    a = derivative(state, on, vin, model, load_ohm)
    b = derivative(shifted(a, .5), on, vin, model, load_ohm)
    c = derivative(shifted(b, .5), on, vin, model, load_ohm)
    d = derivative(shifted(c, 1), on, vin, model, load_ohm)
    return tuple(x + dt * (da + 2 * db + 2 * dc + dd) / 6
                 for x, da, db, dc, dd in zip(state, a, b, c, d))


def phases(duty, model, steps):
    period = 1 / model.frequency_hz
    for on, fraction in ((True, duty), (False, 1 - duty)):
        count = max(1, math.ceil(steps * fraction))
        yield on, period * fraction / count, count


def propagate(state, duty, vin, model, load_ohm, steps):
    for on, dt, count in phases(duty, model, steps):
        for _ in range(count):
            state = advance(state, on, vin, model, load_ohm, dt)
    return state


def periodic_state(duty, vin, model, load_ohm, steps):
    # The two-state ODE is affine within each phase. The cycle is x'=P*x+b.
    # Solving (I-P)*x=b removes arbitrary startup-controller assumptions.
    def cycle(state):
        return propagate(state, duty, vin, model, load_ohm, steps)
    b = cycle((0.0, 0.0))
    c0, c1 = cycle((1.0, 0.0)), cycle((0.0, 1.0))
    a00, a01 = 1 - (c0[0] - b[0]), -(c1[0] - b[0])
    a10, a11 = -(c0[1] - b[1]), 1 - (c1[1] - b[1])
    det = a00 * a11 - a01 * a10
    if abs(det) < 1e-16:
        raise RuntimeError('singular periodic-state map')
    return ((b[0] * a11 - a01 * b[1]) / det,
            (a00 * b[1] - b[0] * a10) / det)


def solve_duty(vin, target_a, model, load_ohm, steps):
    # Mean capacitor voltage equals mean terminal voltage in a periodic orbit.
    # The midpoint of the capacitor extrema is sufficient for duty fitting;
    # the independently integrated mean is checked below.
    low, high = .2, .75
    def current(duty):
        state = periodic_state(duty, vin, model, load_ohm, steps)
        turn_off = state
        on, dt, count = next(phases(duty, model, steps))
        for _ in range(count):
            turn_off = advance(turn_off, on, vin, model, load_ohm, dt)
        return (state[1] + turn_off[1]) / (2 * load_ohm)
    if not current(low) < target_a < current(high):
        raise RuntimeError('target outside duty search bracket')
    for _ in range(27):
        duty = (low + high) / 2
        if current(duty) < target_a:
            low = duty
        else:
            high = duty
    return (low + high) / 2


def simulate_case(name, vin, target_a, model=Model(), steps=100):
    # The LED resistance is chosen for the specified 48 V working point only.
    # R502 remains a separate loss term in the resulting total load.
    led_ohm = model.led_voltage_v / target_a
    load_ohm = led_ohm + model.output_shunt_ohm
    duty = solve_duty(vin, target_a, model, load_ohm, steps)
    start = periodic_state(duty, vin, model, load_ohm, steps)
    state = start
    integrals = {key: 0.0 for key in (
        'vout', 'led_a', 'led_w', 'input_a', 'loss_w', 'r502_w',
    )}
    v_min, i_min = math.inf, math.inf
    v_max, i_max, sense_max = -math.inf, -math.inf, 0.0
    waveform = []
    time_s = 0.0

    def observables(value, on):
        current, _ = value
        voltage = terminal_voltage(value, on, model, load_ohm)
        led_a = voltage / load_ohm
        cap_a = (-led_a if on else current - led_a)
        path_loss = current ** 2 * model.inductor_dcr_ohm
        if on:
            path_loss += current ** 2 * (model.switch_ron_ohm + model.sense_ohm)
        else:
            path_loss += current * model.diode_vf_v + current ** 2 * model.diode_rd_ohm
        r502_w = led_a ** 2 * model.output_shunt_ohm
        loss = path_loss + cap_a ** 2 * model.output_esr_ohm + r502_w
        return {
            'vout': voltage, 'led_a': led_a, 'led_w': led_a ** 2 * led_ohm,
            'input_a': current, 'loss_w': loss, 'r502_w': r502_w,
        }

    for on, dt, count in phases(duty, model, steps):
        for _ in range(count):
            before = observables(state, on)
            following = advance(state, on, vin, model, load_ohm, dt)
            after = observables(following, on)
            for key in integrals:
                integrals[key] += (before[key] + after[key]) * dt / 2
            for sample, data in ((state, before), (following, after)):
                v_min, v_max = min(v_min, data['vout']), max(v_max, data['vout'])
                i_min, i_max = min(i_min, sample[0]), max(i_max, sample[0])
                if on:
                    sense_max = max(sense_max, sample[0] * model.sense_ohm)
            waveform.append({
                'time_us': round(time_s * 1e6, 7),
                'vout_v': round(before['vout'], 7),
                'inductor_a': round(state[0], 7),
                'switch_on': on,
            })
            state, time_s = following, time_s + dt
        # Include the phase endpoint so the SVG depicts ESR jumps correctly.
        waveform.append({
            'time_us': round(time_s * 1e6, 7),
            'vout_v': round(observables(state, on)['vout'], 7),
            'inductor_a': round(state[0], 7),
            'switch_on': on,
        })
    means = {key: value * model.frequency_hz for key, value in integrals.items()}
    input_w = vin * means['input_a']
    residual = input_w - means['led_w'] - means['loss_w']
    if i_min <= 0:
        raise RuntimeError('CCM model left continuous conduction; a DCM model is required')
    return {
        'case': name, 'vin_v': vin, 'target_a': target_a,
        'required_duty': duty,
        'max_time_step_ns': max(dt for _, dt, _ in phases(duty, model, steps)) * 1e9,
        'vout_avg_v': means['vout'], 'vout_pp_v': v_max - v_min,
        'vout_max_v': v_max, 'led_current_avg_a': means['led_a'],
        'led_power_avg_w': means['led_w'],
        'inductor_current_avg_a': means['input_a'],
        'inductor_current_peak_a': i_max, 'inductor_current_min_a': i_min,
        'is_peak_v': sense_max, 'input_power_avg_w': input_w,
        'model_loss_w': means['loss_w'], 'r502_loss_w': means['r502_w'],
        'energy_balance_relative_error': abs(residual) / input_w,
        'periodic_current_error_a': abs(state[0] - start[0]),
        'periodic_voltage_error_v': abs(state[1] - start[1]),
        'current_target_relative_error': abs(means['led_a'] - target_a) / target_a,
        'reference_limit_margin_a': IS_LIMIT_MIN_A - i_max,
        'reference_ovp_margin_v': OVP_V - v_max,
        'waveform': waveform,
    }


def validate_numerics(results):
    for result in results:
        for key, limit in (
            ('energy_balance_relative_error', 1e-4),
            ('periodic_current_error_a', 1e-6),
            ('periodic_voltage_error_v', 1e-6),
            ('current_target_relative_error', 1e-4),
        ):
            if not math.isfinite(result[key]) or result[key] > limit:
                raise RuntimeError(f'{result["case"]}: {key}={result[key]} exceeds {limit}')


def check_convergence(base, refined):
    keys = ('vout_avg_v', 'vout_pp_v', 'inductor_current_peak_a', 'input_power_avg_w')
    result = {}
    for a, b in zip(base, refined):
        deltas = {key: abs(a[key] - b[key]) / max(abs(b[key]), 1e-12) for key in keys}
        if max(deltas.values()) > .002:
            raise RuntimeError(f'{a["case"]}: step-refinement change exceeds 0.2%: {deltas}')
        result[a['case']] = deltas
    return result


def svg_for(case):
    # Eight cycles, with actual time coordinates and two samples at ESR jumps.
    points = []
    period_us = 1e6 / Model().frequency_hz
    for cycle in range(8):
        points.extend({**point, 'time_us': point['time_us'] + cycle * period_us}
                      for point in case['waveform'])
    def polyline(key, top):
        low, high = min(point[key] for point in points), max(point[key] for point in points)
        padding = max((high - low) * .12, .01)
        low, high = low - padding, high + padding
        coords = ' '.join(
            f'{80 + 1040 * point["time_us"] / (8 * period_us):.2f},'
            f'{top + 180 * (high - point[key]) / (high - low):.2f}'
            for point in points
        )
        return coords, low, high
    voltage, vl, vh = polyline('vout_v', 110)
    current, il, ih = polyline('inductor_a', 370)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="640" viewBox="0 0 1200 640">
<rect width="1200" height="640" fill="#f8fafc"/>
<style>text {{font-family: sans-serif; fill:#17212b}} .plot {{fill:none;stroke-width:1.7}}</style>
<text x="60" y="36" font-size="22">EU-LITE LED: periodic power-stage model, Vin = 24 V</text>
<text x="60" y="64" font-size="14">Ideal PWM; required duty {case["required_duty"] * 100:.2f}%; 400 kHz; controller dynamics excluded</text>
<path d="M80 100V290H1120 M80 360V550H1120" fill="none" stroke="#64748b"/>
<polyline class="plot" stroke="#0369a1" points="{voltage}"/>
<polyline class="plot" stroke="#b45309" points="{current}"/>
<text x="80" y="98" font-size="16">V(out) before R502: {vl:.3f} ... {vh:.3f} V</text>
<text x="80" y="358" font-size="16">I(L501): {il:.3f} ... {ih:.3f} A</text>
<text x="80" y="590" font-size="16">Time: 0 ... {8 * period_us:.1f} us</text>
</svg>
'''


def markdown_for(report):
    rows = '\n'.join(
        f'| {case["case"]} | {case["vin_v"]:.1f} | {case["required_duty"] * 100:.2f} | '
        f'{case["led_current_avg_a"]:.3f} | {case["led_power_avg_w"]:.1f} | '
        f'{case["inductor_current_peak_a"]:.3f} | {case["is_peak_v"] * 1000:.1f} | '
        f'{case["vout_pp_v"] * 1000:.0f} |'
        for case in report['cases']
    )
    worst = max(case['inductor_current_peak_a'] for case in report['cases'])
    return f'''# LED boost — периодическая модель силового каскада

Источник: `design.json` SHA-256 `{report["design_sha256"][:12]}`.
Расчёт: `tools/led_boost_simulation.py`; стандартная библиотека Python.

| Режим | Vin, В | Требуемый duty, % | Заданный ILED, А | PLED, Вт | Пик L501, А | Пик IS, мВ | Vout p-p, мВ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{rows}

Нагрузка соответствует 48 В на LED после R502. Установившийся duty подобран
к заданному току; совпадение ILED с уставкой является условием расчёта и
**не подтверждает работу реального регулятора**. Пики L501 рассчитаны без
ограничителя тока. Наибольший пик модели {worst:.3f} А сравнивается со
справочным минимумом IS {IS_LIMIT_MIN_A:.3f} А и не заменяет более
консервативный расчёт `verify.py` с КПД 90% и допусками.

## Метод и численная проверка

Два состояния: ток L501 и напряжение идеальной части выходного конденсатора.
Отдельно интегрируются фазы включённого ключа и проводящего диода (RK4);
периодическое состояние решается из линейного отображения полного периода.
Петля COMP, алгоритм PRO и защитные компараторы LM3429 не моделируются.

Проверены непрерывный ток (CCM), возврат состояния за период, баланс
`Pin = PLED + потери модели`, точность заданной рабочей точки и уменьшение
шага примерно с 25 до 12,5 нс. Для всех четырёх режимов изменение Vout,
пульсаций, пикового тока и входной мощности при уточнении шага меньше 0,2%.
Основной отчёт использует уточнённый шаг; подробные ошибки есть в JSON.

## Принятые параметры

- Частота 283,45 кГц — номинал RCT 82 кОм + 6,2 кОм / 1 нФ по формуле 6 LM3429;
  сама цепь RCT не симулируется, разброс таймера и RC не учтён.
- L501 43,5 мкГн, DCR 30 мОм (DCR принят, физически не измерен).
- Q501: 20,8 мОм при 4,5 В из таблицы проекта; температурный рост,
  заряд затвора и потери переключения не включены.
- D501: приближение 0,60 В + 20 мОм.
- R501: 40,4 мОм (+1%). R502: 150 мОм; в углу верхнего тока — 148,5 мОм.
- C508/C509…C515: 498,3 мкФ, суммарный условный ESR 60 мОм;
  DC-bias керамики и отдельные ветви ESR/ESL не характеризованы.
- LED — один резистивный эквивалент `48 В / заданный ток`, после R502.
  Все J501/J502/J503 представлены одной суммарной нагрузкой.
- Верхний ток получен из VCSH 1,26 В и допусков 1% R503/R505/R506/R502.
  Справочные IS 215 мВ и OVP 1,24 В взяты из [TI LM3429 Rev. H]({SOURCE}).
  Номинальный порог OVP делителя: {OVP_V:.2f} В; допуски OVP здесь не исследованы.

## Практический предел результата

Это начальная модель силового каскада с идеальным PWM, без SPICE-макромоделей
производителей. Она даёт ток и пульсации в заданной рабочей точке.
Пуск, минимальная яркость nDIM, устойчивость COMP, работа OVP/КЗ, EMI,
паразитные выбросы, нагрев и деление тока LED-ветвей остаются стендовыми
вопросами. КПД модели нельзя использовать как прогноз КПД платы:
потери переключения и сердечника отсутствуют.

Повторить расчёт и проверить актуальность результатов:

```powershell
& 'C:\\Program Files\\KiCad\\10.0\\bin\\python.exe' hardware/PCB_EU_LITE/tools/led_boost_simulation.py --check
```

![Установившиеся формы 24 В](led_boost_simulation.svg)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='verify generated artifacts')
    args = parser.parse_args()
    require_design_values()
    coarse, refined = [], []
    for name, vin, target, shunt in CASES:
        model = replace(Model(), output_shunt_ohm=shunt)
        coarse.append(simulate_case(name, vin, target, model, steps=100))
        refined.append(simulate_case(name, vin, target, model, steps=200))
    validate_numerics(coarse + refined)
    convergence = check_convergence(coarse, refined)
    report = {
        'simulation_kind': 'periodic CCM power stage with fitted ideal PWM',
        'design_sha256': hashlib.sha256(DESIGN.read_bytes()).hexdigest(),
        'assumptions': vars(Model()),
        'reference_is_limit_min_a': IS_LIMIT_MIN_A,
        'reference_ovp_v': OVP_V,
        'source': SOURCE,
        'limitations': [
            'Duty fitted to prescribed current; regulation is an input assumption.',
            'No LM3429 PRO/COMP, startup, nDIM or protection simulation.',
            'Resistive LED equivalent; no branch-sharing or thermal prediction.',
            'Switching/core losses, parasitics and capacitor bias are excluded.',
        ],
        'step_refinement_relative_change': convergence,
        'cases': refined,
    }
    files = {
        OUTPUT / 'led_boost_simulation.json': json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        OUTPUT / 'LED_boost_simulation.md': markdown_for(report),
        OUTPUT / 'led_boost_simulation.svg': svg_for(refined[1]),
    }
    for path, content in files.items():
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != content:
                raise RuntimeError(f'stale simulation artifact: {path}')
        else:
            path.write_text(content, encoding='utf-8', newline='\n')
    print('PASS | LED power-stage model: periodic state, energy balance, step refinement')
    for result in refined:
        print(f'{result["case"]}: target {result["target_a"]:.3f} A, '
              f'L501 peak {result["inductor_current_peak_a"]:.3f} A, '
              f'duty {100 * result["required_duty"]:.2f}%')


if __name__ == '__main__':
    main()
