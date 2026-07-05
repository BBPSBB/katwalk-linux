"""Tests for the locomotion math (katwalk/fusion.py + katwalk/locomotion.py).

Locks in the body-relative thumbstick redesign:
  * forward foot-slip -> stick UP, backward -> DOWN
  * the X (strafe) sign is un-mirrored and symmetric
  * the move direction does NOT include the body heading (a thumbstick is already
    head-relative in-game) - the regression that "borked it" when heading was added.

Run:  .venv/bin/python -m unittest discover -s tests
"""

import inspect
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from katwalk.core.fusion import stick_from_state  # noqa: E402
from katwalk.core.locomotion import LocomotionModel  # noqa: E402


class StickFromStateTests(unittest.TestCase):
    def test_zero_speed_is_zero_stick(self):
        self.assertEqual(stick_from_state(0.0, 0.0), (0.0, 0.0))

    def test_forward_is_up(self):
        x, y = stick_from_state(0.0, 8.0)
        self.assertAlmostEqual(x, 0.0, places=6)
        self.assertGreater(y, 0.9)

    def test_backward_is_down(self):
        x, y = stick_from_state(180.0, 8.0)
        self.assertAlmostEqual(x, 0.0, places=6)
        self.assertLess(y, -0.9)

    def test_strafe_directions_have_opposite_sign(self):
        xr, _ = stick_from_state(90.0, 8.0)
        xl, _ = stick_from_state(-90.0, 8.0)
        self.assertGreater(xr, 0.9)
        self.assertLess(xl, -0.9)

    def test_magnitude_clamped_to_unit(self):
        x, y = stick_from_state(37.0, 9999.0)
        self.assertLessEqual(math.hypot(x, y), 1.0 + 1e-9)

    def test_magnitude_scales_with_speed(self):
        _, y = stick_from_state(0.0, 4.0, max_speed=8.0)
        self.assertAlmostEqual(y, 0.5, places=6)

    def test_signature_has_hmd_yaw(self):
        # body-relative locomotion: stick subtracts HMD yaw so movement follows the body
        params = list(inspect.signature(stick_from_state).parameters)
        self.assertEqual(params, ["direction_deg", "speed", "hmd_yaw_deg", "max_speed"])

    def test_hmd_yaw_compensates_so_movement_follows_body(self):
        # world dir = forward (0). Look 90° right (hmd_yaw=90): the stick must rotate the OTHER
        # way so the avatar still travels world-forward (i.e. along the body), not where you look.
        x0, y0 = stick_from_state(0.0, 8.0, 0.0)
        x90, y90 = stick_from_state(0.0, 8.0, 90.0)
        self.assertGreater(y0, 0.9)  # looking forward -> stick up
        self.assertLess(x90, -0.9)  # looking right -> stick left (compensates)
        self.assertAlmostEqual(y90, 0.0, places=6)


def _quat_for_heading(deg):
    """Quaternion whose KAT yaw decode (heading_deg) == deg: a rotation about the yaw (Y) axis."""
    h = math.radians(deg) / 2.0
    return (math.cos(h), 0.0, math.sin(h), 0.0)


