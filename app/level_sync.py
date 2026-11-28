"""Sync level profiles between PCs through the signed-in Discord (Supabase) account.

Each user has one row in public.gdquiet_level_sync (Row Level Security limits
every user to their own row):

    levels   {level_key: {...profile..., "updated_at": <unix seconds>}}
    deleted  {level_key: <unix seconds when deleted>}

Merging is per level: the newest version of each level wins, and a deletion
wins over any older edit, so edits to different levels on two PCs never lose
each other. Locally we remember what each level looked like after the last
sync (a fingerprint and its timestamp); anything that differs since then is
treated as edited "now".
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from auth import DiscordAuth, supabase_settings

SYNC_TABLE = "gdquiet_level_sync"
TOMBSTONE_TTL_S = 90 * 24 * 3600
PERIODIC_SYNC_S = 60.0
ERROR_BACKOFF_S = 30.0
HTTP_TIMEOUT_S = 15.0


class SyncError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Pure merge logic
# ---------------------------------------------------------------------------


def fingerprint(profile: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in profile.items() if k != "updated_at"}, sort_keys=True, default=str)


def merge_levels(
    local: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]] | None,
    local_tombstones: dict[str, float],
    remote_levels: dict[str, dict[str, Any]],
    remote_deleted: dict[str, float],
    now: float,
) -> tuple[dict[str, dict[str, Any]], dict[str, float], dict[str, float]]:
    """Merge local and cloud levels.

    `baseline` is {key: {"fp": fingerprint, "t": timestamp}} from the last sync on
    this PC, or None if this PC has never synced for this account (then levels
    that also exist in the cloud defer to the cloud copy).

    Returns (levels, deleted, timestamps): the merged profiles (without
    "updated_at"), deletion tombstones, and each merged level's timestamp.
    """
    first_sync = baseline is None
    baseline = baseline or {}

    local_t: dict[str, float] = {}
    for key, profile in local.items():
        prev = baseline.get(key)
        if prev and prev.get("fp") == fingerprint(profile):
            local_t[key] = float(prev.get("t") or 0.0)
        elif first_sync and key in remote_levels:
            local_t[key] = 0.0  # first sync on this PC: the cloud copy wins
        else:
            local_t[key] = now

    tombstones: dict[str, float] = {}
    for key, t in local_tombstones.items():
        try:
            tombstones[key] = float(t)
        except (TypeError, ValueError):
            continue
    for key in baseline:
        if key not in local:
            tombstones[key] = max(tombstones.get(key, 0.0), now)
    for key, t in remote_deleted.items():
        try:
            tombstones[key] = max(tombstones.get(key, 0.0), float(t))
        except (TypeError, ValueError):
            continue

    levels: dict[str, dict[str, Any]] = {}
    stamps: dict[str, float] = {}
    for key in set(local) | set(remote_levels) | set(tombstones):
        candidates: list[tuple[float, int, dict[str, Any] | None]] = []
        if key in local:
            candidates.append((local_t[key], 1, local[key]))  # local wins ties
        remote = remote_levels.get(key)
        if isinstance(remote, dict):
            try:
                rt = float(remote.get("updated_at") or 0.0)
            except (TypeError, ValueError):
                rt = 0.0
            candidates.append((rt, 0, {k: v for k, v in remote.items() if k != "updated_at"}))
        if key in tombstones:
            candidates.append((tombstones[key], 2, None))  # deletion wins ties
        if not candidates:
            continue  # malformed cloud entry with nothing usable
        t, _rank, profile = max(candidates, key=lambda c: (c[0], c[1]))
        if profile is not None:
            levels[key] = profile
            stamps[key] = t

    deleted = {
        k: t for k, t in tombstones.items() if k not in levels and now - t < TOMBSTONE_TTL_S
    }
    return levels, deleted, stamps


# ---------------------------------------------------------------------------
# Supabase REST
# ---------------------------------------------------------------------------


def _request(method: str, url: str, api_key: str, token: str, body: Any = None, prefer: str = "") -> Any:
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "GDQuiet",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            err = json.loads(exc.read().decode("utf-8"))
            detail = str(err.get("message") or err.get("hint") or err.get("error") or "")
        except Exception:
            pass
        raise SyncError(detail or f"Supabase returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"Can't reach Supabase: {exc.reason}") from exc
    return json.loads(raw.decode("utf-8")) if raw else None


def fetch_remote(base_url: str, api_key: str, token: str, user_id: str) -> tuple[dict, dict, bool]:
    """Return (levels, deleted, row_exists) for the user's row."""
    query = urllib.parse.urlencode({"select": "levels,deleted", "user_id": f"eq.{user_id}"})
    rows = _request("GET", f"{base_url}/rest/v1/{SYNC_TABLE}?{query}", api_key, token)
    if not rows:
        return {}, {}, False
    row = rows[0] or {}
    levels = row.get("levels") if isinstance(row.get("levels"), dict) else {}
    deleted = row.get("deleted") if isinstance(row.get("deleted"), dict) else {}
    return levels, deleted, True


