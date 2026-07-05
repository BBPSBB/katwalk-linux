"""Read HMD yaw from a running SteamVR via OpenVR as a Background app.

Makes locomotion head-relative: with the game set to head-oriented locomotion, feeding
joystick_angle = body_direction - hmd_yaw moves you in your BODY direction while you look
around freely.

Needs the openvr bindings (.venv) + SteamVR running. Degrades gracefully - if SteamVR
isn't up or openvr is missing, start() returns False and the daemon uses body-heading only.
"""

from __future__ import annotations

import math
import time

RETRY_S = (
    3.0  # throttle (re)connect attempts to SteamVR to once every this many seconds
)


class HmdYaw:
    def __init__(self):
        self._ovr = None
        self._vr = None
        self._yaw = None
        self.ok = False
        self._next_try = 0.0  # monotonic time of the next (re)connect attempt

    def _connect(self) -> bool:
        try:
            import openvr

            self._ovr = openvr
            self._vr = openvr.init(openvr.VRApplication_Background)
            self.ok = True
        except Exception:
            self._vr = None
            self.ok = False
        return self.ok

    def _drop(self) -> None:
        """Tear down a dead session so poll() reconnects (SteamVR restarted)."""
        if self._ovr is not None:
            try:
                self._ovr.shutdown()
            except Exception:
                pass
        self._vr = None
        self.ok = False
        self._next_try = time.monotonic() + RETRY_S

    def start(self) -> bool:
        return self._connect()

    def poll(self):
        """Update and return current HMD yaw (degrees), or the last value / None. Reconnects on
        its own if SteamVR is restarted - like the daemon reconnecting to the hardware.
        """
        if self._vr is None or self._ovr is None:
            # Not connected (startup before SteamVR, or a restart). Retry, throttled.
            now = time.monotonic()
            if now >= self._next_try:
                self._next_try = now + RETRY_S
                if self._connect():
                    print("HMD yaw connected")
            return self._yaw
        try:
            ovr = self._ovr
            poses = self._vr.getDeviceToAbsoluteTrackingPose(
                ovr.TrackingUniverseStanding, 0, ovr.k_unMaxTrackedDeviceCount
            )
            if poses is None:
                return self._yaw
            hmd = poses[ovr.k_unTrackedDeviceIndex_Hmd]
            if hmd.bPoseIsValid:
                m = hmd.mDeviceToAbsoluteTracking
                self._yaw = math.degrees(math.atan2(m[0][2], m[2][2]))
        except Exception:
            # A dead session (SteamVR restarted) throws here - drop it so we reconnect.
            self._drop()
        return self._yaw

    def stop(self) -> None:
        if self._ovr is not None:
            try:
                self._ovr.shutdown()
            except Exception:
                pass
        self._vr = None
        self.ok = False
