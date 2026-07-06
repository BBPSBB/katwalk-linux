"""Interactive setup: pick which KATVR device is which, and pin it, so the daemon works on any
C2 variant (the receiver's USB product id differs across C2 / C2+ / C2 core). Run:

    python -m katwalk.configure

Detection is already automatic by device name - this wizard is for confirming or overriding it
(several units attached, an oddly-named variant, or forcing a specific unit). It saves the
selection (pinned by serial) to ~/.config/katwalk/device.json. Pass --auto (or pipe no input)
to just accept auto-detection and save without prompting.
"""

from __future__ import annotations

import sys

from katwalk.io import devices

G = "\033[0;32m"
Y = "\033[1;33m"
R = "\033[0;31m"
B = "\033[0;34m"
DIM = "\033[2m"
NC = "\033[0m"
ROLES = [("receiver", True), ("seat", False), ("armband", False)]  # (role, required?)


def _fmt(d) -> str:
    return f"{d.name}  {DIM}(c4f4:{d.pid.lower()}  serial {d.serial}  {d.path}){NC}"


def _save(chosen: dict) -> int:
    cfg = {
        role: {"serial": d.serial, "pid": d.pid, "name": d.name}
        for role, d in chosen.items()
    }
    devices.save_config(cfg)
    print(f"\n{G}✓ Saved{NC} to {devices.CONFIG}")
    for role, d in chosen.items():
        print(f"    {role:9} -> {d.name} (serial {d.serial})")
    print(f"\nStart it with: {B}./run{NC}   (or: python -m katwalk.daemon)")
    return 0


def main() -> int:
    print(f"{B}== katwalk-linux device setup =={NC}")
    devs = devices.scan()
    if not devs:
        print(f"{R}No KATVR (c4f4) HID devices found.{NC}")
        print(
            "Is the base powered on and the USB hub authorized? Check with: lsusb | grep c4f4"
        )
        return 1

    print(f"\nFound {len(devs)} KATVR device(s):")
    for i, d in enumerate(devs, 1):
        tag = f" {DIM}[{d.role}]{NC}" if d.role else f" {Y}[unrecognised]{NC}"
        print(f"  {i}) {_fmt(d)}{tag}")

    auto = {r: devices.resolve(r, devs, {}) for r, _ in ROLES}

    # Non-interactive: accept auto-detection and save.
    if "--auto" in sys.argv[1:] or not sys.stdin.isatty():
        chosen = {r: d for r, d in auto.items() if d is not None}
        if "receiver" not in chosen:
            print(
                f"{R}Could not auto-detect a receiver; run interactively to pick one.{NC}"
            )
            return 1
        print(f"\n{DIM}(non-interactive: accepting auto-detection){NC}")
        return _save(chosen)

    chosen: dict = {}
    print(
        f"\n{B}Assign roles{NC} (Enter = accept the auto-detected device; a number picks another; "
        f"'s' skips an optional one):"
    )
    for role, required in ROLES:
        a = auto[role]
        default_i = devs.index(a) + 1 if a else None
        while True:
            hint = (
                f"auto = #{default_i}"
                if a
                else ("REQUIRED - pick a number" if required else "none found")
            )
            try:
                ans = input(f"  {role:9} [{hint}] > ").strip().lower()
            except EOFError:
                ans = ""
            if ans == "" and a:
                chosen[role] = a
                break
            if ans == "" and not required:
                break
            if ans == "s" and not required:
                print(f"           {DIM}skipped {role}{NC}")
                break
            if ans.isdigit() and 1 <= int(ans) <= len(devs):
                chosen[role] = devs[int(ans) - 1]
                break
            opts = f"1-{len(devs)}" + ("" if required else ", or s to skip")
            print(f"    {R}enter {opts}, or Enter for auto{NC}")

    if "receiver" not in chosen:
        print(f"{R}A receiver is required. Nothing saved.{NC}")
        return 1
    return _save(chosen)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ncancelled.")
        raise SystemExit(130)
