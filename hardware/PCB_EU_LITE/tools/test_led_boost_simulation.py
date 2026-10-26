#!/usr/bin/env python3
"""Independent physical checks of the LED power-stage solver.

Run with Python's unittest runner; no KiCad or third-party modules required.
SPDX-License-Identifier: MIT
"""
from dataclasses import replace
import unittest

import led_boost_simulation as sim


class BoostSolverTests(unittest.TestCase):
    def test_lossless_boost_matches_ideal_conversion(self):
        model = replace(
            sim.Model(), inductor_dcr_ohm=0, switch_ron_ohm=0,
            sense_ohm=0, diode_vf_v=0, diode_rd_ohm=0,
            output_esr_ohm=0, output_shunt_ohm=0,
        )
        case = sim.simulate_case('ideal', 24, sim.TARGET_A, model)
        # For an ideal 24->48 V boost in CCM, D=1-Vin/Vout=0.5.
        self.assertAlmostEqual(case['required_duty'], .5, delta=1e-5)
        self.assertAlmostEqual(case['vout_avg_v'], 48, delta=.001)
        self.assertAlmostEqual(case['input_power_avg_w'],
                               case['led_power_avg_w'], delta=.001)
        # Independent inductor ripple estimate, Vin*D/(L*f).
        expected_ripple = 24 * .5 / (model.inductance_h * model.frequency_hz)
        measured_ripple = case['inductor_current_peak_a'] - case['inductor_current_min_a']
        self.assertAlmostEqual(measured_ripple, expected_ripple, delta=.001)
        sim.validate_numerics([case])

    def test_esr_terminal_voltage_obeys_kirchhoff(self):
        model = sim.Model()
        load_ohm, state = 30.0, (4.0, 48.0)
        for on in (True, False):
            voltage = sim.terminal_voltage(state, on, model, load_ohm)
            cap_a = (0 if on else state[0]) - voltage / load_ohm
            self.assertAlmostEqual(voltage, state[1] + cap_a * model.output_esr_ohm)

    def test_actual_model_balances_energy_without_clipping_current(self):
        case = sim.simulate_case('low_line', 21.6, sim.TARGET_A)
        self.assertGreater(case['model_loss_w'], 0)
        self.assertGreater(case['input_power_avg_w'], case['led_power_avg_w'])
        self.assertGreater(case['inductor_current_min_a'], 0)
        sim.validate_numerics([case])
        # The solver must remain free to predict a design failure. It must not
        # enforce the IS reference limit and then claim headroom by construction.
        overloaded = sim.simulate_case('overload', 21.6, 3.0)
        self.assertLess(overloaded['reference_limit_margin_a'], 0)
        sim.validate_numerics([overloaded])


if __name__ == '__main__':
    unittest.main()
