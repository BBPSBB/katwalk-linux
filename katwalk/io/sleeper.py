#!/usr/bin/env python3
"""Put the KAT devices to sleep, in the background.

Spawned detached by katwalk/daemon.py on shutdown so a stop command returns instantly while
the devices go to sleep here. The actual sleep trick (CONFIRMED on hardware, see
docs/PLUSE-PROTOCOL.md): DETACH usbhid from all three devices (receiver 3f12 + base bf12 +
armband bf13) and HOLD it detached for a few seconds. On Linux usbhid polls the IN
endpoints forever, which keeps the devices awake; only stopping that polling lets them
drop to slow-blink. We also SET_IDLE while we own the interface. Then close -> the kernel
auto-reattaches usbhid and the devices stay asleep until the next 0x30 poll + motion.

Self-contained (no katwalk imports) so it runs fine after the parent daemon has exited.
Needs usbfs access - the `SUBSYSTEM=="usb"` rule in udev/70-katvr.rules.

Usage: sleeper.py [hold_seconds]
"""

import fcntl
import glob
import os
import struct
import sys
import time

VID = "c4f4"
USBDEVFS_CONTROL = 0xC0185500
USBDEVFS_DISCONNECT_CLAIM = 0x8108551B
PIDS = ("3f12", "bf12", "bf13")  # receiver, base/seat, armband


def usb_node(pid):
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            if open(d + "/idVendor").read().strip() != VID:
                continue
            if open(d + "/idProduct").read().strip().lower() != pid:
                continue
            return "/dev/bus/usb/%03d/%03d" % (
                int(open(d + "/busnum").read()),
                int(open(d + "/devnum").read()),
            )
        except OSError:
            continue
    return None


def main():
    hold = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    held = []
    for pid in PIDS:
        node = usb_node(pid)
        if not node:
            continue
        try:
            fd = os.open(node, os.O_RDWR)
            # detach usbhid + claim iface0 (stops the polling; also lets SET_IDLE through)
            fcntl.ioctl(
                fd, USBDEVFS_DISCONNECT_CLAIM, struct.pack("<II256s", 0, 0, b"")
            )
            try:
                fcntl.ioctl(
                    fd,
                    USBDEVFS_CONTROL,
                    struct.pack("<BBHHHI4xQ", 0x21, 0x0A, 0, 0, 0, 1000, 0),
                )
            except OSError:
                pass
            held.append(fd)
        except OSError:
            pass
    time.sleep(hold)  # usbhid stays detached -> devices sleep
    for fd in held:
        try:
            os.close(fd)  # reattach usbhid; devices stay asleep
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
