"""Read HMD yaw from the katwalk OpenXR layer via shared memory (SteamVR-free).

Drop-in replacement for the old OpenVR-based reader: the OpenXR API layer publishes the HMD
pose + yaw into /tmp/katwalk/poses every frame while an OpenXR game runs (any runtime - WiVRn,
Monado, SteamVR's OpenXR). Yaw uses the same atan2(R02, R22) convention the OpenVR reader had,
so joystick_angle = body_direction - hmd_yaw keeps working unchanged.

Degrades gracefully: no game running -> no shm updates -> poll() returns the last value (or
None), and the daemon falls back to body-heading, exactly like when SteamVR was absent before.
Struct layout mirrors openxr-driver/src/katwalk_shm.h (KatPoses) - keep in sync.
"""

from __future__ import annotations

import mmap
import os
import struct
import time

PATH = "/tmp/katwalk/poses"
# struct KatPoses: seq, hmd_valid, hmd[12], lctrl_valid, lctrl[12], rctrl_valid, rctrl[12],
#                  hmd_yaw_deg, recenter_seq  (all 4-byte fields, no padding)
POSES = struct.Struct("<II12fI12fI12ffI")
STALE_S = 2.0  # no seq change for this long -> treat the value as gone (game closed)
RETRY_S = 3.0  # throttle mmap (re)attach attempts


class HmdYaw:
    """Same interface as the retired OpenVR reader: start() -> bool, poll() -> deg|None."""

    def __init__(self):
        self._mm = None
        self._yaw = None
        self._last_seq = None
        self._last_change = 0.0
        self._next_try = 0.0
        self.ok = False

    def _attach(self) -> bool:
        try:
            fd = os.open(PATH, os.O_RDONLY)
            self._mm = mmap.mmap(fd, POSES.size, prot=mmap.PROT_READ)
            os.close(fd)
            self.ok = True
        except (OSError, ValueError):
            self._mm = None
            self.ok = False
        return self.ok

    def start(self) -> bool:
        return self._attach()

    def poll(self):
        """Return current HMD yaw in degrees, or the last value / None. Re-attaches on its own
        when the layer (re)creates the shm - like the old reader reconnecting to SteamVR.
        """
        now = time.monotonic()
        if self._mm is None:
            if now >= self._next_try:
                self._next_try = now + RETRY_S
                if self._attach():
                    print("HMD yaw connected (OpenXR layer shm)")
            return self._yaw
        try:
            vals = POSES.unpack(self._mm[:])
        except (ValueError, struct.error):
            self._drop()
            return self._yaw
        seq, hmd_valid, yaw = vals[0], vals[1], vals[-2]
        if seq != self._last_seq:
            self._last_seq = seq
            self._last_change = now
            if hmd_valid:
                self._yaw = yaw
        elif now - self._last_change > STALE_S:
            self._yaw = None  # game gone; body-heading fallback until frames resume
        return self._yaw

    def _drop(self) -> None:
        try:
            if self._mm is not None:
                self._mm.close()
        except (OSError, ValueError):
            pass
        self._mm = None
        self.ok = False
        self._next_try = time.monotonic() + RETRY_S

    def stop(self) -> None:
        self._drop()
