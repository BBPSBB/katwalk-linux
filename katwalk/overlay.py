#!/usr/bin/env python3
"""katwalk-linux HUD renderer - the overlay's UI brain (pure Pillow, no VR APIs).

Draws the wrist panel: DEBUG dashboard (foot pads, heading dial, output stick), SENSORS,
SETUP (steppers), the calibration screen, and the recenter toast. Consumed by
katwalk.overlay_xr, which ships the rendered frames to the OpenXR layer over shared memory
and feeds clicks back into the tab/stepper rectangles defined here.

History: this module once contained a full SteamVR IVROverlay client (OpenVR + GL texture
upload + laser events). That backend is gone - the OpenXR layer owns display, anchoring and
input now (docs/OPENXR-HUD-MIGRATION.md). What remains is the renderer, deliberately free of
VR dependencies so it stays testable headless (see tests/test_overlay.py).
"""

from __future__ import annotations

import math

try:
    from katwalk import __version__ as VERSION
except Exception:  # loaded standalone (tests) without the package on the path
    VERSION = "?"

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


def _tab_rects():
    n, m, gap = len(VIEWS), 20, 12
    wt = (W - 2 * m - gap * (n - 1)) / n
    return [
        (m + i * (wt + gap), _TAB_Y0, m + i * (wt + gap) + wt, _TAB_Y1)
        for i in range(n)
    ]


TABS = _tab_rects()  # (x0, y0, x1, y1) per tab, in image (top-left) coords

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


def _tint(col, frac=0.20):
    """opaque blend of col over the panel BG, so fills don't punch transparent holes."""
    return tuple(round(BG[i] + (col[i] - BG[i]) * frac) for i in range(3)) + (255,)


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
    _bx = 24 + d.textlength("katwalk-linux", font=fonts["label"])
    d.text((_bx + 6, 15), f"v{VERSION}", font=fonts["label"], fill=(90, 100, 120, 255))
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


DEFAULT_DIST = (
    0.65  # m: default visibility distance shown before the conf value arrives
)


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
