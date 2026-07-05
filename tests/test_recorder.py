"""Tests for the motion recorder in the daemon (katwalk/daemon.py Receiver._rec_write etc).

Verifies the calibration-capture JSONL: correct fields per frame kind, label/timestamp
stamping, that unknown frame kinds are skipped, and that it's a no-op when idle.

Run:  .venv/bin/python -m unittest discover -s tests
"""

import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load(rel, name) -> Any:
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load("katwalk/daemon.py", "daemon")
from katwalk.core.parser import parse, HEADER  # noqa: E402
from katwalk.core.locomotion import LocomotionModel  # noqa: E402


def body_frame(w, x, y, z):
    b = bytearray(32)
    b[0:3] = HEADER
    b[5], b[6] = 0x30, 0x00
    struct.pack_into("<4h", b, 7, w, x, y, z)
    return parse(bytes(b))


def foot_frame(subtype, x, y):
    b = bytearray(32)
    b[0:3] = HEADER
    b[5], b[6] = 0x30, subtype
    b[7] = 0x01  # position-mode marker: a real optical-position frame has off7 == 0x01
    struct.pack_into("<h", b, 21, x)
    struct.pack_into("<h", b, 23, y)
    return parse(bytes(b))


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_dir = T.REC_DIR
        T.REC_DIR = Path(self.tmp)
        self.rcv = T.Receiver(LocomotionModel())

    def tearDown(self):
        if self.rcv._rec_fh is not None:
            self.rcv._rec_fh.close()
        T.REC_DIR = self._old_dir

    def test_idle_is_a_noop(self):
        self.rcv._rec_write(0.0, body_frame(16384, 0, 0, 0))
        self.assertEqual(self.rcv._rec_count, 0)
        self.assertIsNone(self.rcv._rec_path)

    def test_writes_labeled_timestamped_jsonl(self):
        self.rcv._open_rec_file()
        self.rcv._rec_label = "forward_slow"
        self.rcv._rec_t0 = 0.0
        self.rcv._rec_write(0.10, body_frame(16384, 0, 0, 0))
        self.rcv._rec_write(0.12, foot_frame(0x01, -120, 480))
        self.rcv._rec_write(0.14, foot_frame(0x02, 90, -300))
        self.rcv._rec_fh.flush()

        with open(self.rcv._rec_path) as fh:
            lines = [json.loads(ln) for ln in fh]
        self.assertEqual(len(lines), 3)
        self.assertEqual(self.rcv._rec_count, 3)

        self.assertEqual(lines[0]["kind"], "body")
        self.assertIn("quat", lines[0])
        self.assertEqual(lines[1]["kind"], "foot_left")
        self.assertEqual((lines[1]["x"], lines[1]["y"]), (-120, 480))
        self.assertEqual(lines[2]["kind"], "foot_right")
        self.assertEqual((lines[2]["x"], lines[2]["y"]), (90, -300))
        for ln in lines:
            self.assertEqual(ln["label"], "forward_slow")
            self.assertIn("raw", ln)  # raw hex retained for re-decoding
            self.assertIn("t", ln)

    def test_skips_unknown_frame_kinds(self):
        self.rcv._open_rec_file()
        self.rcv._rec_label = "x"
        self.rcv._rec_t0 = 0.0
        bogus = bytearray(32)
        bogus[0:3] = HEADER
        bogus[5] = 0x99  # unknown type -> kind "other", not recorded
        self.rcv._rec_write(0.0, parse(bytes(bogus)))
        self.assertEqual(self.rcv._rec_count, 0)

    def test_rec_status_reflects_state(self):
        st = self.rcv.rec_status()
        self.assertFalse(st["recording"])
        self.assertIsNone(st["label"])

        self.rcv._open_rec_file()
        self.rcv._rec_label = "turn_left"
        st = self.rcv.rec_status()
        self.assertTrue(st["recording"])
        self.assertEqual(st["label"], "turn_left")
        self.assertTrue(st["file"].endswith(".jsonl"))

    def test_new_file_lands_in_rec_dir(self):
        self.rcv._open_rec_file()
        self.assertTrue(self.rcv._rec_path.startswith(self.tmp))
        self.assertTrue(os.path.exists(self.rcv._rec_path))


if __name__ == "__main__":
    unittest.main()
