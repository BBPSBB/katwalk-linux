#!/usr/bin/env python3
"""katwalk-linux in-VR overlay - OpenXR backend (SteamVR-free).

The same HUD brain as katwalk.overlay (render(), tabs, steppers) but instead of a SteamVR
IVROverlay it talks to the katwalk OpenXR API layer over shared memory in /tmp/katwalk:

  hud    (we write): 76-byte header + RGBA frame; the layer uploads it into the in-game quad
  laser  (we read):  ray-cast hits + trigger clicks from the pointing hand, in image pixels

The layer owns placement (hand anchor + facing gate, tuned via /tmp/katwalk/hud.conf); we own
pixels + click handling. Struct layouts mirror openxr-driver/src/katwalk_shm.h - keep in sync.

Usage:
    .venv/bin/python -m katwalk.overlay_xr [--port 8770] [--demo]
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import struct
import time
import urllib.request

from katwalk.overlay import (  # the UI brain is shared with the SteamVR backend
    H,
    TABS,
    VIEWS,
    W,
    _DIST_MINUS,
    _DIST_PLUS,
    _DZ_MINUS,
    _DZ_PLUS,
    _SETUP_LOCK,
    _SPD_MINUS,
    _SPD_PLUS,
    _hud_signature,
    demo_state,
    load_fonts,
    render,
)

DIR = "/tmp/katwalk"  # ephemeral per-session shm pipes (hud frames, laser, poses) - NOT settings
CONF = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "katwalk",
    "hud.conf",
)  # persistent settings - must survive reboots; the layer resolves the same path in-game
DIST_MIN, DIST_MAX, DIST_STEP = 0.2, 1.5, 0.05

DEFAULT_CONF = """\
# katwalk wrist HUD - live-tunable while the game runs (reloaded ~1x/s on mtime change)
hand = 0                  # 0=left  1=right
offset = 0.0, 0.02, 0.10  # panel origin in grip space, meters (x,y,z) - drag in VR to adjust
rot = -90, 0, 0           # panel rotation in grip space, degrees (X,Y,Z)
width = 0.13              # panel width in meters
face_show = 0.50          # show when panel-normal . toward-face exceeds this
face_hide = 0.30          # hide when it falls below this
max_dist = 0.65           # hide when the hand is farther than this from the face (m)
hold = 6                  # frames a show/hide flip must persist
always = 0                # 1 = ignore the gate entirely (placement tuning aid)
locked = 0                # 1 = refuse drag-to-move
"""


def ensure_conf() -> None:
    """Seed a commented default hud.conf on first run (never overwrites an existing one)."""
    if not os.path.exists(CONF):
        os.makedirs(os.path.dirname(CONF), exist_ok=True)
        with open(CONF, "w") as fh:
            fh.write(DEFAULT_CONF)
        print(f"[overlay-xr] wrote default {CONF}")


def read_conf(key: str, default: float) -> float:
    """Read one numeric key from hud.conf (the layer live-reloads the same file)."""
    try:
        with open(CONF) as fh:
            for line in fh:
                if line.split("=")[0].strip() == key:
                    return float(line.split("=")[1].split("#")[0].strip())
    except (OSError, ValueError, IndexError):
        pass
    return default


def write_conf(key: str, text: str) -> None:
    """Rewrite only `key`'s line, preserving the rest of the conf (incl. comments)."""
    try:
        lines = open(CONF).read().splitlines(keepends=True)
    except OSError:
        lines = []
    out, done = [], False
    for line in lines:
        if line.split("=")[0].strip() == key:
            out.append(f"{key} = {text}\n")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key} = {text}\n")
    try:
        with open(CONF, "w") as fh:
            fh.writelines(out)
    except OSError:
        pass


def read_max_dist() -> float:
    return read_conf("max_dist", 0.65)


def write_max_dist(val: float) -> None:
    write_conf("max_dist", f"{val:.2f}")


# struct KatHud: 5x u32 (seq_start,w,h,visible,anchor) + f32 width_m + 12x f32 + u32 seq_end
HUD_HDR = struct.Struct("<5If12fI")
LASER = struct.Struct("<3I2fI")  # seq,event,button,x,y,on_panel
EV_MOVE, EV_DOWN, EV_UP = 0, 1, 2
BTN_TRIGGER, BTN_GRIP = 0, 1
IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)


