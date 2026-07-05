#!/usr/bin/env python3
"""Live locomotion tuner / visualizer for the KAT Walk C2+ (plusE).

Reads the receiver, runs the LocomotionModel, and serves a web UI showing the
user's heading + walk direction + speed (a moving avatar with a trail) plus live
sliders mirroring the Gateway walk/run settings - so we can tune the feel before
touching SteamVR. Stdlib only.

Run:  .venv/bin/python -m katwalk.daemon        then open http://localhost:8770
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import select
import signal
import struct
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from katwalk.core.parser import HEADER, parse  # noqa: E402
from katwalk.core.locomotion import LocomotionModel  # noqa: E402
from katwalk.core.fusion import stick_from_state  # noqa: E402
from katwalk.io.gamepad import VirtualGamepad  # noqa: E402
from katwalk.io.openvr_pose import HmdYaw  # noqa: E402
from katwalk.config import (
    load_params,
    save_params,
    list_profiles,  # noqa: E402
    save_profile,
    load_profile,
    delete_profile,
)
from katwalk.io.driverlink import StickWriter  # noqa: E402

VID = "0000C4F4"
PID_RX = "00003F12"
PID_SEAT = "0000BF12"
PID_ARM = "0000BF13"  # armband (optical heart rate)
FRAME = 32
INIT = (
    bytes((0x1F, 0x55, 0xAA, 0, 0, 0x31)),
    bytes((0x1F, 0x55, 0xAA, 0, 0, 0x05)),
    bytes((0x1F, 0x55, 0xAA, 0, 0, 0x21)),
    bytes((0x1F, 0x55, 0xAA, 0, 0, 0x31)),
)
POLL = bytes(
    (0x1F, 0x55, 0xAA, 0, 0, 0x30)
)  # kicks/keeps the receiver stream + body link
VIB_OFF = bytes((0x1F, 0x55, 0xAA, 0, 0, 0xA0, 0x00, 0x02, 0x00, 0x00))
# Short base-motor buzz to CONFIRM a recenter (KAT's app vibrates on recenter too). Uses the
# a1 vibration command, NOT the a0 form above - a0 is sleep/vib-stop and would risk sleeping
# the device mid-session. Intensity 0x0341 ~= level 5 (per the protocol capture); off = 0x0000.
VIB_PULSE_ON = bytes((0x1F, 0x55, 0xAA, 0, 0, 0xA1, 0x00, 0x02, 0x03, 0x41))
VIB_PULSE_OFF = bytes((0x1F, 0x55, 0xAA, 0, 0, 0xA1, 0x00, 0x02, 0x00, 0x00))
STOP = bytes((0x1F, 0x55, 0xAA, 0, 0, 0x31))
RECOVERY_ATTEMPTS = (
    3  # re-wake this many times if the rig comes up mis-moded (Receiver._assess)
)
HTML = Path(__file__).resolve().parent / "web" / "tuner.html"
CAPTURE = Path(__file__).resolve().parent / "web" / "capture.html"
REC_DIR = Path(__file__).resolve().parent.parent / "recordings"
REC_KINDS = {
    "body",
    "foot_left",
    "foot_right",
    "status_left",
    "status_right",
    "status_direction",
}


def find(pid: str):
    for n in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        try:
            t = (n / "device" / "uevent").read_text().upper()
        except OSError:
            continue
        if VID in t and pid in t:
            return f"/dev/{n.name}"
    return None


SLEEPER = str(Path(__file__).resolve().parent / "io" / "sleeper.py")


def usb_node(pid_hex: str):
    """/dev/bus/usb path for a c4f4 device (for usbfs control transfers)."""
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            if open(d + "/idVendor").read().strip() != "c4f4":
                continue
            if open(d + "/idProduct").read().strip().lower() != pid_hex:
                continue
            return "/dev/bus/usb/%03d/%03d" % (
                int(open(d + "/busnum").read()),
                int(open(d + "/devnum").read()),
            )
        except OSError:
            continue
    return None


def kill_sleeper() -> None:
    """Stop a background sleeper from a prior stop and WAIT for it to exit, so its
    usbfs handles close (kernel auto-reattaches usbhid)."""
    pids = []
    for d in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            if b"io/sleeper.py" in open(d, "rb").read():
                pids.append(int(d.split("/")[2]))
        except (OSError, ValueError):
            pass
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        if not any(os.path.exists("/proc/%d" % p) for p in pids):
            break
        time.sleep(0.1)


def rebind_usbhid() -> None:
    """Ensure usbhid is bound to all three c4f4 devices (USBDEVFS_CONNECT via usbfs). An
    interrupted sleeper can leave them detached with no hidraw - this recovers the device
    without a replug or root. No-op (errors ignored) if already bound."""
    for pid in ("3f12", "bf12", "bf13"):
        node = usb_node(pid)
        if not node:
            continue
        try:
            ufd = os.open(node, os.O_RDWR)
            try:
                fcntl.ioctl(
                    ufd, 0xC0105512, struct.pack("<iiQ", 0, 0x5517, 0)  # USBDEVFS_IOCTL
                )  # ifno 0, USBDEVFS_CONNECT
            except OSError:
                pass
            os.close(ufd)
        except OSError:
            pass


def rep(p: bytes) -> bytes:
    return b"\x00" + bytes(p).ljust(FRAME, b"\x00")


def wr(fd: int, d: bytes) -> int:
    for a in range(4):
        try:
            return os.write(fd, d)
        except OSError as e:
            if e.errno == 71 and a < 3:
                time.sleep(0.02)
                continue
            return -1
    return -1


def align(b: bytes) -> bytes:
    i = b.find(HEADER)
    return b[i:] if i >= 0 else b


class Receiver(threading.Thread):
    def __init__(self, model: LocomotionModel, output: str = "vr"):
        super().__init__(daemon=True)
        self.model = model
        # output: "vr" (shmem -> the installed VR driver), "gamepad" (uinput), "none" (web only)
        self.output = output
        self.stop_flag = False
        self.latest = {"connected": False, "status": "starting"}
        self.fl = self.fr = None
        self.fl_conf = self.fr_conf = 0.0
        self.fl_ground = self.fr_ground = False
        self.battery = self.fw = None
        self.hr = None  # armband heart rate (bpm); None when absent/stale
        self._hr_time = 0.0
        self.hmd_yaw = None
        self.cal_heading = 0.0  # body heading captured at recenter (defines "forward")
        self.cal_hmd = 0.0  # HMD yaw captured at recenter (head frame at that instant)
        self._recenter_req = False  # set by HTTP thread; serviced on the reader thread
        self._recenter_seq = (
            0  # bumps on each recenter; the HUD watches it to flash a toast
        )
        self._vib_off_at = 0.0  # deadline to end the recenter confirm-buzz (0.0 = idle)
        # Motion recorder (capture page): label!=None => append every sensor frame to a
        # JSONL file so we can SEE what each action looks like in raw data. All file ops run
        # on the reader thread (HTTP thread only posts _rec_req) to avoid a write/close race.
        self._rec_label = None
        self._rec_fh = None
        self._rec_path = None
        self._rec_count = 0
        self._rec_t0 = 0.0
        self._rec_req = None  # ("start", label, new) | ("stop", None, False)

    def rec_start(self, label: str, new: bool = False) -> None:
        self._rec_req = ("start", label, new)

    def rec_stop(self) -> None:
        self._rec_req = ("stop", None, False)

    def rec_status(self) -> dict:
        return {
            "recording": self._rec_label is not None,
            "label": self._rec_label,
            "count": self._rec_count,
            "file": self._rec_path,
        }

    def _open_rec_file(self) -> None:
        if self._rec_fh is not None:
            try:
                self._rec_fh.close()
            except OSError:
                pass
        REC_DIR.mkdir(exist_ok=True)
        self._rec_path = str(
            REC_DIR / ("motion-" + time.strftime("%Y%m%d-%H%M%S") + ".jsonl")
        )
        self._rec_fh = open(self._rec_path, "w")
        self._rec_count = 0

    def _rec_write(self, now: float, f) -> None:
        """Append one sensor frame (raw hex + decoded foot/quat), timestamped + labeled, so
        a captured action can be analyzed offline. No-op unless a recording is active.
        """
        if self._rec_label is None or self._rec_fh is None or f.kind not in REC_KINDS:
            return
        rec = {
            "t": round(now - self._rec_t0, 4),
            "label": self._rec_label,
            "kind": f.kind,
        }
        if f.foot is not None:
            rec["x"], rec["y"] = f.foot[0], f.foot[1]
        if f.quat is not None:
            rec["quat"] = [round(v, 5) for v in f.quat]
        if f.battery is not None:
            rec["battery"] = f.battery
        rec["raw"] = bytes(f.raw).hex()
        self._rec_fh.write(json.dumps(rec) + "\n")
        self._rec_count += 1

    def recenter(self) -> float:
        """Capture the body + head reference for 'forward'. We store BOTH the body heading and the
        HMD yaw at this instant; the stick is then built relative to body-forward, and the head
        turn since recenter is removed so you can look around while walking. Stand facing your
        chosen forward with body and head aligned, then recenter."""
        self.cal_heading = self.model.heading
        self.cal_hmd = self.hmd_yaw or 0.0
        self._recenter_seq += 1
        return self.cal_heading

    def _assess(self, fd) -> str:
        """Poll the freshly-woken rig for ~1.5 s and classify how it came up. Returns "ok", or a
        reason string if a sensor is mis-moded after an unclean prior stop: a shoe streaming its
        IMU orientation instead of optical position (the parser reports foot=None for those), or
        a waist quaternion that is not unit-length. An idle rig that isn't streaming reads "ok"
        (nothing broken to observe - the feet just wake on motion)."""
        end = time.monotonic() + 1.5
        mags = []
        ok = {"foot_left": 0, "foot_right": 0}
        bad = {"foot_left": 0, "foot_right": 0}
        last_poll = 0.0
        while time.monotonic() < end:
            now = time.monotonic()
            if now - last_poll >= 0.3:
                wr(fd, rep(POLL))
                last_poll = now
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                continue
            try:
                buf = os.read(fd, 64)
            except OSError:
                continue
            if not buf:
                continue
            f = parse(align(bytes(buf)))
            if f.kind == "body" and f.quat:
                mags.append(sum(v * v for v in f.quat) ** 0.5)
            elif f.kind in ok:
                (ok if f.foot is not None else bad)[f.kind] += 1
        if mags and not (0.9 <= sum(mags) / len(mags) <= 1.1):
            return "waist sensor mis-moded (|q|=%.2f)" % (sum(mags) / len(mags))
        for k in ("foot_left", "foot_right"):
            if bad[k] and not ok[k]:
                return "%s mis-moded (IMU, not position)" % k.replace("foot_", "")
        return "ok"

    def run(self):
        kill_sleeper()  # clear a leftover background sleeper, if any
        rx, seat = find(PID_RX), find(PID_SEAT)
        if not rx:  # ONLY touch usbhid when the receiver hidraw is missing
            rebind_usbhid()  # (detached by an interrupted sleep) - never on a healthy
            for _ in range(24):  # start, so a working device is never disrupted.
                time.sleep(0.25)
                rx, seat = find(PID_RX), find(PID_SEAT)
                if rx:
                    break
        if not rx:
            self.latest = {
                "connected": False,
                "status": "receiver c4f4:3f12 not found - base on? hub authorized?",
            }
            return
        sfd = os.open(seat, os.O_RDWR | os.O_NONBLOCK) if seat else None
        arm = find(PID_ARM)  # armband (heart rate): optional
        afd = os.open(arm, os.O_RDWR | os.O_NONBLOCK) if arm else None
        # Output setup (gamepad / OpenVR / shmem) FIRST. These can take a second or two
        # (OpenVR init especially) and MUST NOT sit between the connect handshake and the
        # first poll - extra delay there stretches the quiet window so far the sensors SLEEP
        # instead of settling to connected (this is what broke a fresh auto-start). Set up
        # here, THEN do init -> quiet -> poll tight, exactly like the 5/5 standalone test.
        pad = None
        writer = None
        if self.output == "gamepad":
            try:
                pad = VirtualGamepad()
                print("output: virtual Xbox gamepad (uinput)")
            except Exception as e:
                print("gamepad output disabled:", e)
        elif self.output == "vr":
            try:
                writer = StickWriter()
                print("output: VR driver (shared-memory stick)")
            except Exception as e:
                print("VR output disabled:", e)
        # Head yaw makes walking body-relative rather than gaze-relative - essential once a
        # VR driver is the target. Skipped in 'none' mode. Degrades gracefully without SteamVR.
        hmd = None
        if self.output != "none":
            hmd = HmdYaw()
            if hmd.start():
                print("HMD yaw ON (head-relative direction)")
            else:
                # KEEP the HmdYaw object even though it isn't up yet - poll() reconnects on its
                # own when SteamVR/the headset appears (starting the daemon before the headset
                # must NOT leave head-relative dead forever). Body-heading only until then.
                print(
                    "HMD yaw not up yet (SteamVR/headset?) - reconnecting; body-heading for now"
                )

        # Connect + self-heal. Init (31,05,21,31) then a brief QUIET so the sensors settle to
        # CONNECTED (they latch when the 0x30 poll is NOT running). Then VERIFY the wake: after
        # an unclean prior stop a sensor can come up mis-moded (a shoe streaming its IMU
        # orientation instead of optical position, or a non-unit waist quaternion), which would
        # feed the model garbage. If so, sleep the sensors and re-wake - CONFIRMED that a clean
        # sleep+wake resets the mode - up to RECOVERY_ATTEMPTS times. Keep init->quiet->poll
        # TIGHT: too much quiet and they sleep.
        fd = None
        for attempt in range(1, RECOVERY_ATTEMPTS + 1):
            fd = os.open(rx, os.O_RDWR | os.O_NONBLOCK)
            for c in INIT:
                wr(fd, rep(c))
                time.sleep(0.05)
            time.sleep(3.5)  # quiet: settle to connected
            verdict = self._assess(fd)
            if verdict == "ok":
                if attempt > 1:
                    print(f"[init] sensors recovered on attempt {attempt}")
                break
            if attempt < RECOVERY_ATTEMPTS:
                print(
                    f"[init] {verdict} (attempt {attempt}/{RECOVERY_ATTEMPTS}) - sleeping + re-waking"
                )
                wr(fd, rep(bytes((0x1F, 0x55, 0xAA, 0, 0, 0xA0))))  # a0 - sleep
                time.sleep(0.1)
                wr(fd, rep(STOP))  # 31 - stop stream
                time.sleep(
                    5.0
                )  # quiet hold so the sensors sleep -> reopen is a clean wake
                os.close(fd)
                fd = None
                kill_sleeper()
            else:
                print(
                    f"[init] WARNING: {verdict} after {RECOVERY_ATTEMPTS} attempts - continuing; mis-moded input is ignored"
                )
        assert (
            fd is not None
        )  # the loop always leaves a fd open (healthy or final attempt)

        last_ka = last_poll = last_rc = 0.0
        last_body = time.monotonic()  # last body frame seen = sensor link liveness
        stalled = False
        STALL_S = 2.0  # no body frames this long -> mark disconnected + zero the stick
        RECENTER_FLAG = "/tmp/katwalk/recenter"  # OpenXR layer drops this on a recenter
        try:
            while not self.stop_flag:
                now = time.monotonic()
                # Motion recorder start/stop (capture page) - serviced immediately on
                # this thread so all file ops stay single-threaded (no write/close race).
                if self._rec_req is not None:
                    action, label, new = self._rec_req
                    self._rec_req = None
                    if action == "start":
                        if new or self._rec_fh is None:
                            self._open_rec_file()
                        self._rec_label = label
                        self._rec_t0 = now
                    else:  # stop
                        self._rec_label = None
                        if self._rec_fh is not None:
                            self._rec_fh.flush()
                # The OpenXR layer drops RECENTER_FLAG when the runtime recenters (e.g. the
                # Quest Meta-button hold) - re-zero walk-forward to the current pose to match.
                if now - last_rc >= 0.2:
                    last_rc = now
                    req = self._recenter_req  # HTTP-thread request (/recenter)
                    self._recenter_req = False
                    try:
                        if os.path.exists(RECENTER_FLAG):
                            os.remove(RECENTER_FLAG)
                            req = True
                    except OSError:
                        pass
                    if req:  # do the actual capture on THIS thread
                        self.recenter()  # trim forward to the current step dir
                        wr(
                            fd, rep(VIB_PULSE_ON)
                        )  # confirm with a short base-motor buzz
                        self._vib_off_at = now + 0.25
                # end the recenter confirm-buzz after its short pulse (non-blocking)
                if self._vib_off_at and now >= self._vib_off_at:
                    wr(fd, rep(VIB_PULSE_OFF))
                    self._vib_off_at = 0.0
                # If body frames stop, just mark disconnected and zero the stick (so the
                # driver/game don't drift on a stale value and /state stops reporting a
                # phantom speed). We keep sending ONLY the normal 0x30 poll below: the
                # Gateway capture shows it reconnects purely by repeating 0x30 - it never
                # sends a stop/re-init, which would interrupt the sensors trying to latch.
                if now - last_body > STALL_S and not stalled:
                    stalled = True
                    if writer is not None:
                        writer.write(0.0, 0.0)
                    if pad is not None:
                        pad.set_locomotion(0.0, 0.0, sprint=False)
                    self.model.speed = 0.0
                    self.latest = {
                        **self.latest,
                        "connected": False,
                        "status": "no sensor stream - waiting for sensors",
                        "moving": False,
                        "sprinting": False,
                        "speed": 0.0,
                        "stick": [0.0, 0.0],
                    }
                if now - last_poll >= 1.0:
                    wr(fd, rep(POLL))
                    last_poll = now
                if sfd is not None and now - last_ka >= 0.3:
                    wr(sfd, rep(b""))
                    last_ka = now
                r, _, _ = select.select(
                    [fd] + ([afd] if afd is not None else []), [], [], 0.05
                )
                if not r:
                    continue
                if (
                    afd is not None and afd in r
                ):  # armband stream (separate device): stash HR
                    try:
                        ab = os.read(afd, 64)
                        if ab:
                            af = parse(align(bytes(ab)))
                            if af.kind == "armband" and af.heart_rate:
                                self.hr, self._hr_time = af.heart_rate, now
                    except OSError:
                        pass
                    if fd not in r:
                        continue
                try:
                    buf = os.read(fd, 64)
                except OSError:
                    continue
                if not buf:
                    continue
                f = parse(align(bytes(buf)))
                self._rec_write(now, f)  # motion recorder (no-op unless recording)
                if f.kind == "foot_left":
                    self.fl = (
                        f.foot
                    )  # None if the frame was mis-moded (not position mode)
                    if f.foot is None:
                        self.fl_ground = (
                            False  # a shoe with no valid reading is not grounded
                        )
                    else:
                        sent = (
                            len(f.raw) > 10 and f.raw[9] == 0x4E and f.raw[10] == 0x20
                        )
                        self.fl_conf += 0.2 * ((0.0 if sent else 1.0) - self.fl_conf)
                        self.fl_ground = self.fl_conf > 0.65
                elif f.kind == "foot_right":
                    self.fr = f.foot
                    if f.foot is None:
                        self.fr_ground = False
                    else:
                        sent = (
                            len(f.raw) > 10 and f.raw[9] == 0x4E and f.raw[10] == 0x20
                        )
                        self.fr_conf += 0.2 * ((0.0 if sent else 1.0) - self.fr_conf)
                        self.fr_ground = self.fr_conf > 0.65
                elif f.kind.startswith("status"):
                    if f.battery is not None:
                        self.battery = f.battery
                    if f.firmware is not None:
                        self.fw = f.firmware
                elif f.kind == "body" and f.quat:
                    last_body = now
                    stalled = False
                    # only the grounded foot's slide counts as walk speed (ignore
                    # the airborne foot's spurious optical motion)
                    st = self.model.update(
                        f.quat, self.fl, self.fr, self.fl_ground, self.fr_ground
                    )
                    if hmd is not None:
                        y = hmd.poll()
                        if y is not None:
                            self.hmd_yaw = y
                    # Body-relative: move where the BODY faces, independent of where you look.
                    # move_offset = shoe dir relative to body; body_rot = body turn since recenter
                    # (sign inverted so E/W is not mirrored in-game); dh = head turn since recenter
                    # (added to cancel the game's head-relative stick interpretation).
                    move_offset = st["direction"] - st["heading"]
                    body_rot = st["heading"] - self.cal_heading
                    dh = (self.hmd_yaw or 0.0) - self.cal_hmd
                    angle = move_offset - body_rot + dh
                    sx, sy = stick_from_state(angle, st["speed"], 0.0)
                    if pad is not None:
                        pad.set_locomotion(sx, sy, sprint=st["sprinting"])
                    if writer is not None:
                        writer.write(sx, sy, sprint=st["sprinting"])
                    st["footL"] = list(self.fl) if self.fl else [0, 0]
                    st["footR"] = list(self.fr) if self.fr else [0, 0]
                    st["groundedL"] = self.fl_ground
                    st["groundedR"] = self.fr_ground
                    st["hmd_yaw"] = (
                        round(self.hmd_yaw, 1) if self.hmd_yaw is not None else None
                    )
                    st["stick"] = [round(sx, 3), round(sy, 3)]
                    st.update(
                        connected=True,
                        status="streaming",
                        battery=self.battery,
                        fw=self.fw,
                        hr=(self.hr if self.hr and now - self._hr_time < 5.0 else None),
                        recenter_seq=self._recenter_seq,
                        cal_offset=round(self.cal_heading, 1),
                    )
                    self.latest = st
        finally:
            # Stop -> SLEEP. EXACT sequence proven 5/5 on hardware (tools/stoptest.py): send
            # a0 (sleep), then 31 (stop), HOLD the handle open & QUIET ~5s (no poll/keepalive)
            # so the sensors settle to slow-blink, THEN close. The quiet hold is essential -
            # the old immediate-close stop is what failed. (Matches the Gateway-close capture.)
            wr(fd, rep(bytes((0x1F, 0x55, 0xAA, 0, 0, 0xA0))))  # a0 - sleep
            time.sleep(0.1)
            wr(fd, rep(STOP))  # 31 - stop stream
            time.sleep(5.0)  # quiet hold -> slow-blink
            os.close(fd)
            if sfd is not None:
                os.close(sfd)
            if afd is not None:
                os.close(afd)
            if pad is not None:
                pad.close()
            if hmd is not None:
                hmd.stop()
            if writer is not None:
                writer.close()
            if self._rec_fh is not None:
                try:
                    self._rec_fh.close()
                except OSError:
                    pass
            self.latest = {"connected": False, "status": "stopped (device asleep)"}


class Handler(BaseHTTPRequestHandler):
    model: LocomotionModel  # set on the class in main() before the server starts
    receiver: Receiver  # same

    def log_message(self, format, *args):  # silence default request logging
        pass

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                self._send(200, "text/html; charset=utf-8", HTML.read_bytes())
            except OSError:
                self._send(500, "text/plain", b"katwalk/web/tuner.html missing")
        elif u.path == "/set":
            ok = False
            for k, v in parse_qs(u.query).items():
                if v:
                    ok = self.model.set(k, v[0]) or ok
            if ok:
                save_params(self.model.p)
            self._send(
                200,
                "application/json",
                json.dumps({"ok": ok, "params": asdict(self.model.p)}).encode(),
            )
        elif u.path == "/profiles":
            active, names = list_profiles()
            self._send(
                200,
                "application/json",
                json.dumps({"active": active, "names": names}).encode(),
            )
        elif u.path == "/profile/save":
            name = parse_qs(u.query).get("name", [""])[0].strip()
            if name:
                save_profile(name, self.model.p)
            active, names = list_profiles()
            self._send(
                200,
                "application/json",
                json.dumps(
                    {"ok": bool(name), "active": active, "names": names}
                ).encode(),
            )
        elif u.path == "/profile/load":
            name = parse_qs(u.query).get("name", [""])[0].strip()
            p = load_profile(name) if name else None
            if p is not None:
                self.model.p = p
            self._send(
                200,
                "application/json",
                json.dumps(
                    {"ok": p is not None, "params": asdict(self.model.p)}
                ).encode(),
            )
        elif u.path == "/profile/delete":
            name = parse_qs(u.query).get("name", [""])[0].strip()
            ok = delete_profile(name) if name else False
            if ok:
                self.model.p = load_params()
            active, names = list_profiles()
            self._send(
                200,
                "application/json",
                json.dumps({"ok": ok, "active": active, "names": names}).encode(),
            )
        elif u.path in ("/capture", "/capture.html"):
            try:
                self._send(200, "text/html; charset=utf-8", CAPTURE.read_bytes())
            except OSError:
                self._send(500, "text/plain", b"katwalk/web/capture.html missing")
        elif u.path == "/record/start":
            q = parse_qs(u.query)
            label = q.get("label", ["unlabeled"])[0] or "unlabeled"
            new = q.get("new", ["0"])[0] in ("1", "true", "yes")
            self.receiver.rec_start(label, new)
            self._send(
                200,
                "application/json",
                json.dumps({"ok": True, "label": label}).encode(),
            )
        elif u.path == "/record/stop":
            self.receiver.rec_stop()
            self._send(200, "application/json", json.dumps({"ok": True}).encode())
        elif u.path == "/record/status":
            self._send(
                200, "application/json", json.dumps(self.receiver.rec_status()).encode()
            )
        elif u.path == "/recenter":
            self.receiver._recenter_req = (
                True  # serviced on the reader thread (no race)
            )
            self._send(200, "application/json", json.dumps({"ok": True}).encode())
        elif u.path == "/state":
            self._send(
                200, "application/json", json.dumps(self.receiver.latest).encode()
            )
        elif u.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(self.receiver.latest)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        else:
            self._send(404, "text/plain", b"not found")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read the KAT Walk C2+ and publish locomotion. By default it feeds "
        "the installed VR driver (openvr-driver / openxr-driver) over shared memory."
    )
    ap.add_argument("--port", type=int, default=8770, help="web tuner port")
    ap.add_argument(
        "--output",
        choices=("vr", "gamepad", "none"),
        default="vr",
        help="where locomotion goes: 'vr' (default) feeds the installed VR driver over "
        "shared memory; 'gamepad' drives a virtual Xbox pad; 'none' outputs nothing, "
        "just the web tuner to watch raw sensors",
    )
    args = ap.parse_args()

    model = LocomotionModel(load_params())
    rcv = Receiver(model, output=args.output)
    rcv.start()
    Handler.model = model
    Handler.receiver = rcv
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    mode = {
        "vr": "VR driver output",
        "gamepad": "gamepad output",
        "none": "sensor viewer",
    }[args.output]
    print(
        f"katwalk-linux tuner ({mode}) → http://localhost:{args.port}    (Ctrl-C / SIGTERM to stop; device sleeps on exit)"
    )
    # Run the HTTP server in a thread and block on a stop event the signal handler sets.
    # (Relying on KeyboardInterrupt to break serve_forever is unreliable when launched
    # detached via setsid - the signal didn't reliably unblock it, so the daemon hung on
    # stop and never ran the sleep handshake.)
    stop_evt = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_evt.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_evt.set())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        while not stop_evt.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    print("\nstopping (device sleeping)...")
    rcv.stop_flag = True
    rcv.join(timeout=6)  # lets the reader's finally run the ~4.5s sleep handshake
    srv.shutdown()  # safe: serve_forever runs in the worker thread above
    srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
