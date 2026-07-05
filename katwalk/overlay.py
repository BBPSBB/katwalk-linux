#!/usr/bin/env python3
"""katwalk-linux in-VR overlay (HUD) - a SteamVR IVROverlay on your forearm.

A thin VR client of katwalkd: polls http://localhost:<port>/state and renders a small HUD
(speed, cadence, WALK/SPRINT, battery, heading) anchored to a controller on the inner
forearm - "ArmView" style. It only SHOWS when you rotate your wrist toward your face to
look at it (a facing gate), so it stays out of the way otherwise.

Needs: SteamVR running, openvr + Pillow (.venv), and the daemon (python -m katwalk.daemon) running.
Usage:
    .venv/bin/python -m katwalk.overlay [--port 8770] [--hand left|right]
    .venv/bin/python -m katwalk.overlay --render-test /tmp/hud.png   # render one frame, no VR

NOTE: the forearm transform + facing threshold are a first cut - tune in VR.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import signal
import sys
import time
import urllib.request

W, H = 512, 304  # taller: body + a tab/button row at the bottom
SPEED_MAX = 8.0  # km/h-ish mapped to full stick (matches katwalk.core.fusion)
BG = (12, 16, 25, 255)  # panel fill - opaque so the front overlay fully occludes the
# one being updated behind it (flicker-free double-buffer)
EDGE = (34, 211, 238, 110)  # cyan border
CYAN = (34, 211, 238, 255)
WHITE = (236, 244, 252, 255)
MUTED = (124, 140, 165, 255)
GREEN = (52, 211, 153, 255)
ORANGE = (251, 146, 60, 255)
IDLE = (108, 122, 146, 255)
TRACK = (26, 34, 50, 255)  # meter track
RED = (248, 113, 113, 255)
PANEL2 = (20, 26, 38, 255)  # inactive tab fill

# --- bottom tab row (buttons that switch the body view) ---
VIEWS = ("DEBUG", "SENSORS", "SETUP")

# --- DEBUG dashboard: two foot pads + heading dial + output, in a 4-cell row ---
PAD_Y0, SQ = 66, 80  # pad top y + square size (px)
_PAD_CX = (76, 196, 316, 436)  # cell centers: FOOT L, FOOT R, HEADING, OUTPUT
FOOT_MAX = 1000.0  # raw foot-slip count mapped to a pad edge
GRID = (34, 42, 60, 255)  # pad crosshair / border colour
_TAB_Y0, _TAB_Y1 = 252, 294
_SETUP_LOCK = (300, 50, 488, 90)  # drag lock/unlock toggle (image coords)
# live param steppers: [-] value [+]  (the ± write to the daemon /set so you tune in-VR)
_SPD_MINUS, _SPD_PLUS = (300, 98, 344, 134), (444, 98, 488, 134)  # speed multiplier ±
_DZ_MINUS, _DZ_PLUS = (300, 142, 344, 178), (444, 142, 488, 178)  # deadzone ±
_DIST_MINUS, _DIST_PLUS = (300, 186, 344, 222), (444, 186, 488, 222)  # hide-distance ±
_CALIB_CANCEL = (
    181,
    252,
    331,
    294,
)  # CANCEL button (legacy guided-capture, no longer launched)

# Guided calibration capture, driven from the SETUP tab and recorded via the daemon /record API.
# (label, prompt, sub, seconds)
CALIB_SEQ = [
    ("stand_still", "STAND STILL", "feet at center, don't move", 5),
    ("forward_slow", "WALK FORWARD - SLOW", "gentle alternating steps", 8),
    ("forward_med", "WALK FORWARD - MEDIUM", "normal pace", 8),
    ("forward_fast", "WALK FORWARD - FAST", "quick steps", 8),
    ("back_step", "STEP BACKWARD", "one step · pause · repeat", 10),
    ("left_Lfoot", "LEFT FOOT → STEP LEFT", "out · pause · return · repeat", 10),
    ("right_Rfoot", "RIGHT FOOT → STEP RIGHT", "out · pause · return · repeat", 10),
    ("turn_left", "TURN BODY LEFT", "in place, don't travel", 6),
    ("turn_right", "TURN BODY RIGHT", "in place, don't travel", 6),
]
CALIB_READY, CALIB_REST = 3, 3  # seconds of get-ready before / rest after each action


def _tab_rects():
    n, m, gap = len(VIEWS), 20, 12
    wt = (W - 2 * m - gap * (n - 1)) / n
    return [
        (m + i * (wt + gap), _TAB_Y0, m + i * (wt + gap) + wt, _TAB_Y1)
        for i in range(n)
    ]


TABS = _tab_rects()  # (x0, y0, x1, y1) per tab, in image (top-left) coords

# --- forearm placement in CONTROLLER space (tune in VR) ---
OFFSET_CTRL = (0.0, 0.02, 0.10)  # ~10 cm toward the elbow, 2 cm up from the grip
NORMAL_CTRL = (0.0, 1.0, 0.0)  # panel faces controller +Y (out of the inner wrist)
# overlay-relative rotation: map overlay +Z -> controller +Y (i.e. -90° about X)
ROT_CTRL = ((1, 0, 0), (0, 0, 1), (0, -1, 0))
FACE_THRESHOLD = 0.5  # show when wrist-normal·(toward HMD) > this (~60°)
WIDTH_M = 0.13

_FONT_PATHS = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
]


def load_fonts():
    from PIL import ImageFont

    def f(sz):
        for p in _FONT_PATHS:
            try:
                return ImageFont.truetype(p, sz)
            except OSError:
                continue
        return ImageFont.load_default()

    return {"hero": f(86), "h2": f(30), "badge": f(23), "body": f(22), "label": f(14)}


def _ctext(d, cx, y, text, font, fill):
    """draw text horizontally centered on cx."""
    d.text((cx - d.textlength(text, font=font) / 2, y), text, font=font, fill=fill)


def _clamp01(v):
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _tint(col, frac=0.20):
    """opaque blend of col over the panel BG, so fills don't punch transparent holes."""
    return tuple(round(BG[i] + (col[i] - BG[i]) * frac) for i in range(3)) + (255,)