class HudWriter:
    """Owns /tmp/katwalk/hud: header + RGBA pixels, torn-read-guarded by seq_start==seq_end."""

    def __init__(self):
        os.makedirs(DIR, exist_ok=True)
        size = HUD_HDR.size + W * H * 4
        fd = os.open(os.path.join(DIR, "hud"), os.O_RDWR | os.O_CREAT, 0o666)
        os.ftruncate(fd, size)
        self.mm = mmap.mmap(fd, size)
        os.close(fd)
        self.seq = 0

    def publish(self, rgba: bytes):
        self.seq += 1
        # seq_start first, then payload, then seq_end: the layer copies only when they match
        self.mm[0:4] = struct.pack("<I", self.seq)
        self.mm[4 : HUD_HDR.size] = HUD_HDR.pack(
            self.seq, W, H, 1, 1, 0.13, *IDENTITY, self.seq
        )[4:]
        self.mm[HUD_HDR.size :] = rgba
        self.mm[HUD_HDR.size - 4 : HUD_HDR.size] = struct.pack("<I", self.seq)


class LaserReader:
    """Polls /tmp/katwalk/laser (created by the layer once a session runs)."""

    def __init__(self):
        self.mm = None
        self.last_seq = None

    def poll(self):
        if self.mm is None:
            try:
                fd = os.open(os.path.join(DIR, "laser"), os.O_RDONLY)
                self.mm = mmap.mmap(fd, LASER.size, prot=mmap.PROT_READ)
                os.close(fd)
            except OSError:
                return None
        seq, event, button, x, y, on_panel = LASER.unpack(self.mm[:])
        if seq == self.last_seq:
            return None
        first = self.last_seq is None
        self.last_seq = seq
        if first:  # stale event from a previous run - swallow it
            return None
        return event, button, x, y, on_panel


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--demo", action="store_true", help="synthetic data (no daemon)")
    args = ap.parse_args()

    fonts = load_fonts()
    ensure_conf()
    hud = HudWriter()
    laser = LaserReader()

    def get_state():
        try:
            return json.load(
                urllib.request.urlopen(f"http://localhost:{args.port}/state", timeout=1)
            )
        except Exception:
            return {"connected": False, "status": "katwalkd not running"}

    def rec(path):  # daemon HTTP control (same as the SteamVR backend)
        try:
            urllib.request.urlopen(
                f"http://localhost:{args.port}{path}", timeout=0.5
            ).read()
        except Exception:
            pass

    view = 0
    locked = (
        read_conf("locked", 0) >= 1
    )  # drag lock - persisted in hud.conf for the layer
    max_dist = read_max_dist()  # visibility distance (the layer's gate; see hud.conf)
    last_sig = None
    last_seq = None
    body_zero = (
        None  # BODY display offset captured at recenter (None until first recenter)
    )
    last_pub = 0.0
    cursor = None  # last on-panel laser hit (image px)
    cursor_t = 0.0
    flash_until = 0.0
    state = {"connected": False, "status": "connecting…"}
    state_next = 0.0
    start = time.monotonic()
    print(
        f"katwalk overlay (OpenXR backend) up - frames -> {DIR}/hud, clicks <- {DIR}/laser"
    )

    while True:
        now = time.monotonic()
        if args.demo:
            state = demo_state(now - start)
        elif now >= state_next:
            state_next = now + 0.07
            state = get_state()
        seq = state.get("recenter_seq")
        if seq is not None and seq != last_seq:
            if last_seq is not None:
                flash_until = now + 1.6
                # zero the BODY readout at recenter (display-only; locomotion math untouched).
                # After a recenter both HEAD and BODY read 0, so any nonzero BODY afterwards
                # is drift - and a recenter that "did nothing" becomes visible at a glance.
                if state.get("heading") is not None:
                    body_zero = float(state["heading"])
            last_seq = seq
        flashing = now < flash_until
        # Build the DISPLAY state fresh each tick from the untouched polled state. (The polled
        # dict is cached across 60 Hz ticks; adjusting it in place re-applied the offset every
        # tick until the next poll - the readout cycled raw / raw-off / raw-2*off.)
        disp = state
        if state.get("heading") is not None:
            disp = dict(state)
            # match the HEAD readout's signed convention ([-180, 180], not [0, 360)) so the
            # two values agree in both turn directions (-10 vs 350 was just display units)
            rel = float(state["heading"]) - (body_zero or 0.0)
            disp["heading"] = round((rel + 180.0) % 360.0 - 180.0, 1)

        # clicks from the in-game laser (trigger only; grip is reserved)
        ev = laser.poll()
        if ev is not None:
            event, button, x, y, on_panel = ev
            if on_panel:  # cursor dot follows every event that lands on the panel
                cursor, cursor_t = (x, y), now
            if event == EV_DOWN and button == BTN_TRIGGER and on_panel:
                hit = False
                for i, (x0, y0, x1, y1) in enumerate(TABS):
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        view, hit = i, True
                        print(f"[overlay-xr] tab -> {VIEWS[i]}")
                        break
                if not hit and VIEWS[view] == "SETUP":
                    p = state.get("params") or {}

                    def _in(r):
                        return r[0] <= x <= r[2] and r[1] <= y <= r[3]

                    if _in(_SETUP_LOCK):
                        locked = not locked
                        write_conf("locked", "1" if locked else "0")
                        print(f"[overlay-xr] drag {'LOCKED' if locked else 'UNLOCKED'}")
                    elif _in(_SPD_MINUS) or _in(_SPD_PLUS):
                        cur = float(p.get("speed_multiplier", 1.0))
                        new = round(
                            min(
                                3.0, max(0.2, cur + (0.05 if _in(_SPD_PLUS) else -0.05))
                            ),
                            2,
                        )
                        rec(f"/set?speed_multiplier={new}")
                    elif _in(_DZ_MINUS) or _in(_DZ_PLUS):
                        cur = float(p.get("deadzone", 60))
                        new = int(
                            min(300, max(0, cur + (10 if _in(_DZ_PLUS) else -10)))
                        )
                        rec(f"/set?deadzone={new}")
                    elif _in(_DIST_MINUS) or _in(_DIST_PLUS):
                        # visibility distance: written to hud.conf; the layer live-reloads
                        # it (~1 s) and hides the panel when the hand is farther than this
                        max_dist = round(
                            min(
                                DIST_MAX,
                                max(
                                    DIST_MIN,
                                    max_dist
                                    + (DIST_STEP if _in(_DIST_PLUS) else -DIST_STEP),
                                ),
                            ),
                            2,
                        )
                        write_max_dist(max_dist)
                        print(f"[overlay-xr] visibility distance -> {max_dist:.2f} m")

        if disp is state:  # ensure _dist never mutates the cached polled dict either
            disp = dict(state)
        disp["_dist"] = max_dist  # shown on the SETUP tab
        # cursor: fresh laser hit -> draw a dot; quantized + throttled so hand jitter can't
        # spam GPU uploads (each publish costs the layer one bounded swapchain upload)
        cur_fresh = cursor is not None and (now - cursor_t) < 0.4
        cur_q = (int(cursor[0] // 5), int(cursor[1] // 5)) if cur_fresh else None
        sig = (_hud_signature(disp, flashing, view, locked, None), cur_q)
        if sig != last_sig and (
            sig[0] != (last_sig or (None,))[0] or now - last_pub > 0.08
        ):
            im = render(disp, fonts, flash=flashing, view=view, locked=locked)
            if cur_fresh:
                from PIL import ImageDraw

                d = ImageDraw.Draw(im)
                cx, cy = cursor
                d.ellipse(
                    [cx - 6, cy - 6, cx + 6, cy + 6],
                    outline=(34, 211, 238, 255),
                    width=2,
                )
                d.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=(255, 255, 255, 255))
            hud.publish(im.tobytes())
            last_sig, last_pub = sig, now
        time.sleep(1 / 60.0)


if __name__ == "__main__":
    raise SystemExit(main())
