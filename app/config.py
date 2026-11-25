from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "GDQuiet"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_data_dir() / "config.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "selected_apps": [],
    "auto_deafen": False,
    "deafen_hotkey": None,
    "mute_at_percent": 50.0,
    "show_background_audio": False,
    "watch_enabled": True,
    "watch_mode": "auto_window",
    "target_process": "geometrydash.exe",
    "custom_region": None,
    "unmute_at_100": True,
}


def load_config() -> dict[str, Any]:
    data = dict(DEFAULT_CONFIG)
    path = config_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    return data


def save_config(cfg: dict[str, Any]) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(path)