def push_remote(base_url: str, api_key: str, token: str, user_id: str, levels: dict, deleted: dict) -> None:
    body = {
        "user_id": user_id,
        "levels": levels,
        "deleted": deleted,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _request(
        "POST",
        f"{base_url}/rest/v1/{SYNC_TABLE}?on_conflict=user_id",
        api_key,
        token,
        body,
        prefer="resolution=merge-duplicates,return=minimal",
    )


# ---------------------------------------------------------------------------
# Background syncer
# ---------------------------------------------------------------------------


class LevelSync:
    """Keeps local level profiles in sync with the signed-in user's cloud copy."""

    def __init__(
        self,
        auth: DiscordAuth,
        state_dir: Path,
        get_profiles: Callable[[], dict[str, Any]],
        replace_profiles: Callable[[dict[str, Any], dict[str, Any]], bool],
    ) -> None:
        """`replace_profiles(new, expected)` must swap in `new` only if the current
        profiles still equal `expected` (else return False and we retry later)."""
        self._auth = auth
        self._state_dir = state_dir
        self._get_profiles = get_profiles
        self._replace_profiles = replace_profiles
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "idle"  # idle | syncing | synced | error | signed_out
        self._message = ""
        self._last_sync_at = 0.0
        self._last_attempt_at = 0.0
        self._last_synced_fp = ""
        self._force = True

    # ---- control ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def request_sync(self) -> None:
        self._force = True
        self._wake.set()

    def to_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "message": self._message,
                "last_sync_at": self._last_sync_at,
            }

    def _set(self, status: str, message: str = "") -> None:
        with self._lock:
            self._status = status
            self._message = message

    # ---- local state file (per account) ----

    def _state_path(self, user_id: str) -> Path:
        safe = "".join(ch for ch in user_id if ch.isalnum() or ch == "-")[:64]
        return self._state_dir / f"level_sync_{safe}.json"

    def _load_state(self, user_id: str) -> dict[str, Any] | None:
        path = self._state_path(user_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _save_state(self, user_id: str, state: dict[str, Any]) -> None:
        path = self._state_path(user_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)

    # ---- sync ----

    def sync_once(self) -> bool:
        """Run one sync. Returns True on success (or nothing to do)."""
        if not self._auth.signed_in:
            self._set("signed_out")
            return False
        url, key = supabase_settings()
        token = self._auth.access_token()
        user_id = self._auth.user_id()
        if not (url and key and token and user_id):
            self._set("error", "Couldn't get a sign-in token. Check your internet connection.")
            return False

        self._set("syncing")
        local = deepcopy(self._get_profiles())
        state = self._load_state(user_id)
        baseline = state.get("baseline") if state else None
        local_tombstones = (state or {}).get("tombstones") or {}
        try:
            remote_levels, remote_deleted, _exists = fetch_remote(url, key, token, user_id)
        except SyncError as exc:
            self._set("error", str(exc))
            return False

        now = time.time()
        levels, deleted, stamps = merge_levels(
            local, baseline if isinstance(baseline, dict) else None, local_tombstones,
            remote_levels, remote_deleted, now,
        )

        if levels != local and not self._replace_profiles(levels, local):
            # The user changed something mid-sync; try again shortly.
            self._force = True
            self._set("idle")
            return False

        cloud_levels = {k: {**p, "updated_at": stamps[k]} for k, p in levels.items()}
        if cloud_levels != remote_levels or deleted != remote_deleted:
            try:
                push_remote(url, key, token, user_id, cloud_levels, deleted)
            except SyncError as exc:
                self._set("error", str(exc))
                return False

        self._save_state(
            user_id,
            {
                "baseline": {k: {"fp": fingerprint(p), "t": stamps[k]} for k, p in levels.items()},
                "tombstones": deleted,
            },
        )
        with self._lock:
            self._last_sync_at = time.time()
            self._last_synced_fp = fingerprint(levels)
            self._status = "synced"
            self._message = ""
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if self._stop.is_set():
                return
            if not self._auth.signed_in:
                self._set("signed_out")
                self._force = True  # sync as soon as someone signs in
                continue
            now = time.time()
            with self._lock:
                errored = self._status == "error"
                last_fp = self._last_synced_fp
                last_sync = self._last_sync_at
            try:
                changed = fingerprint(self._get_profiles()) != last_fp
            except Exception:
                changed = True
            due = (
                self._force
                or (changed and now - self._last_attempt_at >= 2.0)
                or now - last_sync >= PERIODIC_SYNC_S
            )
            if errored and not self._force and now - self._last_attempt_at < ERROR_BACKOFF_S:
                continue
            if not due:
                continue
            self._force = False
            self._last_attempt_at = now
            try:
                self.sync_once()
            except Exception as exc:  # never let the thread die
                self._set("error", f"Sync failed: {exc}")