class ModelDirectionTests(unittest.TestCase):
    IDENTITY = (1.0, 0.0, 0.0, 0.0)

    def _step(self, model, quat, foot):
        return model.update(quat, foot, (0, 0), ground_left=True, ground_right=False)

    def test_forward_slip_gives_zero_offset(self):
        m = LocomotionModel()
        self._step(m, self.IDENTITY, (0, 500))
        self.assertAlmostEqual(m.move_offset, 0.0, places=3)

    def test_direction_includes_body_heading(self):
        # body-relative: world move direction = body heading + foot-slip angle.
        m = LocomotionModel()
        self._step(
            m, _quat_for_heading(45.0), (0, 500)
        )  # forward slip -> move_offset ~0
        self.assertNotAlmostEqual(
            m.heading, 0.0, places=1
        )  # heading really is non-zero
        self.assertAlmostEqual(m.move_offset, 0.0, places=3)
        self.assertAlmostEqual(
            m.direction, m.heading, places=6
        )  # direction = heading + offset

    def test_strafe_sign_is_symmetric(self):
        mr = LocomotionModel()
        self._step(mr, self.IDENTITY, (500, 0))
        ml = LocomotionModel()
        self._step(ml, self.IDENTITY, (-500, 0))
        self.assertAlmostEqual(mr.move_offset, -ml.move_offset, places=3)
        self.assertNotAlmostEqual(mr.move_offset, 0.0, places=1)

    def test_deadzone_below_threshold_holds_direction(self):
        m = LocomotionModel()
        # tiny slip under the default deadzone (30) must not redefine direction
        self._step(m, self.IDENTITY, (5, 5))
        self.assertEqual(m.move_offset, 0.0)


class CadenceTests(unittest.TestCase):
    """Cadence must count ALTERNATING footfalls and measure the real interval - no fake floor."""

    QUAT = (1.0, 0.0, 0.0, 0.0)

    def setUp(self):
        import katwalk.core.locomotion as loco

        self.loco = loco
        self._orig = loco.time.monotonic
        self.t = [100.0]
        loco.time.monotonic = lambda: self.t[0]
        self.m = LocomotionModel()

    def tearDown(self):
        self.loco.time.monotonic = self._orig

    def _footfall(self, foot):
        # airborne (both up) then land `foot` -> one airborne->grounded transition at time t
        self.m.update(self.QUAT, (0, 0), (0, 0), ground_left=False, ground_right=False)
        gl, gr = (foot == "L"), (foot == "R")
        self.m.update(
            self.QUAT,
            (0, 400) if gl else (0, 0),
            (0, 400) if gr else (0, 0),
            ground_left=gl,
            ground_right=gr,
        )

    def test_single_step_reports_zero_cadence(self):
        self._footfall("L")
        self.assertEqual(round(self.m.cadence * 60), 0)  # one step != a cadence

    def test_alternating_steps_measure_real_rate(self):
        # land L,R,L,R,L,R every 0.5 s -> 2 steps/sec -> ~120 spm
        for foot in ("L", "R") * 6:
            self._footfall(foot)
            self.t[0] += 0.5
        self.assertAlmostEqual(self.m.cadence, 2.0, delta=0.25)
        self.assertAlmostEqual(round(self.m.cadence * 60), 120, delta=15)

    def test_same_foot_repeats_do_not_count(self):
        self._footfall("L")
        steps_after_first = self.m.steps
        for _ in range(5):  # same foot again = noise
            self.t[0] += 0.3
            self._footfall("L")
        self.assertEqual(self.m.steps, steps_after_first)  # no extra steps counted
        self.assertEqual(round(self.m.cadence * 60), 0)

    def test_cadence_decays_after_stopping(self):
        for foot in ("L", "R") * 4:
            self._footfall(foot)
            self.t[0] += 0.5
        self.assertGreater(self.m.cadence, 1.0)
        self.t[0] += 5.0  # stop for 5 s
        for _ in range(20):  # many loop frames -> decays away
            self.m.update(self.QUAT, (0, 0), (0, 0))
        self.assertLess(self.m.cadence, 0.5)


