"""Tests for the overlay's rigid-transform math + placement persistence
(katwalk/overlay.py). These back the grab-to-move feature - if mat_mul/mat_inv are
wrong, the panel would teleport when grabbed or released.

Run:  .venv/bin/python -m unittest discover -s tests
"""

import importlib.util
import math
import os
import tempfile
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
IDENTITY = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0]]


def rot_y(deg, t=(0.0, 0.0, 0.0)):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [[c, 0.0, s, t[0]], [0.0, 1.0, 0.0, t[1]], [-s, 0.0, c, t[2]]]


def assertClose(test, a, b, tol=1e-9):
    for i in range(3):
        for j in range(4):
            test.assertAlmostEqual(a[i][j], b[i][j], delta=tol)


class MatrixTests(unittest.TestCase):
    def test_mul_identity(self):
        m = rot_y(33.0, (0.1, -0.2, 0.3))
        assertClose(self, vo.mat_mul(IDENTITY, m), m)
        assertClose(self, vo.mat_mul(m, IDENTITY), m)

    def test_inv_roundtrips_to_identity(self):
        m = rot_y(57.0, (0.4, 1.2, -0.7))
        assertClose(self, vo.mat_mul(vo.mat_inv(m), m), IDENTITY)
        assertClose(self, vo.mat_mul(m, vo.mat_inv(m)), IDENTITY)

    def test_grab_release_roundtrip_preserves_world_pose(self):
        # the exact sequence the loop does: anchor relative to host, grab relative to grabber,
        # release back to host. The recovered anchor must equal the original (no teleport).
        host = rot_y(20.0, (1.0, 1.5, -0.5))
        grab = rot_y(-40.0, (0.3, 1.1, -0.9))
        anchor = rot_y(90.0, (0.0, 0.02, 0.10))  # overlay relative to host
        world = vo.mat_mul(host, anchor)  # overlay in world space
        grab_off = vo.mat_mul(vo.mat_inv(grab), world)  # capture at grab start
        world2 = vo.mat_mul(grab, grab_off)  # follow grabber, then release here
        anchor2 = vo.mat_mul(vo.mat_inv(host), world2)  # recompute host-relative
        assertClose(self, anchor2, anchor, tol=1e-9)

    def test_dist_is_translation_distance(self):
        a = rot_y(10.0, (0.0, 0.0, 0.0))
        b = rot_y(80.0, (3.0, 4.0, 0.0))  # rotation must not affect distance
        self.assertAlmostEqual(vo.mat_dist(a, b), 5.0, places=9)

    def test_from_pose_reads_matrix(self):
        import openvr

        m = openvr.HmdMatrix34_t()
        for i in range(3):
            for j in range(4):
                m.m[i][j] = float(i * 4 + j)
        got = vo.mat_from_pose(m)
        self.assertEqual(got, [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]])


class OffsetPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._old = vo.CONFIG_PATH
        self.tmp = tempfile.mkdtemp()
        vo.CONFIG_PATH = os.path.join(self.tmp, "katwalk", "overlay.json")

    def tearDown(self):
        vo.CONFIG_PATH = self._old

    def test_default_offset_shape(self):
        d = vo.default_offset()
        self.assertTrue(vo._valid_offset(d))

    def test_missing_file_returns_default(self):
        self.assertEqual(vo.load_offset("left"), vo.default_offset())

    def test_save_then_load_roundtrip(self):
        off = rot_y(45.0, (0.1, 0.2, 0.3))
        vo.save_offset("left", off)
        assertClose(self, vo.load_offset("left"), off)

    def test_hand_mismatch_falls_back_to_default(self):
        vo.save_offset("right", rot_y(12.0, (0.5, 0.0, 0.0)))
        self.assertEqual(vo.load_offset("left"), vo.default_offset())

    def test_corrupt_offset_falls_back(self):
        os.makedirs(os.path.dirname(vo.CONFIG_PATH), exist_ok=True)
        with open(vo.CONFIG_PATH, "w") as fh:
            fh.write('{"hand":"left","offset":"garbage"}')
        self.assertEqual(vo.load_offset("left"), vo.default_offset())


if __name__ == "__main__":
    unittest.main()