def to_overlay_buffer(im_or_bytes):
    """Convert a PIL RGBA image (or its raw bytes) to a ctypes buffer for setOverlayRaw.
    pyopenvr calls ctypes.byref() on this argument, so it MUST be a ctypes object - passing
    raw bytes raises 'byref() argument must be _ctypes._CData, not bytes'."""
    data = (
        im_or_bytes
        if isinstance(im_or_bytes, (bytes, bytearray))
        else im_or_bytes.tobytes()
    )
    return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)


def _accent(state):
    return (
        ORANGE if state.get("sprinting") else (GREEN if state.get("moving") else IDLE)
    )


def _square_pad(d, fonts, cx, dotx, doty, accent, label, sub, sub_col):
    """Joystick-square viz: bordered box + crosshair + a dot at (dotx, doty),
    each in [-1, 1] with +y = up (forward)."""
    h = SQ // 2
    cy = PAD_Y0 + h
    x0, y0, x1, y1 = cx - h, cy - h, cx + h, cy + h
    d.rectangle([x0, y0, x1, y1], fill=PANEL2, outline=GRID, width=1)
    d.line([cx, y0, cx, y1], fill=GRID, width=1)
    d.line([x0, cy, x1, cy], fill=GRID, width=1)
    dx = cx + max(-1.0, min(1.0, dotx)) * (h - 6)
    dy = cy - max(-1.0, min(1.0, doty)) * (h - 6)
    d.ellipse([dx - 7, dy - 7, dx + 7, dy + 7], fill=accent, outline=WHITE, width=1)
    _ctext(d, cx, y0 - 19, label, fonts["label"], MUTED)
    _ctext(d, cx, y1 + 6, sub, fonts["label"], sub_col)


def _heading_dial(d, fonts, cx, heading):
    """Compass dial: a needle at the body heading (0° = up), numeric value below."""
    r = SQ // 2
    cy = PAD_Y0 + r
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=PANEL2, outline=GRID, width=1)
    for ang in (0, 90, 180, 270):
        a = math.radians(ang)
        d.line(
            [
                cx + (r - 6) * math.sin(a),
                cy - (r - 6) * math.cos(a),
                cx + r * math.sin(a),
                cy - r * math.cos(a),
            ],
            fill=MUTED,
            width=1,
        )
    if heading is not None:
        a = math.radians(heading)
        nx, ny = cx + (r - 9) * math.sin(a), cy - (r - 9) * math.cos(a)
        d.line([cx, cy, nx, ny], fill=CYAN, width=3)
        d.ellipse([nx - 4, ny - 4, nx + 4, ny + 4], fill=CYAN)
    d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=WHITE)
    _ctext(d, cx, PAD_Y0 - 19, "HEADING", fonts["label"], MUTED)
    _ctext(
        d,
        cx,
        cy + r + 6,
        f"{heading:.0f}°" if heading is not None else "-",
        fonts["label"],
        WHITE,
    )


def _body_debug(d, fonts, state, locked=False):
    """True debug dashboard: both foot sensors as joystick squares, the body-heading dial, and
    the FINAL output stick - plus a numeric readout strip. All live daemon data."""
    fl = state.get("footL") or [0, 0]
    fr = state.get("footR") or [0, 0]
    gl, gr = state.get("groundedL"), state.get("groundedR")
    stick = state.get("stick") or [0, 0]
    hd = state.get("heading")
    spd = float(state.get("speed", 0.0) or 0.0)
    a = _accent(state)
    _square_pad(
        d,
        fonts,
        _PAD_CX[0],
        fl[0] / FOOT_MAX,
        fl[1] / FOOT_MAX,
        GREEN if gl else IDLE,
        "FOOT L",
        f"{int(fl[0])} / {int(fl[1])}",
        GREEN if gl else MUTED,
    )
    _square_pad(
        d,
        fonts,
        _PAD_CX[1],
        fr[0] / FOOT_MAX,
        fr[1] / FOOT_MAX,
        GREEN if gr else IDLE,
        "FOOT R",
        f"{int(fr[0])} / {int(fr[1])}",
        GREEN if gr else MUTED,
    )
    _heading_dial(d, fonts, _PAD_CX[2], hd)
    _square_pad(
        d,
        fonts,
        _PAD_CX[3],
        stick[0],
        stick[1],
        a,
        "OUTPUT",
        f"{stick[0]:+.2f},{stick[1]:+.2f}",
        WHITE,
    )

    # diagnostic strip - the angles that decide direction. HEAD shows "OFF" in red if the daemon
    # isn't reading HMD yaw (so a broken head-comp is visible at a glance).
    direction = state.get("direction")
    hy = state.get("hmd_yaw")
    cols = [
        ("HEAD", "OFF" if hy is None else f"{hy:.0f}°", RED if hy is None else WHITE),
        ("BODY", f"{hd:.0f}°" if hd is not None else "-", WHITE),
        ("MOVE", f"{direction:.0f}°" if direction is not None else "-", WHITE),
        ("SPEED", f"{spd:.1f}", a),
    ]
    for i, (lab, val, col) in enumerate(cols):
        cx = W * (2 * i + 1) / 8
        _ctext(d, cx, 188, lab, fonts["label"], MUTED)
        _ctext(d, cx, 206, val, fonts["body"], col)


