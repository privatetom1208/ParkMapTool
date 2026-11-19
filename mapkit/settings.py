"""Tiny JSON settings store (library folder, open-folder-when-done)."""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_library() -> Path:
    return Path.home() / "Documents" / "ParkMaps"


def _config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "ParkMapTool" / "settings.json"


def load_settings() -> dict:
    settings = {"library": str(default_library()), "open_when_done": True}
    try:
        data = json.loads(_config_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("library"):
            settings["library"] = str(data["library"])
        if isinstance(data, dict) and isinstance(data.get("open_when_done"), bool):
            settings["open_when_done"] = data["open_when_done"]
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    try:
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass
