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
from functools import cmp_to_key
from pathlib import Path
from typing import Callable

APP_VERSION = "4.0.0"
GITHUB_REPO = "kodymcmonagle808-cmd/gdquiet"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_LIST_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=100"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases"
_VERSION_RE = re.compile(
    r"^[vV]?(?P<release>\d+(?:\.\d+)*)(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


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
    is_installer: bool = False


def parse_version(version_str: str) -> tuple[int, ...]:
    cleaned = (version_str or "").strip().lstrip("vV")
    match = _VERSION_RE.fullmatch(cleaned)
    if match:
        return tuple(int(part) for part in match.group("release").split("."))
    nums = [int(part) for part in re.findall(r"\d+", cleaned)]
    return tuple(nums) if nums else (0,)


def _version_parts(version_str: str) -> tuple[tuple[int, ...], tuple[str, ...] | None]:
    match = _VERSION_RE.fullmatch((version_str or "").strip())
    if not match:
        return parse_version(version_str), None
    release = tuple(int(part) for part in match.group("release").split("."))
    prerelease = match.group("prerelease")
    identifiers = tuple(part.lower() for part in prerelease.split(".")) if prerelease else None
    return release, identifiers


def _compare_versions(left: str, right: str) -> int:
    left_release, left_pre = _version_parts(left)
    right_release, right_pre = _version_parts(right)
    width = max(3, len(left_release), len(right_release))
    left_release += (0,) * (width - len(left_release))
    right_release += (0,) * (width - len(right_release))
    if left_release != right_release:
        return 1 if left_release > right_release else -1
    if left_pre is None or right_pre is None:
        if left_pre is right_pre:
            return 0
        return 1 if left_pre is None else -1

    for left_id, right_id in zip(left_pre, right_pre):
        if left_id == right_id:
            continue
        left_numeric = left_id.isdigit()
        right_numeric = right_id.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_id) > int(right_id) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_id > right_id else -1
    if len(left_pre) == len(right_pre):
        return 0
    return 1 if len(left_pre) > len(right_pre) else -1


def is_newer_version(latest: str, current: str) -> bool:
    return _compare_versions(latest, current) > 0


def _is_installer_asset_name(name: str) -> bool:
    low = (name or "").lower()
    return "setup" in low or "installer" in low


def _select_exe_asset(assets: list[dict[str, object]]) -> dict[str, object] | None:
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name == "gdquiet-setup.exe":
            return asset
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".exe") and _is_installer_asset_name(name):
            return asset
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
    include_prereleases: bool = False,
) -> UpdateInfo:
    request_url = RELEASES_LIST_API_URL if include_prereleases else RELEASES_API_URL
    req = urllib.request.Request(
        request_url,
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

    if include_prereleases and isinstance(data, list):
        releases = [
            release
            for release in data
            if isinstance(release, dict)
            and not release.get("draft")
            and _VERSION_RE.fullmatch(str(release.get("tag_name") or "").strip())
        ]
        if releases:
            data = max(
                releases,
                key=cmp_to_key(
                    lambda left, right: _compare_versions(
                        str(left.get("tag_name") or ""),
                        str(right.get("tag_name") or ""),
                    )
                ),
            )
        else:
            data = {}
    if not isinstance(data, dict):
        raise UpdateError("Unexpected response from GitHub Releases.")

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
    asset_name = str(asset.get("name") or "GDQuiet-Setup.exe")
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
        is_installer=_is_installer_asset_name(asset_name),
    )


def _choose_staging_path(target_exe: Path, version_tag: str, is_installer: bool = False) -> Path:
    safe_ver = re.sub(r"[^0-9A-Za-z._-]", "_", version_tag or "latest")
    if is_installer:
        return Path(tempfile.gettempdir()) / f"GDQuiet-Setup-{safe_ver}.exe"
    candidate = target_exe.with_name(f".{target_exe.name}.{safe_ver}.update")
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        with candidate.open("wb"):
            pass
        return candidate
    except OSError:
        return Path(tempfile.gettempdir()) / f"gdquiet_update_{safe_ver}.exe"


def cleanup_stale_update_files(target_exe: Path) -> None:
    patterns = [f".{target_exe.name}.update", f".{target_exe.name}.*.update"]
    for pat in patterns:
        for stale in target_exe.parent.glob(pat):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass


def download_update(
    update: UpdateInfo,
    target_exe: Path,
    progress_cb: Callable[[int, int], None] | None = None,
    timeout_s: float = 30.0,
) -> Path:
    if not update.download_url.startswith("https://"):
        raise UpdateError("Invalid release download URL.")

    is_inst = bool(update.is_installer) or _is_installer_asset_name(update.asset_name)
    staged = _choose_staging_path(target_exe, update.latest_version, is_installer=is_inst)
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


def write_self_update_script(current_pid: int, is_installer: bool = False) -> Path:
    script_path = Path(tempfile.gettempdir()) / f"gdquiet_self_update_{current_pid}.ps1"
    wait_loop = [
        "param(",
        "    [int]$TargetPid,",
        "    [int]$ParentPid,",
        "    [string]$Src,",
        "    [string]$Dst,",
        "    [string]$SelfScript",
        ")",
        "for ($i = 0; $i -lt 40; $i++) {",
        "    $p1 = if ($TargetPid -gt 0) { Get-Process -Id $TargetPid -ErrorAction SilentlyContinue } else { $null }",
        "    $p2 = if ($ParentPid -gt 0) { Get-Process -Id $ParentPid -ErrorAction SilentlyContinue } else { $null }",
        "    if (-not $p1 -and -not $p2) { break }",
        "    Start-Sleep -Milliseconds 250",
        "}",
    ]
    if is_installer:
        body = [
            "Unblock-File -LiteralPath $Src -ErrorAction SilentlyContinue",
            "$workDir = [System.IO.Path]::GetDirectoryName($Src)",
            "if ($workDir) {",
            "    Start-Process -FilePath $Src -WorkingDirectory $workDir -WindowStyle Normal -Wait",
            "} else {",
            "    Start-Process -FilePath $Src -WindowStyle Normal -Wait",
            "}",
            "Remove-Item -LiteralPath $Src -Force -ErrorAction SilentlyContinue",
            "Remove-Item -LiteralPath $SelfScript -Force -ErrorAction SilentlyContinue",
            "",
        ]
    else:
        body = [
            "$swapped = $false",
            "for ($j = 0; $j -lt 40; $j++) {",
            "    try {",
            "        Copy-Item -LiteralPath $Src -Destination $Dst -Force -ErrorAction Stop",
            "        $swapped = $true",
            "        break",
            "    } catch {",
            "        Start-Sleep -Milliseconds 250",
            "    }",
            "}",
            "if ($swapped) {",
            "    Remove-Item -LiteralPath $Src -Force -ErrorAction SilentlyContinue",
            "    Unblock-File -LiteralPath $Dst -ErrorAction SilentlyContinue",
            "    $workDir = [System.IO.Path]::GetDirectoryName($Dst)",
            "    if ($workDir) {",
            "        Start-Process -FilePath $Dst -WorkingDirectory $workDir",
            "    } else {",
            "        Start-Process -FilePath $Dst",
            "    }",
            "}",
            "Remove-Item -LiteralPath $SelfScript -Force -ErrorAction SilentlyContinue",
            "",
        ]
    content = "\n".join(wait_loop + body)
    with script_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return script_path


def _resolve_bootloader_parent_pid(target_exe: Path) -> int:
    if not is_packaged_executable():
        return 0
    try:
        import psutil

        ppid = os.getppid()
        if ppid > 0 and psutil.Process(ppid).name().lower() == target_exe.name.lower():
            return int(ppid)
    except Exception:
        pass
    return 0


def launch_update_swap_and_restart(
    staged_exe: Path,
    target_exe: Path,
    current_pid: int | None = None,
    is_installer: bool | None = None,
) -> Path:
    pid = os.getpid() if current_pid is None else int(current_pid)
    parent_pid = _resolve_bootloader_parent_pid(target_exe)
    use_installer = (
        bool(is_installer)
        if is_installer is not None
        else _is_installer_asset_name(staged_exe.name)
    )
    script_path = write_self_update_script(pid, is_installer=use_installer)
    # Never combine CREATE_NO_WINDOW with DETACHED_PROCESS (causes immediate child exit on Windows).
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("_PYI") and k != "_MEIPASS2"
    }
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-TargetPid",
            str(pid),
            "-ParentPid",
            str(parent_pid),
            "-Src",
            str(staged_exe),
            "-Dst",
            str(target_exe),
            "-SelfScript",
            str(script_path),
        ],
        env=clean_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )
    return script_path


def is_packaged_executable() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_executable_path() -> Path:
    return Path(sys.executable).resolve()