def _body_sensors(d, fonts, state, locked=False):
    hd, bat = state.get("heading"), state.get("battery")
    fl, fr = state.get("footL") or [0, 0], state.get("footR") or [0, 0]
    stick = state.get("stick") or [0, 0]
    gl, gr = state.get("groundedL"), state.get("groundedR")
    # foot value turns green when that foot is grounded (on the deck)
    rows = [
        ("Heading", f"{hd:.0f}°" if hd is not None else "-", WHITE),
        ("Cadence", f"{state.get('cadence_spm', 0) or 0:.0f} spm", WHITE),
        ("Foot L", f"{int(fl[0])} / {int(fl[1])}", GREEN if gl else WHITE),
        ("Foot R", f"{int(fr[0])} / {int(fr[1])}", GREEN if gr else WHITE),
        ("Stick", f"{stick[0]:+.2f}, {stick[1]:+.2f}", WHITE),
        (
            "Battery",
            f"{bat}%  fw V{state.get('fw')}" if bat is not None else "-",
            WHITE,
        ),
    ]
    y = 50
    for k, v, col in rows:
        d.text((28, y), k, font=fonts["label"], fill=MUTED)
        d.text((188, y), str(v), font=fonts["body"], fill=col)
        y += 33


def _pill(d, fonts, rect, text, col, filled=True):
    x0, y0, x1, y1 = rect
    d.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=min(12, (y1 - y0) // 2),
        fill=_tint(col) if filled else PANEL2,
        outline=col,
        width=2,
    )
    _ctext(
        d,
        (x0 + x1) / 2,
        (y0 + y1) / 2 - fonts["badge"].size / 2 + 1,
        text,
        fonts["badge"],
        col,
    )


def _stepper(d, fonts, label, minus, plus, val):
    """A '[-] value [+]' row: label left, ± pills right, value centered between them."""
    d.text((30, minus[1] + 7), label, font=fonts["body"], fill=WHITE)
    _pill(d, fonts, minus, "–", MUTED, filled=False)
    _pill(d, fonts, plus, "+", MUTED, filled=False)
    _ctext(d, (minus[2] + plus[0]) / 2, minus[1] + 5, val, fonts["badge"], WHITE)


def _body_setup(d, fonts, state, locked=False):
    p = state.get("params") or {}
    d.text((30, 56), "Drag lock", font=fonts["body"], fill=WHITE)
    _pill(
        d,
        fonts,
        _SETUP_LOCK,
        "LOCKED" if locked else "UNLOCKED",
        ORANGE if locked else GREEN,
    )
    _stepper(
        d,
        fonts,
        "Speed ×",
        _SPD_MINUS,
        _SPD_PLUS,
        f"{float(p.get('speed_multiplier', 1.0)):.2f}",
    )
    _stepper(
        d, fonts, "Deadzone", _DZ_MINUS, _DZ_PLUS, f"{float(p.get('deadzone', 60)):.0f}"
    )
    _stepper(
        d,
        fonts,
        "Hide dist",
        _DIST_MINUS,
        _DIST_PLUS,
        f"{state.get('_dist', DEFAULT_DIST):.2f}m",
    )


def _render_calib(d, fonts, c):
    pcol = c["color"]
    _ctext(
        d, W / 2, 54, f"CALIBRATION   {c['idx']} / {c['total']}", fonts["label"], MUTED
    )
    _ctext(d, W / 2, 86, c["prompt"], fonts["h2"], WHITE)
    _ctext(d, W / 2, 130, c["sub"], fonts["label"], MUTED)
    _ctext(
        d, W / 2, 160, f"{c['phase']}   -   {int(c['rem']) + 1}s", fonts["badge"], pcol
    )
    bx0, bx1, by = 60, W - 60, 206  # remaining-time bar
    d.rounded_rectangle([bx0, by, bx1, by + 10], radius=5, fill=TRACK)
    if c["frac"] > 0:
        d.rounded_rectangle(
            [bx0, by, bx0 + (bx1 - bx0) * c["frac"], by + 10], radius=5, fill=pcol
        )
    _pill(d, fonts, _CALIB_CANCEL, "CANCEL", RED, filled=False)


def _draw_tabs(d, fonts, view):
    for i, (x0, y0, x1, y1) in enumerate(TABS):
        active = i == view
        d.rounded_rectangle(
            [x0, y0, x1, y1],
            radius=10,
            fill=_tint(CYAN) if active else PANEL2,
            outline=CYAN if active else (40, 50, 70, 255),
            width=2 if active else 1,
        )
        _ctext(
            d,
            (x0 + x1) / 2,
            (y0 + y1) / 2 - fonts["badge"].size / 2 + 1,
            VIEWS[i],
            fonts["badge"],
            CYAN if active else MUTED,
        )


_BODIES = (_body_debug, _body_sensors, _body_setup)


def render(state, fonts, flash=False, view=0, locked=False, calib=None):
    from PIL import Image, ImageDraw

    im = Image.new(
        "RGBA", (W, H), (0, 0, 0, 0)
    )  # transparent outside the rounded panel
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([1, 1, W - 2, H - 2], radius=24, fill=BG, outline=EDGE, width=2)

    # header: brand · connection dot · active profile
    d.text((24, 15), "katwalk", font=fonts["label"], fill=MUTED)
    d.text(
        (24 + d.textlength("katwalk", font=fonts["label"]), 15),
        "-linux",
        font=fonts["label"],
        fill=CYAN,
    )
    hr = state.get("hr")
    if hr:
        txt = f"{int(hr)} bpm"
        pw = d.textlength(txt, font=fonts["label"])
        d.text((W - 24 - pw, 15), txt, font=fonts["label"], fill=WHITE)
        dotx = W - 24 - pw - 16
    else:
        dotx = W - 32
    d.ellipse([dotx, 16, dotx + 9, 25], fill=GREEN if state.get("connected") else RED)
    d.line([20, 37, W - 20, 37], fill=(255, 255, 255, 20), width=1)

    if calib is not None:  # guided calibration capture overrides everything
        _render_calib(d, fonts, calib)
        return im

    if flash:  # recenter confirmation toast - overrides the body
        tw = d.textlength("RECENTERED", font=fonts["h2"])
        d.rounded_rectangle(
            [(W - tw) / 2 - 28, 104, (W + tw) / 2 + 28, 158], radius=27, fill=GREEN
        )
        _ctext(d, W / 2, 112, "RECENTERED", fonts["h2"], (7, 18, 14, 255))
    elif not state.get("connected"):
        _ctext(d, W / 2, 130, state.get("status", "disconnected"), fonts["body"], MUTED)
    else:
        _BODIES[view % len(_BODIES)](d, fonts, state, locked)

    _draw_tabs(d, fonts, view)
    return im


