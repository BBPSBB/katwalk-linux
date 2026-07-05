"""Virtual Xbox-style gamepad via Linux uinput.

Pure stdlib (raw ioctls on /dev/uinput) so there are NO compiled dependencies on this
immutable host. Presents as a wired Xbox 360 pad (045e:028e) so Steam/games treat it as
a standard controller. Map per the Gateway: left stick = walk vector, L3 = sprint, A = jump.

Needs write access to /dev/uinput: install udev/71-katvr-uinput.rules (or run as root).
If /dev/uinput is missing: sudo modprobe uinput.

The main path is the VR drivers fed over shared memory (openvr-driver/, openxr-driver/);
this gamepad is a simpler alternative output for flatscreen/gamepad games.
"""

from __future__ import annotations

import fcntl
import os
import struct
import time


# ---- ioctl encoding (_IOC: dir<<30 | size<<16 | type<<8 | nr), type 'U' = 0x55 ----
def _IOW(nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (ord("U") << 8) | nr


def _IO(nr: int) -> int:
    return (ord("U") << 8) | nr


UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT = _IOW(100, 4), _IOW(101, 4), _IOW(103, 4)
UI_DEV_SETUP, UI_ABS_SETUP = _IOW(3, 92), _IOW(4, 28)
UI_DEV_CREATE, UI_DEV_DESTROY = _IO(1), _IO(2)

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT, BUS_USB = 0x00, 0x03
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
BTN_A, BTN_B, BTN_X, BTN_Y = 0x130, 0x131, 0x133, 0x134
BTN_TL, BTN_TR, BTN_SELECT, BTN_START = 0x136, 0x137, 0x13A, 0x13B
BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13C, 0x13D, 0x13E

AXIS_MIN, AXIS_MAX = -32768, 32767
VENDOR, PRODUCT, VERSION = 0x045E, 0x028E, 0x0110

_BUTTONS = [
    BTN_A,
    BTN_B,
    BTN_X,
    BTN_Y,
    BTN_TL,
    BTN_TR,
    BTN_SELECT,
    BTN_START,
    BTN_MODE,
    BTN_THUMBL,
    BTN_THUMBR,
]
_AXES = [  # (code, min, max)
    (ABS_X, AXIS_MIN, AXIS_MAX),
    (ABS_Y, AXIS_MIN, AXIS_MAX),
    (ABS_RX, AXIS_MIN, AXIS_MAX),
    (ABS_RY, AXIS_MIN, AXIS_MAX),
    (ABS_Z, 0, 255),
    (ABS_RZ, 0, 255),
    (ABS_HAT0X, -1, 1),
    (ABS_HAT0Y, -1, 1),
]


class VirtualGamepad:
    def __init__(self, name: str = "katwalk-linux Gamepad"):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            for ev in (EV_KEY, EV_ABS, EV_SYN):
                fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
            for b in _BUTTONS:
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, b)
            for code, lo, hi in _AXES:
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
                # uinput_abs_setup: __u16 code (+2 pad) + input_absinfo(6×__s32)
                absinfo = struct.pack("6i", 0, lo, hi, 0, 0, 0)
                fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("Hxx", code) + absinfo)
            # uinput_setup: input_id(4×u16) + char name[80] + __u32 ff_effects_max
            setup = (
                struct.pack("4H", BUS_USB, VENDOR, PRODUCT, VERSION)
                + name.encode()[:79].ljust(80, b"\x00")
                + struct.pack("I", 0)
            )
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
            time.sleep(0.1)  # let the kernel/udev create the device node
        except Exception:
            os.close(self.fd)
            raise

    def _emit(self, etype: int, code: int, value: int) -> None:
        # struct input_event: timeval(2×long) + __u16 type + __u16 code + __s32 value
        os.write(self.fd, struct.pack("llHHi", 0, 0, etype, code, value))

    @staticmethod
    def _scale(v: float) -> int:
        v = max(-1.0, min(1.0, v))
        return int(round(v * (AXIS_MAX if v >= 0 else -AXIS_MIN)))

    def left_stick(self, x: float, y: float) -> None:
        """x, y in [-1, 1]; +y = forward (ABS_Y is +down, so invert)."""
        self._emit(EV_ABS, ABS_X, self._scale(x))
        self._emit(EV_ABS, ABS_Y, self._scale(-y))

    def button(self, code: int, pressed: bool) -> None:
        self._emit(EV_KEY, code, 1 if pressed else 0)

    def set_locomotion(
        self, x: float, y: float, sprint: bool = False, jump: bool = False
    ) -> None:
        """Walk vector on the left stick, L3 = sprint, A = jump (Gateway mapping)."""
        self.left_stick(x, y)
        self.button(BTN_THUMBL, sprint)
        self.button(BTN_A, jump)
        self.syn()

    def syn(self) -> None:
        self._emit(EV_SYN, SYN_REPORT, 0)

    def close(self) -> None:
        try:
            self.left_stick(0.0, 0.0)
            self.button(BTN_THUMBL, False)
            self.button(BTN_A, False)
            self.syn()
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass

    def __enter__(self) -> "VirtualGamepad":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
