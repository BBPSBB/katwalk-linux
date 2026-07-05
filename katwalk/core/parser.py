"""Decode KAT Walk C2+ ("plusE") USB sensor frames.

CONFIRMED against two live Windows captures (2026-06-23, see
docs/PLUSE-PROTOCOL.md). This unit is the "plusE" revision (VID c4f4, three
USB devices behind a hub):

    bf12 = position   (foot ground-contact; base/desktop position)
    3f12 = receiver   (body IMU + both feet)   <- the main stream
    bf13 = armband    (heart rate + battery)

All frames are fixed 32 bytes:

    off 0  1  2  | 3  4 | 5    | 6       | 7 ...
        1f 55 aa | xx 00| type | subtype | payload

The receiver multiplexes type 0x30 by subtype: 0x00 body quaternion,
0x01 left foot, 0x02 right foot. Idle payloads use the sentinel
"4e 20 00 64 05" (config constants, not live data).

Reference: https://medium.com/@datacompboy/katwalk-c2-part-2-peeking-eavesdropping-sniffing-and-learning-how-to-communicate-with-unknown-bb390c089a00
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER = bytes((0x1F, 0x55, 0xAA))  # CONFIRMED

# receiver body-orientation: type 0x30 / subtype 0x00, 4x int16 LE @ off 7.
QUAT_OFFSET = 7
QUAT_SCALE = 2**-14  # CONFIRMED: yields unit quaternion

# receiver foot position: type 0x30 / subtype 0x01 (left) or 0x02 (right).
# Two signed int16 LE, centred at 0, observed range ~+/-600 (more at hard slide).
#   X @ off 21 = lateral (strafe);  Y @ off 23 = fore/aft.
# NOTE the inversion: the optical sensor reads the shoe sliding on the deck, so
# shoe-slides-backward => +Y => user walks FORWARD;  -Y => backward.
FOOT_X_OFFSET = 21
FOOT_Y_OFFSET = 23
# A valid optical-position frame has 0x01 here. A shoe that woke in the wrong mode streams its
# IMU orientation quaternion in this slot instead (off7 = the low byte of the first component),
# so off7 != 0x01 means "not a position frame" and its X/Y must NOT be trusted.
FOOT_MODE_OFFSET = 7
FOOT_MODE_POSITION = 0x01


@dataclass
class SensorFrame:
    type: int | None
    subtype: int | None
    kind: str  # body|foot_left|foot_right|contact|armband|other|invalid
    quat: (
        tuple[float, float, float, float] | None
    )  # body orientation (w,x,y,z), assumed order
    foot: tuple[int, int] | None  # (x, y) signed int16: x=strafe, +y=walk forward
    contact: bool | None  # foot on ground (position device)
    heart_rate: int | None  # bpm (armband)
    battery: int | None  # percent
    firmware: int | None  # firmware major version (e.g. 3,4,5)
    raw: bytes


def _blank(buf: bytes, kind: str = "invalid") -> SensorFrame:
    return SensorFrame(
        type=None,
        subtype=None,
        kind=kind,
        quat=None,
        foot=None,
        contact=None,
        heart_rate=None,
        battery=None,
        firmware=None,
        raw=buf,
    )


def parse(buf: bytes) -> SensorFrame:
    """Decode one 32-byte plusE frame. Only fields confirmed by capture are
    populated; everything else stays None. Pass frames from any of the three
    devices - the (type, subtype, off3) tuple disambiguates."""
    if len(buf) < 8 or buf[0:3] != HEADER:
        return _blank(buf)

    off3, ftype, subtype = buf[3], buf[5], buf[6]
    f = _blank(buf, "other")
    f.type, f.subtype = ftype, subtype

    if ftype == 0x30 and subtype == 0x00 and len(buf) >= QUAT_OFFSET + 8:
        q = struct.unpack_from("<4h", buf, QUAT_OFFSET)
        f.kind, f.quat = "body", tuple(v * QUAT_SCALE for v in q)

    elif ftype == 0x30 and subtype in (0x01, 0x02) and len(buf) >= FOOT_Y_OFFSET + 2:
        f.kind = "foot_left" if subtype == 0x01 else "foot_right"
        # Only decode a position when this is actually a position frame. A mis-moded shoe
        # streams an IMU orientation quaternion here whose bytes look like a stuck position
        # and would inject phantom motion; leave foot=None ("no reading") for those.
        if buf[FOOT_MODE_OFFSET] == FOOT_MODE_POSITION:
            x = struct.unpack_from("<h", buf, FOOT_X_OFFSET)[0]
            y = struct.unpack_from("<h", buf, FOOT_Y_OFFSET)[0]
            f.foot = (x, y)

    elif ftype == 0x05 and len(buf) >= 8:  # receiver self-report (init)
        f.kind, f.firmware = "receiver_info", buf[7]  # off7 = receiver fw major (V3)

    elif ftype == 0x32 and subtype in (0x00, 0x01, 0x02) and len(buf) >= 12:
        # per-sensor status: off10 = battery %, off11 = firmware major
        # (both CONFIRMED exact vs app readout)
        f.kind = {0x00: "status_direction", 0x01: "status_left", 0x02: "status_right"}[
            subtype
        ]
        f.battery = buf[10]
        f.firmware = buf[11]

    elif ftype == 0x40 and off3 == 0x00:  # armband
        f.kind, f.battery, f.heart_rate = "armband", buf[7], buf[9]

    elif ftype == 0x40 and off3 == 0x05:  # Vehicle Hub / seat (dev24 bf12)
        # CONFIRMED via seat-only capture: dev24 is the seat, not foot-contact.
        # off9 = seat connected/active bit.
        f.kind, f.contact = "seat", bool(buf[9])
        if buf[7] == 0x02:  # status frame: off8 = battery RAW
            # raw tracks battery: 156->54%, 180->60% (2-point linear fit).
            # app applies its own raw->% curve; this is a local approximation.
            f.battery = round(buf[8] / 4 + 15)

    return f