def _cols_pos(m):
    """rotation columns [c0,c1,c2] and position from an HmdMatrix34."""
    cols = [(m[0][c], m[1][c], m[2][c]) for c in range(3)]
    return cols, (m[0][3], m[1][3], m[2][3])


def _apply(cols, v):
    return tuple(
        cols[0][i] * v[0] + cols[1][i] * v[1] + cols[2][i] * v[2] for i in range(3)
    )


# ---- rigid 3x4 transform math (so the overlay can be grabbed & re-anchored in VR) ----
# A transform is a list of 3 rows of 4: [R | t]; the implied bottom row is [0,0,0,1].


def mat_from_pose(m34):
    """3x4 list from an openvr HmdMatrix34_t."""
    return [[m34.m[i][j] for j in range(4)] for i in range(3)]


def mat_mul(a, b):
    """compose two rigid transforms: a · b."""
    out = [[0.0] * 4 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            out[i][j] = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j]
        out[i][3] = a[i][0] * b[0][3] + a[i][1] * b[1][3] + a[i][2] * b[2][3] + a[i][3]
    return out


def mat_inv(a):
    """inverse of a rigid transform: R^T, -R^T·t."""
    out = [[0.0] * 4 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            out[i][j] = a[j][i]
    for i in range(3):
        out[i][3] = -(a[0][i] * a[0][3] + a[1][i] * a[1][3] + a[2][i] * a[2][3])
    return out


def mat_trans(a):
    return (a[0][3], a[1][3], a[2][3])


def mat_dist(a, b):
    pa, pb = mat_trans(a), mat_trans(b)
    return math.sqrt(sum((pa[i] - pb[i]) ** 2 for i in range(3)))


def default_offset():
    """first-cut inner-forearm placement (relative to the host controller)."""
    return [
        [
            float(ROT_CTRL[r][0]),
            float(ROT_CTRL[r][1]),
            float(ROT_CTRL[r][2]),
            float(OFFSET_CTRL[r]),
        ]
        for r in range(3)
    ]


CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "katwalk",
    "overlay.json",
)


def _valid_offset(o):
    return (
        isinstance(o, list)
        and len(o) == 3
        and all(isinstance(r, list) and len(r) == 4 for r in o)
    )


def _read_cfg():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


DEFAULT_DIST = 0.4  # m: default hide distance (panel->HMD)


def _write_cfg(hand, offset, locked, dist):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as fh:
            json.dump(
                {
                    "hand": hand,
                    "offset": offset,
                    "locked": bool(locked),
                    "dist": float(dist),
                },
                fh,
                indent=2,
            )
    except OSError:
        pass


def load_offset(hand):
    """load the saved placement for this hand, or the default first cut."""
    d = _read_cfg()
    if d.get("hand") == hand and _valid_offset(d.get("offset")):
        return [[float(v) for v in row] for row in d["offset"]]
    return default_offset()


def load_locked(hand):
    d = _read_cfg()
    return bool(d.get("hand") == hand and d.get("locked", False))


def load_dist(hand):
    d = _read_cfg()
    if d.get("hand") == hand:
        try:
            return float(d.get("dist", DEFAULT_DIST))
        except (TypeError, ValueError):
            pass
    return DEFAULT_DIST


def save_offset(hand, offset):
    _write_cfg(hand, offset, load_locked(hand), load_dist(hand))


def save_locked(hand, locked):
    _write_cfg(hand, load_offset(hand), locked, load_dist(hand))


def save_dist(hand, dist):
    _write_cfg(hand, load_offset(hand), load_locked(hand), dist)


def demo_state(t):
    """Synthetic animated locomotion state for --demo (no hardware needed). Loops every 22s:
    idle -> walk -> sprint -> ramp down -> idle, with the heading sweeping and a recenter
    toast firing ~every 10s, so the whole HUD can be previewed in VR with just SteamVR.
    """
    c = t % 22.0
    if c < 3:
        speed = 0.0
    elif c < 10:
        speed = (c - 3) / 7.0 * 7.8  # ramp up through walk into sprint
    elif c < 14:
        speed = 7.8  # hold sprint
    elif c < 20:
        speed = 7.8 * (1.0 - (c - 14) / 6.0)  # ramp back down
    else:
        speed = 0.0
    speed = max(0.0, speed)
    moving = speed > 0.1
    return {
        "connected": True,
        "status": "demo",
        "speed": round(speed, 2),
        "moving": moving,
        "sprinting": speed >= 6.0,
        "cadence_spm": round(40 + speed * 16) if moving else 0,
        "heading": round((t * 18.0) % 360.0, 1),
        "direction": round((t * 18.0) % 360.0, 1),
        "stick": [
            round(math.sin(t * 0.8) * speed / 8.0, 2),
            round(math.cos(t * 0.8) * speed / 8.0, 2),
        ],
        "footL": [round(320 * math.sin(t * 4)), round(700 * (speed / 8.0))],
        "footR": [round(-320 * math.sin(t * 4)), round(700 * (speed / 8.0))],
        "groundedL": moving and int(t * 2) % 2 == 0,
        "groundedR": moving and int(t * 2) % 2 == 1,
        "battery": 82,
        "fw": 5,
        "params": {"sprint_threshold": 6.0},
        "recenter_seq": int((t + 4.0) / 10.0),  # flips ~every 10s -> fires the toast
        "hr": round(72 + speed * 7),  # demo heart rate: rises with pace
    }


