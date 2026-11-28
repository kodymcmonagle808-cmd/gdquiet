from __future__ import annotations

import ctypes
import os
import threading
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Iterable

import comtypes
import psutil
from pycaw.pycaw import AudioUtilities

EXCLUDED_PROCESSES = {
    "geometrydash.exe",
    "gdquiet.exe",
    "shellexperiencehost.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "textinputhost.exe",
    "lockapp.exe",
    "systemsettings.exe",
}

FRIENDLY_APP_NAMES = {
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Mozilla Firefox",
    "opera.exe": "Opera",
    "brave.exe": "Brave Browser",
    "discord.exe": "Discord",
    "discordptb.exe": "Discord PTB",
    "discordcanary.exe": "Discord Canary",
    "spotify.exe": "Spotify",
    "vlc.exe": "VLC Media Player",
    "obs64.exe": "OBS Studio",
    "steamwebhelper.exe": "Steam",
    "applemusic.exe": "Apple Music",
}

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14

user32 = ctypes.windll.user32
try:
    dwmapi = ctypes.windll.dwmapi
except OSError:
    dwmapi = None

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


@contextmanager
def com_scope():
    initialized = False
    try:
        comtypes.CoInitialize()
        initialized = True
    except OSError:
        pass
    try:
        yield
    finally:
        if initialized:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


@dataclass
class MuteTarget:
    process_name: str
    display_name: str
    window_titles: list[str] = field(default_factory=list)
    has_window: bool = False
    has_audio: bool = False
    currently_muted: bool = False


def _is_window_cloaked(hwnd: int) -> bool:
    if dwmapi is None:
        return False
    cloaked = wintypes.DWORD(0)
    res = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_CLOAKED),
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return res == 0 and cloaked.value != 0


def _friendly_name(proc_name: str) -> str:
    lower = proc_name.lower()
    if lower in FRIENDLY_APP_NAMES:
        return FRIENDLY_APP_NAMES[lower]
    stem = os.path.splitext(proc_name)[0]
    return stem[:1].upper() + stem[1:] if stem else proc_name


def _enum_open_windows_by_process() -> dict[str, list[str]]:
    own_pid = os.getpid()
    pid_name_cache: dict[int, str] = {}
    windows_by_proc: dict[str, list[str]] = {}

    def get_proc_name(pid: int) -> str:
        if pid in pid_name_cache:
            return pid_name_cache[pid]
        name = ""
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = ""
        pid_name_cache[pid] = name
        return name

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex_style & WS_EX_TOOLWINDOW:
            return True
        if _is_window_cloaked(hwnd):
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title == "Program Manager":
            return True

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0 or pid.value == own_pid:
            return True

        proc_name = get_proc_name(pid.value)
        if not proc_name:
            return True
        lower = proc_name.lower()
        if lower in EXCLUDED_PROCESSES:
            return True

        titles = windows_by_proc.setdefault(lower, [])
        if title not in titles:
            titles.append(title)
        return True

    enum_proc = WNDENUMPROC(callback)
    user32.EnumWindows(enum_proc, 0)
    return windows_by_proc


def list_open_window_processes() -> list[tuple[str, str]]:
    """Return sorted [(process_name, sample_title)] for visible non-system windows."""
    windows_by_proc = _enum_open_windows_by_process()
    items = [
        (proc_name, f"{_friendly_name(proc_name)} — {titles[0][:45]}")
        for proc_name, titles in windows_by_proc.items()
        if titles
    ]
    return sorted(items, key=lambda x: x[1].lower())


def list_mute_targets(include_background_audio: bool = False) -> list[MuteTarget]:
    windows_by_proc = _enum_open_windows_by_process()
    targets: dict[str, MuteTarget] = {}

    for proc_name, titles in windows_by_proc.items():
        targets[proc_name] = MuteTarget(
            process_name=proc_name,
            display_name=_friendly_name(proc_name),
            window_titles=titles,
            has_window=True,
        )

    own_pid = os.getpid()
    with com_scope():
        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            sessions = []

        for session in sessions:
            try:
                if not session.Process or session.Process.pid == own_pid:
                    continue
                proc_name = session.Process.name().lower()
                if proc_name in EXCLUDED_PROCESSES:
                    continue
                is_muted = bool(session.SimpleAudioVolume.GetMute())
                if proc_name in targets:
                    targets[proc_name].has_audio = True
                    targets[proc_name].currently_muted = (
                        targets[proc_name].currently_muted or is_muted
                    )
                elif include_background_audio:
                    targets[proc_name] = MuteTarget(
                        process_name=proc_name,
                        display_name=_friendly_name(proc_name),
                        window_titles=[],
                        has_window=False,
                        has_audio=True,
                        currently_muted=is_muted,
                    )
            except Exception:
                continue

    return sorted(
        targets.values(),
        key=lambda t: (not t.has_window, not t.has_audio, t.display_name.lower()),
    )


def is_geometry_dash_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name == "geometrydash.exe":
                return True
        except Exception:
            continue
    return False


