from __future__ import annotations

from copy import deepcopy
from typing import Any

from screen_reader import level_name_similarity, normalize_level_name, split_level_id

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


def clean_level_id(level_id: object) -> str | None:
    """Digits-only level ID without leading zeros, or None."""
    digits = "".join(ch for ch in str(level_id or "") if ch.isdigit())
    return str(int(digits)) if digits else None


def level_key(level_name: str, level_id: object = None) -> str:
    """Profile key: "id:<levelID>" when the ID is known, else the normalised name."""
    lid = clean_level_id(level_id)
    return f"id:{lid}" if lid else profile_key(level_name)


def find_matching_profile(profiles: dict[str, Any], label: str) -> str | None:
    """Return the key of the saved level for an OCR'd label, or None.

    "{levelName} ({levelID})" labels match on the level ID only; the name is
    ignored because different levels can share a name. Name-only labels match
    the closest saved name, tolerating misreads like "clubstep sii@ttpos".
    """
    name, level_id = split_level_id(label)
    if level_id:
        for key, profile in profiles.items():
            if isinstance(profile, dict) and (
                key == f"id:{level_id}" or clean_level_id(profile.get("level_id")) == level_id
            ):
                return key
        return None

    key = profile_key(name)
    if not key:
        return None
    if isinstance(profiles.get(key), dict):
        return key
    best_key, best_score = None, 0.0
    for other_key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        score = level_name_similarity(name, str(profile.get("name") or other_key))
        if score > best_score:
            best_key, best_score = other_key, score
    return best_key if best_score >= MATCH_CUTOFF else None


def build_level_profile(
    level_name: str, config: dict[str, Any], enabled: bool = True, level_id: object = None
) -> dict[str, Any]:
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
        "level_id": clean_level_id(level_id),
    }


def profile_enabled(profile: dict[str, Any]) -> bool:
    return bool(profile.get("enabled", True))


def new_auto_added_profile(level_name: str, level_id: object = None) -> dict[str, Any]:
    """A level added automatically the first time it's seen.

    It starts switched off with no threshold, and stores no apps or capture
    settings, so it changes nothing until the user sets it up.
    """
    return {
        "name": level_name.strip(),
        "level_id": clean_level_id(level_id),
        "enabled": False,
        "mute_at_percent": None,
        "auto_added": True,
    }


def profile_threshold(profile: dict[str, Any]) -> float | None:
    value = profile.get("mute_at_percent")
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def apply_level_profile(config: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Copy a level's saved settings into the config. Settings the level doesn't
    have (or an empty threshold) leave the current values alone."""
    updated = deepcopy(config)
    for key in PROFILE_FIELDS:
        if key not in profile or profile[key] is None:
            continue
        if key == "mute_at_percent":
            threshold = profile_threshold(profile)
            if threshold is None:
                continue
            updated[key] = threshold
        else:
            updated[key] = deepcopy(profile[key])
    return updated
