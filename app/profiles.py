from __future__ import annotations

from copy import deepcopy
from typing import Any

from screen_reader import normalize_level_name

PROFILE_FIELDS = (
    "mute_at_percent",
    "selected_apps",
    "watch_mode",
    "target_process",
    "custom_region",
)


def profile_key(level_name: str) -> str:
    return normalize_level_name(level_name)


def build_level_profile(level_name: str, config: dict[str, Any]) -> dict[str, Any]:
    threshold = float(config.get("mute_at_percent", 50.0))
    selected_apps = config.get("selected_apps")
    apps = (
        sorted({name.strip().lower() for name in selected_apps if isinstance(name, str) and name.strip()})
        if isinstance(selected_apps, list)
        else []
    )
    return {
        "name": level_name.strip(),
        "mute_at_percent": max(0.0, min(100.0, threshold)),
        "selected_apps": apps,
        "watch_mode": str(config.get("watch_mode") or "auto_window"),
        "target_process": str(config.get("target_process") or "geometrydash.exe"),
        "custom_region": deepcopy(config.get("custom_region")),
    }


def apply_level_profile(config: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(config)
    for key in PROFILE_FIELDS:
        if key in profile:
            updated[key] = deepcopy(profile[key])
    return updated
