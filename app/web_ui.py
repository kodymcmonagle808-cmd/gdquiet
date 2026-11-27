from __future__ import annotations

import atexit
import base64
import ctypes
from ctypes import wintypes
import io
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from typing import Any
import webbrowser

from PIL import Image
import psutil
import webview

from config import DEFAULT_CONFIG, app_data_dir, config_path, load_config, save_config
from discord_hotkey import DiscordDeafenController, GlobalHotkeyWatcher, send_hotkey
from obs_overlay import OverlayServer
from profiles import apply_level_profile, build_level_profile, profile_key
from screen_reader import (
    ScreenPercentWatcher,
    ScreenReading,
    ScreenTestSession,
    find_window_client_rect,
    normalize_level_name,
)
from updater import (
    APP_VERSION,
    UpdateError,
    UpdateInfo,
    check_for_update,
    cleanup_stale_update_files,
    current_executable_path,
    download_update,
    is_packaged_executable,
    launch_update_swap_and_restart,
)
from window_audio import (
    AudioMuteController,
    MuteTarget,
    list_mute_targets,
    list_open_window_processes,
)

from gdquiet import (
    PERCENT_PRESETS,
    RegionSelectorOverlay,
    _resource_path,
    fade_volume_for_percent,
    format_ocr_diagnostics,
)

_PRESET_MAP: dict[str, tuple[float, float, float, float]] = {
    label: (rx, ry, rw, rh) for label, rx, ry, rw, rh in PERCENT_PRESETS
}

_NUMERIC_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "mute_at_percent": (0.0, 100.0, False),
    "ducking_volume": (0.0, 100.0, False),
    "missing_timeout_s": (0.05, 5.0, False),
    "remute_interval_s": (0.1, 5.0, False),
    "scan_interval_playing_ms": (5.0, 500.0, True),
    "scan_interval_idle_ms": (10.0, 1000.0, True),
    "preview_interval_ms": (50.0, 2000.0, True),
    "test_duration_s": (5.0, 120.0, True),
    "discord_cooldown_ms": (0.0, 2000.0, True),
}


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _setup_icon_win32() -> tuple[Any, Any, Any] | None:
    try:
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HICON),
            ctypes.POINTER(wintypes.HICON),
            wintypes.UINT,
        ]
        shell32.ExtractIconExW.restype = wintypes.UINT
        user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(_ICONINFO)]
        user32.GetIconInfo.restype = wintypes.BOOL
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteObject.restype = wintypes.BOOL
        return shell32, user32, gdi32
    except Exception:
        return None


_ICON_WIN32 = _setup_icon_win32()