def _hud_signature(state, flash, view, locked, calib):
    """Tuple of everything that affects the rendered pixels - re-render only when it changes."""
    if calib is not None:  # during calibration only the calib screen matters
        return ("calib", calib["idx"], calib["phase"], int(calib["rem"]))
    hd = state.get("heading")
    return (
        view,
        bool(locked),
        state.get("_dist"),
        bool(state.get("connected")),
        round(float(state.get("speed", 0) or 0), 1),
        state.get("moving"),
        state.get("sprinting"),
        int(state.get("cadence_spm", 0) or 0),
        None if hd is None else int(hd),
        None if state.get("direction") is None else int(state.get("direction")),
        None if state.get("hmd_yaw") is None else int(state.get("hmd_yaw")),
        state.get("battery"),
        state.get("hr"),
        state.get("status"),
        bool(flash),
        tuple(state.get("footL") or ()),
        tuple(state.get("footR") or ()),
        state.get("groundedL"),
        state.get("groundedR"),
        tuple(round(v, 2) for v in (state.get("stick") or ())),
        round(float((state.get("params") or {}).get("speed_multiplier", 1) or 1), 2),
        int(float((state.get("params") or {}).get("deadzone", 0) or 0)),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--hand", choices=["left", "right"], default="left")
    ap.add_argument(
        "--render-test",
        metavar="PNG",
        help="render one HUD frame to PNG and exit (no VR)",
    )
    ap.add_argument(
        "--always", action="store_true", help="skip the facing gate (always visible)"
    )
    ap.add_argument(
        "--dashboard-only",
        action="store_true",
        help="only show while the SteamVR dashboard/menu is open (game paused)",
    )
    ap.add_argument(
        "--demo",
        action="store_true",
        help="drive the HUD with synthetic animated data (no hardware) so you can "
        "preview it in VR with just SteamVR running",
    )
    args = ap.parse_args()
    if args.demo:
        args.always = True  # demo should always be visible

    try:
        fonts = load_fonts()
    except ImportError:
        print("Pillow missing:  .venv/bin/pip install pillow")
        return 2

    def get_state():
        try:
            return json.load(
                urllib.request.urlopen(f"http://localhost:{args.port}/state", timeout=1)
            )
        except Exception:
            return {"connected": False, "status": "katwalkd not running"}

    def get_profile():
        try:
            return json.load(
                urllib.request.urlopen(
                    f"http://localhost:{args.port}/profiles", timeout=1
                )
            ).get("active")
        except Exception:
            return None

    def rec(path):  # drive the daemon's motion recorder over HTTP
        try:
            urllib.request.urlopen(
                f"http://localhost:{args.port}{path}", timeout=0.5
            ).read()
        except Exception:
            pass

    if args.render_test:
        from PIL import Image

        st = demo_state(8.0) if args.demo else get_state()
        st["_hand"] = args.hand
        cols = [render(st, fonts, view=i) for i in range(len(VIEWS))]
        sheet = Image.new(
            "RGBA", (W, H * len(cols) + 12 * (len(cols) + 1)), (30, 34, 42, 255)
        )
        for i, im in enumerate(cols):
            sheet.alpha_composite(im, (0, 12 + i * (H + 12)))
        sheet.convert("RGB").save(args.render_test)
        print("wrote", args.render_test, "(views:", ", ".join(VIEWS) + ")")
        return 0

    try:
        import openvr
    except ImportError:
        print("openvr missing:  .venv/bin/pip install openvr")
        return 2
    try:
        import glfw
        from OpenGL.GL import (
            glGenTextures,
            glBindTexture,
            glTexParameteri,
            glTexImage2D,
            glTexSubImage2D,
            glFlush,
            GL_TEXTURE_2D,
            GL_TEXTURE_MIN_FILTER,
            GL_TEXTURE_MAG_FILTER,
            GL_LINEAR,
            GL_RGBA8,
            GL_RGBA,
            GL_UNSIGNED_BYTE,
        )
    except ImportError as e:
        print("GL deps missing:  .venv/bin/pip install glfw PyOpenGL  (", e, ")")
        return 2

    # GPU-texture path (proven flicker-free via tools/overlay_gl_probe): render into an OpenGL
    # texture and hand it to a SINGLE overlay with SetOverlayTexture. A hidden GLFW window
    # provides the GL context.
    if not glfw.init():
        print("glfw.init failed")
        return 1
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    glwin = glfw.create_window(64, 64, "katwalk-hud-gl", None, None)
    if not glwin:
        print("could not create GL context")
        return 1
    glfw.make_context_current(glwin)
    gl_tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, gl_tex)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)

    # Retry init forever: we can be launched before SteamVR is up, and we re-exec on a SteamVR
    # restart (VREvent_Quit, below), so on the way back this patiently waits for it to return.
    # Ctrl-C / SIGTERM during the wait still exits.
    vr = None
    attempt = 0
    while vr is None:
        try:
            vr = openvr.init(openvr.VRApplication_Background)
        except Exception as e:
            if attempt == 0:
                print("waiting for SteamVR…", e)
            attempt += 1
            time.sleep(3)
    sys.stdout.reconfigure(  # type: ignore[attr-defined]  # runtime stdout is a TextIOWrapper
        line_buffering=True
    )  # log file was buffered -> update it live
    print(
        f"[overlay] init OK - demo={args.demo} hand={args.hand} (GL texture id {int(gl_tex)})"
    )

    texture = openvr.Texture_t()
    texture.handle = int(gl_tex)
    texture.eType = openvr.TextureType_OpenGL
    texture.eColorSpace = openvr.ColorSpace_Gamma

    role = (
        openvr.TrackedControllerRole_LeftHand
        if args.hand == "left"
        else openvr.TrackedControllerRole_RightHand
    )
    ov = openvr.VROverlay()
    handle = ov.createOverlay("katwalk-linux.hud", "katwalk-linux HUD")
    ov.setOverlayWidthInMeters(handle, WIDTH_M)
    mouse_scale = openvr.HmdVector2_t()
    mouse_scale.v[0], mouse_scale.v[1] = float(W), float(
        H
    )  # clicks reported in pixel coords
    ov.setOverlayMouseScale(handle, mouse_scale)
    ov.setOverlayInputMethod(handle, openvr.VROverlayInputMethod_Mouse)
    ov.setOverlayFlag(
        handle, openvr.VROverlayFlags_MakeOverlaysInteractiveIfVisible, True
    )
    tb = openvr.VRTextureBounds_t()
    tb.uMin, tb.vMin, tb.uMax, tb.vMax = (
        0.0,
        1.0,
        1.0,
        0.0,
    )  # flip V (GL textures are bottom-up)
    ov.setOverlayTextureBounds(handle, tb)

    # Interaction (all via the overlay laser): TRIGGER (left mouse button) taps the bottom tabs;
    # GRIP (the other mouse button the controller emits while pointing) drags the panel with the
    # pointing (other) hand.
    INVALID = openvr.k_unTrackedDeviceIndexInvalid
    HMD = openvr.k_unTrackedDeviceIndex_Hmd
    DIST_MIN, DIST_MAX, DIST_STEP = 0.2, 1.5, 0.05  # hide-distance adjust range/step
    DIST_HYST = (
        0.15  # m past `dist` before hiding, so the panel cannot flicker on the edge
    )
    NEAR_PERSIST = (
        6  # frames a near/far flip must hold (kills in-game pose-read jitter)
    )
    grab_role = (
        openvr.TrackedControllerRole_RightHand
        if args.hand == "left"
        else openvr.TrackedControllerRole_LeftHand
    )

    def to_hmd(a):
        m = openvr.HmdMatrix34_t()
        for i in range(3):
            for j in range(4):
                m.m[i][j] = float(a[i][j])
        return m

    def pose_mat(poses, idx):
        if idx == INVALID or not poses[idx].bPoseIsValid:
            return None
        return mat_from_pose(poses[idx].mDeviceToAbsoluteTracking)

    anchor = load_offset(args.hand)  # 3x4 overlay pose relative to the controller
    locked = load_locked(args.hand)  # drag locked? (toggled on the SETUP tab)
    dist = load_dist(args.hand)  # hide distance (panel→HMD), adjusted on SETUP
    dragging = False
    drag_dev = INVALID  # controller dragging the panel (grip)
    drag_offset = None  # overlay pose relative to that controller while dragging
    view = 0  # which body tab is shown
    calib = None  # active guided calibration: {"i", "phase", "end"}
    rebind = False
    bound = None  # ("drag",idx)|("dev",idx) - transform re-set only on change
    shown = False
    near = False  # hysteresis gate state, persists across frames
    near_hold = 0
    ev = openvr.VREvent_t()
    oev = openvr.VREvent_t()  # overlay (laser/mouse) events
    other = "right" if args.hand == "left" else "left"
    print(
        f"katwalk-linux forearm HUD ({args.hand} hand). point the laser at the panel; tap the "
        f"bottom tabs; hold the {other} GRIP while pointing to drag it. Ctrl-C to stop."
    )
    last_seq = None
    flash_until = 0.0
    prof_name = None
    prof_next = 0.0
    state_next = 0.0
    live_state = {"connected": False, "status": "connecting…"}
    last_sig = None
    last_warn = 0.0
    last_hb = None
    frames = 0
    start = time.monotonic()
    last_ok = (
        start  # last fully-successful frame; if SteamVR stops answering we reconnect
    )

    def _reexec():
        """SteamVR went away (shutting down or crashed). Re-exec this same process (keeps the
        PID, so ./run still tracks it) to cleanly re-init and wait for SteamVR to return - the
        overlay's equivalent of the daemon reconnecting to the hardware. Does not return.
        """
        print("[overlay] SteamVR gone - reconnecting (restarting overlay)…", flush=True)
        for teardown in (
            lambda: ov.destroyOverlay(handle),
            openvr.shutdown,
            glfw.terminate,
        ):
            try:
                teardown()
            except Exception:
                pass
        os.execv(
            sys.executable, [sys.executable, "-m", "katwalk.overlay", *sys.argv[1:]]
        )

    # Clean teardown on Ctrl-C AND on `kill`/SIGTERM. Default SIGTERM exits WITHOUT running the
    # finally below, which would orphan the overlay key -> OverlayError_KeyInUse next launch.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            now = time.monotonic()
            try:
                if args.demo:
                    state = demo_state(now - start)
                else:
                    if (
                        now >= state_next
                    ):  # poll the daemon ~15 Hz, not every 60fps frame
                        state_next = now + 0.07
                        live_state = get_state()
                        if now >= prof_next:  # refresh active profile name infrequently
                            prof_next = now + 2.0
                            prof_name = get_profile()
                        live_state["_profile"] = prof_name
                    state = live_state
                seq = state.get("recenter_seq")
                if seq is not None and seq != last_seq:
                    if last_seq is not None:  # not the first read - an actual recenter
                        flash_until = now + 1.6
                    last_seq = seq
                flashing = now < flash_until
                state["_hand"] = args.hand
                state["_dist"] = dist

                poses = vr.getDeviceToAbsoluteTrackingPose(
                    openvr.TrackingUniverseStanding, 0, openvr.k_unMaxTrackedDeviceCount
                )
                ctrl = vr.getTrackedDeviceIndexForControllerRole(role)
                host_m = pose_mat(poses, ctrl)

                while vr.pollNextEvent(ev):  # drain system events
                    if ev.eventType == openvr.VREvent_Quit:
                        _reexec()  # SteamVR shutting down: restart & wait for it to come back

                # Laser/mouse events: MouseMove marks the laser as on the panel (enables grip-
                # drag); a trigger click hit-tests the bottom tabs to switch the body view.
                while True:
                    okp, oev = ov.pollNextOverlayEvent(
                        handle, oev
                    )  # returns (ok, event)
                    if not okp:
                        break
                    et = oev.eventType
                    mx, my = (
                        oev.data.mouse.x,
                        oev.data.mouse.y,
                    )  # mouse y already = image y (top-left)
                    if et == openvr.VREvent_MouseButtonDown:
                        btn = oev.data.mouse.button
                        print(f"[overlay] CLICK x={mx:.0f} y={my:.0f} btn={btn}")
                        if calib is not None:  # calibrating: only CANCEL
                            if btn == openvr.VRMouseButton_Left:
                                cx0, cy0, cx1, cy1 = _CALIB_CANCEL
                                if cx0 <= mx <= cx1 and cy0 <= my <= cy1:
                                    rec("/record/stop")
                                    calib = None
                                    print("[overlay] calibration cancelled")
                        elif (
                            btn == openvr.VRMouseButton_Left
                        ):  # TRIGGER -> tabs / controls
                            hit = False
                            for i, (x0, y0, x1, y1) in enumerate(TABS):
                                if x0 <= mx <= x1 and y0 <= my <= y1:
                                    if i != view:
                                        view = i
                                        print(f"[overlay] tab -> {VIEWS[i]}")
                                    hit = True
                                    break
                            if not hit and VIEWS[view] == "SETUP":
                                p = state.get("params") or {}

                                def _in(r):
                                    return r[0] <= mx <= r[2] and r[1] <= my <= r[3]

                                if _in(_SETUP_LOCK):  # lock toggle
                                    locked = not locked
                                    save_locked(args.hand, locked)
                                    print(
                                        f"[overlay] drag {'LOCKED' if locked else 'UNLOCKED'}"
                                    )
                                elif _in(_SPD_MINUS) or _in(
                                    _SPD_PLUS
                                ):  # speed multiplier ±
                                    cur = float(p.get("speed_multiplier", 1.0))
                                    new = round(
                                        min(
                                            3.0,
                                            max(
                                                0.2,
                                                cur
                                                + (0.05 if _in(_SPD_PLUS) else -0.05),
                                            ),
                                        ),
                                        2,
                                    )
                                    rec(f"/set?speed_multiplier={new}")
                                    print(f"[overlay] speed_multiplier -> {new}")
                                elif _in(_DZ_MINUS) or _in(_DZ_PLUS):  # deadzone ±
                                    cur = float(p.get("deadzone", 60))
                                    new = int(
                                        min(
                                            300,
                                            max(
                                                0, cur + (10 if _in(_DZ_PLUS) else -10)
                                            ),
                                        )
                                    )
                                    rec(f"/set?deadzone={new}")
                                    print(f"[overlay] deadzone -> {new}")
                                elif _in(_DIST_MINUS) or _in(
                                    _DIST_PLUS
                                ):  # hide-distance ±
                                    dist = round(
                                        min(
                                            DIST_MAX,
                                            max(
                                                DIST_MIN,
                                                dist
                                                + (
                                                    DIST_STEP
                                                    if _in(_DIST_PLUS)
                                                    else -DIST_STEP
                                                ),
                                            ),
                                        ),
                                        2,
                                    )
                                    save_dist(args.hand, dist)
                                    print(f"[overlay] hide distance -> {dist:.2f} m")
                        elif not locked and calib is None:  # GRIP -> drag (unlocked)
                            gi = vr.getTrackedDeviceIndexForControllerRole(grab_role)
                            dp = pose_mat(poses, gi)
                            if dp is not None and host_m is not None:
                                drag_offset = mat_mul(
                                    mat_inv(dp), mat_mul(host_m, anchor)
                                )
                                drag_dev, dragging = gi, True
                                print(f"[overlay] drag start (dev {gi})")
                            else:
                                print(
                                    f"[overlay] drag FAILED gi={gi} dp={'ok' if dp else 'NONE'} "
                                    f"host={'ok' if host_m is not None else 'NONE'}"
                                )
                    elif et == openvr.VREvent_MouseButtonUp:
                        if dragging:
                            dp = pose_mat(poses, drag_dev)
                            if (
                                dp is not None
                                and host_m is not None
                                and drag_offset is not None
                            ):
                                anchor = mat_mul(
                                    mat_inv(host_m), mat_mul(dp, drag_offset)
                                )
                                save_offset(args.hand, anchor)
                                rebind = True
                            dragging = False
                            print("[overlay] drag end -> re-anchored & saved")

                # --- guided calibration state machine (records via the daemon /record API) ---
                calib_view = None
                if calib is not None:
                    if now >= calib["end"]:
                        i, ph = calib["i"], calib["phase"]
                        if ph == "ready":
                            rec(
                                f"/record/start?new={1 if i == 0 else 0}&label={CALIB_SEQ[i][0]}"
                            )
                            calib["phase"], calib["end"] = "rec", now + CALIB_SEQ[i][3]
                        elif ph == "rec":
                            rec("/record/stop")
                            if i + 1 < len(CALIB_SEQ):
                                calib.update(
                                    i=i + 1, phase="rest", end=now + CALIB_REST
                                )
                            else:
                                calib["phase"], calib["end"] = "done", now + 5
                        elif ph == "rest":
                            calib["phase"], calib["end"] = "ready", now + CALIB_READY
                        else:  # done -> back to the HUD
                            calib = None
                    if calib is not None:
                        i, ph = calib["i"], calib["phase"]
                        prompt, sub, dur = (
                            CALIB_SEQ[i][1],
                            CALIB_SEQ[i][2],
                            CALIB_SEQ[i][3],
                        )
                        span = {
                            "ready": CALIB_READY,
                            "rec": dur,
                            "rest": CALIB_REST,
                            "done": 5,
                        }[ph]
                        rem = max(0.0, calib["end"] - now)
                        cv = {
                            "ready": ("GET READY", ORANGE, prompt, sub),
                            "rec": ("RECORDING", GREEN, prompt, sub),
                            "rest": ("REST", MUTED, "REST", "next: " + CALIB_SEQ[i][1]),
                            "done": ("DONE", CYAN, "DONE ✓", "capture saved"),
                        }[ph]
                        calib_view = dict(
                            phase=cv[0],
                            color=cv[1],
                            prompt=cv[2],
                            sub=cv[3],
                            idx=min(i + 1, len(CALIB_SEQ)),
                            total=len(CALIB_SEQ),
                            rem=rem,
                            frac=(rem / span if span else 0),
                        )

                # Transform: drag rides the pointing controller; else the host controller.
                # Set only on change (every-frame set causes jitter).
                want = (
                    ("drag", drag_dev)
                    if dragging
                    else (("dev", ctrl) if ctrl != INVALID else None)
                )
                if want is not None and (want != bound or rebind):
                    if want[0] == "drag":
                        ov.setOverlayTransformTrackedDeviceRelative(
                            handle, drag_dev, to_hmd(drag_offset)
                        )
                    else:
                        ov.setOverlayTransformTrackedDeviceRelative(
                            handle, ctrl, to_hmd(anchor)
                        )
                    bound, rebind = want, False
                    print(f"[overlay] bound transform: {want}")

                # Visibility gate, hardened against jitter. As a background overlay app our pose
                # reads go noisy the moment a game is presenting, which made the distance test
                # flicker across its threshold and the panel stutter in and out. Fix: only flip the
                # decision when the panel is CLEARLY near or CLEARLY far (hysteresis), held for a few
                # frames; a bad/absent pose holds the last decision instead of flipping.
                hmd_m = pose_mat(poses, HMD)
                target = near
                if not dragging and host_m is not None and hmd_m is not None:
                    d_now = mat_dist(mat_mul(host_m, anchor), hmd_m)
                    if d_now < dist:
                        target = True
                    elif d_now > dist + DIST_HYST:
                        target = False
                    # inside [dist, dist+HYST]: keep the current decision
                if target != near:
                    near_hold += 1
                    if near_hold >= NEAR_PERSIST:
                        near, near_hold = target, 0
                else:
                    near_hold = 0
                vis = bound is not None and (
                    args.always
                    or near
                    or dragging
                    or flashing
                    or calib_view is not None
                )
                if args.dashboard_only:  # optional: only show in the SteamVR menu
                    try:
                        vis = vis and ov.isDashboardVisible()
                    except Exception:
                        pass
                if vis and not shown:
                    ov.showOverlay(handle)
                    shown = True
                    last_sig = None  # force a fresh frame
                elif not vis and shown:
                    ov.hideOverlay(handle)
                    shown = False

                # Render -> GL texture -> overlay, only when shown and the content changed. The
                # GPU texture is persistent, so unchanged frames need no work and never flicker.
                if shown:
                    sig = _hud_signature(state, flashing, view, locked, calib_view)
                    if sig != last_sig:
                        data = render(
                            state,
                            fonts,
                            flash=flashing,
                            view=view,
                            locked=locked,
                            calib=calib_view,
                        ).tobytes()
                        glBindTexture(GL_TEXTURE_2D, gl_tex)
                        glTexSubImage2D(
                            GL_TEXTURE_2D,
                            0,
                            0,
                            0,
                            W,
                            H,
                            GL_RGBA,
                            GL_UNSIGNED_BYTE,
                            data,
                        )
                        glFlush()
                        ov.setOverlayTexture(handle, texture)
                        last_sig = sig
                        frames += 1

                hb = (
                    shown,
                    ctrl != INVALID,
                    view,
                    dragging,
                )  # log only on a state change
                if hb != last_hb:
                    last_hb = hb
                    print(
                        f"[overlay] {'shown' if shown else 'hidden'}, "
                        f"ctrl={'ok' if ctrl != INVALID else 'lost'}, view={VIEWS[view]}"
                        f"{', dragging' if dragging else ''}"
                    )
                last_ok = now  # a full clean iteration -> SteamVR is responsive
            except openvr.OpenVRError as e:
                if now - last_warn > 5.0:
                    print("[overlay] SteamVR busy:", e)
                    last_warn = now
                if now - last_ok > 10.0:  # unresponsive too long (crash?) -> reconnect
                    _reexec()
                last_sig = None
                time.sleep(0.2)
            except Exception as e:  # log runtime errors instead of crashing
                import traceback

                print("[overlay] UNEXPECTED ERROR:", repr(e))
                traceback.print_exc()
                if now - last_ok > 10.0:
                    _reexec()
                time.sleep(0.5)
            time.sleep(1 / 60.0)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ov.destroyOverlay(
                handle
            )  # release the overlay key at once for a clean relaunch
        except Exception:
            pass
        openvr.shutdown()
        glfw.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