class AudioMuteController:
    """Applies per-app mute or ducking while preserving the previous session state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._muted_sessions: set[tuple[str, int, str]] = set()
        self._ducked_sessions: dict[tuple[str, int, str], float] = {}
        self._active_mode: str | None = None
        self._is_muted = False

    @property
    def is_muted(self) -> bool:
        with self._lock:
            return self._is_muted

    @property
    def is_ducking(self) -> bool:
        with self._lock:
            return bool(self._ducked_sessions)

    @property
    def has_active_effect(self) -> bool:
        with self._lock:
            return bool(self._muted_sessions or self._ducked_sessions)

    @property
    def muted_session_count(self) -> int:
        with self._lock:
            return len(self._muted_sessions)

    def restore(self) -> None:
        """Unmute any audio sessions previously muted by this controller."""
        self.apply([], mute=False)

    def restore_process(self, proc_name: str) -> None:
        """Restore (unmute/unduck) audio sessions for a single process."""
        target = proc_name.lower().strip()
        if not target:
            return
        with self._lock:
            with com_scope():
                try:
                    sessions = AudioUtilities.GetAllSessions()
                except Exception:
                    return
                for session in sessions:
                    try:
                        if not session.Process:
                            continue
                        name = session.Process.name().lower()
                        if name != target:
                            continue
                        pid = int(session.Process.pid)
                        key = self._session_key(session, name, pid)
                        self._restore_session(key, session.SimpleAudioVolume)
                    except Exception:
                        continue
                self._is_muted = bool(self._muted_sessions)

    def set_duck_volume(
        self,
        selected_processes: Iterable[str],
        volume_percent: float,
        force: bool = False,
    ) -> None:
        """Duck selected apps to `volume_percent` (0-100). Every call re-applies; `force` is kept for API compatibility."""
        self.apply(selected_processes, mute=True, duck_to=volume_percent)

    @staticmethod
    def _session_key(session: Any, proc_name: str, pid: int) -> tuple[str, int, str]:
        inst_id = str(getattr(session, "InstanceIdentifier", "") or "")
        return (proc_name, pid, inst_id)

    def apply(
        self,
        selected_processes: Iterable[str],
        mute: bool,
        duck_to: float | None = None,
        force: bool = False,
    ) -> None:
        selected = {p.lower().strip() for p in selected_processes if p and p.strip()}
        selected -= EXCLUDED_PROCESSES

        with self._lock:
            with com_scope():
                try:
                    sessions = AudioUtilities.GetAllSessions()
                except Exception:
                    return

                mode = "duck" if duck_to is not None else "mute"
                target_volume = max(0.0, min(1.0, float(duck_to or 0.0) / 100.0))
                if mute and self._active_mode not in (None, mode):
                    self._restore_sessions(sessions)
                self._active_mode = mode if mute else None

                if mute:
                    for session in sessions:
                        try:
                            if not session.Process:
                                continue
                            proc_name = session.Process.name().lower()
                            pid = int(session.Process.pid)
                            key = self._session_key(session, proc_name, pid)
                            vol = session.SimpleAudioVolume

                            if proc_name in selected:
                                if mode == "duck":
                                    if int(vol.GetMute()):
                                        continue
                                    original = self._ducked_sessions.get(key)
                                    current = float(vol.GetMasterVolume())
                                    if original is None and current > target_volume:
                                        self._ducked_sessions[key] = current
                                        vol.SetMasterVolume(target_volume, None)
                                    elif original is not None:
                                        vol.SetMasterVolume(min(original, target_volume), None)
                                else:
                                    if key in self._ducked_sessions:
                                        original = self._ducked_sessions[key]
                                        vol.SetMasterVolume(original, None)
                                        self._ducked_sessions.pop(key)
                                    if int(vol.GetMute()) == 0:
                                        vol.SetMute(1, None)
                                        self._muted_sessions.add(key)
                            else:
                                self._restore_session(key, vol)
                        except Exception:
                            continue
                    self._is_muted = bool(self._muted_sessions)
                else:
                    self._restore_sessions(sessions)
                    self._is_muted = bool(self._muted_sessions)

    def _restore_session(self, key: tuple[str, int, str], volume: Any) -> None:
        if key in self._muted_sessions:
            volume.SetMute(0, None)
            self._muted_sessions.discard(key)
        if key in self._ducked_sessions:
            volume.SetMasterVolume(self._ducked_sessions[key], None)
            self._ducked_sessions.pop(key)

    def _restore_sessions(self, sessions: Iterable[Any]) -> None:
        remaining_muted = set(self._muted_sessions)
        remaining_ducked = dict(self._ducked_sessions)
        seen: set[tuple[str, int, str]] = set()
        all_identified = True
        for session in sessions:
            key: tuple[str, int, str] | None = None
            try:
                if not session.Process:
                    continue
                key = self._session_key(
                    session,
                    session.Process.name().lower(),
                    int(session.Process.pid),
                )
                seen.add(key)
                self._restore_session(key, session.SimpleAudioVolume)
                remaining_muted.discard(key)
                remaining_ducked.pop(key, None)
            except Exception:
                if key is None:
                    all_identified = False
                if key in self._muted_sessions:
                    remaining_muted.add(key)
                if key in self._ducked_sessions:
                    remaining_ducked[key] = self._ducked_sessions[key]

        # Forget sessions that no longer exist (e.g. the app was closed while muted).
        # Without this they stay tracked forever, the controller reports "muted",
        # and the apply worker keeps retrying a restore that can never succeed.
        def is_gone(key: tuple[str, int, str]) -> bool:
            if key in seen:
                return False
            if all_identified:
                return True
            try:
                return not psutil.pid_exists(key[1])
            except Exception:
                return False

        remaining_muted = {k for k in remaining_muted if not is_gone(k)}
        remaining_ducked = {k: v for k, v in remaining_ducked.items() if not is_gone(k)}
        self._muted_sessions = remaining_muted
        self._ducked_sessions = remaining_ducked
        self._is_muted = bool(self._muted_sessions)
