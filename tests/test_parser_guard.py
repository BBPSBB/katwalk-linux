"""Regression test for the foot-frame mode guard (katwalk/core/parser.py).

A shoe that wakes in the wrong mode streams its IMU orientation quaternion on the foot channel
instead of an optical position; its bytes decode to a bogus stuck position (-1, 255) that injects
phantom forward motion. The parser must only trust a foot frame as a position when off7 == 0x01.

Frames below are REAL captures (2026-07-05): the broken left shoe (orientation mode) and, after a
clean sleep+wake recovery, the same shoe back in position mode.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from katwalk.core.parser import parse  # noqa: E402

# left shoe, mis-moded (off7 = 0x31, IMU quaternion, optical field = 0xffffff)
BROKEN_LEFT = bytes.fromhex(
    "1f55aa0000300131ec0fd82c2a2b12000000000000ffffff0000000000000000"
)
# left shoe, healthy position frame (off7 = 0x01), standing still -> (0, 0)
GOOD_LEFT = bytes.fromhex(
    "1f55aa0000300101000063f91d05ffffffffffffff00000000001c0000000000"
)
# a moving position frame from the old good capture (off7 = 0x01, non-zero slip)
GOOD_MOVING = bytes.fromhex(
    "1f55aa0000300101000045c85705ffffffffffffff0000000000440000000000"
)


class FootModeGuard(unittest.TestCase):
    def test_mismoded_frame_reports_no_position(self):
        f = parse(BROKEN_LEFT)
        self.assertEqual(
            f.kind, "foot_left"
        )  # still recognised as a left-foot frame...
        self.assertIsNone(
            f.foot
        )  # ...but NOT decoded as a position (would be a bogus -1,255)

    def test_valid_idle_frame_decodes_zero(self):
        f = parse(GOOD_LEFT)
        self.assertEqual(f.kind, "foot_left")
        self.assertEqual(f.foot, (0, 0))

    def test_valid_moving_frame_decodes_position(self):
        f = parse(GOOD_MOVING)
        self.assertEqual(f.kind, "foot_left")
        self.assertIsNotNone(f.foot)


if __name__ == "__main__":
    unittest.main()
