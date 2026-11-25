from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import build_config


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "GDQuiet"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_data_dir() / "config.json"


def _read_dotenv() -> dict[str, str]:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    candidates.append(Path.cwd() / ".env")

    values: dict[str, str] = {}
    for env_path in candidates:
        if not env_path.is_file():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in values:
                    values[k] = v
        except OSError:
            pass
    return values


DEFAULT_CONFIG: dict[str, Any] = {
    "supabase_url": "",
    "supabase_anon_key": "",
    "session": None,
    "selected_apps": [],
    "auto_deafen": False,
    "deafen_hotkey": None,
    "mute_at_percent": 50.0,
    "show_background_audio": False,
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


def resolve_supabase_credentials(cfg: dict[str, Any]) -> tuple[str, str]:
    dotenv = _read_dotenv()

    url = (
        str(cfg.get("supabase_url") or "").strip()
        or os.environ.get("GDQUIET_SUPABASE_URL", "").strip()
        or os.environ.get("SUPABASE_URL", "").strip()
        or dotenv.get("GDQUIET_SUPABASE_URL", "").strip()
        or dotenv.get("SUPABASE_URL", "").strip()
        or str(getattr(build_config, "SUPABASE_URL", "") or "").strip()
    ).rstrip("/")

    anon_key = (
        str(cfg.get("supabase_anon_key") or "").strip()
        or os.environ.get("GDQUIET_SUPABASE_ANON_KEY", "").strip()
        or os.environ.get("SUPABASE_ANON_KEY", "").strip()
        or os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        or dotenv.get("GDQUIET_SUPABASE_ANON_KEY", "").strip()
        or dotenv.get("SUPABASE_ANON_KEY", "").strip()
        or dotenv.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        or str(getattr(build_config, "SUPABASE_ANON_KEY", "") or "").strip()
    )

    return url, anon_key
