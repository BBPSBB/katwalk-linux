"""Tests for the in-VR overlay HUD (katwalk/overlay.py).

Covers rendering for every state and the demo-mode animation (the renderer is pure
Pillow - the OpenXR layer handles display/anchoring, see katwalk/overlay_xr.py).

Run:  .venv/bin/python -m unittest discover -s tests
"""

import importlib.util
import os
import unittest
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(rel, name) -> Any:
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vo = _load("katwalk/overlay.py", "overlay")

WALK = {
    "connected": True,
    "speed": 3.4,
    "moving": True,
    "sprinting": False,
    "cadence_spm": 94,
    "heading": 138,
    "battery": 78,
    "hr": 76,
    "params": {"sprint_threshold": 6.0},
}
SPRINT = {**WALK, "speed": 7.2, "sprinting": True, "cadence_spm": 168}
IDLE = {**WALK, "speed": 0.0, "moving": False, "sprinting": False, "cadence_spm": 0}
DISC = {"connected": False, "status": "device asleep"}


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.fonts = vo.load_fonts()

    def test_render_size_and_mode_all_states(self):
        for st in (WALK, SPRINT, IDLE, DISC):
            im = vo.render(st, self.fonts)
            self.assertEqual(im.size, (vo.W, vo.H))
            self.assertEqual(im.mode, "RGBA")

    def test_render_flash_toast(self):
        im = vo.render(WALK, self.fonts, flash=True)
        self.assertEqual(im.size, (vo.W, vo.H))

    def test_render_handles_missing_optional_fields(self):
        # heading/battery/profile/params absent must not raise
        im = vo.render({"connected": True, "speed": 1.0, "moving": True}, self.fonts)
        self.assertEqual(im.size, (vo.W, vo.H))

    def test_tobytes_length(self):
        self.assertEqual(len(vo.render(WALK, self.fonts).tobytes()), vo.W * vo.H * 4)


class DemoStateTests(unittest.TestCase):
    KEYS = {
        "connected",
        "speed",
        "moving",
        "sprinting",
        "cadence_spm",
        "heading",
        "battery",
        "params",
        "recenter_seq",
        "hr",
    }

    def test_keys_types_and_invariants_over_time(self):
        for i in range(0, 90):  # 0..45s in 0.5s steps
            s = vo.demo_state(i * 0.5)
            self.assertLessEqual(self.KEYS, set(s))
            self.assertIsInstance(s["recenter_seq"], int)
            self.assertTrue(s["connected"])
            self.assertGreaterEqual(s["speed"], 0.0)
            self.assertTrue(0.0 <= s["heading"] < 360.0)
            self.assertEqual(s["moving"], s["speed"] > 0.1)
            self.assertEqual(s["sprinting"], s["speed"] >= 6.0)
            if s["sprinting"]:
                self.assertTrue(s["moving"])  # sprint implies moving

    def test_cycle_reaches_idle_walk_and_sprint(self):
        seen = set()
        for i in range(0, 88):  # one full 22s loop
            s = vo.demo_state(i * 0.25)
            seen.add(
                "SPRINT" if s["sprinting"] else ("WALK" if s["moving"] else "IDLE")
            )
        self.assertEqual(seen, {"IDLE", "WALK", "SPRINT"})

    def test_recenter_seq_is_monotonic_nondecreasing(self):
        prev = None
        for i in range(0, 120):
            seq = vo.demo_state(i * 0.5)["recenter_seq"]
            if prev is not None:
                self.assertGreaterEqual(seq, prev)
            prev = seq

    def test_recenter_seq_actually_advances(self):
        # the toast only fires when the counter changes - make sure it does within a loop
        first = vo.demo_state(0.0)["recenter_seq"]
        later = vo.demo_state(25.0)["recenter_seq"]
        self.assertGreater(later, first)

    def test_demo_state_renders(self):
        fonts = vo.load_fonts()
        im = vo.render(vo.demo_state(8.0), fonts)
        self.assertEqual(im.size, (vo.W, vo.H))


if __name__ == "__main__":
    unittest.main()