def _extract_exe_icon_png(exe_path: str) -> str | None:
    if _ICON_WIN32 is None or not exe_path or not os.path.isfile(exe_path):
        return None
    shell32, user32, gdi32 = _ICON_WIN32
    large = wintypes.HICON()
    count = shell32.ExtractIconExW(exe_path, 0, ctypes.byref(large), None, 1)
    if count == 0 or not large.value:
        return None
    hicon = large.value
    try:
        ii = _ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
            return None
        try:
            hbmp = ii.hbmColor or ii.hbmMask
            bm = _BITMAP()
            if not gdi32.GetObjectW(hbmp, ctypes.sizeof(bm), ctypes.byref(bm)):
                return None
            w, h = int(bm.bmWidth), int(bm.bmHeight)
            if w <= 0 or h <= 0:
                return None
            bmi = _BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0
            buf = (ctypes.c_ubyte * (w * h * 4))()
            hdc = user32.GetDC(None)
            try:
                rows = gdi32.GetDIBits(hdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
            finally:
                user32.ReleaseDC(None, hdc)
            if not rows:
                return None
            img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
            img = img.resize((32, 32), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="PNG")
            return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        finally:
            if ii.hbmColor:
                gdi32.DeleteObject(ii.hbmColor)
            if ii.hbmMask:
                gdi32.DeleteObject(ii.hbmMask)
    except Exception:
        return None
    finally:
        user32.DestroyIcon(hicon)


class GDQuietWebApp:
    """Backend controller powering the WebView2 HTML/CSS/JS desktop UI."""

    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self._maximized = False

        self.cfg_lock = threading.RLock()
        self.cfg = load_config()
        self._active_level_name = ""
        self._active_level_key = ""
        self._tray_audio_override: bool | None = None
        self.tray_icon: Any | None = None

        self.audio_ctrl = AudioMuteController()
        self.discord_ctrl = DiscordDeafenController(
            toggle_cooldown_s=float(self.cfg.get("discord_cooldown_ms", 180)) / 1000.0
        )
        self.apply_lock = threading.Lock()
        self._apply_cond = threading.Condition()
        self._pending_mute: bool | None = None
        self._pending_fade_volume: float | None = None
        self._force_reapply = False
        self._apply_stop = False
        self._apply_thread = threading.Thread(target=self._apply_worker, daemon=True)
        self._apply_thread.start()

        self.reading_lock = threading.Lock()
        self.test_lock = threading.RLock()
        self.test_session = ScreenTestSession(duration_s=float(self.cfg.get("test_duration_s", 30.0)))

        self.current_want_mute = False
        self._current_fade_volume: float | None = None
        self.global_hotkey = GlobalHotkeyWatcher(on_trigger=self._on_global_watch_toggle)
        self._watch_hotkey_status = "Inactive"

        self._pending_update: UpdateInfo | None = None
        self._dismissed_update_version = ""
        self._update_btn_text = "Check for updates"
        self._footer_message = ""
        self._updating = False

        self._pending_level_raw = ""
        self._pending_level_display = ""
        self._level_prompt_visible = False

        self.obs_overlay_server = OverlayServer(
            get_status=self._get_obs_overlay_status,
            on_control=self._queue_obs_overlay_control,
        )

        self._closing = False
        self._force_exit = False
        self._last_audio_enforce_at = 0.0
        self._all_targets: list[MuteTarget] = []
        self._open_processes: list[dict[str, str]] = []
        self._icon_cache: dict[str, str | None] = {}
        self._latest_reading: ScreenReading | None = None
        self._cached_preview_reading_id: int | None = None
        self._cached_preview_url: str | None = None
        self._region_picking = False

        if self.cfg.get("obs_overlay_enabled", False):
            self._start_obs_overlay(persist=False)

        self._apply_watch_toggle_hotkey()
        self._refresh_windows()
        self._refresh_target_windows()

        self.watcher = ScreenPercentWatcher(
            get_settings=self._snapshot_cfg,
            on_reading=self._on_screen_reading,
        )
        self.watcher.start()

        self._start_tray_icon()
        atexit.register(self._cleanup_sync)

        if is_packaged_executable():
            cleanup_stale_update_files(current_executable_path())

        self._update_thread = threading.Thread(target=self._update_scheduler_loop, daemon=True)
        self._update_thread.start()

    def attach_window(self, window: webview.Window) -> None:
        self.window = window
        window.events.closing += self._on_window_closing

    def _on_window_closing(self) -> bool:
        if self._force_exit:
            self._shutdown_and_save()
            return True
        with self.cfg_lock:
            close_to_tray = bool(self.cfg.get("close_to_tray", False))
        if close_to_tray and self.tray_icon is not None and self.window is not None:
            try:
                self.window.hide()
            except Exception:
                pass
            return False
        self._shutdown_and_save()
        return True

    def _shutdown_and_save(self) -> None:
        self._cleanup_sync()
        self._save_cfg()

    def _snapshot_cfg(self) -> dict[str, Any]:
        with self.cfg_lock:
            return dict(self.cfg)

    def _save_cfg(self) -> None:
        with self.cfg_lock:
            save_config(self.cfg)

    # ---- Window actions ----

    def window_minimize(self) -> None:
        if self.window is not None:
            try:
                self.window.minimize()
            except Exception:
                pass

    def window_toggle_maximize(self) -> None:
        if self.window is None:
            return
        try:
            if self._maximized:
                self.window.restore()
                self._maximized = False
            else:
                self.window.maximize()
                self._maximized = True
        except Exception:
            pass

    def window_close(self) -> None:
        with self.cfg_lock:
            close_to_tray = bool(self.cfg.get("close_to_tray", False))
        if close_to_tray and self.tray_icon is not None and self.window is not None:
            try:
                self.window.hide()
            except Exception:
                pass
            return
        self._force_exit = True
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    def _apply_window_preferences(self) -> None:
        if self.window is None:
            return
        with self.cfg_lock:
            on_top = bool(self.cfg.get("always_on_top", False))
        try:
            self.window.on_top = on_top
        except Exception:
            pass

    # ---- Tray ----

    def _start_tray_icon(self) -> None:
        try:
            import pystray
        except Exception:
            return
        logo_path = _resource_path("logo.png")
        try:
            if logo_path.is_file():
                with Image.open(logo_path) as im:
                    tray_img = im.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
            else:
                tray_img = Image.new("RGBA", (64, 64), (90, 82, 201, 255))
        except Exception:
            tray_img = Image.new("RGBA", (64, 64), (90, 82, 201, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Show GD Quiet", lambda _i, _item: self._show_window(), default=True),
            pystray.MenuItem(
                lambda _item: "Pause Watching" if self.cfg.get("watch_enabled", True) else "Resume Watching",
                lambda _i, _item: self.toggle_watching(),
            ),
            pystray.MenuItem("Unmute & Undeafen All", lambda _i, _item: self.force_restore_all()),
            pystray.MenuItem("Exit", lambda _i, _item: self._exit_from_tray()),
        )
        try:
            self.tray_icon = pystray.Icon("gdquiet", tray_img, "GD Quiet", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception:
            self.tray_icon = None

    def _show_window(self) -> None:
        if self.window is None:
            return
        try:
            self.window.show()
            self.window.restore()
        except Exception:
            pass

    def _exit_from_tray(self) -> None:
        self._force_exit = True
        self._shutdown_and_save()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    # ---- State Snapshot for UI ----

    def _describe_region_mode(self) -> str:
        with self.cfg_lock:
            mode = self.cfg.get("watch_mode")
            reg = self.cfg.get("custom_region")
        if mode == "custom_region" and isinstance(reg, dict):
            preset = reg.get("preset")
            if preset:
                return f"Preset: {preset}"
            if reg.get("kind") == "window_rel":
                return "Custom box (locked to window)"
            if reg.get("kind") == "screen":
                return f"Custom screen ({reg.get('width')}x{reg.get('height')})"
        return "Auto top 10%"

    def _get_preview_data_url(self, reading: ScreenReading | None) -> str | None:
        if reading is None or reading.preview_image is None:
            return None
        rid = id(reading)
        if self._cached_preview_reading_id == rid and self._cached_preview_url is not None:
            return self._cached_preview_url
        try:
            thumb = reading.preview_image.copy()
            thumb.thumbnail((520, 180), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            thumb.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            url = f"data:image/png;base64,{encoded}"
            self._cached_preview_reading_id = rid
            self._cached_preview_url = url
            return url
        except Exception:
            return None

    def get_state(self) -> dict[str, Any]:
        with self.cfg_lock:
            cfg_copy = dict(self.cfg)

        deafened = self.discord_ctrl.deafened_by_us
        apps_muted = self.audio_ctrl.is_muted
        apps_ducked = self.audio_ctrl.is_ducking

        with self.test_lock:
            active_test = self.test_session.active
            completed_test = self.test_session.completed
            remaining = self.test_session.remaining_seconds()
            elapsed = self.test_session.elapsed_seconds()
            duration = self.test_session.duration_s
            valid_reads = self.test_session.valid_reads
            peak = self.test_session.peak_percent
            last_pct = self.test_session.last_percent
            milestones = list(self.test_session.milestones)
            samples = list(self.test_session.samples)
            if active_test and remaining <= 0.0:
                self.test_session.stop()
                active_test = False
                completed_test = True
            passed, headline, detail = self.test_session.summary() if completed_test else (False, "", "")

        if active_test:
            st_code, st_head = "testing", "TESTING (30S)"
        elif not cfg_copy.get("watch_enabled", True):
            st_code, st_head = "paused", "WATCHER PAUSED"
        elif apps_muted or deafened:
            st_code, st_head = "muted", "MUTED"
        elif apps_ducked:
            st_code, st_head = "ducking", "DUCKING"
        else:
            st_code, st_head = "watching", "WATCHING"

        if deafened and apps_muted:
            st_sub = "Apps & Discord: Muted"
        elif deafened:
            st_sub = "Discord: Deafened" + (" · Apps Ducked" if apps_ducked else "")
        elif apps_muted:
            st_sub = "Apps: Muted"
        elif apps_ducked:
            st_sub = "Apps: Ducked"
        elif not cfg_copy.get("watch_enabled", True):
            st_sub = "Waiting for a run"
        else:
            st_sub = "Audio & Discord: Clear"

        with self.reading_lock:
            reading = self._latest_reading

        if reading is not None:
            diag, _ = format_ocr_diagnostics(reading)
            reading_dict = {
                "percent": reading.percent,
                "raw_text": reading.raw_text,
                "should_mute": reading.should_mute,
                "is_playing": reading.is_playing,
                "status_text": reading.status_text,
                "scan_ms": reading.scan_ms,
                "diagnostic": diag,
                "preview_data_url": self._get_preview_data_url(reading),
            }
        else:
            reading_dict = {
                "percent": None,
                "raw_text": "",
                "should_mute": False,
                "is_playing": False,
                "status_text": "Waiting for Geometry Dash..." if cfg_copy.get("watch_enabled", True) else "Screen watcher paused",
                "scan_ms": 0.0,
                "diagnostic": "Raw OCR: (waiting)",
                "preview_data_url": None,
            }

        reg = cfg_copy.get("custom_region")
        if cfg_copy.get("watch_mode") == "custom_region" and isinstance(reg, dict):
            capture_short = str(reg.get("preset")) if reg.get("preset") else "Custom"
        else:
            capture_short = "Auto"

        if active_test:
            test_status_text = f"{remaining:4.1f}s left"
            verdict = "Test running — switch to Geometry Dash and play a level!"
        elif completed_test:
            test_status_text = f"Done ({int(round(duration))}s)"
            verdict = f"{headline} — {detail}"
        else:
            test_status_text = "Ready to test"
            verdict = 'Start a level in Geometry Dash with "Show Percentage" enabled, then select Start 30s test.'

        pending_upd = None
        show_banner = False
        if self._pending_update is not None and self._pending_update.update_available:
            pending_upd = {
                "latest_version": self._pending_update.latest_version,
                "tag_name": self._pending_update.tag_name,
            }
            show_banner = self._pending_update.latest_version != self._dismissed_update_version

        level_prompt = (
            {"raw": self._pending_level_raw, "display": self._pending_level_display}
            if self._level_prompt_visible and self._pending_level_display
            else None
        )
        level_det_status = (
            f"Level detection beta is on ({len(cfg_copy.get('level_profiles') or {})} saved profiles)"
            if cfg_copy.get("level_detection_beta", False)
            else "Level detection beta is off"
        )
        active_profile = (
            f"Level profile: {self._active_level_name}"
            if self._active_level_name
            else "No confirmed level profile"
        )

        return {
            "app_version": APP_VERSION,
            "config_path": str(config_path()),
            "cfg": cfg_copy,
            "status": {
                "code": st_code,
                "headline": st_head,
                "sub": st_sub,
                "deafened_by_us": deafened,
            },
            "reading": reading_dict,
            "targets": [
                {
                    "process_name": t.process_name,
                    "display_name": t.display_name,
                    "window_titles": list(t.window_titles),
                    "has_audio": t.has_audio,
                    "currently_muted": t.currently_muted,
                    "icon_data_url": self._icon_cache.get(t.process_name.lower()),
                }
                for t in self._all_targets
            ],
            "open_processes": list(self._open_processes),
            "capture_short": capture_short,
            "region_description": self._describe_region_mode(),
            "watch_hotkey_status": self._watch_hotkey_status,
            "active_level_profile": active_profile,
            "level_detection_status": level_det_status,
            "level_prompt": level_prompt,
            "pending_update": pending_upd,
            "show_update_banner": show_banner,
            "update_btn_text": self._update_btn_text,
            "footer_message": self._footer_message,
            "obs": {
                "running": self.obs_overlay_server.running,
                "url": self.obs_overlay_server.url,
                "status_text": (
                    f"Running locally on {self.obs_overlay_server.url}"
                    if self.obs_overlay_server.running
                    else "Stopped"
                ),
            },
            "test": {
                "active": active_test,
                "completed": completed_test,
                "passed": passed,
                "ratio": max(0.0, min(1.0, elapsed / duration if duration > 0 else 1.0)) if (active_test or completed_test) else 0.0,
                "duration_s": duration,
                "status_text": test_status_text,
                "last_percent": last_pct,
                "peak_percent": peak,
                "valid_reads": valid_reads,
                "milestones": milestones,
                "samples": samples,
                "verdict": verdict,
            },
        }

    # ---- Settings & Controls ----

    def set_threshold(self, pct: float) -> dict[str, Any]:
        clamped = round(max(0.0, min(100.0, float(pct))), 3)
        with self.cfg_lock:
            self.cfg["mute_at_percent"] = clamped
        self._save_cfg()
        return self.get_state()

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        if key in _NUMERIC_BOUNDS:
            min_v, max_v, is_int = _NUMERIC_BOUNDS[key]
            parsed = float(value)
            clamped = max(min_v, min(max_v, parsed))
            value = int(round(clamped)) if is_int else round(clamped, 3)

        with self.cfg_lock:
            self.cfg[key] = value
        self._save_cfg()

        if key == "audio_mode":
            if self.audio_ctrl.has_active_effect:
                self.audio_ctrl.restore()
            with self._apply_cond:
                self._force_reapply = True
                self._apply_cond.notify_all()
        elif key == "ducking_volume":
            if self.audio_ctrl.is_ducking:
                with self._apply_cond:
                    self._force_reapply = True
                    self._apply_cond.notify_all()
        elif key == "discord_cooldown_ms":
            self.discord_ctrl.set_cooldown_ms(float(value))
        elif key == "test_duration_s":
            with self.test_lock:
                self.test_session.duration_s = float(value)
        elif key == "show_background_audio":
            self._refresh_windows()
        elif key == "watch_enabled":
            self._tray_audio_override = None
            if not value:
                self._apply_mute_state(False)
        elif key == "always_on_top":
            self._apply_window_preferences()
        elif key == "beta_updates":
            threading.Thread(target=lambda: self._check_updates_sync(silent=False), daemon=True).start()
        elif key == "auto_deafen" and not value:
            if self.discord_ctrl.deafened_by_us:
                with self.cfg_lock:
                    hk = self.cfg.get("deafen_hotkey")
                self.discord_ctrl.set_deafened(False, hk, False)
        elif key == "level_detection_beta":
            if not value:
                self._active_level_name = ""
                self._active_level_key = ""
                self._pending_level_raw = ""
                self._pending_level_display = ""
                self._level_prompt_visible = False

        return self.get_state()

    def toggle_watching(self) -> dict[str, Any]:
        with self.cfg_lock:
            new_state = not bool(self.cfg.get("watch_enabled", True))
            self.cfg["watch_enabled"] = new_state
        self._save_cfg()
        self._tray_audio_override = None
        if not new_state:
            self._apply_mute_state(False)
        return self.get_state()

    def _on_global_watch_toggle(self) -> None:
        if not self._closing:
            self.toggle_watching()

    def _apply_watch_toggle_hotkey(self) -> None:
        with self.cfg_lock:
            hk = self.cfg.get("watch_toggle_hotkey")
        ok, status = self.global_hotkey.set_hotkey(hk if isinstance(hk, dict) else None)
        if not isinstance(hk, dict):
            self._watch_hotkey_status = "Inactive"
        else:
            self._watch_hotkey_status = status

    def set_watch_hotkey(self, spec: dict[str, Any]) -> dict[str, Any]:
        with self.cfg_lock:
            self.cfg["watch_toggle_hotkey"] = spec
        self._save_cfg()
        self._apply_watch_toggle_hotkey()
        return self.get_state()

    def clear_watch_hotkey(self) -> dict[str, Any]:
        with self.cfg_lock:
            self.cfg["watch_toggle_hotkey"] = None
        self._save_cfg()
        self._apply_watch_toggle_hotkey()
        return self.get_state()

    def set_deafen_hotkey(self, spec: dict[str, Any]) -> dict[str, Any]:
        with self.cfg_lock:
            self.cfg["deafen_hotkey"] = spec
        self._save_cfg()
        return self.get_state()

    def clear_deafen_hotkey(self) -> dict[str, Any]:
        with self.cfg_lock:
            self.cfg["deafen_hotkey"] = None
            self.cfg["auto_deafen"] = False
        self._save_cfg()
        return self.get_state()

    def test_deafen_hotkey(self) -> dict[str, Any]:
        with self.cfg_lock:
            hk = self.cfg.get("deafen_hotkey")
        if not isinstance(hk, dict):
            self._footer_message = "No Discord hotkey set. Click Record hotkey first."
            return self.get_state()
        ok = send_hotkey(hk)
        if ok:
            self._footer_message = f"Sent {hk.get('label', 'hotkey')} — check Discord deafen icon."
        else:
            self._footer_message = "Could not send hotkey."
        return self.get_state()

    # ---- Apps ----

    def _refresh_windows(self) -> None:
        with self.cfg_lock:
            include_bg = bool(self.cfg.get("show_background_audio", False))
        targets = list_mute_targets(include_background_audio=include_bg)
        missing = {t.process_name.lower() for t in targets if t.process_name.lower() not in self._icon_cache}
        if missing:
            exe_by_proc: dict[str, str] = {}
            try:
                for proc in psutil.process_iter(["name", "exe"]):
                    pname = str(proc.info.get("name") or "").lower()
                    if pname in missing and pname not in exe_by_proc:
                        exe_path = proc.info.get("exe")
                        if exe_path:
                            exe_by_proc[pname] = str(exe_path)
            except Exception:
                pass
            for pname in missing:
                exe_path = exe_by_proc.get(pname, "")
                self._icon_cache[pname] = _extract_exe_icon_png(exe_path) if exe_path else None
        self._all_targets = targets

    def refresh_windows(self) -> dict[str, Any]:
        self._refresh_windows()
        return self.get_state()

    def toggle_app(self, proc_name: str) -> dict[str, Any]:
        target = proc_name.strip().lower()
        if not target:
            return self.get_state()
        with self.cfg_lock:
            selected = [str(p).lower() for p in (self.cfg.get("selected_apps") or [])]
            if target in selected:
                selected = [p for p in selected if p != target]
                unchecked = True
            else:
                selected.append(target)
                unchecked = False
            self.cfg["selected_apps"] = sorted(set(selected))
        self._save_cfg()

        if unchecked:
            if self.audio_ctrl.has_active_effect:
                self.audio_ctrl.restore_process(target)
        elif self.current_want_mute or self._current_fade_volume is not None:
            with self._apply_cond:
                self._force_reapply = True
                self._apply_cond.notify_all()
        return self.get_state()

    def clear_selected_apps(self) -> dict[str, Any]:
        with self.cfg_lock:
            self.cfg["selected_apps"] = []
        self._save_cfg()
        if self.audio_ctrl.has_active_effect:
            self.audio_ctrl.restore()
        return self.get_state()

    # ---- Capture & Region ----

    def _refresh_target_windows(self) -> None:
        raw = list_open_window_processes()
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                val = str(item[0]).strip().lower()
                lbl = str(item[1]).strip() or val
            else:
                val = str(item).strip().lower()
                lbl = val
            if val and val not in seen:
                seen.add(val)
                normalized.append({"value": val, "label": lbl})
        with self.cfg_lock:
            cur = str(self.cfg.get("target_process") or "geometrydash.exe").lower()
        if cur and cur not in seen:
            normalized.insert(0, {"value": cur, "label": cur})
        self._open_processes = normalized

    def refresh_target_windows(self) -> dict[str, Any]:
        self._refresh_target_windows()
        return self.get_state()

    def set_target_process(self, proc: str) -> dict[str, Any]:
        val = (proc or "").strip().lower() or "geometrydash.exe"
        with self.cfg_lock:
            self.cfg["target_process"] = val
        self._save_cfg()
        self._refresh_target_windows()
        return self.get_state()

    def set_region_preset(self, preset: str) -> dict[str, Any]:
        if preset == "auto":
            with self.cfg_lock:
                self.cfg["watch_mode"] = "auto_window"
                self.cfg["custom_region"] = None
            self._save_cfg()
            return self.get_state()

        coords = _PRESET_MAP.get(preset)
        if coords is not None:
            rx, ry, rw, rh = coords
            with self.cfg_lock:
                self.cfg["watch_mode"] = "custom_region"
                self.cfg["custom_region"] = {
                    "kind": "window_rel",
                    "preset": preset,
                    "rx": round(rx, 5),
                    "ry": round(ry, 5),
                    "rw": round(rw, 5),
                    "rh": round(rh, 5),
                }
            self._save_cfg()
        return self.get_state()

    def pick_region_now(self) -> dict[str, Any]:
        if self._region_picking:
            return self.get_state()
        self._region_picking = True

        done = threading.Event()

        def worker() -> None:
            try:
                if self.window is not None:
                    try:
                        self.window.minimize()
                    except Exception:
                        pass
                time.sleep(0.18)
                root = tk.Tk()
                root.withdraw()

                def on_sel(left: int, top: int, width: int, height: int) -> None:
                    with self.cfg_lock:
                        target_proc = str(self.cfg.get("target_process") or "geometrydash.exe")
                    rect = find_window_client_rect(target_proc)
                    if rect is not None:
                        wx, wy, ww, wh = rect
                        if ww > 0 and wh > 0:
                            cx = left + width / 2
                            cy = top + height / 2
                            if wx <= cx <= wx + ww and wy <= cy <= wy + wh:
                                reg = {
                                    "kind": "window_rel",
                                    "rx": round((left - wx) / ww, 5),
                                    "ry": round((top - wy) / wh, 5),
                                    "rw": round(width / ww, 5),
                                    "rh": round(height / wh, 5),
                                }
                            else:
                                reg = {"kind": "screen", "left": left, "top": top, "width": width, "height": height}
                        else:
                            reg = {"kind": "screen", "left": left, "top": top, "width": width, "height": height}
                    else:
                        reg = {"kind": "screen", "left": left, "top": top, "width": width, "height": height}
                    with self.cfg_lock:
                        self.cfg["watch_mode"] = "custom_region"
                        self.cfg["custom_region"] = reg
                    self._save_cfg()
                    try:
                        root.destroy()
                    except Exception:
                        pass

                def on_cancel() -> None:
                    try:
                        root.destroy()
                    except Exception:
                        pass

                RegionSelectorOverlay(root, on_selected=on_sel, on_cancel=on_cancel)
                root.mainloop()
            except Exception:
                pass
            finally:
                self._region_picking = False
                if self.window is not None:
                    try:
                        self.window.restore()
                    except Exception:
                        pass
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        done.wait(timeout=120)
        return self.get_state()

    # ---- Level Profiles & Detection ----

    def save_setup(self) -> dict[str, Any]:
        if self._active_level_key:
            with self.cfg_lock:
                profiles = dict(self.cfg.get("level_profiles") or {})
                profiles[self._active_level_key] = build_level_profile(self.cfg, level_name=self._active_level_name)
                self.cfg["level_profiles"] = profiles
            self._save_cfg()
            self._footer_message = f"Saved profile for {self._active_level_name}."
        else:
            self._save_cfg()
            self._footer_message = "Saved current settings."
        return self.get_state()

    def confirm_level_candidate(self) -> dict[str, Any]:
        display = self._pending_level_display.strip()
        if display:
            self._save_level_detection_feedback(display)
        return self.get_state()

    def dismiss_level_candidate(self) -> dict[str, Any]:
        self._level_prompt_visible = False
        return self.get_state()

    def save_level_correction(self, corrected: str) -> dict[str, Any]:
        text = (corrected or "").strip()
        if text:
            self._save_level_detection_feedback(text)
        return self.get_state()

    def _save_level_detection_feedback(self, accepted_name: str) -> None:
        raw_key = normalize_level_name(self._pending_level_raw)
        if raw_key:
            with self.cfg_lock:
                corrections = dict(self.cfg.get("level_name_corrections") or {})
                corrections[raw_key] = accepted_name
                self.cfg["level_name_corrections"] = corrections
        self._activate_level_profile(accepted_name, profile_key(accepted_name))
        self._level_prompt_visible = False

    def _activate_level_profile(self, level_name: str, key: str) -> None:
        with self.cfg_lock:
            profiles = dict(self.cfg.get("level_profiles") or {})
            profile = profiles.get(key)
            if isinstance(profile, dict):
                apply_level_profile(self.cfg, profile)
            else:
                profiles[key] = build_level_profile(self.cfg, level_name=level_name)
                self.cfg["level_profiles"] = profiles
        self._active_level_name = level_name
        self._active_level_key = key
        self._save_cfg()

    # ---- 30s Screen Test ----

    def toggle_screen_test(self) -> dict[str, Any]:
        stopped_early = False
        need_enable_watch = False
        with self.test_lock:
            if self.test_session.active:
                self.test_session.stop()
                stopped_early = True
            else:
                if not self.cfg.get("watch_enabled", True):
                    with self.cfg_lock:
                        self.cfg["watch_enabled"] = True
                    need_enable_watch = True
                self.test_session.start()

        if stopped_early:
            return self.get_state()

        if need_enable_watch:
            self._save_cfg()
        if self.current_want_mute:
            self._apply_mute_state(False)
        return self.get_state()

    # ---- Screen Reading & Audio Pipeline ----

    def _on_screen_reading(self, reading: ScreenReading) -> None:
        if self._closing:
            return
        with self.test_lock:
            testing = self.test_session.active
            if testing:
                self.test_session.record(reading.percent)
                if self.test_session.remaining_seconds() <= 0.0:
                    self.test_session.stop()
                    testing = False

        if reading.detected_level_name and self.cfg.get("level_detection_beta", False):
            raw_name = reading.detected_level_name.strip()
            if raw_name:
                raw_key = normalize_level_name(raw_name)
                with self.cfg_lock:
                    corrections = self.cfg.get("level_name_corrections") or {}
                    display = corrections.get(raw_key, raw_name) if raw_key else raw_name
                self._pending_level_raw = raw_name
                self._pending_level_display = display
                self._level_prompt_visible = True

        with self.cfg_lock:
            watch_enabled = bool(self.cfg.get("watch_enabled", True))
            remute_interval_s = max(0.1, min(5.0, float(self.cfg.get("remute_interval_s", 0.4))))
            sound_feedback = bool(self.cfg.get("sound_feedback", False))
            fade_mode = str(self.cfg.get("fade_in_mode", "off"))
            threshold = float(self.cfg.get("mute_at_percent", 50.0))
            audio_mode = str(self.cfg.get("audio_mode", "mute"))
            ducking_volume = float(self.cfg.get("ducking_volume", 20.0))

        effective_mute = (
            self._tray_audio_override
            if self._tray_audio_override is not None
            else bool(not testing and watch_enabled and reading.should_mute)
        )
        fade_volume = None
        if self._tray_audio_override is None and not testing and watch_enabled and not effective_mute:
            fade_volume = fade_volume_for_percent(
                reading.percent,
                threshold,
                fade_mode,
                ducking_volume if audio_mode == "duck" else 0.0,
            )

        now_m = time.monotonic()
        mute_changed = effective_mute != self.current_want_mute
        fade_changed = fade_volume != self._current_fade_volume
        fade_due = fade_changed and (
            fade_volume is None
            or self._current_fade_volume is None
            or now_m - self._last_audio_enforce_at >= 0.15
        )
        if mute_changed or fade_due:
            if effective_mute or fade_due:
                self._last_audio_enforce_at = now_m
            self._apply_mute_state(effective_mute, fade_volume=fade_volume)
            if mute_changed and sound_feedback:
                try:
                    ctypes.windll.user32.MessageBeep(0x00000040 if effective_mute else 0x0)
                except Exception:
                    pass
        elif effective_mute and (now_m - self._last_audio_enforce_at) >= remute_interval_s:
            self._last_audio_enforce_at = now_m
            self._apply_mute_state(True, force_reapply=True)

        with self.reading_lock:
            self._latest_reading = reading

    def _apply_worker(self) -> None:
        while True:
            with self._apply_cond:
                while (
                    self._pending_mute is None
                    and not self._force_reapply
                    and not self._apply_stop
                ):
                    self._apply_cond.wait()
                if self._apply_stop:
                    return
                want_mute = self._pending_mute
                fade_volume = self._pending_fade_volume
                force_reapply = self._force_reapply
                self._pending_mute = None
                self._force_reapply = False

            if want_mute is None:
                want_mute = self.current_want_mute
                fade_volume = self._current_fade_volume

            with self.cfg_lock:
                apps = list(self.cfg.get("selected_apps") or [])
                hk = self.cfg.get("deafen_hotkey")
                auto_deaf = bool(self.cfg.get("auto_deafen", False))
                audio_mode = str(self.cfg.get("audio_mode", "mute"))
                duck_vol = float(self.cfg.get("ducking_volume", 20.0))

            with self.apply_lock:
                if want_mute:
                    if audio_mode == "duck":
                        self.audio_ctrl.set_duck_volume(apps, duck_vol, force=force_reapply)
                    else:
                        self.audio_ctrl.apply(apps, True, force=force_reapply)
                elif fade_volume is not None:
                    self.audio_ctrl.set_duck_volume(apps, fade_volume, force=force_reapply)
                else:
                    self.audio_ctrl.restore()
                if not force_reapply:
                    self.discord_ctrl.set_deafened(want_mute, hk, auto_deaf)

            if not want_mute and fade_volume is None and self.audio_ctrl.has_active_effect:
                time.sleep(0.08)
                with self._apply_cond:
                    if self._pending_mute is None and not self._apply_stop:
                        self._pending_mute = False
                        self._pending_fade_volume = None
                        self._force_reapply = False

    def _apply_mute_state(
        self,
        muted: bool,
        force_reapply: bool = False,
        fade_volume: float | None = None,
    ) -> None:
        self.current_want_mute = muted
        self._current_fade_volume = None if muted else fade_volume
        with self._apply_cond:
            self._pending_mute = muted
            self._pending_fade_volume = None if muted else fade_volume
            if force_reapply:
                self._force_reapply = True
            self._apply_cond.notify_all()

    def force_restore_all(self) -> dict[str, Any]:
        self._tray_audio_override = None
        self._apply_mute_state(False)
        with self.cfg_lock:
            hk = self.cfg.get("deafen_hotkey")
            auto_deaf = bool(self.cfg.get("auto_deafen", False))
        with self.apply_lock:
            self.audio_ctrl.restore()
            self.discord_ctrl.set_deafened(False, hk, auto_deaf)
        return self.get_state()

    # ---- OBS Overlay ----

    def _get_obs_overlay_status(self) -> dict[str, Any]:
        with self.cfg_lock:
            watching = bool(self.cfg.get("watch_enabled", True))
            threshold = float(self.cfg.get("mute_at_percent", 50.0))
        with self.reading_lock:
            reading = self._latest_reading
        return {
            "watching": watching,
            "percent": reading.percent if reading is not None else None,
            "threshold": threshold,
            "muted": bool(self.current_want_mute or self.audio_ctrl.is_muted or self.discord_ctrl.deafened_by_us),
            "ducking": bool(self.audio_ctrl.is_ducking),
        }

    def _queue_obs_overlay_control(self, payload: dict[str, Any]) -> bool:
        if self._closing:
            return False
        if "watch_enabled" in payload:
            self.set_setting("watch_enabled", bool(payload["watch_enabled"]))
        if "mute_at_percent" in payload:
            self.set_threshold(float(payload["mute_at_percent"]))
        return True

    def _start_obs_overlay(self, persist: bool = True) -> bool:
        try:
            self.obs_overlay_server.start()
        except Exception as exc:
            self._footer_message = f"OBS overlay failed: {exc}"
            return False
        if persist:
            with self.cfg_lock:
                self.cfg["obs_overlay_enabled"] = True
            self._save_cfg()
        return True

    def _stop_obs_overlay(self, persist: bool = True) -> None:
        self.obs_overlay_server.stop()
        if persist:
            with self.cfg_lock:
                self.cfg["obs_overlay_enabled"] = False
            self._save_cfg()

    def toggle_obs_overlay(self) -> dict[str, Any]:
        if self.obs_overlay_server.running:
            self._stop_obs_overlay(persist=True)
        else:
            self._start_obs_overlay(persist=True)
        return self.get_state()

    def copy_obs_url(self) -> dict[str, Any]:
        self._footer_message = f"Copied {self.obs_overlay_server.url}"
        return self.get_state()

    def preview_obs_overlay(self) -> dict[str, Any]:
        if not self.obs_overlay_server.running and not self._start_obs_overlay(persist=True):
            return self.get_state()
        webbrowser.open(self.obs_overlay_server.url)
        return self.get_state()

    # ---- Config & Reset ----

    def open_config_folder(self) -> None:
        folder = str(app_data_dir())
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:
            webbrowser.open(Path(folder).as_uri())

    def reset_defaults(self) -> dict[str, Any]:
        self._tray_audio_override = None
        if self.current_want_mute or self.discord_ctrl.deafened_by_us or self.audio_ctrl.is_muted:
            self.force_restore_all()

        with self.cfg_lock:
            self.cfg.clear()
            self.cfg.update(dict(DEFAULT_CONFIG))
        self._active_level_name = ""
        self._active_level_key = ""
        self._pending_level_raw = ""
        self._pending_level_display = ""
        self._level_prompt_visible = False
        self._save_cfg()

        self.discord_ctrl.set_cooldown_ms(float(DEFAULT_CONFIG["discord_cooldown_ms"]))
        with self.test_lock:
            self.test_session.duration_s = float(DEFAULT_CONFIG["test_duration_s"])
        self._stop_obs_overlay(persist=False)
        self._apply_window_preferences()
        self._apply_watch_toggle_hotkey()
        self._refresh_windows()
        self._refresh_target_windows()
        self._footer_message = "All settings reset to defaults."
        return self.get_state()

    # ---- Updater ----

    def _update_scheduler_loop(self) -> None:
        time.sleep(1.5)
        if not self._closing and self.cfg.get("auto_check_updates", True):
            self._check_updates_sync(silent=True)
        while not self._closing:
            for _ in range(300):
                if self._closing:
                    return
                time.sleep(1.0)
            if not self._updating and self.cfg.get("auto_check_updates", True):
                self._check_updates_sync(silent=True)

    def _check_updates_sync(self, silent: bool = True) -> None:
        if self._updating or self._closing:
            return
        if not silent:
            self._update_btn_text = "Checking..."
            self._footer_message = ""
        try:
            with self.cfg_lock:
                include_pre = bool(self.cfg.get("beta_updates", False))
            info = check_for_update(current_version=APP_VERSION, include_prereleases=include_pre)
            if info.update_available:
                self._pending_update = info
                self._update_btn_text = f"Install v{info.latest_version} update"
                self._footer_message = f"Update available: v{info.latest_version}"
            else:
                self._pending_update = None
                self._update_btn_text = "Check for updates"
                if not silent:
                    self._footer_message = f"You are up to date (v{APP_VERSION})."
        except UpdateError as exc:
            self._update_btn_text = "Check for updates"
            if not silent:
                self._footer_message = f"Update check failed: {exc}"
        except Exception as exc:
            self._update_btn_text = "Check for updates"
            if not silent:
                self._footer_message = f"Update check failed: {exc}"

    def check_or_install_update(self) -> dict[str, Any]:
        if self._pending_update is not None and self._pending_update.update_available:
            return self.install_pending_update()
        threading.Thread(target=lambda: self._check_updates_sync(silent=False), daemon=True).start()
        return self.get_state()

    def dismiss_update_banner(self) -> dict[str, Any]:
        if self._pending_update is not None:
            self._dismissed_update_version = self._pending_update.latest_version
        return self.get_state()

    def install_pending_update(self) -> dict[str, Any]:
        info = self._pending_update
        if info is None or not info.update_available or self._updating:
            return self.get_state()

        if not is_packaged_executable():
            webbrowser.open(info.release_url)
            self._footer_message = f"Opened release page for v{info.latest_version}."
            return self.get_state()

        self._updating = True
        self._update_btn_text = "Downloading update..."
        self._footer_message = f"Downloading v{info.latest_version}..."

        def worker() -> None:
            try:
                target_exe = current_executable_path()
                staged_file = download_update(info, target_exe)
                self._update_btn_text = "Restarting to update..."
                self._cleanup_sync()
                self._save_cfg()
                launch_update_swap_and_restart(
                    staged_file,
                    target_exe,
                    current_pid=os.getpid(),
                    is_installer=info.is_installer,
                )
                self._force_exit = True
                if self.window is not None:
                    try:
                        self.window.destroy()
                    except Exception:
                        pass
            except Exception as exc:
                self._updating = False
                self._update_btn_text = f"Install v{info.latest_version} update"
                self._footer_message = f"Update failed: {exc}"

        threading.Thread(target=worker, daemon=True).start()
        return self.get_state()

    # ---- Cleanup ----

    def _cleanup_sync(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self.watcher.stop()
        except Exception:
            pass
        try:
            self.global_hotkey.stop()
        except Exception:
            pass
        try:
            self.obs_overlay_server.stop()
        except Exception:
            pass
        try:
            with self._apply_cond:
                self._apply_stop = True
                self._apply_cond.notify_all()
        except Exception:
            pass
        try:
            self.audio_ctrl.restore()
        except Exception:
            pass
        try:
            with self.cfg_lock:
                hk = self.cfg.get("deafen_hotkey")
                auto_deaf = bool(self.cfg.get("auto_deafen", False))
            self.discord_ctrl.set_deafened(False, hk, auto_deaf)
        except Exception:
            pass
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass


class WebApi:
    """Thin pywebview JS API wrapper exposing methods of GDQuietWebApp."""

    def __init__(self, app: GDQuietWebApp) -> None:
        self._app = app

    def get_state(self) -> dict[str, Any]:
        return self._app.get_state()

    def window_minimize(self) -> None:
        self._app.window_minimize()

    def window_toggle_maximize(self) -> None:
        self._app.window_toggle_maximize()

    def window_close(self) -> None:
        self._app.window_close()

    def set_threshold(self, pct: float) -> dict[str, Any]:
        return self._app.set_threshold(pct)

    def set_setting(self, key: str, value: Any) -> dict[str, Any]:
        return self._app.set_setting(key, value)

    def toggle_watching(self) -> dict[str, Any]:
        return self._app.toggle_watching()

    def force_restore_all(self) -> dict[str, Any]:
        return self._app.force_restore_all()

    def check_or_install_update(self) -> dict[str, Any]:
        return self._app.check_or_install_update()

    def install_pending_update(self) -> dict[str, Any]:
        return self._app.install_pending_update()

    def dismiss_update_banner(self) -> dict[str, Any]:
        return self._app.dismiss_update_banner()

    def set_watch_hotkey(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._app.set_watch_hotkey(spec)

    def clear_watch_hotkey(self) -> dict[str, Any]:
        return self._app.clear_watch_hotkey()

    def set_deafen_hotkey(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._app.set_deafen_hotkey(spec)

    def clear_deafen_hotkey(self) -> dict[str, Any]:
        return self._app.clear_deafen_hotkey()

    def test_deafen_hotkey(self) -> dict[str, Any]:
        return self._app.test_deafen_hotkey()

    def toggle_app(self, proc_name: str) -> dict[str, Any]:
        return self._app.toggle_app(proc_name)

    def refresh_windows(self) -> dict[str, Any]:
        return self._app.refresh_windows()

    def clear_selected_apps(self) -> dict[str, Any]:
        return self._app.clear_selected_apps()

    def toggle_screen_test(self) -> dict[str, Any]:
        return self._app.toggle_screen_test()

    def set_target_process(self, proc: str) -> dict[str, Any]:
        return self._app.set_target_process(proc)

    def refresh_target_windows(self) -> dict[str, Any]:
        return self._app.refresh_target_windows()

    def set_region_preset(self, preset: str) -> dict[str, Any]:
        return self._app.set_region_preset(preset)

    def pick_region_now(self) -> dict[str, Any]:
        return self._app.pick_region_now()

    def save_setup(self) -> dict[str, Any]:
        return self._app.save_setup()

    def confirm_level_candidate(self) -> dict[str, Any]:
        return self._app.confirm_level_candidate()

    def dismiss_level_candidate(self) -> dict[str, Any]:
        return self._app.dismiss_level_candidate()

    def save_level_correction(self, corrected: str) -> dict[str, Any]:
        return self._app.save_level_correction(corrected)

    def toggle_obs_overlay(self) -> dict[str, Any]:
        return self._app.toggle_obs_overlay()

    def copy_obs_url(self) -> dict[str, Any]:
        return self._app.copy_obs_url()

    def preview_obs_overlay(self) -> dict[str, Any]:
        return self._app.preview_obs_overlay()

    def open_config_folder(self) -> None:
        self._app.open_config_folder()

    def reset_defaults(self) -> dict[str, Any]:
        return self._app.reset_defaults()


def run_web_app() -> None:
    app = GDQuietWebApp()
    api = WebApi(app)
    ui_index = _resource_path("ui/index.html")
    ico_path = _resource_path("assets/gdquiet.ico")

    window = webview.create_window(
        "GD Quiet",
        url=str(ui_index),
        js_api=api,
        width=1160,
        height=760,
        min_size=(920, 620),
        resizable=True,
        frameless=True,
        easy_drag=False,
        shadow=True,
        minimized=bool(app.cfg.get("start_minimized", False)),
        on_top=bool(app.cfg.get("always_on_top", False)),
        background_color="#0e0e11",
    )
    if window is not None:
        app.attach_window(window)

    storage = str(app_data_dir() / "webview")
    try:
        webview.start(
            gui="edgechromium",
            debug=False,
            storage_path=storage,
            icon=str(ico_path) if ico_path.is_file() else None,
        )
    finally:
        app._shutdown_and_save()
