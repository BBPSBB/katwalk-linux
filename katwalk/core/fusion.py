"""Heading + head-relative stick vector for locomotion.

Heading comes from the receiver body quaternion (see heading_deg). Verified live
(rotation_test, 2026-06-23) that turning the base sweeps a clean 360°.

stick_from_state turns the locomotion model's (world move direction, speed) into a left-stick
vector. Movement is in your BODY direction (waist IMU), so you can look around while walking -
direction already includes the body heading, and here we subtract the HMD yaw so the
head-relative in-game thumbstick still drives you along your body direction.
"""

from __future__ import annotations

import math


def heading_deg(quat) -> float:
    """Body heading (yaw, degrees) from the receiver quaternion (w,x,y,z).

    Yaw is extracted after an axis remap that matches the waist sensor's mounting, with
    gimbal-lock guards when the waist pitches near ±90°. Verified against live hardware.
    """
    w, x, y, z = quat
    norm = w * w + x * x + y * y + z * z
    test = x * w - y * z  # pitch-singularity term
    if test > 0.4995 * norm:  # waist pitched ~ +90° (folded forward)
        return math.degrees(2.0 * math.atan2(y, x))
    if test < -0.4995 * norm:  # waist pitched ~ -90°
        return math.degrees(-2.0 * math.atan2(y, x))
    return math.degrees(math.atan2(2 * (w * y + x * z), 1 - 2 * (x * x + y * y)))


def angle_confine(deg) -> float:
    """Wrap an angle to (-180, 180], used after a recenter/reset."""
    return (deg + 180.0) % 360.0 - 180.0


def stick_from_state(direction_deg, speed, hmd_yaw_deg=0.0, max_speed=5.0):
    """Left-stick vector (x, y in [-1, 1]). +y = forward, +x = strafe right.

    direction_deg is the WORLD move direction (body heading + foot-slip angle). The in-game
    thumbstick is interpreted relative to the HEAD, so we subtract the HMD yaw: that makes the
    avatar travel along your BODY direction regardless of where you look (body-relative
    locomotion). With head and body aligned this is just 'step forward -> stick up'.

    speed (km/h-ish) is normalized to magnitude by max_speed."""
    mag = 0.0 if max_speed <= 0 else max(0.0, min(1.0, speed / max_speed))
    if mag <= 0.0:
        return (0.0, 0.0)
    rel = math.radians(direction_deg - hmd_yaw_deg)
    return (math.sin(rel) * mag, math.cos(rel) * mag)
