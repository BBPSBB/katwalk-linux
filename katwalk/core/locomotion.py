"""Locomotion model: foot-slip velocity -> walk/run speed + body-relative direction.

Speed = the stance foot's optical SLIP VELOCITY (how fast it slides on the deck), low-pass
smoothed. It does NOT use step-cadence x gain, and it does NOT scale by height/leg length
(foot-slip already physically encodes stride, so a height term would double-count). Calibrated
against a real walk capture (2026-06-27). Cadence (rate of alternating footfalls) is measured and
reported for the HUD readout only; it feeds neither speed nor run detection (run/sprint is decided
by speed vs sprint_threshold).

Tunables: overall multiplier, per-direction sensitivity (forward/lateral/back), and a
linear-vs-constant speed mode.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass

from .fusion import heading_deg

STEP_TIMEOUT = 1.5  # s without a footfall -> stepping has stopped, cadence decays
CAD_MAX = 4.0  # steps/sec upper clamp (~240 spm); no lower floor (measure real rate)
STEP_MIN_DT = (
    0.15  # ignore alternating footfalls closer than this (sensor bounce/noise)
)
DIR_TRUST = 1.8  # only RE-READ the move direction when slip exceeds deadzone*this - the
# slip ANGLE is pure noise at low magnitude (the "random directions at
# onset" bug), so hold the last direction until the slide is unambiguous.


@dataclass
class Params:
    speed_multiplier: float = 1.0  # overall speed scale (default 1.0)
    deadzone: float = 60.0  # slip counts below this = not moving. Raised from 30:
    # low slip is noisy at onset. Tunable in the overlay.
    accuracy: float = 0.35  # slip smoothing 0(smooth)..1(responsive)
    player_height: float = (
        1.78  # metres. NOT used for speed: walk speed is driven by foot-slip,
    )
    # which already encodes stride length, so a height term would
    # double-count. Kept for body IK.
    # Per-direction gains (forward / lateral / back). 1.0 = no change. Lets you damp the
    # harder-to-control strafe / back-step independently of forward speed.
    forward_sens: float = 1.0
    lateral_sens: float = 1.0
    back_sens: float = 1.0
    # Speed mode. <0.5 = linear (speed ~ slip velocity);
    # >=0.5 = constant (a fixed pace whenever stepping, regardless of effort).
    walk_speed_type: float = 0.0
    constant_speed: float = 4.0  # km/h used when walk_speed_type selects constant mode
    # Cruise - optional auto-walk, OFF by default. Sustained forward walking latches a steady
    # cruise speed so long treks don't need constant stepping; a deliberate back-step releases it.
    # (Engage time is exposed as a param.)
    cruise_enabled: float = 0.0  # >=0.5 enables cruise latching
    cruise_speed: float = 3.5  # km/h sustained while cruising
    cruise_engage_s: float = (
        1.0  # seconds of continuous forward walking to latch cruise
    )
    sprint_threshold: float = (
        3.8  # km/h above which = running (75% of the 5 km/h full-stick ceiling)
    )
    speed_scale: float = (
        1.0  # km/h per unit normalized foot-slip velocity. Replaying the
    )
    # 2026-06-27 walk capture: fastest walk (~5 km/h, user-confirmed)
    # maps to full stick; slow≈1.5, med≈3 (linear mode).
    base_scale: float = 1.0 / 350.0  # raw slip counts -> normalized slip velocity
    accel_up: float = 0.15  # km/h per frame ramp up (~0.2s to walk speed)
    accel_down: float = 0.30  # decel responsiveness (quick stop)
    cadence_gain: float = (
        1.5  # (legacy, unused) - cadence feeds nothing; it is a HUD readout only for now.
    )
    min_cadence: float = 1.4  # (legacy, unused)


class LocomotionModel:
    def __init__(self, params: Params | None = None):
        self.p = params or Params()
        self.heading = 0.0
        self.speed = 0.0  # km/h-ish output
        self.direction = 0.0
        self.move_offset = 0.0  # move dir relative to heading, from the foot-slip angle
        self.backward = False
        self.amp = 0.0  # smoothed slip amplitude (move gate)
        self.cadence = 0.0  # steps/sec (measured rate of ALTERNATING footfalls)
        self.steps = 0
        self._last_step = None  # time of the last counted (alternating) footfall
        self._last_foot = None  # which foot took the last counted step ('L'/'R')
        self._pg_l = self._pg_r = False
        self._cruise_on = False  # cruise currently latched
        self._cruise_since = (
            None  # when continuous forward walking began (for the engage timer)
        )

    def _update_cruise(self, now, moving) -> bool:
        """Cruise latch: sustained forward walking engages; a back-step releases. Off => never."""
        if self.p.cruise_enabled < 0.5:
            self._cruise_on = False
            self._cruise_since = None
            return False
        cos_mo = math.cos(math.radians(self.move_offset))
        if moving and cos_mo < 0.0:  # deliberate back-step disengages
            self._cruise_on = False
            self._cruise_since = None
        elif moving and cos_mo >= 0.0:  # walking forward -> run the engage timer
            if self._cruise_since is None:
                self._cruise_since = now
            if now - self._cruise_since >= self.p.cruise_engage_s:
                self._cruise_on = True
        # feet idle while engaged: stays latched (that's the auto-walk)
        return self._cruise_on

    def set(self, key: str, value) -> bool:
        if key in Params.__dataclass_fields__:
            setattr(self.p, key, float(value))
            return True
        return False

    @staticmethod
    def _active(fl, fr):
        fl = fl or (0, 0)
        fr = fr or (0, 0)
        return fl if (fl[0] ** 2 + fl[1] ** 2) >= (fr[0] ** 2 + fr[1] ** 2) else fr

    def update(
        self,
        body_quat,
        foot_left,
        foot_right,
        ground_left: bool = False,
        ground_right: bool = False,
    ) -> dict:
        now = time.monotonic()
        if body_quat:
            # Body heading must agree in SIGN with the HMD yaw that fusion.stick_from_state
            # subtracts later - otherwise turning the body rotates movement the WRONG way (and
            # doubles it). Live VR measurement (2026-06-28, DEBUG tab): on a right turn the HMD
            # yaw counts UP while the raw decode counts DOWN - opposite - so negate it here.
            self.heading = -heading_deg(body_quat)

        # A STEP = a foot landing (airborne->grounded) with the OTHER foot than the previous
        # step. Same-foot repeats (sensor bounce/noise) do NOT count - walking alternates feet.
        # Cadence is the measured rate of these alternating footfalls; no assumed floor.
        fell = (
            "L"
            if (ground_left and not self._pg_l)
            else "R" if (ground_right and not self._pg_r) else None
        )
        self._pg_l, self._pg_r = ground_left, ground_right
        if fell is not None and fell != self._last_foot:
            if self._last_step is not None:
                dt = now - self._last_step
                if dt > STEP_MIN_DT:  # interval between alternating steps
                    self.cadence += 0.5 * (min(CAD_MAX, 1.0 / dt) - self.cadence)
            self._last_step = now
            self._last_foot = fell
            self.steps += 1

        # No recent step -> stepping stopped: decay cadence and reset the alternation clock so a
        # fresh start measures cleanly (a single isolated step then reads 0, not a fake number).
        if self._last_step is None or (now - self._last_step) > STEP_TIMEOUT:
            self.cadence *= 0.90
            self._last_step = None
            self._last_foot = None

        # SPEED comes from the grounded (stance) foot's optical SLIP VELOCITY - how fast it
        # slides on the deck IS how fast you're moving. It does NOT use cadence × gain; we
        # low-pass the slip velocity. Cadence is measured separately for the HUD readout only for now.
        # `amp` is the smoothed normalized slip magnitude; it also sets the move gate + the
        # direction sign.
        fx, fy = self._active(
            foot_left if ground_left else None, foot_right if ground_right else None
        )
        mag = math.hypot(fx, fy)
        if mag < self.p.deadzone:
            mag = 0.0
        a = 0.10 + 0.40 * max(0.0, min(1.0, self.p.accuracy))
        self.amp += a * (mag * self.p.base_scale - self.amp)
        moving = self.amp > 0.03

        # SPEED - two modes:
        #   linear   (default): proportional to foot-slip velocity (amp * speed_scale).
        #   constant: a fixed pace whenever you're stepping, regardless of how hard.
        # NO height term (foot-slip already encodes stride).
        if not moving:
            target = 0.0
        elif self.p.walk_speed_type >= 0.5:
            target = self.p.constant_speed * self.p.speed_multiplier
        else:
            target = self.amp * self.p.speed_scale * self.p.speed_multiplier
        if target > self.speed:
            self.speed = min(target, self.speed + self.p.accel_up)  # gentle ramp up
        else:
            self.speed += self.p.accel_down * (target - self.speed)  # quick stop
        if self.speed < 0.02:
            self.speed = 0.0

        # move direction = the foot-slip ANGLE relative to the BODY, so sliding the feet
        # sideways/diagonally walks you that way (omnidirectional), not just fwd/back.
        # +Y slip = forward; -fx so +X (a step to the right) maps to a rightward stick
        # (the raw sensor X is mirrored). The body heading is added below to form the world
        # direction. Hold the last angle while slip is below the trust threshold so direction
        # doesn't snap to 0 between steps.
        if (
            mag >= self.p.deadzone * DIR_TRUST
        ):  # trust the angle only when slip is clear
            self.move_offset = math.degrees(math.atan2(-fx, fy))

        # CRUISE - optional auto-walk; latches a steady forward speed after sustained walking so
        # long treks don't need constant stepping. Off by default; back-step releases.
        if self._update_cruise(now, moving):
            self.speed = max(self.speed, self.p.cruise_speed * self.p.speed_multiplier)
            moving = True

        # PER-DIRECTION SENSITIVITY (separate forward / lateral / back gains). Decompose the
        # body-frame velocity, scale each axis independently, recombine - so e.g. you can damp the
        # harder-to-control strafe/back-step without touching forward speed. With all gains 1.0 this
        # is the identity (out_speed == self.speed, out_offset == move_offset).
        mo = math.radians(self.move_offset)
        cos_mo = math.cos(mo)
        vx = self.speed * math.sin(mo) * self.p.lateral_sens
        vy = (
            self.speed
            * cos_mo
            * (self.p.forward_sens if cos_mo >= 0 else self.p.back_sens)
        )
        out_speed = math.hypot(vx, vy)
        out_offset = (
            math.degrees(math.atan2(vx, vy)) if out_speed > 1e-6 else self.move_offset
        )

        # BODY-relative direction: WORLD move direction = body heading (waist IMU) +
        # foot-slip angle. You travel where your BODY faces and can look around freely - the head
        # yaw is removed later (fusion.stick_from_state) so the head-relative thumbstick still
        # produces body-direction movement.
        self.direction = self.heading + out_offset
        self.backward = abs(((out_offset + 180.0) % 360.0) - 180.0) > 90.0

        sprinting = moving and out_speed >= self.p.sprint_threshold
        return {
            "heading": round(self.heading, 1),
            "direction": round(self.direction, 1),
            "speed": round(out_speed, 2),
            "cadence_spm": round(self.cadence * 60),  # steps per minute
            "moving": moving,
            "sprinting": sprinting,
            "footL": list(foot_left) if foot_left else [0, 0],
            "footR": list(foot_right) if foot_right else [0, 0],
            "params": asdict(self.p),
        }
