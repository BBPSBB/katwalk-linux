"""Discover the KATVR treadmill's HID devices by ROLE, not by a hardcoded product id.

Different C2 variants use different USB product ids (this author's C2+ receiver is 3f12, the
C2 core is 3f37, the C2+ shows 2f37, ...), but the kernel's HID_NAME always carries the role
word - "receiver", "position" (the seat/base), or "armband" - so we match on that instead.
A saved selection from `python -m katwalk.configure` can pin a specific device by serial to
override detection (useful with several units attached or an oddly-named variant).
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from pathlib import Path

VID = "C4F4"  # KATVR, uppercase for HID_ID matching
VID_LOWER = "c4f4"  # sysfs idVendor is lowercase
# role -> the keyword that appears in HID_NAME for that device, on any variant
ROLE_KEYWORD = {"receiver": "receiver", "seat": "position", "armband": "armband"}
# last-resort fallback ids if a device has no recognisable HID_NAME (the author's C2+ plusE)
LEGACY_PID = {"receiver": "3F12", "seat": "BF12", "armband": "BF13"}
CONFIG = Path.home() / ".config" / "katwalk" / "device.json"


@dataclass
class HidDevice:
    path: str  # /dev/hidrawN
    pid: str  # 4-hex, uppercase (e.g. "3F37")
    name: str  # HID_NAME (e.g. "KATVR walk c2 core receiver")
    serial: str  # HID_UNIQ
    role: str | None  # "receiver" | "seat" | "armband" | None if unrecognised


def _uevent(hidraw_dir: Path) -> dict:
    out: dict[str, str] = {}
    try:
        for line in (hidraw_dir / "device" / "uevent").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    except OSError:
        pass
    return out


def scan() -> list[HidDevice]:
    """Every connected KATVR HID device, with its decoded role (by HID_NAME keyword)."""
    devs: list[HidDevice] = []
    for n in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        ue = _uevent(n)
        hid_id = ue.get("HID_ID", "").upper()  # 0003:0000C4F4:00003F12
        if VID not in hid_id:
            continue
        pid = hid_id.split(":")[-1][-4:] if ":" in hid_id else ""
        name = ue.get("HID_NAME", "")
        low = name.lower()
        role = next((r for r, kw in ROLE_KEYWORD.items() if kw in low), None)
        devs.append(
            HidDevice(f"/dev/{n.name}", pid, name, ue.get("HID_UNIQ", ""), role)
        )
    return devs


def c4f4_usb_nodes() -> list[str]:
    """/dev/bus/usb/BBB/DDD paths for ALL KATVR USB devices (any variant/pid). Used for the
    usbfs usbhid detach/rebind, which must not depend on specific product ids."""
    nodes = []
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            if open(d + "/idVendor").read().strip().lower() != VID_LOWER:
                continue
            nodes.append(
                "/dev/bus/usb/%03d/%03d"
                % (int(open(d + "/busnum").read()), int(open(d + "/devnum").read()))
            )
        except OSError:
            continue
    return nodes


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def resolve(
    role: str, devs: list[HidDevice] | None = None, cfg: dict | None = None
) -> HidDevice | None:
    """Resolve a role to a device: a pinned serial (saved config) wins, then the HID_NAME role
    keyword (variant-agnostic), then the legacy hardcoded pid. None if nothing matches.
    """
    devs = scan() if devs is None else devs
    cfg = load_config() if cfg is None else cfg
    pinned = (cfg.get(role) or {}).get("serial")
    if pinned:
        for d in devs:
            if d.serial == pinned:
                return d
    for d in devs:
        if d.role == role:
            return d
    for d in devs:
        if d.pid == LEGACY_PID.get(role):
            return d
    return None