class SensitivityAndModeTests(unittest.TestCase):
    """Stolen from KAT: per-direction sensitivity (Forward/Lateral/Back) + Linear/Constant mode.
    Defaults (gains 1.0, linear) must be a no-op vs the calibrated model."""

    IDENTITY = (1.0, 0.0, 0.0, 0.0)

    def _run(self, model, foot, n=120):
        s = {}
        for _ in range(n):  # let the speed ramp converge to steady state
            s = model.update(
                self.IDENTITY, foot, (0, 0), ground_left=True, ground_right=False
            )
        return s

    def test_defaults_are_identity(self):
        m = LocomotionModel()
        s = self._run(m, (0, 600))  # forward slip
        self.assertAlmostEqual(s["speed"], round(m.speed, 2), places=2)
        self.assertAlmostEqual(m.move_offset, 0.0, places=3)

    def test_forward_sens_scales_forward_speed(self):
        a = LocomotionModel()
        b = LocomotionModel()
        b.set("forward_sens", 2.0)
        self.assertAlmostEqual(
            self._run(b, (0, 600))["speed"] / self._run(a, (0, 600))["speed"],
            2.0,
            delta=0.05,
        )

    def test_lateral_sens_scales_strafe(self):
        a = LocomotionModel()
        b = LocomotionModel()
        b.set("lateral_sens", 0.5)
        self.assertAlmostEqual(
            self._run(b, (600, 0))["speed"] / self._run(a, (600, 0))["speed"],
            0.5,
            delta=0.05,
        )

    def test_back_sens_scales_backward(self):
        a = LocomotionModel()
        b = LocomotionModel()
        b.set("back_sens", 0.5)
        self.assertAlmostEqual(
            self._run(b, (0, -600))["speed"] / self._run(a, (0, -600))["speed"],
            0.5,
            delta=0.05,
        )

    def test_forward_sens_does_not_touch_strafe(self):
        a = LocomotionModel()
        b = LocomotionModel()
        b.set("forward_sens", 3.0)
        self.assertAlmostEqual(
            self._run(b, (600, 0))["speed"], self._run(a, (600, 0))["speed"], delta=0.05
        )

    def test_constant_mode_is_fixed_regardless_of_slip(self):
        small = LocomotionModel()
        big = LocomotionModel()
        for m in (small, big):
            m.set("walk_speed_type", 1.0)
            m.set("constant_speed", 4.0)
        self.assertAlmostEqual(
            self._run(small, (0, 120))["speed"], 4.0, delta=0.2
        )  # tiny slip
        self.assertAlmostEqual(
            self._run(big, (0, 2000))["speed"], 4.0, delta=0.2
        )  # huge slip


class CruiseTests(unittest.TestCase):
    """Stolen from KAT's CCS: sustained forward walking latches an auto-walk; back-step releases."""

    IDENTITY = (1.0, 0.0, 0.0, 0.0)

    def setUp(self):
        import katwalk.core.locomotion as loco

        self.loco = loco
        self._orig = loco.time.monotonic
        self.t = [0.0]
        loco.time.monotonic = lambda: self.t[0]
        self.m = LocomotionModel()
        self.m.set("cruise_enabled", 1.0)
        self.m.set("cruise_speed", 3.5)
        self.m.set("cruise_engage_s", 1.0)

    def tearDown(self):
        self.loco.time.monotonic = self._orig

    def _tick(self, foot, dt=0.1, gl=True):
        s = self.m.update(
            self.IDENTITY, foot, (0, 0), ground_left=gl, ground_right=False
        )
        self.t[0] += dt
        return s

    def _walk_forward(self, n=20):
        for _ in range(n):
            self._tick((0, 600))

    def _coast(self, n=20):
        s = {}
        for _ in range(n):
            s = self._tick((0, 0), gl=False)
        return s

    def test_engages_then_sustains_when_feet_idle(self):
        self._walk_forward()  # > engage_s of forward walking
        s = self._coast()  # stop stepping -> cruise should hold
        self.assertGreaterEqual(s["speed"], 3.4)
        self.assertTrue(s["moving"])

    def test_backstep_releases(self):
        self._walk_forward()
        self._tick((0, -600))  # deliberate back-step
        s = self._coast()
        self.assertLess(s["speed"], 0.5)

    def test_disabled_does_not_sustain(self):
        self.m.set("cruise_enabled", 0.0)
        self._walk_forward()
        s = self._coast()
        self.assertLess(s["speed"], 0.5)


if __name__ == "__main__":
    unittest.main()
