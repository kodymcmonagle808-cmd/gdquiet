from __future__ import annotations

from copy import deepcopy
from typing import Any

from screen_reader import level_name_similarity, normalize_level_name

# OCR reads within this similarity of a saved level name snap to that level.
MATCH_CUTOFF = 0.8

PROFILE_FIELDS = (
    "mute_at_percent",
    "selected_apps",
    "watch_mode",
    "target_process",
    "custom_region",
)


def profile_key(level_name: str) -> str:
    return normalize_level_name(level_name)


def find_matching_profile(profiles: dict[str, Any], level_name: str) -> str | None:
    """Return the key of the saved level closest to `level_name` (exact key first),
    or None when nothing is similar enough. Tolerates OCR misreads like
    "clubstep sii@ttpos" for "clubstep startpos"."""
    key = profile_key(level_name)
    if not key:
        return None
    if isinstance(profiles.get(key), dict):
        return key
    best_key, best_score = None, 0.0
    for other_key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        score = level_name_similarity(level_name, str(profile.get("name") or other_key))
        if score > best_score:
            best_key, best_score = other_key, score
    return best_key if best_score >= MATCH_CUTOFF else None


def build_level_profile(level_name: str, config: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
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
        "enabled": bool(enabled),
    }


def profile_enabled(profile: dict[str, Any]) -> bool:
    return bool(profile.get("enabled", True))


def apply_level_profile(config: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(config)
    for key in PROFILE_FIELDS:
        if key in profile:
            updated[key] = deepcopy(profile[key])
    return updated
