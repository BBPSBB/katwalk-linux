"""Persist locomotion params + named profiles to ~/.config/katwalk/config.json.

Schema: {"active": "<name>", "profiles": {"<name>": {param: value, ...}, ...}}
Legacy {"params": {...}} is migrated to a single "default" profile.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .core.locomotion import Params

CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "katwalk"
)
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT = "default"


def _params_from(d: dict) -> Params:
    p = Params()
    for k, v in (d or {}).items():
        if k in Params.__dataclass_fields__:
            try:
                setattr(p, k, float(v))
            except (TypeError, ValueError):
                pass
    return p


def _load_store() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {"active": DEFAULT, "profiles": {DEFAULT: asdict(Params())}}
    store = (
        data
        if "profiles" in data
        else {  # migrate legacy {"params": {...}}
            "active": DEFAULT,
            "profiles": {DEFAULT: data.get("params", {})},
        }
    )
    store.setdefault("active", DEFAULT)
    if not store.get("profiles"):
        store["profiles"] = {DEFAULT: asdict(Params())}
    if store["active"] not in store["profiles"]:
        store["active"] = next(iter(store["profiles"]))
    return store


def _save_store(store: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(store, indent=2))
    except OSError:
        pass


def load_params() -> Params:
    store = _load_store()
    return _params_from(store["profiles"].get(store["active"], {}))


def save_params(params: Params) -> None:
    """Persist current params into the active profile (autosave on /set)."""
    store = _load_store()
    store["profiles"][store["active"]] = asdict(params)
    _save_store(store)


def list_profiles():
    store = _load_store()
    return store["active"], sorted(store["profiles"].keys())


def save_profile(name: str, params: Params) -> None:
    store = _load_store()
    store["profiles"][name] = asdict(params)
    store["active"] = name
    _save_store(store)


def load_profile(name: str):
    store = _load_store()
    if name in store["profiles"]:
        store["active"] = name
        _save_store(store)
        return _params_from(store["profiles"][name])
    return None


def delete_profile(name: str) -> bool:
    store = _load_store()
    if name in store["profiles"] and len(store["profiles"]) > 1:
        del store["profiles"][name]
        if store["active"] not in store["profiles"]:
            store["active"] = next(iter(store["profiles"]))
        _save_store(store)
        return True
    return False
