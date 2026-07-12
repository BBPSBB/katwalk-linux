"""Shared-memory stick I/O between katwalkd and the OpenVR driver / OpenXR layer.

Layout (little-endian, matches `struct KatInput` in openxr-driver/src/katwalk_shm.h):
    float x, float y, uint32 buttons, uint32 seq         # 16 bytes
buttons: bit0 = sprint, bit1 = jump.

We mirror the stick to TWO files so every consumer can read it:
  - $XDG_RUNTIME_DIR/katwalk/input  - the native OpenVR driver (runs in vrserver)
  - /tmp/katwalk/input              - the OpenXR layer inside Proton's pressure-vessel
    sandbox, where $XDG_RUNTIME_DIR and /run/user are NOT mounted but /tmp IS shared.
"""

from __future__ import annotations

import mmap
import os
import struct
from pathlib import Path

SIZE = 16


def input_path() -> Path:
    """Primary path (XDG runtime); kept for back-compat / native consumers."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "katwalk" / "input"


def input_paths() -> list[Path]:
    """All paths the stick is mirrored to (deduplicated, order-preserving)."""
    paths = [input_path(), Path("/tmp") / "katwalk" / "input"]
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            out.append(p)
    return out


class StickWriter:
    def __init__(self):
        self._maps: list[mmap.mmap] = []
        self._files = []
        for p in input_paths():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "ab") as f:  # ensure the file is at least SIZE bytes
                    if f.tell() < SIZE:
                        f.write(b"\x00" * (SIZE - f.tell()))
                fh = open(p, "r+b")
                self._files.append(fh)
                self._maps.append(mmap.mmap(fh.fileno(), SIZE))
            except Exception:
                pass  # one path failing must not kill the rest
        self._seq = 0

    def write(
        self, x: float, y: float, sprint: bool = False, jump: bool = False
    ) -> None:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        btn = (1 if sprint else 0) | (2 if jump else 0)
        data = struct.pack("<ffII", float(x), float(y), btn, self._seq)
        for m in self._maps:
            try:
                m[:SIZE] = data
            except Exception:
                pass

    def close(self) -> None:
        for m in self._maps:
            try:
                m.close()
            except Exception:
                pass
        for f in self._files:
            try:
                f.close()
            except Exception:
                pass
