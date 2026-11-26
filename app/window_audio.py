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
    """Applies per-app mute only to selected processes and restores only what we muted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._muted_sessions: set[tuple[str, int, str]] = set()
        self._is_muted = False

    @property
    def is_muted(self) -> bool:
        with self._lock:
            return self._is_muted

    def restore(self) -> None:
        """Unmute any audio sessions previously muted by this controller."""
        self.apply([], mute=False)

    @staticmethod
    def _session_key(session: Any, proc_name: str, pid: int) -> tuple[str, int, str]:
        inst_id = str(getattr(session, "InstanceIdentifier", "") or "")
        return (proc_name, pid, inst_id)

    def apply(self, selected_processes: Iterable[str], mute: bool) -> None:
        selected = {p.lower().strip() for p in selected_processes if p and p.strip()}
        selected -= EXCLUDED_PROCESSES

        with self._lock:
            with com_scope():
                try:
                    sessions = AudioUtilities.GetAllSessions()
                except Exception:
                    return

                if mute:
                    self._is_muted = True
                    for session in sessions:
                        try:
                            if not session.Process:
                                continue
                            proc_name = session.Process.name().lower()
                            pid = int(session.Process.pid)
                            key = self._session_key(session, proc_name, pid)
                            vol = session.SimpleAudioVolume

                            if proc_name in selected:
                                current = int(vol.GetMute())
                                if current == 0:
                                    vol.SetMute(1, None)
                                    self._muted_sessions.add(key)
                            elif key in self._muted_sessions:
                                # User unchecked an app while already muted: unmute it immediately.
                                vol.SetMute(0, None)
                                self._muted_sessions.discard(key)
                        except Exception:
                            continue
                else:
                    self._is_muted = False
                    if self._muted_sessions:
                        for session in sessions:
                            try:
                                if not session.Process:
                                    continue
                                key = self._session_key(
                                    session,
                                    session.Process.name().lower(),
                                    int(session.Process.pid),
                                )
                                if key in self._muted_sessions:
                                    session.SimpleAudioVolume.SetMute(0, None)
                            except Exception:
                                continue
                        self._muted_sessions.clear()

    def restore(self) -> None:
        self.apply([], False)
