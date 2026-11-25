from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

APP_VERSION = "3.0.7"
GITHUB_REPO = "kodymcmonagle808-cmd/gdquiet"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases"


class UpdateError(RuntimeError):
    """Raised when checking, downloading, or verifying a GD Quiet update fails."""


@dataclass
class UpdateInfo:
    current_version: str
    latest_version: str
    tag_name: str
    update_available: bool
    download_url: str
    release_url: str
    asset_name: str
    size_bytes: int
    sha256: str
    release_notes: str


def parse_version(version_str: str) -> tuple[int, ...]:
    cleaned = (version_str or "").strip().lstrip("vV")
    nums = [int(part) for part in re.findall(r"\d+", cleaned)]
    return tuple(nums) if nums else (0,)


def is_newer_version(latest: str, current: str) -> bool:
    a = list(parse_version(latest))
    b = list(parse_version(current))
    n = max(len(a), len(b))
    a.extend([0] * (n - len(a)))
    b.extend([0] * (n - len(b)))
    return tuple(a) > tuple(b)


def _select_exe_asset(assets: list[dict[str, object]]) -> dict[str, object] | None:
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name == "gdquiet.exe":
            return asset
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".exe"):
            return asset
    return None


def check_for_update(
    current_version: str = APP_VERSION,
    timeout_s: float = 10.0,
) -> UpdateInfo:
    req = urllib.request.Request(
        RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"GDQuiet/{current_version}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"GitHub API HTTP {exc.code}") from exc
    except Exception as exc:
        raise UpdateError(f"Update check failed: {exc}") from exc

    tag_name = str(data.get("tag_name") or "").strip()
    latest_version = tag_name.lstrip("vV") or current_version
    release_url = str(data.get("html_url") or RELEASES_PAGE_URL)
    release_notes = str(data.get("body") or "").strip()

    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    asset = _select_exe_asset([a for a in assets if isinstance(a, dict)])
    if asset is None:
        return UpdateInfo(
            current_version=current_version,
            latest_version=latest_version,
            tag_name=tag_name or f"v{latest_version}",
            update_available=False,
            download_url="",
            release_url=release_url,
            asset_name="",
            size_bytes=0,
            sha256="",
            release_notes=release_notes,
        )

    download_url = str(asset.get("browser_download_url") or "")
    asset_name = str(asset.get("name") or "GDQuiet.exe")
    size_bytes = int(asset.get("size") or 0)
    digest = str(asset.get("digest") or "").strip()
    sha256 = digest[7:].strip().lower() if digest.lower().startswith("sha256:") else ""

    available = bool(download_url) and is_newer_version(latest_version, current_version)
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        tag_name=tag_name or f"v{latest_version}",
        update_available=available,
        download_url=download_url,
        release_url=release_url,
        asset_name=asset_name,
        size_bytes=size_bytes,
        sha256=sha256,
        release_notes=release_notes,
    )


def _choose_staging_path(target_exe: Path, version_tag: str) -> Path:
    candidate = target_exe.with_name(f".{target_exe.name}.update")
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        with candidate.open("wb"):
            pass
        return candidate
    except OSError:
        safe_ver = re.sub(r"[^0-9A-Za-z._-]", "_", version_tag or "latest")
        return Path(tempfile.gettempdir()) / f"gdquiet_update_{safe_ver}.exe"


def download_update(
    update: UpdateInfo,
    target_exe: Path,
    progress_cb: Callable[[int, int], None] | None = None,
    timeout_s: float = 30.0,
) -> Path:
    if not update.download_url.startswith("https://"):
        raise UpdateError("Invalid release download URL.")

    staged = _choose_staging_path(target_exe, update.latest_version)
    req = urllib.request.Request(
        update.download_url,
        headers={"User-Agent": f"GDQuiet/{update.current_version}"},
    )

    hasher = hashlib.sha256()
    downloaded = 0
    first_chunk = True

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp, staged.open("wb") as out:
            header_total = int(resp.headers.get("Content-Length") or update.size_bytes or 0)
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if first_chunk:
                    first_chunk = False
                    if len(chunk) < 2 or chunk[:2] != b"MZ":
                        raise UpdateError("Downloaded file is not a valid Windows executable.")
                out.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                if progress_cb is not None:
                    progress_cb(downloaded, header_total)

        if downloaded <= 0:
            raise UpdateError("Downloaded update file is empty.")
        if update.size_bytes > 0 and downloaded != update.size_bytes:
            raise UpdateError(
                f"Download incomplete ({downloaded} of {update.size_bytes} bytes received)."
            )
        if update.sha256 and hasher.hexdigest().lower() != update.sha256.lower():
            raise UpdateError("Update SHA-256 verification failed.")
        return staged
    except Exception as exc:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"Download failed: {exc}") from exc


def write_self_update_script(current_pid: int) -> Path:
    script_path = Path(tempfile.gettempdir()) / f"gdquiet_self_update_{current_pid}.cmd"
    content = "\r\n".join(
        [
            "@echo off",
            "setlocal",
            'set "PID=%~1"',
            'set "SRC=%~2"',
            'set "DST=%~3"',
            "for /l %%i in (1,1,40) do (",
            '  tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul',
            "  if errorlevel 1 goto apply",
            "  ping -n 2 127.0.0.1 >nul",
            ")",
            ":apply",
            "for /l %%j in (1,1,15) do (",
            '  copy /y "%SRC%" "%DST%" >nul 2>&1',
            "  if not errorlevel 1 (",
            '    del /f /q "%SRC%" >nul 2>&1',
            '    start "" "%DST%"',
            '    del /f /q "%~f0" >nul 2>&1',
            "    exit /b 0",
            "  )",
            "  ping -n 2 127.0.0.1 >nul",
            ")",
            "exit /b 1",
            "",
        ]
    )
    script_path.write_text(content, encoding="utf-8")
    return script_path


def launch_update_swap_and_restart(
    staged_exe: Path,
    target_exe: Path,
    current_pid: int | None = None,
) -> Path:
    pid = os.getpid() if current_pid is None else int(current_pid)
    script_path = write_self_update_script(pid)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("_PYI") and k != "_MEIPASS2"
    }
    subprocess.Popen(
        [
            "cmd.exe",
            "/c",
            str(script_path),
            str(pid),
            str(staged_exe),
            str(target_exe),
        ],
        env=clean_env,
        creationflags=creation_flags,
        close_fds=True,
    )
    return script_path


def is_packaged_executable() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_executable_path() -> Path:
    return Path(sys.executable).resolve()
