from __future__ import annotations

import atexit
import ctypes
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable
import webbrowser

import mss
from PIL import Image, ImageEnhance, ImageTk

from config import DEFAULT_CONFIG, app_data_dir, config_path, load_config, save_config
from discord_hotkey import DiscordDeafenController, GlobalHotkeyWatcher, parse_tk_hotkey, send_hotkey
from obs_overlay import OverlayServer
from profiles import apply_level_profile, build_level_profile, find_matching_profile, level_key, profile_key
from screen_reader import (
    ScreenPercentWatcher,
    ScreenReading,
    ScreenTestSession,
    WinRtPercentOcr,
    find_window_client_rect,
    get_virtual_screen_bounds,
    split_level_id,
)
from updater import (
    APP_VERSION,
    RELEASES_PAGE_URL,
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

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


PERCENT_PRESETS: tuple[tuple[str, float, float, float, float], ...] = (
    ("Top Center", 0.34, 0.00, 0.32, 0.11),
    ("Top Left", 0.00, 0.00, 0.28, 0.11),
    ("Top Right", 0.72, 0.00, 0.28, 0.11),
    ("Bottom Center", 0.34, 0.88, 0.32, 0.12),
)


COLORS = {
    "bg": "#0e0e11",
    "sidebar": "#131317",
    "card": "#1a1a1f",
    "card2": "#191920",
    "card_alt": "#16161b",
    "plot": "#14141a",
    "hover": "#24242d",
    "border": "#25252d",
    "border2": "#31313b",
    "input": "#131318",
    "text": "#f2f2f4",
    "subtext": "#a1a1aa",
    "faint": "#6f6f7a",
    "accent": "#5a52c9",
    "accent_hover": "#6a61d8",
    "accent_soft": "#252344",
    "accent_text": "#cfc9ff",
    "cyan": "#38bdf8",
    "cyan_dark": "#0284c7",
    "green": "#22c55e",
    "green_text": "#4ade80",
    "green_bg": "#15291e",
    "red": "#ef4444",
    "red_bg": "#2c1518",
    "amber": "#facc15",
    "amber_bg": "#2b2410",
    "pill_idle": "#24242c",
}


class RegionSelectorOverlay:
    """Fullscreen screenshot overlay letting the user drag a box over the % text."""

    def __init__(self, parent: tk.Tk, on_selected: Any, on_cancel: Any) -> None:
        self.parent = parent
        self.on_selected = on_selected
        self.on_cancel = on_cancel
        self.vx, self.vy, self.vw, self.vh = get_virtual_screen_bounds()

        with mss.MSS() as sct:
            shot = sct.grab({"left": self.vx, "top": self.vy, "width": self.vw, "height": self.vh})
            raw_img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        dimmed = ImageEnhance.Brightness(raw_img).enhance(0.55)
        self._photo = ImageTk.PhotoImage(dimmed)
        self._start: tuple[int, int] | None = None
        self._rect_id: int | None = None
        self._size_id: int | None = None

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.vw}x{self.vh}+{self.vx}+{self.vy}")
        self.win.configure(cursor="crosshair")

        self.canvas = tk.Canvas(self.win, width=self.vw, height=self.vh, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self.canvas.create_rectangle(
            self.vw // 2 - 350, 14, self.vw // 2 + 350, 52,
            fill="#0b0f17", outline="#38bdf8", width=1,
        )
        self.canvas.create_text(
            self.vw // 2,
            33,
            text="Drag a box tightly around the % number  •  Release to save  •  Esc to cancel",
            fill="#f8fafc",
            font=("Segoe UI", 11, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", self._cancel)
        self.win.focus_force()

    def _on_press(self, event: tk.Event) -> None:
        self._start = (int(event.x), int(event.y))
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        if self._size_id is not None:
            self.canvas.delete(self._size_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#38bdf8",
            width=2,
        )
        self._size_id = self.canvas.create_text(
            event.x + 8,
            max(64, event.y - 12),
            text="0×0 px",
            fill="#38bdf8",
            anchor="w",
            font=("Consolas", 10, "bold"),
        )

    def _on_drag(self, event: tk.Event) -> None:
        if not self._start or self._rect_id is None:
            return
        x0, y0 = self._start
        x1, y1 = int(event.x), int(event.y)
        self.canvas.coords(self._rect_id, x0, y0, x1, y1)
        if self._size_id is not None:
            self.canvas.coords(self._size_id, min(x0, x1), max(64, min(y0, y1) - 12))
            self.canvas.itemconfigure(self._size_id, text=f"{abs(x1 - x0)}×{abs(y1 - y0)} px")

    def _on_release(self, event: tk.Event) -> None:
        if not self._start:
            self._cancel()
            return
        x1, y1 = self._start
        x2, y2 = int(event.x), int(event.y)
        left = self.vx + min(x1, x2)
        top = self.vy + min(y1, y2)
        width = abs(x2 - x1)
        height = abs(y2 - y1)
        self.win.destroy()
        if width < 12 or height < 10:
            self.on_cancel()
            return
        self.on_selected(left, top, width, height)

    def _cancel(self, _event: Any = None) -> None:
        try:
            self.win.destroy()
        except Exception:
            pass
        self.on_cancel()


def _resource_path(rel_path: str) -> Path:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(str(meipass)) / rel_path)
    app_dir = Path(__file__).resolve().parent
    candidates.append(app_dir / rel_path)
    candidates.append(app_dir.parent / rel_path)
    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[0]


def format_ocr_diagnostics(reading: ScreenReading) -> tuple[str, str]:
    raw_text = " ".join(reading.raw_text.split())
    if len(raw_text) > 100:
        raw_text = raw_text[:97] + "..."
    if reading.percent is not None:
        return f"OCR valid • {reading.scan_ms:.0f} ms • raw: {raw_text or '(empty)'}", COLORS["green"]
    if reading.is_playing or reading.consecutive_missed_reads > 0:
        text = (
            f"No valid % in {reading.consecutive_missed_reads} consecutive scans • "
            f"{reading.scan_ms:.0f} ms • raw: {raw_text or '(empty)'}"
        )
        if reading.is_playing and reading.consecutive_missed_reads >= 8:
            color = COLORS["red"]
        elif reading.consecutive_missed_reads >= 8:
            color = COLORS["amber"]
        else:
            color = COLORS["amber"] if reading.is_playing else COLORS["subtext"]
        return text, color
    return (
        f"Capture status • {reading.status_text} • raw: {raw_text or '(empty)'}",
        COLORS["subtext"],
    )


def fade_volume_for_percent(
    percent: float | None,
    threshold: float,
    mode: str,
    end_volume: float = 0.0,
) -> float | None:
    if percent is None or mode not in {"slow", "fast"} or threshold <= 0 or percent >= threshold:
        return None

    fade_width = 15.0 if mode == "slow" else 5.0
    fade_start = max(0.0, threshold - fade_width)
    if percent <= fade_start:
        return None

    progress = min(1.0, (percent - fade_start) / (threshold - fade_start))
    end = max(0.0, min(100.0, end_volume))
    return 100.0 - progress * (100.0 - end)


class GDQuietApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("GD Quiet")
        self.root.geometry("1120x740")
        self.root.minsize(920, 640)
        self.root.configure(bg=COLORS["bg"])
        self._window_icon_photo: ImageTk.PhotoImage | None = None
        self._sidebar_logo_photo: ImageTk.PhotoImage | None = None
        self._apply_window_icons()

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
        self.recording_hotkey = False
        self._hk_capture_state: tuple[tk.Button, str, str, Callable[[], None] | None] | None = None
        self.global_hotkey = GlobalHotkeyWatcher(on_trigger=self._on_global_watch_toggle)
        self._pending_update: UpdateInfo | None = None
        self._dismissed_update_version: str = ""
        self.obs_overlay_server = OverlayServer(
            get_status=self._get_obs_overlay_status,
            on_control=self._queue_obs_overlay_control,
        )
        self._updating = False
        self._closing = False
        self._region_delay_id: str | None = None
        self._last_audio_enforce_at = 0.0
        self.window_vars: dict[str, tk.BooleanVar] = {}
        self._all_targets: list[MuteTarget] = []
        self._latest_reading: ScreenReading | None = None
        self._ui_reading_pending = False
        self._preview_photo_main: ImageTk.PhotoImage | None = None
        self._active_page = "dashboard"
        self._nav_buttons: dict[str, tuple[tk.Frame, tk.Frame, tk.Button]] = {}
        self._pages: dict[str, tk.Frame] = {}

        self._setup_styles()
        self._build_ui()
        if self.cfg.get("obs_overlay_enabled", False):
            self._start_obs_overlay(show_error=False)
        self._apply_window_preferences()
        self._apply_watch_toggle_hotkey()
        self._show_page("dashboard")
        self._refresh_windows()
        self._refresh_target_windows()

        self.watcher = ScreenPercentWatcher(
            get_settings=self._snapshot_cfg,
            on_reading=self._on_screen_reading,
        )
        self.watcher.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_tray_icon()
        atexit.register(self._cleanup_sync)
        self._test_tick_id: str | None = self.root.after(150, self._test_ui_tick)
        self._startup_update_id: str | None = (
            self.root.after(1500, lambda: self._check_updates_async(silent=True))
            if self.cfg.get("auto_check_updates", True)
            else None
        )
        self._periodic_update_id: str | None = self.root.after(
            300000, self._schedule_periodic_update_check
        )
        if is_packaged_executable():
            cleanup_stale_update_files(current_executable_path())

    def _snapshot_cfg(self) -> dict[str, Any]:
        with self.cfg_lock:
            return dict(self.cfg)

    def _save_cfg(self) -> None:
        with self.cfg_lock:
            save_config(self.cfg)

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Vertical.TScrollbar",
            gripcount=0,
            background=COLORS["card_alt"],
            darkcolor=COLORS["card_alt"],
            lightcolor=COLORS["card_alt"],
            troughcolor=COLORS["input"],
            bordercolor=COLORS["bg"],
            arrowcolor=COLORS["subtext"],
            relief="flat",
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", COLORS["hover"]), ("pressed", COLORS["cyan_dark"])],
            arrowcolor=[("active", COLORS["text"])],
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground=COLORS["input"],
            background=COLORS["card_alt"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["text"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["input"],
            darkcolor=COLORS["input"],
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", COLORS["input"])],
            selectbackground=[("readonly", COLORS["card_alt"])],
            selectforeground=[("readonly", COLORS["text"])],
            foreground=[("readonly", COLORS["text"])],
        )

    def _make_button(
        self,
        parent: tk.Widget,
        text: str,
        command: Any,
        bg: str = COLORS["card_alt"],
        fg: str = COLORS["text"],
        hover_bg: str | None = None,
        padx: int = 14,
        pady: int = 7,
        font: tuple[Any, ...] = ("Segoe UI", 9, "bold"),
    ) -> tk.Button:
        hbg = hover_bg or (COLORS["hover"] if bg == COLORS["card_alt"] else bg)
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hbg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            cursor="hand2",
            font=font,
            padx=padx,
            pady=pady,
        )
        btn._base_bg = bg  # type: ignore[attr-defined]
        btn._hover_bg = hbg  # type: ignore[attr-defined]
        btn.bind(
            "<Enter>",
            lambda _e, b=btn: b.configure(bg=b._hover_bg) if str(b["state"]) != "disabled" else None,  # type: ignore[attr-defined]
        )
        btn.bind(
            "<Leave>",
            lambda _e, b=btn: b.configure(bg=b._base_bg) if str(b["state"]) != "disabled" else None,  # type: ignore[attr-defined]
        )
        return btn

    def _make_card(self, parent: tk.Widget, padx: int = 16, pady: int = 15, bg: str | None = None) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=bg or COLORS["card"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=padx,
            pady=pady,
        )

    def _make_page_header(
        self,
        parent: tk.Widget,
        crumb: str,
        title: str,
        subtitle: str,
    ) -> tuple[tk.Frame, tk.Label, tk.Label]:
        hdr = tk.Frame(parent, bg=COLORS["bg"])
        hdr.pack(fill="x", pady=(0, 18))

        left = tk.Frame(hdr, bg=COLORS["bg"])
        left.pack(side="left", fill="x", expand=True)

        tk.Label(
            left,
            text=crumb,
            bg=COLORS["bg"],
            fg="#5f5f68",
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        ).pack(anchor="w")

        title_lbl = tk.Label(
            left,
            text=title,
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Segoe UI", 19, "bold"),
            anchor="w",
        )
        title_lbl.pack(anchor="w", pady=(3, 2))

        desc_lbl = tk.Label(
            left,
            text=subtitle,
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 10),
            anchor="w",
        )
        desc_lbl.pack(anchor="w")

        saved_pill = tk.Frame(
            hdr,
            bg=COLORS["green_bg"],
            highlightthickness=1,
            highlightbackground="#1e462d",
            padx=10,
            pady=5,
        )
        saved_pill.pack(side="right", anchor="n", pady=(4, 0))
        dot_cv = tk.Canvas(saved_pill, width=8, height=8, bg=COLORS["green_bg"], highlightthickness=0)
        dot_cv.pack(side="left", padx=(0, 6))
        dot_cv.create_oval(1, 1, 7, 7, fill=COLORS["green_text"], outline="")
        tk.Label(
            saved_pill,
            text="Settings saved",
            bg=COLORS["green_bg"],
            fg=COLORS["green_text"],
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")

        return hdr, title_lbl, desc_lbl

    def _apply_window_icons(self) -> None:
        ico_path = _resource_path("assets/gdquiet.ico")
        if ico_path.is_file():
            try:
                self.root.iconbitmap(default=str(ico_path))
            except Exception:
                try:
                    self.root.iconbitmap(str(ico_path))
                except Exception:
                    pass
        logo_path = _resource_path("logo.png")
        if logo_path.is_file():
            try:
                with Image.open(logo_path) as img:
                    icon_img = img.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
                self._window_icon_photo = ImageTk.PhotoImage(icon_img)
                self.root.iconphoto(True, self._window_icon_photo)
            except Exception:
                pass

    def _draw_logo(self, canvas: tk.Canvas) -> None:
        logo_path = _resource_path("logo.png")
        if logo_path.is_file():
            try:
                with Image.open(logo_path) as img:
                    badge = img.convert("RGBA").resize((42, 42), Image.Resampling.LANCZOS)
                self._sidebar_logo_photo = ImageTk.PhotoImage(badge)
                canvas.create_image(21, 21, image=self._sidebar_logo_photo)
                return
            except Exception:
                pass
        canvas.create_rectangle(2, 2, 40, 40, fill=COLORS["accent"], outline="#818cf8", width=1)
        canvas.create_line(11, 31, 31, 11, fill="#f8fafc", width=3, capstyle="round")

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=COLORS["bg"])
        shell.pack(fill="both", expand=True)

        # Left Sidebar
        sidebar = tk.Frame(shell, bg=COLORS["sidebar"], width=248, padx=12, pady=16)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=COLORS["sidebar"])
        brand.pack(fill="x", padx=4, pady=(0, 16))
        logo_cv = tk.Canvas(brand, width=42, height=42, bg=COLORS["sidebar"], highlightthickness=0)
        logo_cv.pack(side="left", padx=(0, 10))
        self._draw_logo(logo_cv)

        brand_txt = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_txt.pack(side="left", fill="x")
        tk.Label(brand_txt, text="GD Quiet", bg=COLORS["sidebar"], fg=COLORS["text"], font=("Segoe UI", 13, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            brand_txt,
            text="SCREEN MUTER",
            bg=COLORS["sidebar"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w")

        # Navigation Tabs
        tk.Label(
            sidebar,
            text="WORKSPACE",
            bg=COLORS["sidebar"],
            fg="#5b5b66",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", padx=8, pady=(0, 6))

        nav_wrap = tk.Frame(sidebar, bg=COLORS["sidebar"])
        nav_wrap.pack(fill="x", expand=True, anchor="n")

        nav_items = [
            ("dashboard", "Overview"),
            ("apps", f"Apps to {'Duck' if self.cfg.get('audio_mode') == 'duck' else 'Mute'}"),
            ("discord", "Discord Auto-Deafen"),
            ("capture", "Screen Capture"),
            ("settings", "Settings"),
        ]
        for key, label in nav_items:
            row = tk.Frame(nav_wrap, bg=COLORS["sidebar"])
            row.pack(fill="x", pady=2)
            indicator = tk.Frame(row, bg=COLORS["sidebar"], width=3)
            indicator.pack(side="left", fill="y", padx=(0, 2))
            btn = tk.Button(
                row,
                text=label,
                command=lambda k=key: self._show_page(k),
                bg=COLORS["sidebar"],
                fg=COLORS["subtext"],
                activebackground=COLORS["accent_soft"],
                activeforeground=COLORS["accent_text"],
                relief="flat",
                bd=0,
                anchor="w",
                cursor="hand2",
                font=("Segoe UI", 10, "bold"),
                padx=10,
                pady=8,
            )
            btn.pack(side="left", fill="x", expand=True)
            btn.bind(
                "<Enter>",
                lambda _e, k=key, r=row, b=btn: (
                    r.configure(bg=COLORS["hover"]),
                    b.configure(bg=COLORS["hover"], fg=COLORS["text"]),
                )
                if self._active_page != k
                else None,
            )
            btn.bind(
                "<Leave>",
                lambda _e, k=key, r=row, b=btn: (
                    r.configure(bg=COLORS["sidebar"]),
                    b.configure(bg=COLORS["sidebar"], fg=COLORS["subtext"]),
                )
                if self._active_page != k
                else None,
            )
            self._nav_buttons[key] = (row, indicator, btn)

        # Live Sidebar Watcher Card
        status_box = tk.Frame(
            sidebar,
            bg=COLORS["card2"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=13,
            pady=12,
        )
        status_box.pack(fill="x", pady=(10, 12))

        st_row = tk.Frame(status_box, bg=COLORS["card2"])
        st_row.pack(fill="x")
        self.status_dot = tk.Canvas(st_row, width=10, height=10, bg=COLORS["card2"], highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self._dot_id = self.status_dot.create_oval(1, 1, 9, 9, fill=COLORS["green"], outline="")

        self.sidebar_status_lbl = tk.Label(
            st_row,
            text="WATCHING",
            bg=COLORS["card2"],
            fg=COLORS["green"],
            font=("Segoe UI", 8, "bold"),
        )
        self.sidebar_status_lbl.pack(side="left")

        self.sidebar_pct_lbl = tk.Label(
            status_box,
            text="-- %",
            bg=COLORS["card2"],
            fg=COLORS["text"],
            font=("Segoe UI", 19, "bold"),
        )
        self.sidebar_pct_lbl.pack(anchor="w", pady=(6, 2))

        self.sidebar_deaf_lbl = tk.Label(
            status_box,
            text="Audio & Discord: Clear",
            bg=COLORS["card2"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
        )
        self.sidebar_deaf_lbl.pack(anchor="w", pady=(0, 8))

        self.sidebar_pct_bar = tk.Canvas(
            status_box,
            height=5,
            bg=COLORS["hover"],
            highlightthickness=0,
        )
        self.sidebar_pct_bar.pack(fill="x", pady=(0, 6))

        self.sidebar_thresh_lbl = tk.Label(
            status_box,
            text=(
                f"{'Ducks at' if self.cfg.get('audio_mode') == 'duck' else 'Mutes past'} "
                f"{float(self.cfg.get('mute_at_percent', 50.0)):.0f}%"
            ),
            bg=COLORS["card2"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
        )
        self.sidebar_thresh_lbl.pack(anchor="center")

        # Sidebar bottom controls
        self.watch_toggle_btn = self._make_button(
            sidebar,
            "Pause Watching" if self.cfg.get("watch_enabled", True) else "Resume Watching",
            self._toggle_watching,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
            pady=7,
        )
        self.watch_toggle_btn.pack(fill="x", pady=(0, 6))

        self.reset_mute_btn = self._make_button(
            sidebar,
            "Restore Audio & Undeafen" if self.cfg.get("audio_mode") == "duck" else "Unmute & Undeafen All",
            self._force_restore_all,
            bg=COLORS["card_alt"],
            fg=COLORS["subtext"],
            pady=5,
            font=("Segoe UI", 8, "bold"),
        )
        self.reset_mute_btn.pack(fill="x", pady=(0, 8))

        ver_row = tk.Frame(sidebar, bg=COLORS["sidebar"])
        ver_row.pack(fill="x", pady=(0, 2))
        tk.Label(
            ver_row,
            text=f"v{APP_VERSION}",
            bg=COLORS["sidebar"],
            fg="#5b5b66",
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(2, 0))

        self.update_btn = self._make_button(
            ver_row,
            "Check for Updates",
            self._on_update_button_click,
            bg=COLORS["sidebar"],
            fg=COLORS["faint"],
            hover_bg=COLORS["hover"],
            padx=6,
            pady=2,
            font=("Segoe UI", 8),
        )
        self.update_btn.configure(highlightthickness=0)
        self.update_btn.pack(side="right")

        self.footer_error = tk.Label(
            sidebar,
            text="",
            bg=COLORS["sidebar"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
            wraplength=220,
            justify="left",
        )
        self.footer_error.pack(anchor="w", pady=(2, 0))

        # Divider
        tk.Frame(shell, bg=COLORS["border"], width=1).pack(side="left", fill="y")

        # Right Main Content Area
        self.main_area = tk.Frame(shell, bg=COLORS["bg"], padx=28, pady=22)
        self.main_area.pack(side="left", fill="both", expand=True)

        # Prominent top Update Banner slot (packed/shown dynamically when a newer release exists)
        self._banner_slot = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._banner_slot.pack(fill="x")

        self.update_banner = tk.Frame(
            self._banner_slot,
            bg="#0f2942",
            highlightthickness=1,
            highlightbackground=COLORS["cyan"],
            padx=12,
            pady=8,
        )
        self.update_banner_lbl = tk.Label(
            self.update_banner,
            text="",
            bg="#0f2942",
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self.update_banner_lbl.pack(side="left", fill="x", expand=True)

        self.update_banner_btn = self._make_button(
            self.update_banner,
            "Update Now",
            self._on_update_banner_install,
            bg=COLORS["cyan"],
            fg="#082f49",
            hover_bg="#7dd3fc",
            padx=12,
            pady=4,
            font=("Segoe UI", 8, "bold"),
        )
        self.update_banner_btn.pack(side="left", padx=(8, 6))

        self._make_button(
            self.update_banner,
            "Later",
            self._dismiss_update_banner,
            bg=COLORS["card_alt"],
            padx=10,
            pady=4,
            font=("Segoe UI", 8),
        ).pack(side="left")

        self._build_dashboard_page()
        self._build_apps_page()
        self._build_discord_page()
        self._build_capture_page()
        self._build_settings_page()

        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel)

        self._update_sidebar_badges()
        self._update_status_indicators()

    def _show_page(self, page_key: str) -> None:
        self._active_page = page_key
        for k, frame in self._pages.items():
            if k == page_key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()

        for k, (row, ind, btn) in self._nav_buttons.items():
            if k == page_key:
                row.configure(bg=COLORS["accent_soft"])
                ind.configure(bg=COLORS["accent"])
                btn.configure(bg=COLORS["accent_soft"], fg=COLORS["accent_text"])
            else:
                row.configure(bg=COLORS["sidebar"])
                ind.configure(bg=COLORS["sidebar"])
                btn.configure(bg=COLORS["sidebar"], fg=COLORS["subtext"])

    def _build_dashboard_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["dashboard"] = page

        self._make_page_header(
            page,
            "GD QUIET / OVERVIEW",
            "Overview",
            "Set your mute threshold and test percentage detection.",
        )

        # Two-column top layout
        cols = tk.Frame(page, bg=COLORS["bg"])
        cols.pack(fill="x", pady=(0, 14))
        cols.columnconfigure(0, weight=16)
        cols.columnconfigure(1, weight=10)

        # Left Card: Mute Threshold & Hotkey
        trig_card = self._make_card(cols)
        trig_card.grid(row=0, column=0, sticky="nsew", padx=(0, 14))

        tk.Label(
            trig_card,
            text="MUTE THRESHOLD",
            bg=COLORS["card"],
            fg="#7b7b84",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w")

        self.trigger_heading_lbl = tk.Label(
            trig_card,
            text="Audio Action at Threshold",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        )
        self.trigger_heading_lbl.pack(anchor="w", pady=(3, 2))
        tk.Label(
            trig_card,
            text="Mute selected apps when your progress passes the threshold.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 10))

        pct_display_row = tk.Frame(trig_card, bg=COLORS["card"])
        pct_display_row.pack(fill="x", pady=(0, 6))

        self.percent_var = tk.DoubleVar(value=float(self.cfg.get("mute_at_percent", 50.0)))
        self.percent_entry_var = tk.StringVar(value=f"{self.percent_var.get():.0f}")

        tk.Entry(
            pct_display_row,
            textvariable=self.percent_entry_var,
            width=4,
            bg=COLORS["card"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            font=("Segoe UI", 28, "bold"),
            justify="left",
        ).pack(side="left")
        tk.Label(
            pct_display_row,
            text="%",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 15, "bold"),
        ).pack(side="left", anchor="s", pady=(0, 8), padx=(0, 16))

        self.save_pct_btn = self._make_button(
            pct_display_row,
            "Apply %",
            self._save_mute_percent,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
        )
        self.save_pct_btn.pack(side="right", anchor="center")

        self.percent_scale = tk.Scale(
            trig_card,
            from_=0,
            to=100,
            resolution=1,
            orient="horizontal",
            variable=self.percent_var,
            showvalue=False,
            bg=COLORS["card"],
            fg=COLORS["text"],
            troughcolor=COLORS["hover"],
            activebackground=COLORS["accent"],
            highlightthickness=0,
            command=self._on_scale_move,
        )
        self.percent_scale.pack(fill="x")

        scale_labels = tk.Frame(trig_card, bg=COLORS["card"])
        scale_labels.pack(fill="x", pady=(2, 10))
        tk.Label(scale_labels, text="0%", bg=COLORS["card"], fg="#5b5b66", font=("Segoe UI", 8)).pack(side="left")
        tk.Label(scale_labels, text="100%", bg=COLORS["card"], fg="#5b5b66", font=("Segoe UI", 8)).pack(side="right")

        tk.Frame(trig_card, bg=COLORS["border"], height=1).pack(fill="x", pady=(2, 10))

        u100_row = tk.Frame(trig_card, bg=COLORS["card"])
        u100_row.pack(fill="x", pady=(0, 10))
        u100_txt = tk.Frame(u100_row, bg=COLORS["card"])
        u100_txt.pack(side="left", fill="x", expand=True)
        tk.Label(
            u100_txt,
            text="Restore audio at 100%",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            u100_txt,
            text="Bring everything back when a level is completed",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
        ).pack(anchor="w")

        self.unmute_100_var = tk.BooleanVar(value=bool(self.cfg.get("unmute_at_100", True)))
        tk.Checkbutton(
            u100_row,
            variable=self.unmute_100_var,
            command=self._on_toggle_unmute_100,
            bg=COLORS["card"],
            fg=COLORS["text"],
            selectcolor=COLORS["input"],
            activebackground=COLORS["card"],
            activeforeground=COLORS["text"],
        ).pack(side="right")

        watch_hk_box = tk.Frame(
            trig_card,
            bg=COLORS["card_alt"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=12,
            pady=10,
        )
        watch_hk_box.pack(fill="x")

        hk_top = tk.Frame(watch_hk_box, bg=COLORS["card_alt"])
        hk_top.pack(fill="x", pady=(0, 8))
        tk.Label(
            hk_top,
            text="Toggle checks",
            bg=COLORS["card_alt"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        watch_hk = self.cfg.get("watch_toggle_hotkey")
        watch_hk_text = watch_hk.get("label", "Not set") if isinstance(watch_hk, dict) else "Not set"
        self.watch_hk_var = tk.StringVar(value=watch_hk_text)
        tk.Label(
            hk_top,
            textvariable=self.watch_hk_var,
            bg=COLORS["hover"],
            fg=COLORS["text"],
            font=("Consolas", 8, "bold"),
            padx=8,
            pady=2,
        ).pack(side="right")

        self.watch_hk_status_lbl = tk.Label(
            hk_top,
            text="Inactive",
            bg=COLORS["card_alt"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        )
        self.watch_hk_status_lbl.pack(side="right", padx=(0, 8))

        hk_btns = tk.Frame(watch_hk_box, bg=COLORS["card_alt"])
        hk_btns.pack(fill="x")
        self.record_watch_hk_btn = self._make_button(
            hk_btns,
            "Record Hotkey",
            self._start_watch_hotkey_record,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
            padx=10,
            pady=4,
            font=("Segoe UI", 8, "bold"),
        )
        self.record_watch_hk_btn.pack(side="left", padx=(0, 8))
        self._make_button(
            hk_btns,
            "Clear",
            self._clear_watch_hotkey,
            bg=COLORS["card"],
            padx=10,
            pady=4,
            font=("Segoe UI", 8),
        ).pack(side="left")

        # Right Card: Current Setup & Level Profile
        setup_card = self._make_card(cols)
        setup_card.grid(row=0, column=1, sticky="nsew")

        tk.Label(
            setup_card,
            text="CURRENT SETUP",
            bg=COLORS["card"],
            fg="#7b7b84",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            setup_card,
            text="Current setup",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(3, 8))

        self.summary_apps_btn = self._make_button(
            setup_card,
            "Apps: 0 selected",
            lambda: self._show_page("apps"),
            bg=COLORS["card_alt"],
            padx=12,
            pady=6,
            font=("Segoe UI", 9, "bold"),
        )
        self.summary_apps_btn.configure(anchor="w")
        self.summary_apps_btn.pack(fill="x", pady=(0, 6))

        self.summary_discord_btn = self._make_button(
            setup_card,
            "Discord Deafen: Off",
            lambda: self._show_page("discord"),
            bg=COLORS["card_alt"],
            padx=12,
            pady=6,
            font=("Segoe UI", 9, "bold"),
        )
        self.summary_discord_btn.configure(anchor="w")
        self.summary_discord_btn.pack(fill="x", pady=(0, 6))

        self.summary_capture_btn = self._make_button(
            setup_card,
            "Capture: Auto (GeometryDash.exe)",
            lambda: self._show_page("capture"),
            bg=COLORS["card_alt"],
            padx=12,
            pady=6,
            font=("Segoe UI", 9, "bold"),
        )
        self.summary_capture_btn.configure(anchor="w")
        self.summary_capture_btn.pack(fill="x", pady=(0, 6))

        self._make_button(
            setup_card,
            "Pick % Spot",
            self._start_region_pick,
            bg=COLORS["card_alt"],
            padx=12,
            pady=5,
            font=("Segoe UI", 8, "bold"),
        ).pack(fill="x", pady=(0, 10))

        tk.Frame(setup_card, bg=COLORS["border"], height=1).pack(fill="x", pady=(0, 10))

        self.active_level_profile_lbl = tk.Label(
            setup_card,
            text="No confirmed level profile active",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        )
        self.active_level_profile_lbl.pack(fill="x")

        self.level_detection_status_lbl = tk.Label(
            setup_card,
            text=(
                "Reading the Eclipse level-name label."
                if self.cfg.get("level_label_enabled", False)
                else "Level label reading is off."
            ),
            bg=COLORS["card"],
            fg=COLORS["amber"] if self.cfg.get("level_label_enabled", False) else "#5b5b66",
            font=("Segoe UI", 8),
            anchor="w",
            wraplength=280,
            justify="left",
        )
        self.level_detection_status_lbl.pack(fill="x", pady=(2, 10))

        self.save_level_profile_btn = self._make_button(
            setup_card,
            "Save Current Settings",
            self._save_active_level_profile,
            bg=COLORS["card_alt"],
            padx=10,
            pady=6,
            font=("Segoe UI", 8, "bold"),
        )
        self.save_level_profile_btn.pack(fill="x")

        # Card 2: 30-Second Screen Test
        test_card = self._make_card(page)
        test_card.pack(fill="both", expand=True)

        tc_top = tk.Frame(test_card, bg=COLORS["card"])
        tc_top.pack(fill="x")

        tc_title = tk.Frame(tc_top, bg=COLORS["card"])
        tc_title.pack(side="left", fill="x", expand=True)
        tk.Label(
            tc_title,
            text="30-second screen test",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            tc_title,
            text="Check percentage detection before your next run. Audio stays unchanged during the test.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))

        self.test_btn = self._make_button(
            tc_top,
            "Start 30s Test",
            self._toggle_screen_test,
            bg=COLORS["accent"],
            fg=COLORS["text"],
            hover_bg=COLORS["accent_hover"],
            padx=14,
            pady=7,
        )
        self.test_btn.pack(side="right", padx=(10, 0))

        # Stats Strip
        stats_strip = tk.Frame(
            test_card,
            bg=COLORS["card_alt"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=12,
            pady=8,
        )
        stats_strip.pack(fill="x", pady=(12, 8))

        self.test_timer_lbl = tk.Label(
            stats_strip,
            text="Ready (30s)",
            bg=COLORS["card_alt"],
            fg=COLORS["text"],
            font=("Consolas", 9, "bold"),
        )
        self.test_timer_lbl.pack(side="left", padx=(0, 18))

        self.test_live_pct_lbl = tk.Label(
            stats_strip,
            text="Live Read: --%",
            bg=COLORS["card_alt"],
            fg=COLORS["text"],
            font=("Consolas", 9, "bold"),
        )
        self.test_live_pct_lbl.pack(side="left", padx=(0, 18))

        self.test_stats_lbl = tk.Label(
            stats_strip,
            text="Peak: --%  •  Reads: 0",
            bg=COLORS["card_alt"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        )
        self.test_stats_lbl.pack(side="left")

        dur_s = int(round(float(self.cfg.get("test_duration_s", 30.0))))
        tk.Label(
            stats_strip,
            text=f"Duration: {dur_s}s",
            bg=COLORS["card_alt"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
        ).pack(side="right")

        self.test_bar_canvas = tk.Canvas(
            test_card,
            height=6,
            bg=COLORS["plot"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self.test_bar_canvas.pack(fill="x", pady=(0, 10))

        # Live Chart + Percentages Seen Feed
        body_row = tk.Frame(test_card, bg=COLORS["card"])
        body_row.pack(fill="both", expand=True, pady=(0, 8))

        chart_wrap = tk.Frame(
            body_row,
            bg=COLORS["plot"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=10,
            pady=8,
        )
        chart_wrap.pack(side="left", fill="both", expand=True, padx=(0, 14))

        tk.Label(
            chart_wrap,
            text="PROGRESS (%)",
            bg=COLORS["plot"],
            fg="#5b5b66",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        self.test_chart = tk.Canvas(chart_wrap, bg=COLORS["plot"], highlightthickness=0, height=175)
        self.test_chart.pack(fill="both", expand=True)
        self.test_chart.bind("<Configure>", lambda _e: self._draw_test_chart())

        # Milestones list on the right
        feed_wrap = tk.Frame(
            body_row,
            bg=COLORS["plot"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            width=225,
            padx=10,
            pady=8,
        )
        feed_wrap.pack(side="right", fill="y")
        feed_wrap.pack_propagate(False)

        tk.Label(
            feed_wrap,
            text="PERCENTAGES SEEN",
            bg=COLORS["plot"],
            fg="#6b6b76",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        self.test_listbox = tk.Listbox(
            feed_wrap,
            bg=COLORS["plot"],
            fg=COLORS["accent_text"],
            selectbackground=COLORS["hover"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Consolas", 9),
        )
        self.test_listbox.pack(fill="both", expand=True)

        # Result Banner
        self.test_verdict_lbl = tk.Label(
            test_card,
            text='Start a level in Geometry Dash with "Show Percentage" enabled, then select Start 30s Test.',
            bg=COLORS["card_alt"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
            padx=12,
            pady=7,
            anchor="w",
            justify="left",
        )
        self.test_verdict_lbl.pack(fill="x")

    def _build_apps_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["apps"] = page

        hdr, self.apps_page_heading_lbl, self.apps_page_desc_lbl = self._make_page_header(
            page,
            "GD QUIET / APPS TO MUTE",
            "Apps to Mute",
            "Select the apps to mute during a run.",
        )

        # Summary card
        summary = self._make_card(page, padx=16, pady=12)
        summary.pack(fill="x", pady=(0, 12))

        sum_left = tk.Frame(summary, bg=COLORS["card"])
        sum_left.pack(side="left", fill="x", expand=True)
        self.apps_summary_count_lbl = tk.Label(
            sum_left,
            text="0 apps selected",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 14, "bold"),
        )
        self.apps_summary_count_lbl.pack(anchor="w")
        self.apps_summary_sub_lbl = tk.Label(
            sum_left,
            text="These apps will mute after 50%. Their audio returns when the run ends.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        )
        self.apps_summary_sub_lbl.pack(anchor="w", pady=(2, 0))

        sum_right = tk.Frame(summary, bg=COLORS["card"])
        sum_right.pack(side="right")
        self.apps_summary_thresh_lbl = tk.Label(
            sum_right,
            text="50%",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 16, "bold"),
        )
        self.apps_summary_thresh_lbl.pack(anchor="e")
        tk.Label(
            sum_right,
            text="MUTE THRESHOLD",
            bg=COLORS["card"],
            fg="#5f5f6a",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="e")

        # Toolbar
        toolbar = tk.Frame(page, bg=COLORS["bg"])
        toolbar.pack(fill="x", pady=(0, 10))

        self.app_search_var = tk.StringVar()
        self.app_search_var.trace_add("write", lambda *_a: self._render_apps_list())
        search_wrap = tk.Frame(
            toolbar,
            bg=COLORS["input"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=8,
        )
        search_wrap.pack(side="left", fill="x", expand=True, padx=(0, 10))
        search_box = tk.Entry(
            search_wrap,
            textvariable=self.app_search_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            font=("Segoe UI", 9),
        )
        search_box.pack(fill="x", ipady=5)

        self.bg_audio_var = tk.BooleanVar(value=bool(self.cfg.get("show_background_audio", False)))
        tk.Checkbutton(
            toolbar,
            text="Show background audio",
            variable=self.bg_audio_var,
            command=self._on_toggle_bg_audio,
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            selectcolor=COLORS["input"],
            activebackground=COLORS["bg"],
            activeforeground=COLORS["text"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 10))

        self._make_button(toolbar, "Refresh Windows", self._refresh_windows, bg=COLORS["card_alt"], padx=10, pady=5).pack(
            side="left", padx=(0, 8)
        )
        self._make_button(toolbar, "Clear All", self._clear_selected_apps, bg=COLORS["card_alt"], padx=10, pady=5).pack(
            side="left"
        )

        # List card
        list_wrap = tk.Frame(page, bg=COLORS["card_alt"], highlightthickness=1, highlightbackground=COLORS["border"])
        list_wrap.pack(fill="both", expand=True)

        thead = tk.Frame(list_wrap, bg=COLORS["card2"], padx=14, pady=8)
        thead.pack(fill="x")
        tk.Label(thead, text="APPLICATION", bg=COLORS["card2"], fg="#5f5f6a", font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Label(thead, text="ACTION", bg=COLORS["card2"], fg="#5f5f6a", font=("Segoe UI", 8, "bold"), width=12, anchor="w").pack(
            side="right"
        )
        tk.Label(thead, text="AUDIO STATUS", bg=COLORS["card2"], fg="#5f5f6a", font=("Segoe UI", 8, "bold"), width=16, anchor="w").pack(
            side="right", padx=(0, 10)
        )
        tk.Frame(list_wrap, bg=COLORS["border"], height=1).pack(fill="x")

        self.win_canvas = tk.Canvas(list_wrap, bg=COLORS["card_alt"], highlightthickness=0)
        self.win_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.win_canvas.yview)
        self.win_list_frame = tk.Frame(self.win_canvas, bg=COLORS["card_alt"])

        self.win_list_frame.bind(
            "<Configure>",
            lambda _e: self.win_canvas.configure(scrollregion=self.win_canvas.bbox("all")),
        )
        self.win_canvas_window = self.win_canvas.create_window((0, 0), window=self.win_list_frame, anchor="nw")
        self.win_canvas.bind(
            "<Configure>",
            lambda e: self.win_canvas.itemconfigure(self.win_canvas_window, width=e.width),
        )
        self.win_canvas.configure(yscrollcommand=self.win_scroll.set)

        self.win_canvas.pack(side="left", fill="both", expand=True)
        self.win_scroll.pack(side="right", fill="y")

        # Bottom callout
        never_box = tk.Frame(
            page,
            bg=COLORS["card_alt"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=14,
            pady=10,
        )
        never_box.pack(fill="x", pady=(12, 0))
        tk.Frame(never_box, bg=COLORS["accent"], width=3).pack(side="left", fill="y", padx=(0, 12))
        never_txt = tk.Frame(never_box, bg=COLORS["card_alt"])
        never_txt.pack(side="left", fill="x", expand=True)
        tk.Label(
            never_txt,
            text="Geometry Dash is never muted",
            bg=COLORS["card_alt"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            never_txt,
            text="GD Quiet only changes audio for your selected apps. Geometry Dash is never muted or controlled.",
            bg=COLORS["card_alt"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        ).pack(anchor="w")

    def _build_discord_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["discord"] = page

        self._make_page_header(
            page,
            "GD QUIET / DISCORD AUTO-DEAFEN",
            "Discord Auto-Deafen",
            "Set up automatic Discord deafen for your runs.",
        )

        cols = tk.Frame(page, bg=COLORS["bg"])
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=15)
        cols.columnconfigure(1, weight=10)

        left_stack = tk.Frame(cols, bg=COLORS["bg"])
        left_stack.grid(row=0, column=0, sticky="nsew", padx=(0, 14))

        # Card 1: Hotkey Setup Controls
        deaf_card = self._make_card(left_stack, pady=14)
        deaf_card.pack(fill="x", pady=(0, 14))

        deaf_hdr = tk.Frame(deaf_card, bg=COLORS["card"])
        deaf_hdr.pack(fill="x")
        hdr_txt = tk.Frame(deaf_hdr, bg=COLORS["card"])
        hdr_txt.pack(side="left", fill="x", expand=True)
        tk.Label(
            hdr_txt,
            text="AUTO-DEAFEN",
            bg=COLORS["card"],
            fg="#7b7b84",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            hdr_txt,
            text="Auto-deafen settings",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(2, 0))

        self.auto_deafen_var = tk.BooleanVar(value=bool(self.cfg.get("auto_deafen", False)))
        tk.Checkbutton(
            deaf_hdr,
            text="Enabled",
            variable=self.auto_deafen_var,
            command=self._on_toggle_auto_deafen,
            bg=COLORS["card"],
            fg=COLORS["text"],
            selectcolor=COLORS["input"],
            activebackground=COLORS["card"],
            activeforeground=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right", anchor="n")

        tk.Label(
            deaf_card,
            text="Automatically toggle Discord deafen past your threshold, then undeafen on death, 100%, or exit.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
            wraplength=500,
            justify="left",
        ).pack(anchor="w", pady=(4, 12))

        tk.Label(
            deaf_card,
            text="TOGGLE DEAFEN HOTKEY",
            bg=COLORS["card"],
            fg="#5f5f6a",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        hk_row = tk.Frame(deaf_card, bg=COLORS["card"])
        hk_row.pack(fill="x")

        hk = self.cfg.get("deafen_hotkey")
        hk_text = hk.get("label", "Not set") if isinstance(hk, dict) else "Not set"
        self.hotkey_var = tk.StringVar(value=hk_text)
        tk.Label(
            hk_row,
            textvariable=self.hotkey_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            highlightthickness=1,
            highlightbackground=COLORS["accent"],
            font=("Consolas", 10, "bold"),
            padx=12,
            pady=6,
            width=18,
            anchor="center",
        ).pack(side="left", padx=(0, 10))

        self.record_hk_btn = self._make_button(
            hk_row,
            "Record Hotkey",
            self._start_hotkey_record,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
        )
        self.record_hk_btn.pack(side="left", padx=(0, 8))
        self._make_button(hk_row, "Test Hotkey", self._test_hotkey, bg=COLORS["card_alt"]).pack(side="left", padx=(0, 8))
        self._make_button(hk_row, "Clear", self._clear_hotkey, bg=COLORS["card_alt"]).pack(side="left")

        tk.Frame(deaf_card, bg=COLORS["border"], height=1).pack(fill="x", pady=(14, 10))

        self.discord_state_badge = tk.Label(
            deaf_card,
            text="Disabled",
            bg=COLORS["pill_idle"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8, "bold"),
            padx=9,
            pady=3,
        )
        self.discord_state_badge.pack(anchor="w")

        # Card 2: Interactive Discord Deafen Setup Tutorial
        guide_card = self._make_card(left_stack, pady=14)
        guide_card.pack(fill="both", expand=True)

        guide_hdr = tk.Frame(guide_card, bg=COLORS["card"])
        guide_hdr.pack(fill="x", pady=(0, 10))

        tk.Label(
            guide_hdr,
            text="How to set it up",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")

        tab_box = tk.Frame(guide_hdr, bg=COLORS["card"])
        tab_box.pack(side="right")

        self._guide_tab_btns: dict[str, tk.Button] = {}
        for tab_key, tab_label in (("steps", "Step-by-Step Guide"), ("fixes", "Troubleshooting")):
            btn = self._make_button(
                tab_box,
                tab_label,
                lambda k=tab_key: self._show_discord_guide_tab(k),
                padx=10,
                pady=4,
                font=("Segoe UI", 8, "bold"),
            )
            btn.pack(side="left", padx=(6, 0))
            self._guide_tab_btns[tab_key] = btn

        self.guide_steps_frame = tk.Frame(guide_card, bg=COLORS["card"])
        self.guide_fixes_frame = tk.Frame(guide_card, bg=COLORS["card"])

        steps = [
            (
                "1",
                "Open Discord Keybinds",
                "Open User Settings → System → Keybinds.",
                None,
            ),
            (
                "2",
                "Create a Toggle Deafen keybind",
                "Add a keybind and choose Toggle Deafen, not Toggle Mute. Use an unused combination, such as Ctrl+Shift+D or F8.",
                None,
            ),
            (
                "3",
                "Match it in GD Quiet",
                "Enable auto-deafen, select Record hotkey, and press the exact same key combination.",
                "step3",
            ),
            (
                "4",
                "Test before starting a level",
                "Join a voice channel and select Test hotkey. Confirm your headphone icon switches between deafened and undeafened.",
                None,
            ),
        ]

        self.tutorial_step3_badge: tk.Label | None = None
        for num, title, desc, tag in steps:
            row = tk.Frame(
                self.guide_steps_frame,
                bg=COLORS["card_alt"],
                highlightthickness=1,
                highlightbackground=COLORS["border"],
                padx=10,
                pady=8,
            )
            row.pack(fill="x", pady=(0, 6))

            num_lbl = tk.Label(
                row,
                text=num,
                bg=COLORS["accent_soft"],
                fg=COLORS["accent_text"],
                font=("Segoe UI", 9, "bold"),
                width=2,
                pady=1,
            )
            num_lbl.pack(side="left", anchor="n", padx=(0, 10), pady=(2, 0))

            body = tk.Frame(row, bg=COLORS["card_alt"])
            body.pack(side="left", fill="x", expand=True)

            top_line = tk.Frame(body, bg=COLORS["card_alt"])
            top_line.pack(fill="x")
            tk.Label(
                top_line,
                text=title,
                bg=COLORS["card_alt"],
                fg=COLORS["text"],
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left")

            if tag == "step3":
                self.tutorial_step3_badge = tk.Label(
                    top_line,
                    text="Needs hotkey",
                    bg=COLORS["amber_bg"],
                    fg=COLORS["amber"],
                    font=("Segoe UI", 8, "bold"),
                    padx=7,
                    pady=1,
                )
                self.tutorial_step3_badge.pack(side="right")

            tk.Label(
                body,
                text=desc,
                bg=COLORS["card_alt"],
                fg=COLORS["subtext"],
                font=("Segoe UI", 8),
                wraplength=480,
                justify="left",
                anchor="w",
            ).pack(fill="x", pady=(2, 0))

        fixes = [
            (
                "Test Hotkey does nothing in Discord",
                "1) Make sure Discord's keybind Action is 'Toggle Deafen' (not Mute) and its toggle switch is ON. 2) Admin mismatch: if Discord runs as Administrator, run GDQuiet.exe as Administrator too. 3) Try F8 or Ctrl+Shift+F9 if another overlay steals the key.",
            ),
            (
                "Discord toggles backwards (deafens when you die)",
                "Start your level UNDEAFENED in Discord. GD Quiet only sends the toggle key when crossing your % and sends it again when you die, exit, or reach 100%. Click Test Hotkey once or uncheck Auto-Deafen to resync.",
            ),
            (
                "Want to manage YouTube, Spotify, or browser audio instead?",
                "Open the Apps to Mute tab and select those windows — GD Quiet adjusts their audio through Windows without needing any keybinds.",
            ),
        ]

        for title, desc in fixes:
            box = tk.Frame(
                self.guide_fixes_frame,
                bg=COLORS["card_alt"],
                highlightthickness=1,
                highlightbackground=COLORS["border"],
                padx=10,
                pady=8,
            )
            box.pack(fill="x", pady=(0, 8))
            tk.Label(
                box,
                text=title,
                bg=COLORS["card_alt"],
                fg=COLORS["accent_text"],
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                box,
                text=desc,
                bg=COLORS["card_alt"],
                fg=COLORS["subtext"],
                font=("Segoe UI", 8),
                wraplength=500,
                justify="left",
                anchor="w",
            ).pack(fill="x", pady=(3, 0))

        self._show_discord_guide_tab("steps")

        # Right Card: When Discord Undeafens
        right_card = self._make_card(cols, pady=14)
        right_card.grid(row=0, column=1, sticky="new")

        tk.Label(
            right_card,
            text="When Discord undeafens",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            right_card,
            text="Discord undeafens when you:",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(3, 10))

        for item in (
            "Die or leave the level",
            "Reach 100% completion",
            "Unmute & undeafen all",
        ):
            chk_row = tk.Frame(right_card, bg=COLORS["card"])
            chk_row.pack(fill="x", pady=3)
            tk.Label(chk_row, text="✓", bg=COLORS["card"], fg=COLORS["green_text"], font=("Segoe UI", 9, "bold")).pack(
                side="left", padx=(0, 8)
            )
            tk.Label(chk_row, text=item, bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 9)).pack(side="left")

        before_box = tk.Frame(
            right_card,
            bg=COLORS["card_alt"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=12,
            pady=10,
        )
        before_box.pack(fill="x", pady=(14, 12))
        tk.Frame(before_box, bg=COLORS["accent"], width=2).pack(side="left", fill="y", padx=(0, 10))
        b_txt = tk.Frame(before_box, bg=COLORS["card_alt"])
        b_txt.pack(side="left", fill="x", expand=True)
        tk.Label(
            b_txt,
            text="BEFORE EVERY RUN",
            bg=COLORS["card_alt"],
            fg="#6b6b76",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 3))
        tk.Label(
            b_txt,
            text="Start undeafened so GD Quiet can restore your audio correctly at the end of the attempt.",
            bg=COLORS["card_alt"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
            wraplength=240,
            justify="left",
        ).pack(anchor="w")

        self._make_button(
            right_card,
            "Troubleshooting",
            lambda: self._show_discord_guide_tab("fixes"),
            bg=COLORS["card_alt"],
            padx=10,
            pady=6,
            font=("Segoe UI", 9, "bold"),
        ).pack(fill="x", pady=(0, 10))

        tk.Label(
            right_card,
            text="Avoid Space, Up, W, or Esc as hotkeys.",
            bg=COLORS["card"],
            fg="#5f5f6a",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(anchor="w")

    def _show_discord_guide_tab(self, tab_key: str) -> None:
        self.guide_steps_frame.pack_forget()
        self.guide_fixes_frame.pack_forget()
        if tab_key == "fixes":
            self.guide_fixes_frame.pack(fill="both", expand=True)
        else:
            self.guide_steps_frame.pack(fill="both", expand=True)

        for key, btn in self._guide_tab_btns.items():
            bg = COLORS["accent"] if key == tab_key else COLORS["card_alt"]
            hover = COLORS["accent_hover"] if key == tab_key else COLORS["hover"]
            btn.configure(bg=bg)
            btn._base_bg = bg  # type: ignore[attr-defined]
            btn._hover_bg = hover  # type: ignore[attr-defined]

    def _build_capture_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["capture"] = page

        self._make_page_header(
            page,
            "GD QUIET / SCREEN CAPTURE",
            "Screen Capture",
            "Choose a game window and the area to read.",
        )

        cols = tk.Frame(page, bg=COLORS["bg"])
        cols.pack(fill="both", expand=True)
        cols.columnconfigure(0, weight=14)
        cols.columnconfigure(1, weight=10)

        left_stack = tk.Frame(cols, bg=COLORS["bg"])
        left_stack.grid(row=0, column=0, sticky="nsew", padx=(0, 14))

        # Target Window Card
        target_card = self._make_card(left_stack, pady=14)
        target_card.pack(fill="x", pady=(0, 14))

        tk.Label(target_card, text="Target window", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            target_card,
            text="Choose the Geometry Dash window to watch.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 10))

        row1 = tk.Frame(target_card, bg=COLORS["card"])
        row1.pack(fill="x")
        self.target_proc_var = tk.StringVar(value=str(self.cfg.get("target_process") or "geometrydash.exe"))
        self.target_combo = ttk.Combobox(
            row1,
            textvariable=self.target_proc_var,
            state="normal",
            width=30,
            style="Dark.TCombobox",
        )
        self.target_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_target_proc_change())
        self.target_combo.bind("<Return>", lambda _e: self._on_target_proc_change())
        self._make_button(row1, "Refresh Windows", self._refresh_target_windows, bg=COLORS["card_alt"], padx=10, pady=4).pack(
            side="left"
        )

        self.watch_status_label = tk.Label(
            target_card,
            text="Waiting for Geometry Dash...",
            bg=COLORS["card"],
            fg=COLORS["amber"],
            font=("Segoe UI", 8, "bold"),
            anchor="w",
        )
        self.watch_status_label.pack(anchor="w", pady=(10, 0))

        # Live Capture Preview Card
        prev_card = self._make_card(left_stack, pady=14)
        prev_card.pack(fill="both", expand=True)

        tk.Label(prev_card, text="Capture preview", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w"
        )
        tk.Label(prev_card, text="What GD Quiet sees", bg=COLORS["card"], fg=COLORS["subtext"], font=("Segoe UI", 9)).pack(
            anchor="w", pady=(2, 10)
        )

        prev_box = tk.Frame(
            prev_card,
            bg=COLORS["plot"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=14,
            pady=14,
        )
        prev_box.pack(fill="both", expand=True, pady=(0, 10))

        self.preview_label = tk.Label(
            prev_box,
            text="Waiting for capture...",
            bg="#101014",
            fg=COLORS["subtext"],
            width=44,
            height=6,
            font=("Segoe UI", 9),
        )
        self.preview_label.pack(expand=True, pady=(6, 10))

        pbar = tk.Frame(prev_card, bg=COLORS["card"])
        pbar.pack(fill="x")
        self.detected_pct_label = tk.Label(
            pbar,
            text="Detected: --%",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Consolas", 11, "bold"),
        )
        self.detected_pct_label.pack(anchor="w")

        self.ocr_diagnostics_label = tk.Label(
            prev_card,
            text="OCR diagnostics: waiting for first scan",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Consolas", 8),
            anchor="w",
            justify="left",
            wraplength=480,
        )
        self.ocr_diagnostics_label.pack(anchor="w", fill="x", pady=(4, 0))

        # Right Card: Percentage Position & Custom Region
        pos_card = self._make_card(cols, pady=14)
        pos_card.grid(row=0, column=1, sticky="new")

        tk.Label(pos_card, text="Percentage position", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            pos_card,
            text="Choose where the percentage appears.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 12))

        opts_grid = tk.Frame(pos_card, bg=COLORS["card"])
        opts_grid.pack(fill="x")
        opts_grid.columnconfigure(0, weight=1)
        opts_grid.columnconfigure(1, weight=1)

        self._make_button(
            opts_grid,
            "Auto Top 10%",
            self._reset_auto_region,
            bg=COLORS["card_alt"],
            padx=8,
            pady=6,
            font=("Segoe UI", 8, "bold"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 6))

        for idx, (p_label, p_rx, p_ry, p_rw, p_rh) in enumerate(PERCENT_PRESETS, start=1):
            r, c = divmod(idx, 2)
            span = 2 if idx == len(PERCENT_PRESETS) and idx % 2 == 0 else 1
            btn = self._make_button(
                opts_grid,
                p_label,
                lambda lbl=p_label, rx=p_rx, ry=p_ry, rw=p_rw, rh=p_rh: self._apply_region_preset(
                    lbl, rx, ry, rw, rh
                ),
                bg=COLORS["card_alt"],
                padx=8,
                pady=6,
                font=("Segoe UI", 8, "bold"),
            )
            btn.grid(
                row=r,
                column=c,
                columnspan=span,
                sticky="ew",
                padx=(0, 6) if c == 0 and span == 1 else (0, 0),
                pady=(0, 6),
            )

        tk.Label(
            pos_card,
            text="CUSTOM REGION",
            bg=COLORS["card"],
            fg="#5b5b66",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(12, 6))

        self._make_button(
            pos_card,
            "Drag Box on Screen",
            self._start_region_pick,
            bg=COLORS["accent"],
            fg=COLORS["text"],
            hover_bg=COLORS["accent_hover"],
        ).pack(fill="x", pady=(0, 6))
        self.delay_pick_btn = self._make_button(
            pos_card,
            "Pick with 3s Delay",
            self._start_region_pick_delayed,
            bg=COLORS["card_alt"],
            padx=10,
            pady=6,
            font=("Segoe UI", 8, "bold"),
        )
        self.delay_pick_btn.pack(fill="x")

        tk.Frame(pos_card, bg=COLORS["border"], height=1).pack(fill="x", pady=(14, 10))
        tk.Label(
            pos_card,
            text="CURRENT REGION",
            bg=COLORS["card"],
            fg="#5b5b66",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 3))
        self.region_mode_label = tk.Label(
            pos_card,
            text=self._describe_region_mode(),
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self.region_mode_label.pack(anchor="w")

        # Bottom tip box
        tip_box = tk.Frame(
            page,
            bg=COLORS["card_alt"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=14,
            pady=10,
        )
        tip_box.pack(fill="x", pady=(14, 0))
        tk.Label(
            tip_box,
            text="Tips for accurate detection",
            bg=COLORS["card_alt"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            tip_box,
            text='Run Geometry Dash in Borderless or Windowed mode with "Show Percentage" enabled for the best accuracy.',
            bg=COLORS["card_alt"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(2, 0))

    def _on_global_mousewheel(self, event: tk.Event) -> None:
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta == 0:
            return
        if self._active_page == "apps" and hasattr(self, "win_canvas"):
            self.win_canvas.yview_scroll(delta, "units")
        elif self._active_page == "settings" and hasattr(self, "settings_canvas"):
            self.settings_canvas.yview_scroll(delta, "units")

    def _build_settings_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["settings"] = page

        self._make_page_header(
            page,
            "GD QUIET / SETTINGS",
            "Settings",
            "Adjust audio, capture, hotkeys, and window settings.",
        )

        scroll_wrap = tk.Frame(page, bg=COLORS["bg"])
        scroll_wrap.pack(fill="both", expand=True)

        self.settings_canvas = tk.Canvas(scroll_wrap, bg=COLORS["bg"], highlightthickness=0, bd=0)
        settings_scroll = ttk.Scrollbar(scroll_wrap, orient="vertical", command=self.settings_canvas.yview)
        self.settings_body = tk.Frame(self.settings_canvas, bg=COLORS["bg"])

        self.settings_body.bind(
            "<Configure>",
            lambda _e: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")),
        )
        self._settings_win_id = self.settings_canvas.create_window((0, 0), window=self.settings_body, anchor="nw")
        self.settings_canvas.bind(
            "<Configure>",
            lambda e: self.settings_canvas.itemconfigure(self._settings_win_id, width=e.width),
        )
        self.settings_canvas.configure(yscrollcommand=settings_scroll.set)
        self.settings_canvas.pack(side="left", fill="both", expand=True)
        settings_scroll.pack(side="right", fill="y")

        self.settings_body.columnconfigure(0, weight=1)
        self.settings_body.columnconfigure(1, weight=1)

        self._settings_num_vars: dict[str, tuple[tk.StringVar, float, float, bool]] = {}

        def add_toggle(
            parent: tk.Frame,
            title: str,
            desc: str,
            var: tk.BooleanVar,
            command: Callable[[], None],
        ) -> None:
            row = tk.Frame(parent, bg=COLORS["card"])
            row.pack(fill="x", pady=4)
            chk = tk.Checkbutton(
                row,
                text=title,
                variable=var,
                command=command,
                bg=COLORS["card"],
                fg=COLORS["text"],
                selectcolor=COLORS["input"],
                activebackground=COLORS["card"],
                activeforeground=COLORS["text"],
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            )
            chk.pack(anchor="w")
            if desc:
                tk.Label(
                    row,
                    text=desc,
                    bg=COLORS["card"],
                    fg=COLORS["faint"],
                    font=("Segoe UI", 8),
                    anchor="w",
                    wraplength=360,
                    justify="left",
                ).pack(anchor="w", padx=(24, 0))

        def add_number_row(
            parent: tk.Frame,
            key: str,
            label: str,
            unit: str,
            min_v: float,
            max_v: float,
            is_int: bool = False,
            on_apply: Callable[[float], None] | None = None,
        ) -> None:
            row = tk.Frame(parent, bg=COLORS["card"])
            row.pack(fill="x", pady=4)
            tk.Label(
                row,
                text=label,
                bg=COLORS["card"],
                fg="#d4d4d8",
                font=("Segoe UI", 9),
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            val = self.cfg.get(key, DEFAULT_CONFIG[key])
            sval = f"{int(round(float(val)))}" if is_int else f"{float(val):.2f}".rstrip("0").rstrip(".")
            var = tk.StringVar(value=sval)
            self._settings_num_vars[key] = (var, min_v, max_v, is_int)

            right = tk.Frame(row, bg=COLORS["card"])
            right.pack(side="right")
            entry = tk.Entry(
                right,
                textvariable=var,
                width=6,
                bg=COLORS["input"],
                fg=COLORS["text"],
                insertbackground=COLORS["text"],
                highlightthickness=1,
                highlightbackground=COLORS["border"],
                relief="flat",
                font=("Consolas", 9, "bold"),
                justify="right",
            )
            entry.pack(side="left", ipady=3, padx=(0, 5))
            tk.Label(right, text=unit, bg=COLORS["card"], fg="#5f5f6a", font=("Segoe UI", 8), width=5, anchor="w").pack(
                side="left", padx=(0, 5)
            )

            def apply_num(_e: Any = None) -> None:
                try:
                    parsed = float(var.get().strip())
                except ValueError:
                    messagebox.showwarning("Invalid Value", f"Enter a number between {min_v} and {max_v} for {label}.")
                    return
                clamped = max(min_v, min(max_v, parsed))
                final_val: int | float = int(round(clamped)) if is_int else round(clamped, 3)
                var.set(str(final_val))
                with self.cfg_lock:
                    self.cfg[key] = final_val
                self._save_cfg()
                if on_apply is not None:
                    on_apply(float(final_val))

            entry.bind("<Return>", apply_num)
            self._make_button(
                right,
                "Apply",
                apply_num,
                bg=COLORS["card_alt"],
                fg=COLORS["subtext"],
                padx=8,
                pady=2,
                font=("Segoe UI", 8, "bold"),
            ).pack(side="left")

        # Card 1: Trigger & muting (row 0, col 0)
        c1 = self._make_card(self.settings_body, pady=12)
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        tk.Label(c1, text="Trigger & muting", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        tk.Label(c1, text="When and how selected apps are muted.", bg=COLORS["card"], fg=COLORS["faint"], font=("Segoe UI", 8)).pack(
            anchor="w", pady=(1, 8)
        )
        add_number_row(
            c1,
            "mute_at_percent",
            "Audio action threshold",
            "%",
            0.0,
            100.0,
            is_int=False,
            on_apply=self._on_settings_percent_applied,
        )
        mode_row = tk.Frame(c1, bg=COLORS["card"])
        mode_row.pack(fill="x", pady=4)
        tk.Label(mode_row, text="Audio action", bg=COLORS["card"], fg="#d4d4d8", font=("Segoe UI", 9)).pack(side="left")
        self.audio_mode_var = tk.StringVar(value=str(self.cfg.get("audio_mode", "mute")))
        audio_mode_combo = ttk.Combobox(
            mode_row,
            textvariable=self.audio_mode_var,
            values=("mute", "duck"),
            state="readonly",
            width=10,
            style="Dark.TCombobox",
        )
        audio_mode_combo.pack(side="right")
        audio_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_audio_mode_changed())

        fade_row = tk.Frame(c1, bg=COLORS["card"])
        fade_row.pack(fill="x", pady=4)
        tk.Label(fade_row, text="Fade before threshold", bg=COLORS["card"], fg="#d4d4d8", font=("Segoe UI", 9)).pack(
            side="left"
        )
        self.fade_in_mode_var = tk.StringVar(value=str(self.cfg.get("fade_in_mode", "off")))
        fade_mode_combo = ttk.Combobox(
            fade_row,
            textvariable=self.fade_in_mode_var,
            values=("off", "slow", "fast"),
            state="readonly",
            width=10,
            style="Dark.TCombobox",
        )
        fade_mode_combo.pack(side="right")
        fade_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_fade_in_mode_changed())

        add_number_row(
            c1,
            "ducking_volume",
            "Ducked app volume",
            "%",
            0.0,
            100.0,
            is_int=False,
            on_apply=self._on_ducking_volume_applied,
        )
        add_number_row(
            c1,
            "missing_timeout_s",
            "Restore delay after death / exit",
            "sec",
            0.05,
            5.0,
            is_int=False,
        )
        add_number_row(
            c1,
            "remute_interval_s",
            "Re-enforce interval (new streams)",
            "sec",
            0.1,
            5.0,
            is_int=False,
        )
        tk.Frame(c1, bg=COLORS["border"], height=1).pack(fill="x", pady=(6, 6))
        add_toggle(
            c1,
            "Restore audio & undeafen at 100%",
            "Automatically restore selected-app audio and Discord when reaching 100%.",
            self.unmute_100_var,
            self._on_toggle_unmute_100,
        )
        add_toggle(
            c1,
            "Show background audio processes",
            "Include apps without visible windows in the audio apps list.",
            self.bg_audio_var,
            self._on_toggle_bg_audio,
        )
        self.sound_feedback_var = tk.BooleanVar(value=bool(self.cfg.get("sound_feedback", False)))
        add_toggle(
            c1,
            "Play sound chime on audio change",
            "Plays a subtle Windows notification sound when audio changes.",
            self.sound_feedback_var,
            self._on_toggle_sound_feedback,
        )

        # Card 2: OCR & screen capture (row 0, col 1)
        c2 = self._make_card(self.settings_body, pady=12)
        c2.grid(row=0, column=1, sticky="nsew", pady=(0, 12))
        tk.Label(c2, text="OCR & screen capture", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            c2,
            text="How often the percentage is read from the game.",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(1, 8))
        self.settings_watch_enabled_var = tk.BooleanVar(value=bool(self.cfg.get("watch_enabled", True)))
        add_toggle(
            c2,
            "Enable screen percentage watching",
            "Master toggle for capturing the screen and muting apps automatically.",
            self.settings_watch_enabled_var,
            self._on_settings_watch_toggle,
        )
        add_number_row(c2, "scan_interval_playing_ms", "Scan interval while playing", "ms", 5.0, 500.0, is_int=True)
        add_number_row(c2, "scan_interval_idle_ms", "Scan interval in menus / idle", "ms", 10.0, 1000.0, is_int=True)
        add_number_row(c2, "preview_interval_ms", "Live preview refresh interval", "ms", 50.0, 2000.0, is_int=True)
        add_number_row(
            c2,
            "test_duration_s",
            "Screen test duration",
            "sec",
            5.0,
            120.0,
            is_int=True,
            on_apply=self._on_settings_test_duration_applied,
        )

        # Card 3: Discord & hotkeys (row 1, col 0)
        c3 = self._make_card(self.settings_body, pady=12)
        c3.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        tk.Label(c3, text="Discord & hotkeys", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            c3,
            text="Automatic deafen and the cooldown between toggles.",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(1, 8))
        add_toggle(
            c3,
            "Enable Discord auto-deafen",
            "Send your Discord Toggle Deafen keybind when crossing the threshold.",
            self.auto_deafen_var,
            self._on_toggle_auto_deafen,
        )
        add_number_row(
            c3,
            "discord_cooldown_ms",
            "Discord hotkey toggle cooldown",
            "ms",
            0.0,
            2000.0,
            is_int=True,
            on_apply=lambda v: self.discord_ctrl.set_cooldown_ms(v),
        )
        tk.Label(
            c3,
            text="Set the same Toggle Deafen keybind in Discord and in GD Quiet so both stay in sync.",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        # Card 4: Window & updates (row 1, col 1)
        c4 = self._make_card(self.settings_body, pady=12)
        c4.grid(row=1, column=1, sticky="nsew", pady=(0, 12))
        tk.Label(c4, text="Window & updates", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            c4,
            text="How GD Quiet behaves on your desktop.",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(1, 8))
        self.always_on_top_var = tk.BooleanVar(value=bool(self.cfg.get("always_on_top", False)))
        add_toggle(
            c4,
            "Always on top",
            "Keep the GD Quiet window floating above other windows.",
            self.always_on_top_var,
            self._on_toggle_always_on_top,
        )
        self.start_minimized_var = tk.BooleanVar(value=bool(self.cfg.get("start_minimized", False)))
        add_toggle(
            c4,
            "Start minimized",
            "Minimize GD Quiet to the taskbar automatically when launched.",
            self.start_minimized_var,
            self._on_toggle_start_minimized,
        )
        self.close_to_tray_var = tk.BooleanVar(value=bool(self.cfg.get("close_to_tray", False)))
        add_toggle(
            c4,
            "Close to system tray",
            "Keep GD Quiet running in the tray when closed.",
            self.close_to_tray_var,
            self._on_toggle_close_to_tray,
        )
        self.auto_updates_var = tk.BooleanVar(value=bool(self.cfg.get("auto_check_updates", True)))
        add_toggle(
            c4,
            "Automatically check for updates",
            "Check GitHub Releases on startup and every 5 minutes.",
            self.auto_updates_var,
            self._on_toggle_auto_updates,
        )
        self.beta_updates_var = tk.BooleanVar(value=bool(self.cfg.get("beta_updates", False)))
        add_toggle(
            c4,
            "Beta updates (opt in)",
            "Allow prerelease versions to appear in the updater.",
            self.beta_updates_var,
            self._on_toggle_beta_updates,
        )

        # Card 5: Experimental features (row 2, col 0)
        c5 = self._make_card(self.settings_body, pady=12)
        c5.grid(row=2, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
        tk.Label(c5, text="Experimental features", bg=COLORS["card"], fg=COLORS["amber"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        tk.Label(c5, text="Early features that may still change.", bg=COLORS["card"], fg=COLORS["faint"], font=("Segoe UI", 8)).pack(
            anchor="w", pady=(1, 8)
        )
        self.level_detection_var = tk.BooleanVar(value=bool(self.cfg.get("level_label_enabled", False)))
        add_toggle(
            c5,
            "Read level name from Eclipse label",
            "Reads the Eclipse mod's level-name label (left side of the game) to load per-level profiles.",
            self.level_detection_var,
            self._on_toggle_level_detection,
        )

        # Card 6: Local settings (row 2, col 1)
        c_local = self._make_card(self.settings_body, pady=12)
        c_local.grid(row=2, column=1, sticky="nsew", pady=(0, 12))
        tk.Label(c_local, text="Local settings", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        tk.Label(c_local, text="Where your configuration lives.", bg=COLORS["card"], fg=COLORS["faint"], font=("Segoe UI", 8)).pack(
            anchor="w", pady=(1, 8)
        )
        tk.Label(
            c_local,
            text="Every setting on this page saves automatically to a local config file on this PC. Nothing is uploaded, and no account is required.",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))
        tk.Label(
            c_local,
            text="Resetting to defaults restores the original values for audio, capture, hotkeys and window behaviour.",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
            wraplength=360,
            justify="left",
        ).pack(anchor="w")

        # Card 7: OBS Studio overlay (row 3, col 0..1 span 2)
        obs_card = self._make_card(self.settings_body, pady=12)
        obs_card.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        tk.Label(obs_card, text="OBS Studio overlay", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            obs_card,
            text="Show live progress and mute state in your stream (Browser Source at 560 x 300).",
            bg=COLORS["card"],
            fg=COLORS["faint"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(1, 8))

        self.obs_overlay_status_lbl = tk.Label(
            obs_card,
            text=self._obs_overlay_status_text(),
            bg=COLORS["green_bg"] if self.obs_overlay_server.running else COLORS["pill_idle"],
            fg=COLORS["green_text"] if self.obs_overlay_server.running else COLORS["subtext"],
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=3,
        )
        self.obs_overlay_status_lbl.pack(anchor="w", pady=(0, 8))

        tk.Label(
            obs_card,
            text="BROWSER SOURCE URL",
            bg=COLORS["card"],
            fg="#5f5f68",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        obs_url_row = tk.Frame(obs_card, bg=COLORS["card"])
        obs_url_row.pack(fill="x", pady=(0, 8))
        self.obs_overlay_url_var = tk.StringVar(value=self.obs_overlay_server.url)
        tk.Label(
            obs_url_row,
            textvariable=self.obs_overlay_url_var,
            bg=COLORS["input"],
            fg=COLORS["subtext"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            font=("Consolas", 9),
            padx=10,
            pady=6,
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._make_button(
            obs_url_row,
            "Copy URL",
            self._copy_obs_overlay_url,
            bg=COLORS["card_alt"],
            padx=11,
            pady=5,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")

        obs_controls = tk.Frame(obs_card, bg=COLORS["card"])
        obs_controls.pack(fill="x")
        self.obs_overlay_toggle_btn = self._make_button(
            obs_controls,
            "Stop OBS Overlay" if self.obs_overlay_server.running else "Enable OBS Overlay",
            self._toggle_obs_overlay,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
            padx=12,
            pady=5,
            font=("Segoe UI", 8, "bold"),
        )
        self.obs_overlay_toggle_btn.pack(side="left", padx=(0, 8))
        self._make_button(
            obs_controls,
            "Preview Overlay",
            self._open_obs_overlay_preview,
            bg=COLORS["card_alt"],
            padx=12,
            pady=5,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")

        # Card 8: Config & data (row 4, col 0..1 span 2)
        c6 = self._make_card(self.settings_body, pady=12)
        c6.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(0, 12))
        tk.Label(c6, text="Config & data", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        tk.Label(c6, text="Open your settings folder or reset to defaults.", bg=COLORS["card"], fg=COLORS["faint"], font=("Segoe UI", 8)).pack(
            anchor="w", pady=(1, 8)
        )
        tk.Label(
            c6,
            text=str(config_path()),
            bg=COLORS["input"],
            fg="#8b8b95",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            font=("Consolas", 8),
            padx=10,
            pady=7,
            anchor="w",
        ).pack(fill="x", pady=(0, 10))
        btn_row = tk.Frame(c6, bg=COLORS["card"])
        btn_row.pack(fill="x")
        self._make_button(
            btn_row,
            "Open Config Folder",
            self._open_config_folder,
            bg=COLORS["card_alt"],
            padx=12,
            pady=6,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left", padx=(0, 8))
        self._make_button(
            btn_row,
            "Reset All Settings to Defaults",
            self._on_reset_defaults,
            bg=COLORS["red_bg"],
            fg="#fca5a5",
            hover_bg=COLORS["red"],
            padx=12,
            pady=6,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")

    # ---- 30-Second Screen Test Logic ----

    def _toggle_screen_test(self) -> None:
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
            self._finish_screen_test_ui()
            return

        if need_enable_watch:
            self._save_cfg()
            self.watch_toggle_btn.configure(text=self._watch_toggle_btn_text())

        # Clear any active mute immediately when starting dry-run test
        if self.current_want_mute:
            self._apply_mute_state(False)

        self.test_listbox.delete(0, "end")
        self.test_btn.configure(text="Stop Test", bg=COLORS["red"], fg=COLORS["text"], activebackground="#dc2626")
        self.test_btn._base_bg = COLORS["red"]  # type: ignore[attr-defined]
        self.test_btn._hover_bg = "#dc2626"  # type: ignore[attr-defined]
        self.test_verdict_lbl.configure(
            text="Test running — switch to Geometry Dash and play a level for 30 seconds!",
            bg=COLORS["card_alt"],
            fg=COLORS["cyan"],
        )
        self._update_status_indicators()

    def _test_ui_tick(self) -> None:
        if self._closing:
            return
        just_completed = False
        with self.test_lock:
            active = self.test_session.active
            completed = self.test_session.completed
            remaining = self.test_session.remaining_seconds()
            elapsed = self.test_session.elapsed_seconds()
            duration = self.test_session.duration_s
            valid_reads = self.test_session.valid_reads
            peak = self.test_session.peak_percent
            last_pct = self.test_session.last_percent
            milestones = list(self.test_session.milestones)

            if active and remaining <= 0.0:
                self.test_session.stop()
                active = False
                completed = True
                just_completed = True

        if just_completed:
            self._finish_screen_test_ui()

        if active or completed:
            ratio = max(0.0, min(1.0, elapsed / duration if duration > 0 else 1.0))
            self._draw_test_progress(ratio)
            if active:
                self.test_timer_lbl.configure(text=f"{remaining:4.1f}s left", fg=COLORS["cyan"])
            else:
                self.test_timer_lbl.configure(text="Done (30s)", fg=COLORS["green"])

            self.test_live_pct_lbl.configure(
                text=f"Live Read: {last_pct:.2f}%" if last_pct is not None else "Live Read: --%"
            )
            peak_txt = f"{peak:.2f}%" if peak is not None else "--%"
            self.test_stats_lbl.configure(text=f"Peak: {peak_txt}  •  Reads: {valid_reads}")

            cur_len = self.test_listbox.size()
            if len(milestones) > cur_len:
                for item in milestones[cur_len:]:
                    self.test_listbox.insert("end", item)
                self.test_listbox.see("end")

            self._draw_test_chart()
        else:
            self._draw_test_progress(0.0)

        if not self._closing:
            self._test_tick_id = self.root.after(150, self._test_ui_tick)

    def _finish_screen_test_ui(self) -> None:
        with self.test_lock:
            passed, headline, detail = self.test_session.summary()
        self.test_btn.configure(text="Start 30s Test", bg=COLORS["cyan"], fg="#082f49", activebackground="#7dd3fc")
        self.test_btn._base_bg = COLORS["cyan"]  # type: ignore[attr-defined]
        self.test_btn._hover_bg = "#7dd3fc"  # type: ignore[attr-defined]
        self.test_verdict_lbl.configure(
            text=f"{headline}  —  {detail}",
            bg=COLORS["green_bg"] if passed else COLORS["amber_bg"],
            fg=COLORS["text"],
        )
        self._update_status_indicators()

    def _draw_test_progress(self, ratio: float) -> None:
        c = self.test_bar_canvas
        w = max(10, c.winfo_width())
        h = max(6, c.winfo_height())
        c.delete("all")
        fill_w = int(w * max(0.0, min(1.0, ratio)))
        if fill_w > 0:
            c.create_rectangle(0, 0, fill_w, h, fill=COLORS["cyan"], outline="")

    def _draw_test_chart(self) -> None:
        c = self.test_chart
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 40 or h < 40:
            return
        c.delete("all")

        pad_l, pad_r, pad_t, pad_b = 34, 10, 10, 20
        pw = max(10, w - pad_l - pad_r)
        ph = max(10, h - pad_t - pad_b)

        # Horizontal grid lines (0%, 50%, 100%)
        for pct in (0, 50, 100):
            y = pad_t + ph - (pct / 100.0) * ph
            c.create_line(pad_l, y, pad_l + pw, y, fill="#1e293b", width=1)
            c.create_text(pad_l - 6, y, text=f"{pct}%", fill=COLORS["subtext"], font=("Consolas", 7), anchor="e")

        # Threshold dashed line
        thresh = float(self.cfg.get("mute_at_percent", 50.0))
        ty = pad_t + ph - (thresh / 100.0) * ph
        c.create_line(pad_l, ty, pad_l + pw, ty, fill=COLORS["amber"], dash=(4, 4), width=1)

        with self.test_lock:
            samples = list(self.test_session.samples)
            duration = self.test_session.duration_s

        if not samples:
            c.create_text(
                pad_l + pw / 2,
                pad_t + ph / 2,
                text="Percentages will plot here during the 30s test",
                fill=COLORS["subtext"],
                font=("Segoe UI", 9),
            )
            return

        # Plot valid points connected smoothly across continuous runs
        segment: list[float] = []
        for elapsed, pct in samples:
            if pct is None:
                if len(segment) >= 4:
                    c.create_line(*segment, fill=COLORS["cyan"], width=2, smooth=True)
                elif len(segment) == 2:
                    x, y = segment
                    c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=COLORS["cyan"], outline="")
                segment = []
                continue
            x = pad_l + (min(duration, max(0.0, elapsed)) / duration) * pw
            y = pad_t + ph - (pct / 100.0) * ph
            segment.extend([x, y])

        if len(segment) >= 4:
            c.create_line(*segment, fill=COLORS["cyan"], width=2, smooth=True)
        elif len(segment) == 2:
            x, y = segment
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=COLORS["cyan"], outline="")

    # ---- Capture & Mute Pipeline ----

    def _describe_region_mode(self) -> str:
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

    def _watch_toggle_btn_text(self) -> str:
        enabled = bool(self.cfg.get("watch_enabled", True))
        hk = self.cfg.get("watch_toggle_hotkey")
        base = "Pause Watching" if enabled else "Resume Watching"
        if isinstance(hk, dict) and hk.get("label"):
            return f"{base} ({hk['label']})"
        return base

    def _on_global_watch_toggle(self) -> None:
        try:
            self.root.after(0, self._toggle_watching)
        except Exception:
            pass

    def _apply_watch_toggle_hotkey(self) -> None:
        with self.cfg_lock:
            hk = self.cfg.get("watch_toggle_hotkey")
        if isinstance(hk, dict):
            self.watch_hk_var.set(str(hk.get("label") or "Set"))
            active, status_msg = self.global_hotkey.set_hotkey(hk)
            self.watch_hk_status_lbl.configure(
                text=status_msg,
                fg=COLORS["green"] if active else COLORS["amber"],
            )
        else:
            self.watch_hk_var.set("Not set")
            self.global_hotkey.set_hotkey(None)
            self.watch_hk_status_lbl.configure(text="Inactive", fg=COLORS["subtext"])
        self.watch_toggle_btn.configure(text=self._watch_toggle_btn_text())

    def _start_watch_hotkey_record(self) -> None:
        self.global_hotkey.set_hotkey(None)

        def on_captured(parsed: dict[str, Any]) -> None:
            with self.cfg_lock:
                self.cfg["watch_toggle_hotkey"] = parsed
            self._save_cfg()
            self._apply_watch_toggle_hotkey()

        self._begin_hotkey_capture(
            button=self.record_watch_hk_btn,
            base_bg=COLORS["card_alt"],
            restore_text="Record Hotkey",
            on_captured=on_captured,
            on_cancel=self._apply_watch_toggle_hotkey,
        )

    def _clear_watch_hotkey(self) -> None:
        with self.cfg_lock:
            self.cfg["watch_toggle_hotkey"] = None
        self._save_cfg()
        self._apply_watch_toggle_hotkey()

    def _toggle_watching(self) -> None:
        new_val = not bool(self.cfg.get("watch_enabled", True))
        with self.cfg_lock:
            self.cfg["watch_enabled"] = new_val
        self._save_cfg()

        if hasattr(self, "settings_watch_enabled_var"):
            self.settings_watch_enabled_var.set(new_val)
        self.watch_toggle_btn.configure(text=self._watch_toggle_btn_text())
        if not new_val:
            watcher = getattr(self, "watcher", None)
            if watcher is not None and hasattr(watcher, "_debouncer"):
                watcher._debouncer.reset()
            self._apply_mute_state(self._tray_audio_override is True)
        self._update_status_indicators()

    def _apply_window_preferences(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.cfg.get("always_on_top", False)))
        except Exception:
            pass
        if self.cfg.get("start_minimized", False):
            try:
                self.root.iconify()
            except Exception:
                pass

    def _start_tray_icon(self) -> None:
        try:
            from pystray import Icon, Menu, MenuItem

            with Image.open(_resource_path("assets/gdquiet.ico")) as icon_image:
                image = icon_image.convert("RGBA")
            self.tray_icon = Icon(
                "GD Quiet",
                image,
                "GD Quiet",
                menu=Menu(
                    MenuItem("Open GD Quiet", lambda _icon, _item: self._schedule_tray_action(self._show_window)),
                    MenuItem(
                        "Pause / Resume Watching",
                        lambda _icon, _item: self._schedule_tray_action(self._toggle_watching),
                    ),
                    MenuItem(
                        "Toggle Audio Action",
                        lambda _icon, _item: self._schedule_tray_action(self._toggle_tray_mute),
                    ),
                    Menu.SEPARATOR,
                    MenuItem("Exit", lambda _icon, _item: self._schedule_tray_action(self._exit_from_tray)),
                ),
            )
            self.tray_icon.run_detached()
        except Exception as exc:
            self.tray_icon = None
            self.footer_error.configure(text=f"System tray unavailable: {exc}", fg=COLORS["amber"])

    def _schedule_tray_action(self, action: Callable[[], None]) -> None:
        if self._closing:
            return
        try:
            self.root.after(0, action)
        except tk.TclError:
            return

    def _show_window(self) -> None:
        if self._closing:
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _exit_from_tray(self) -> None:
        self._on_close(force_exit=True)

    def _toggle_tray_mute(self) -> None:
        current = self.current_want_mute or self.audio_ctrl.has_active_effect
        if self._tray_audio_override is not None:
            current = self._tray_audio_override
        self._tray_audio_override = not current
        self._apply_mute_state(self._tray_audio_override, force_reapply=True)
        self._update_status_indicators()

    def _on_toggle_close_to_tray(self) -> None:
        with self.cfg_lock:
            self.cfg["close_to_tray"] = bool(self.close_to_tray_var.get())
        self._save_cfg()

    def _on_audio_mode_changed(self) -> None:
        mode = self.audio_mode_var.get()
        if mode not in {"mute", "duck"}:
            mode = "mute"
            self.audio_mode_var.set(mode)
        with self.cfg_lock:
            self.cfg["audio_mode"] = mode
        self._save_cfg()
        self._update_sidebar_badges()
        self._apply_mute_state(self.current_want_mute, force_reapply=True)

    def _on_fade_in_mode_changed(self) -> None:
        mode = self.fade_in_mode_var.get()
        if mode not in {"off", "slow", "fast"}:
            mode = "off"
            self.fade_in_mode_var.set(mode)
        with self.cfg_lock:
            self.cfg["fade_in_mode"] = mode
        self._save_cfg()

    def _on_ducking_volume_applied(self, _volume: float) -> None:
        self._apply_mute_state(self.current_want_mute, force_reapply=True)

    def _on_settings_percent_applied(self, pct: float) -> None:
        self.percent_var.set(pct)
        self.percent_entry_var.set(f"{pct:.0f}")
        self._update_sidebar_badges()

    def _on_settings_test_duration_applied(self, dur: float) -> None:
        with self.test_lock:
            self.test_session.duration_s = dur
        if hasattr(self, "test_timer_lbl") and not self.test_session.active:
            self.test_timer_lbl.configure(text=f"Ready ({int(round(dur))}s)")

    def _on_settings_watch_toggle(self) -> None:
        desired = bool(self.settings_watch_enabled_var.get())
        if desired != bool(self.cfg.get("watch_enabled", True)):
            self._toggle_watching()

    def _on_toggle_sound_feedback(self) -> None:
        with self.cfg_lock:
            self.cfg["sound_feedback"] = bool(self.sound_feedback_var.get())
        self._save_cfg()

    def _on_toggle_always_on_top(self) -> None:
        val = bool(self.always_on_top_var.get())
        with self.cfg_lock:
            self.cfg["always_on_top"] = val
        self._save_cfg()
        try:
            self.root.attributes("-topmost", val)
        except Exception:
            pass

    def _on_toggle_start_minimized(self) -> None:
        with self.cfg_lock:
            self.cfg["start_minimized"] = bool(self.start_minimized_var.get())
        self._save_cfg()

    def _on_toggle_auto_updates(self) -> None:
        with self.cfg_lock:
            self.cfg["auto_check_updates"] = bool(self.auto_updates_var.get())
        self._save_cfg()

    def _on_toggle_beta_updates(self) -> None:
        # No working beta builds are published right now; keep everyone on stable.
        self.beta_updates_var.set(False)
        with self.cfg_lock:
            self.cfg["beta_updates"] = False
        self._save_cfg()
        messagebox.showinfo(
            "Beta updates",
            "There are no beta updates that work right now. GD Quiet will keep installing stable updates.",
            parent=self.root,
        )

    def _on_toggle_level_detection(self) -> None:
        enabled = bool(self.level_detection_var.get())
        with self.cfg_lock:
            self.cfg["level_label_enabled"] = enabled
        self._save_cfg()
        if enabled:
            self.level_detection_status_lbl.configure(
                text="Reading the Eclipse level-name label.",
                fg=COLORS["amber"],
            )
        else:
            self.level_detection_status_lbl.configure(text="Level label reading is off.", fg=COLORS["subtext"])
            self._active_level_name = ""
            self._active_level_key = ""

    def _activate_level_profile(self, level_name: str, key: str) -> None:
        if not key:
            return
        with self.cfg_lock:
            profiles = self.cfg.get("level_profiles")
            profiles = dict(profiles) if isinstance(profiles, dict) else {}
            existing = profiles.get(key)
            if isinstance(existing, dict):
                profile = dict(existing)
                self.cfg = apply_level_profile(self.cfg, profile)
                message = f"Loaded saved settings for {level_name}."
            else:
                profile = build_level_profile(level_name, self.cfg)
                profiles[key] = profile
                self.cfg["level_profiles"] = profiles
                message = f"Created a profile for {level_name}. Adjust settings, then save them here."
            self._active_level_name = str(profile.get("name") or level_name)
            self._active_level_key = key
            self.cfg["level_profiles"] = profiles
        self._save_cfg()
        if hasattr(self, "active_level_profile_lbl"):
            self.active_level_profile_lbl.configure(
                text=f"Active level profile: {self._active_level_name} — {message}",
                fg=COLORS["green"] if isinstance(existing, dict) else COLORS["amber"],
            )
        self._sync_level_profile_widgets()

    def _save_active_level_profile(self) -> None:
        if not self._active_level_key:
            self.active_level_profile_lbl.configure(
                text="Play a level with the Eclipse level label visible first.",
                fg=COLORS["amber"],
            )
            return
        with self.cfg_lock:
            profiles = self.cfg.get("level_profiles")
            profiles = dict(profiles) if isinstance(profiles, dict) else {}
            profiles[self._active_level_key] = build_level_profile(self._active_level_name, self.cfg)
            self.cfg["level_profiles"] = profiles
        self._save_cfg()
        self.active_level_profile_lbl.configure(
            text=f"Saved current threshold, app selection, and capture region for {self._active_level_name}.",
            fg=COLORS["green"],
        )

    def _sync_level_profile_widgets(self) -> None:
        threshold = float(self.cfg.get("mute_at_percent", 50.0))
        if hasattr(self, "percent_var"):
            self.percent_var.set(threshold)
            self.percent_entry_var.set(f"{threshold:.0f}")
        if hasattr(self, "_settings_num_vars") and "mute_at_percent" in self._settings_num_vars:
            self._settings_num_vars["mute_at_percent"][0].set(f"{threshold:.3f}".rstrip("0").rstrip("."))
        if hasattr(self, "target_proc_var"):
            self.target_proc_var.set(str(self.cfg.get("target_process") or "geometrydash.exe"))
            self.region_mode_label.configure(text=self._describe_region_mode())
            self._refresh_target_windows()
        if hasattr(self, "win_list_frame"):
            self._refresh_windows()
        self._update_sidebar_badges()
        if self.current_want_mute:
            self._apply_mute_state(True, force_reapply=True)

    def _on_level_detected(self, level_name: str) -> None:
        """Auto-load (or create) the profile for a level read from the Eclipse label."""
        if self._closing:
            return
        with self.cfg_lock:
            if not self.cfg.get("level_label_enabled", False):
                return
        with self.cfg_lock:
            profiles = dict(self.cfg.get("level_profiles") or {})
        name, level_id = split_level_id(level_name)
        key = find_matching_profile(profiles, level_name) or level_key(name, level_id)
        if not key or key == self._active_level_key:
            return
        self._activate_level_profile(name or f"Level {level_id}", key)
        self.level_detection_status_lbl.configure(
            text=f"Detected from Eclipse label: {self._active_level_name}",
            fg=COLORS["green"],
        )

    def _open_config_folder(self) -> None:
        try:
            os.startfile(str(app_data_dir()))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Open Folder Failed", str(exc))

    def _on_reset_defaults(self) -> None:
        if not messagebox.askyesno(
            "Reset All Settings",
            "Reset all GD Quiet settings to their default values?",
        ):
            return
        with self.cfg_lock:
            self.cfg = dict(DEFAULT_CONFIG)
        self._active_level_name = ""
        self._active_level_key = ""
        self._save_cfg()
        if self.obs_overlay_server.running:
            self._stop_obs_overlay(persist=False)
        else:
            self.obs_overlay_toggle_btn.configure(text="Enable OBS Overlay")
            self.obs_overlay_status_lbl.configure(
                text="Overlay server stopped. It only listens on this computer.",
                fg=COLORS["subtext"],
            )

        pct = float(self.cfg["mute_at_percent"])
        self.percent_var.set(pct)
        self.percent_entry_var.set(f"{pct:.0f}")
        self.unmute_100_var.set(bool(self.cfg["unmute_at_100"]))
        self.bg_audio_var.set(bool(self.cfg["show_background_audio"]))
        self.auto_deafen_var.set(bool(self.cfg["auto_deafen"]))
        self.hotkey_var.set("Not set")
        self.target_proc_var.set(str(self.cfg["target_process"]))
        self.region_mode_label.configure(text=self._describe_region_mode())
        self.settings_watch_enabled_var.set(bool(self.cfg["watch_enabled"]))
        self.sound_feedback_var.set(bool(self.cfg["sound_feedback"]))
        self.always_on_top_var.set(bool(self.cfg["always_on_top"]))
        self.start_minimized_var.set(bool(self.cfg["start_minimized"]))
        self.close_to_tray_var.set(bool(self.cfg["close_to_tray"]))
        self.auto_updates_var.set(bool(self.cfg["auto_check_updates"]))
        self.beta_updates_var.set(bool(self.cfg["beta_updates"]))
        self.level_detection_var.set(bool(self.cfg["level_label_enabled"]))
        self.audio_mode_var.set(str(self.cfg["audio_mode"]))
        for key, (var, _mn, _mx, is_int) in self._settings_num_vars.items():
            val = self.cfg.get(key, DEFAULT_CONFIG[key])
            sval = f"{int(round(float(val)))}" if is_int else f"{float(val):.2f}".rstrip("0").rstrip(".")
            var.set(sval)

        self.discord_ctrl.set_cooldown_ms(float(self.cfg["discord_cooldown_ms"]))
        with self.test_lock:
            self.test_session.duration_s = float(self.cfg["test_duration_s"])
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass
        self._apply_watch_toggle_hotkey()
        self.level_detection_status_lbl.configure(text="Level label reading is off.", fg=COLORS["subtext"])
        self.active_level_profile_lbl.configure(
            text="No confirmed level profile active",
            fg=COLORS["subtext"],
        )
        self.trigger_heading_lbl.configure(text="Audio Action at Threshold")
        self._force_restore_all()
        self._refresh_windows()
        self._update_sidebar_badges()
        self._update_status_indicators()

    def _refresh_target_windows(self) -> None:
        procs = ["geometrydash.exe"]
        seen = {"geometrydash.exe"}
        for proc_name, _label in list_open_window_processes():
            if proc_name not in seen:
                seen.add(proc_name)
                procs.append(proc_name)
        self.target_combo["values"] = procs

    def _on_target_proc_change(self) -> None:
        val = self.target_proc_var.get().strip().lower() or "geometrydash.exe"
        self.target_proc_var.set(val)
        with self.cfg_lock:
            self.cfg["target_process"] = val
        self._save_cfg()
        self._update_sidebar_badges()

    def _cancel_delay_pick(self) -> None:
        if self._region_delay_id is not None:
            try:
                self.root.after_cancel(self._region_delay_id)
            except Exception:
                pass
            self._region_delay_id = None
        btn = getattr(self, "delay_pick_btn", None)
        if btn is not None:
            btn.configure(text="Pick with 3s Delay", bg=COLORS["card_alt"])
            btn._base_bg = COLORS["card_alt"]  # type: ignore[attr-defined]

    def _start_region_pick_delayed(self) -> None:
        if self._region_delay_id is not None:
            self._cancel_delay_pick()
            return

        def step(remaining: int) -> None:
            if remaining <= 0:
                self._cancel_delay_pick()
                self._start_region_pick()
                return
            self.delay_pick_btn.configure(text=f"Switch to GD ({remaining}s)...", bg=COLORS["amber"], fg="#111827")
            self.delay_pick_btn._base_bg = COLORS["amber"]  # type: ignore[attr-defined]
            self._region_delay_id = self.root.after(1000, lambda r=remaining - 1: step(r))

        step(3)

    def _start_region_pick(self) -> None:
        self._cancel_delay_pick()
        self.root.withdraw()
        self.root.after(220, self._open_region_overlay)

    def _open_region_overlay(self) -> None:
        try:
            RegionSelectorOverlay(
                self.root,
                on_selected=self._on_region_selected,
                on_cancel=self._on_region_cancelled,
            )
        except Exception as exc:
            self.root.deiconify()
            self.root.lift()
            messagebox.showerror("Capture Failed", f"Could not grab screen for region selection: {exc}")

    def _apply_region_preset(self, label: str, rx: float, ry: float, rw: float, rh: float) -> None:
        target_proc = self.target_proc_var.get().strip().lower() or "geometrydash.exe"
        region: dict[str, Any] = {
            "kind": "window_rel",
            "process": target_proc,
            "preset": label,
            "rx": rx,
            "ry": ry,
            "rw": rw,
            "rh": rh,
        }
        with self.cfg_lock:
            self.cfg["watch_mode"] = "custom_region"
            self.cfg["custom_region"] = region
        self._save_cfg()
        self.region_mode_label.configure(text=self._describe_region_mode())
        self._update_sidebar_badges()

    def _on_region_selected(self, left: int, top: int, width: int, height: int) -> None:
        self.root.deiconify()
        self.root.lift()

        target_proc = self.target_proc_var.get().strip().lower() or "geometrydash.exe"
        win_rect = find_window_client_rect(target_proc)
        region: dict[str, Any]
        if win_rect is not None:
            wx, wy, ww, wh = win_rect
            cx = left + width / 2.0
            cy = top + height / 2.0
            if wx <= cx <= (wx + ww) and wy <= cy <= (wy + wh):
                region = {
                    "kind": "window_rel",
                    "process": target_proc,
                    "rx": max(0.0, (left - wx) / ww),
                    "ry": max(0.0, (top - wy) / wh),
                    "rw": min(1.0, width / ww),
                    "rh": min(1.0, height / wh),
                }
            else:
                region = {"kind": "screen", "left": left, "top": top, "width": width, "height": height}
        else:
            region = {"kind": "screen", "left": left, "top": top, "width": width, "height": height}

        with self.cfg_lock:
            self.cfg["watch_mode"] = "custom_region"
            self.cfg["custom_region"] = region
        self._save_cfg()
        self.region_mode_label.configure(text=self._describe_region_mode())
        self._update_sidebar_badges()

    def _on_region_cancelled(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def _reset_auto_region(self) -> None:
        with self.cfg_lock:
            self.cfg["watch_mode"] = "auto_window"
            self.cfg["custom_region"] = None
        self._save_cfg()
        self.region_mode_label.configure(text=self._describe_region_mode())
        self._update_sidebar_badges()

    def _on_toggle_unmute_100(self) -> None:
        with self.cfg_lock:
            self.cfg["unmute_at_100"] = bool(self.unmute_100_var.get())
        self._save_cfg()

    def _on_screen_reading(self, reading: ScreenReading) -> None:
        if self._closing:
            return
        if reading.level_name_candidate:
            try:
                self.root.after(
                    0,
                    lambda name=reading.level_name_candidate: self._on_level_detected(name),
                )
            except Exception:
                pass
        if reading.level_detection_error:
            try:
                self.root.after(
                    0,
                    lambda error=reading.level_detection_error: self.level_detection_status_lbl.configure(
                        text=f"Beta scan error: {error[:100]}",
                        fg=COLORS["red"],
                    ),
                )
            except Exception:
                pass
        just_finished = False
        with self.test_lock:
            testing = self.test_session.active
            if testing:
                just_finished = self.test_session.record(reading.percent)

        with self.cfg_lock:
            watch_enabled = bool(self.cfg.get("watch_enabled", True))
            remute_interval_s = max(0.1, min(5.0, float(self.cfg.get("remute_interval_s", 0.4))))
            sound_feedback = bool(self.cfg.get("sound_feedback", False))
        effective_mute = (
            self._tray_audio_override
            if self._tray_audio_override is not None
            else bool(not testing and watch_enabled and reading.should_mute)
        )
        with self.cfg_lock:
            fade_mode = str(self.cfg.get("fade_in_mode", "off"))
            threshold = float(self.cfg.get("mute_at_percent", 50.0))
            audio_mode = str(self.cfg.get("audio_mode", "mute"))
            ducking_volume = float(self.cfg.get("ducking_volume", 20.0))
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
            if effective_mute:
                self._last_audio_enforce_at = now_m
            elif fade_due:
                self._last_audio_enforce_at = now_m
            self._apply_mute_state(effective_mute, fade_volume=fade_volume)
            if mute_changed and sound_feedback:
                try:
                    ctypes.windll.user32.MessageBeep(0x00000040 if effective_mute else 0x0)
                except Exception:
                    pass
        elif effective_mute and (now_m - self._last_audio_enforce_at) >= remute_interval_s:
            # Periodically re-mute so newly opened Chrome/browser audio streams stay muted.
            self._last_audio_enforce_at = now_m
            self._apply_mute_state(True, force_reapply=True)

        with self.reading_lock:
            self._latest_reading = reading
            if not self._ui_reading_pending and not self._closing:
                self._ui_reading_pending = True
                try:
                    self.root.after(0, lambda jf=just_finished: self._render_screen_reading(jf))
                except Exception:
                    self._ui_reading_pending = False

    def _render_screen_reading(self, test_just_finished: bool = False) -> None:
        with self.reading_lock:
            self._ui_reading_pending = False
            reading = self._latest_reading
        if reading is None:
            return

        if test_just_finished:
            self._finish_screen_test_ui()

        if reading.percent is not None:
            self.sidebar_pct_lbl.configure(text=f"{reading.percent:.1f}%")
            self._draw_sidebar_pct_bar(reading.percent, reading.should_mute)
            ms_part = f"  •  {reading.scan_ms:.0f}ms" if reading.scan_ms > 0 else ""
            self.detected_pct_label.configure(
                text=f"Detected: {reading.percent:.2f}%{ms_part}   (OCR: {reading.raw_text!r})",
                fg=COLORS["red"] if reading.should_mute else COLORS["green"],
            )
        else:
            self.sidebar_pct_lbl.configure(text="--%")
            self._draw_sidebar_pct_bar(None, False)
            self.detected_pct_label.configure(text="Detected: --%", fg=COLORS["text"])

        self.watch_status_label.configure(text=reading.status_text)
        diagnostic, diagnostic_color = format_ocr_diagnostics(reading)
        self.ocr_diagnostics_label.configure(text=diagnostic, fg=diagnostic_color)

        if reading.preview_image is not None:
            thumb = reading.preview_image.copy()
            thumb.thumbnail((360, 88), Image.Resampling.LANCZOS)
            self._preview_photo_main = ImageTk.PhotoImage(thumb)
            self.preview_label.configure(
                image=self._preview_photo_main,
                text="",
                width=thumb.width,
                height=thumb.height,
            )

    def _draw_sidebar_pct_bar(self, percent: float | None, muted: bool) -> None:
        bar = getattr(self, "sidebar_pct_bar", None)
        if bar is None:
            return
        w = max(20, bar.winfo_width())
        h = max(6, bar.winfo_height())
        bar.delete("all")
        if percent is not None:
            fill_w = int(w * max(0.0, min(100.0, percent)) / 100.0)
            if fill_w > 0:
                bar.create_rectangle(
                    0, 0, fill_w, h,
                    fill=COLORS["red"] if muted else COLORS["cyan"],
                    outline="",
                )
        with self.cfg_lock:
            thresh = float(self.cfg.get("mute_at_percent", 50.0))
        tx = int(w * max(0.0, min(100.0, thresh)) / 100.0)
        bar.create_line(tx, 0, tx, h, fill=COLORS["amber"], width=2)

    def _update_status_indicators(self) -> None:
        with self.test_lock:
            testing = self.test_session.active

        deafened = self.discord_ctrl.deafened_by_us
        apps_muted = self.audio_ctrl.is_muted
        apps_ducked = self.audio_ctrl.is_ducking
        with self.cfg_lock:
            auto_deaf = bool(self.cfg.get("auto_deafen", False))
            hk = self.cfg.get("deafen_hotkey")

        if testing:
            color, txt = COLORS["cyan"], "TESTING (30s)"
        elif not self.cfg.get("watch_enabled", True):
            color, txt = COLORS["subtext"], "PAUSED"
        elif apps_muted or deafened:
            color, txt = COLORS["red"], "MUTED"
        elif apps_ducked:
            color, txt = COLORS["amber"], "DUCKING"
        else:
            color, txt = COLORS["green"], "WATCHING"

        self.status_dot.itemconfigure(self._dot_id, fill=color)
        self.sidebar_status_lbl.configure(text=txt, fg=color)

        deaf_lbl = getattr(self, "sidebar_deaf_lbl", None)
        if deaf_lbl is not None:
            if deafened and apps_muted:
                deaf_lbl.configure(text="Apps & Discord: Muted", fg=COLORS["red"])
            elif deafened:
                app_state = " • Apps Ducked" if apps_ducked else ""
                deaf_lbl.configure(text=f"Discord: Deafened{app_state}", fg=COLORS["red"])
            elif apps_muted:
                deaf_lbl.configure(text="Apps: Muted", fg=COLORS["red"])
            elif apps_ducked:
                deaf_lbl.configure(text="Apps: Ducked", fg=COLORS["amber"])
            else:
                deaf_lbl.configure(text="Audio & Discord: Clear", fg=COLORS["subtext"])

        dc_badge = getattr(self, "discord_state_badge", None)
        if dc_badge is not None:
            if deafened:
                dc_badge.configure(text="Deafened by GD Quiet", bg=COLORS["red_bg"], fg=COLORS["red"])
            elif auto_deaf and isinstance(hk, dict):
                dc_badge.configure(text="Auto-Undeafen Ready", bg=COLORS["green_bg"], fg=COLORS["green"])
            elif auto_deaf:
                dc_badge.configure(text="Set Hotkey", bg=COLORS["amber_bg"], fg=COLORS["amber"])
            else:
                dc_badge.configure(text="Disabled", bg=COLORS["pill_idle"], fg=COLORS["subtext"])

    def _update_sidebar_badges(self) -> None:
        with self.cfg_lock:
            n_apps = len(self.cfg.get("selected_apps") or [])
            auto_deaf = bool(self.cfg.get("auto_deafen", False))
            hk = self.cfg.get("deafen_hotkey")
            proc = str(self.cfg.get("target_process") or "geometrydash.exe")
            pct = float(self.cfg.get("mute_at_percent", 50.0))
            audio_mode = str(self.cfg.get("audio_mode", "mute"))

        action = "Ducks at" if audio_mode == "duck" else "Mutes past"
        self.sidebar_thresh_lbl.configure(text=f"{action} {pct:.0f}%")
        if hasattr(self, "apps_summary_thresh_lbl"):
            self.apps_summary_thresh_lbl.configure(text=f"{pct:.0f}%")
        if hasattr(self, "apps_summary_sub_lbl"):
            verb = "duck" if audio_mode == "duck" else "mute"
            self.apps_summary_sub_lbl.configure(
                text=f"These apps will {verb} after {pct:.0f}%. Their audio returns when the run ends."
            )
        self.reset_mute_btn.configure(
            text="Restore Audio & Undeafen" if audio_mode == "duck" else "Unmute & Undeafen All"
        )
        if "apps" in self._nav_buttons:
            app_action = "Duck" if audio_mode == "duck" else "Mute"
            self._nav_buttons["apps"][2].configure(text=f"Apps to {app_action} ({n_apps})")
        app_action = "Duck" if audio_mode == "duck" else "Mute"
        app_past = "ducked" if audio_mode == "duck" else "muted"
        self.apps_page_heading_lbl.configure(text=f"Apps to {app_action}")
        self.apps_page_desc_lbl.configure(
            text=(
                f"Select only the windows/apps you want {app_past} when passing your %. "
                "Geometry Dash is never affected."
            )
        )

        if hasattr(self, "apps_summary_count_lbl"):
            noun = "app" if n_apps == 1 else "apps"
            self.apps_summary_count_lbl.configure(text=f"{n_apps} {noun} selected")
        if hasattr(self, "apps_summary_sub_lbl"):
            verb = "duck" if audio_mode == "duck" else "mute"
            self.apps_summary_sub_lbl.configure(
                text=f"These apps will {verb} after {pct:.0f}%. Their audio returns when the run ends."
            )
        if hasattr(self, "apps_summary_thresh_lbl"):
            self.apps_summary_thresh_lbl.configure(text=f"{pct:.0f}%")

        self.summary_apps_btn.configure(text=f"Apps: {n_apps} selected")
        if auto_deaf and isinstance(hk, dict):
            self.summary_discord_btn.configure(text=f"Discord Deafen: {hk.get('label', 'On')}")
        elif auto_deaf:
            self.summary_discord_btn.configure(text="Discord Deafen: Set hotkey")
        else:
            self.summary_discord_btn.configure(text="Discord Deafen: Off")

        reg = self.cfg.get("custom_region")
        if self.cfg.get("watch_mode") == "custom_region":
            reg_mode = str(reg.get("preset")) if isinstance(reg, dict) and reg.get("preset") else "Custom"
        else:
            reg_mode = "Auto"
        self.summary_capture_btn.configure(text=f"Capture: {reg_mode} ({proc})")

        badge = getattr(self, "tutorial_step3_badge", None)
        if badge is not None:
            if auto_deaf and isinstance(hk, dict):
                badge.configure(
                    text=f"Ready ({hk.get('label', 'Set')})",
                    bg=COLORS["green_bg"],
                    fg=COLORS["green"],
                )
            elif isinstance(hk, dict):
                badge.configure(
                    text="Enable checkbox above",
                    bg=COLORS["amber_bg"],
                    fg=COLORS["amber"],
                )
            else:
                badge.configure(
                    text="Needs hotkey",
                    bg=COLORS["amber_bg"],
                    fg=COLORS["amber"],
                )

    def _on_scale_move(self, _val: str) -> None:
        pct = float(self.percent_var.get())
        self.percent_entry_var.set(f"{pct:.0f}")
        with self.cfg_lock:
            self.cfg["mute_at_percent"] = pct
            audio_mode = str(self.cfg.get("audio_mode", "mute"))
        action = "Ducks at" if audio_mode == "duck" else "Mutes past"
        self.sidebar_thresh_lbl.configure(text=f"{action} {pct:.0f}%")
        if hasattr(self, "apps_summary_thresh_lbl"):
            self.apps_summary_thresh_lbl.configure(text=f"{pct:.0f}%")
        if hasattr(self, "apps_summary_sub_lbl"):
            verb = "duck" if audio_mode == "duck" else "mute"
            self.apps_summary_sub_lbl.configure(
                text=f"These apps will {verb} after {pct:.0f}%. Their audio returns when the run ends."
            )

    def _save_mute_percent(self) -> None:
        try:
            raw = float(self.percent_entry_var.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid Percent", "Enter a number between 0 and 100.")
            return
        pct = max(0.0, min(100.0, raw))
        self.percent_var.set(pct)
        self.percent_entry_var.set(f"{pct:.0f}")
        with self.cfg_lock:
            self.cfg["mute_at_percent"] = pct
        self._save_cfg()
        if hasattr(self, "_settings_num_vars") and "mute_at_percent" in self._settings_num_vars:
            self._settings_num_vars["mute_at_percent"][0].set(f"{pct:.0f}")
        self._update_sidebar_badges()

    def _get_obs_overlay_status(self) -> dict[str, Any]:
        with self.cfg_lock:
            watching = bool(self.cfg.get("watch_enabled", True))
            threshold = float(self.cfg.get("mute_at_percent", 50.0))
        with self.reading_lock:
            reading = self._latest_reading
        with self.test_lock:
            testing = bool(self.test_session.active)

        return {
            "watching": watching,
            "muted": bool(self.audio_ctrl.muted_session_count or self.discord_ctrl.deafened_by_us),
            "percent": reading.percent if reading is not None else None,
            "threshold": threshold,
            "testing": testing,
        }

    def _queue_obs_overlay_control(self, payload: dict[str, Any]) -> bool:
        if self._closing:
            return False
        try:
            self.root.after(0, lambda values=dict(payload): self._apply_obs_overlay_control(values))
        except Exception:
            return False
        return True

    def _apply_obs_overlay_control(self, payload: dict[str, Any]) -> None:
        if self._closing:
            return
        action = payload.get("action")
        if action == "watch":
            with self.cfg_lock:
                currently_watching = bool(self.cfg.get("watch_enabled", True))
            if currently_watching != payload.get("enabled"):
                self._toggle_watching()
        elif action == "threshold":
            try:
                value = float(payload["value"])
            except (KeyError, TypeError, ValueError):
                return
            if not 0 <= value <= 100:
                return
            self.percent_var.set(value)
            self.percent_entry_var.set(f"{value:.0f}")
            self._save_mute_percent()

    def _obs_overlay_status_text(self) -> str:
        if self.obs_overlay_server.running:
            return f"Overlay server running at {self.obs_overlay_server.url}"
        if self.cfg.get("obs_overlay_enabled", False):
            return "Overlay is enabled in settings but the local server is not running."
        return "Overlay server is stopped. It only listens on this computer."

    def _start_obs_overlay(self, show_error: bool = True, persist: bool = True) -> bool:
        try:
            url = self.obs_overlay_server.start()
        except OSError as exc:
            self.obs_overlay_status_lbl.configure(
                text=f"Could not start OBS overlay: {exc}",
                fg=COLORS["red"],
            )
            self.obs_overlay_toggle_btn.configure(text="Enable OBS Overlay")
            if show_error:
                messagebox.showerror("OBS Overlay", f"Could not start the local overlay server:\n{exc}")
            return False

        self.obs_overlay_url_var.set(url)
        self.obs_overlay_toggle_btn.configure(text="Stop OBS Overlay")
        self.obs_overlay_status_lbl.configure(
            text=f"Running locally at {url}. Add that URL as an OBS Browser Source.",
            fg=COLORS["green"],
        )
        if persist:
            with self.cfg_lock:
                self.cfg["obs_overlay_enabled"] = True
            self._save_cfg()
        return True

    def _stop_obs_overlay(self, persist: bool = True) -> None:
        self.obs_overlay_server.stop()
        self.obs_overlay_toggle_btn.configure(text="Enable OBS Overlay")
        self.obs_overlay_status_lbl.configure(
            text="Overlay server stopped. OBS will show it again when you enable it.",
            fg=COLORS["subtext"],
        )
        if persist:
            with self.cfg_lock:
                self.cfg["obs_overlay_enabled"] = False
            self._save_cfg()

    def _toggle_obs_overlay(self) -> None:
        if self.obs_overlay_server.running:
            self._stop_obs_overlay()
        else:
            self._start_obs_overlay()

    def _copy_obs_overlay_url(self) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.obs_overlay_server.url)
            self.obs_overlay_status_lbl.configure(
                text="OBS Browser Source URL copied to clipboard.",
                fg=COLORS["green"],
            )
        except tk.TclError as exc:
            self.obs_overlay_status_lbl.configure(text=f"Could not copy URL: {exc}", fg=COLORS["red"])

    def _open_obs_overlay_preview(self) -> None:
        if not self.obs_overlay_server.running and not self._start_obs_overlay():
            return
        try:
            webbrowser.open(self.obs_overlay_server.url)
        except Exception as exc:
            messagebox.showerror("OBS Overlay", f"Could not open the preview:\n{exc}")

    def _apply_worker(self) -> None:
        last_applied: bool | None = None
        last_fade_volume: float | None = None
        while True:
            with self._apply_cond:
                while not self._apply_stop and (
                    self._pending_mute is None
                    or (
                        self._pending_mute == last_applied
                        and self._pending_fade_volume == last_fade_volume
                        and not self._force_reapply
                    )
                ):
                    self._apply_cond.wait(timeout=0.5)
                    if (
                        not self._apply_stop
                        and self._pending_mute is None
                        and last_applied is False
                        and last_fade_volume is None
                        and self.audio_ctrl.has_active_effect
                    ):
                        self._pending_mute = False
                        self._pending_fade_volume = None
                        self._force_reapply = True
                if self._apply_stop:
                    return
                target_mute = bool(self._pending_mute)
                target_fade_volume = self._pending_fade_volume
                self._pending_mute = None
                self._pending_fade_volume = None
                self._force_reapply = False

            with self.apply_lock:
                with self.cfg_lock:
                    selected = list(self.cfg.get("selected_apps") or [])
                    hotkey = self.cfg.get("deafen_hotkey")
                    auto_deafen = bool(self.cfg.get("auto_deafen", False))
                    audio_mode = str(self.cfg.get("audio_mode", "mute"))
                    ducking_volume = float(self.cfg.get("ducking_volume", 20.0))
                self.audio_ctrl.apply(
                    selected,
                    target_mute or target_fade_volume is not None,
                    duck_to=(
                        target_fade_volume
                        if target_fade_volume is not None
                        else ducking_volume if audio_mode == "duck" else None
                    ),
                )
                self.discord_ctrl.set_deafened(
                    want_deaf=target_mute,
                    hotkey=hotkey,
                    enabled=auto_deafen,
                )
                last_applied = target_mute
                last_fade_volume = target_fade_volume
            try:
                self.root.after(0, self._update_status_indicators)
            except Exception:
                pass

    def _apply_mute_state(
        self,
        want_mute: bool,
        force_reapply: bool = False,
        fade_volume: float | None = None,
    ) -> None:
        with self._apply_cond:
            self.current_want_mute = want_mute
            self._current_fade_volume = fade_volume
            self._pending_mute = want_mute
            self._pending_fade_volume = fade_volume
            if force_reapply:
                self._force_reapply = True
            self._apply_cond.notify_all()

    def _force_restore_all(self) -> None:
        self._tray_audio_override = None
        watcher = getattr(self, "watcher", None)
        if watcher is not None and hasattr(watcher, "_debouncer"):
            watcher._debouncer.reset()
        self._apply_mute_state(False, force_reapply=True)
        self._update_status_indicators()

    # ---- Apps Page ----

    def _on_toggle_bg_audio(self) -> None:
        with self.cfg_lock:
            self.cfg["show_background_audio"] = bool(self.bg_audio_var.get())
        self._save_cfg()
        self._refresh_windows()

    def _clear_selected_apps(self) -> None:
        with self.cfg_lock:
            self.cfg["selected_apps"] = []
        self._save_cfg()
        for var in self.window_vars.values():
            var.set(False)
        self._update_sidebar_badges()
        if self.current_want_mute:
            self._apply_mute_state(True, force_reapply=True)

    def _on_app_checkbox_toggle(self, proc_name: str, var: tk.BooleanVar) -> None:
        with self.cfg_lock:
            selected = {p.lower() for p in (self.cfg.get("selected_apps") or []) if p}
            if var.get():
                selected.add(proc_name.lower())
            else:
                selected.discard(proc_name.lower())
            self.cfg["selected_apps"] = sorted(selected)
        self._save_cfg()
        self._update_sidebar_badges()

        if self.current_want_mute:
            self._apply_mute_state(True, force_reapply=True)

    def _refresh_windows(self) -> None:
        include_bg = bool(self.bg_audio_var.get())
        self._all_targets = list_mute_targets(include_background_audio=include_bg)
        self._render_apps_list()

    def _render_apps_list(self) -> None:
        for child in self.win_list_frame.winfo_children():
            child.destroy()
        self.window_vars.clear()

        query = self.app_search_var.get().strip().lower() if hasattr(self, "app_search_var") else ""
        selected = {p.lower() for p in (self.cfg.get("selected_apps") or [])}

        targets = [
            t
            for t in self._all_targets
            if not query
            or query in t.display_name.lower()
            or query in t.process_name.lower()
            or any(query in wt.lower() for wt in t.window_titles)
        ]

        if not targets:
            msg = (
                "No open windows match your search."
                if query
                else "No open windows detected. Open YouTube, Discord, or Spotify and click Refresh Windows."
            )
            tk.Label(
                self.win_list_frame,
                text=msg,
                bg=COLORS["input"],
                fg=COLORS["subtext"],
                font=("Segoe UI", 9),
                padx=14,
                pady=16,
            ).pack(anchor="w")
            return

        for idx, target in enumerate(targets):
            row_bg = COLORS["card_alt"] if idx % 2 == 0 else COLORS["card2"]
            row = tk.Frame(self.win_list_frame, bg=row_bg, padx=14, pady=9, cursor="hand2")
            row.pack(fill="x")
            tk.Frame(self.win_list_frame, bg=COLORS["border"], height=1).pack(fill="x")

            var = tk.BooleanVar(value=(target.process_name in selected))
            self.window_vars[target.process_name] = var

            chk = tk.Checkbutton(
                row,
                variable=var,
                command=lambda p=target.process_name, v=var: self._on_app_checkbox_toggle(p, v),
                bg=row_bg,
                fg=COLORS["text"],
                selectcolor=COLORS["input"],
                activebackground=row_bg,
            )
            chk.pack(side="left", padx=(0, 8))

            text_col = tk.Frame(row, bg=row_bg)
            text_col.pack(side="left", fill="x", expand=True)

            top_line = tk.Frame(text_col, bg=row_bg)
            top_line.pack(fill="x")
            name_lbl = tk.Label(
                top_line,
                text=target.display_name,
                bg=row_bg,
                fg=COLORS["text"],
                font=("Segoe UI", 10, "bold"),
            )
            name_lbl.pack(side="left")
            proc_lbl = tk.Label(
                top_line,
                text=f"  {target.process_name}",
                bg=row_bg,
                fg=COLORS["faint"],
                font=("Consolas", 8),
            )
            proc_lbl.pack(side="left")

            if target.window_titles:
                preview = " • ".join(target.window_titles[:2])
                if len(preview) > 74:
                    preview = preview[:71] + "..."
            else:
                preview = "Background audio session"
            sub_lbl = tk.Label(
                text_col,
                text=preview,
                bg=row_bg,
                fg="#5f5f6a",
                font=("Segoe UI", 8),
                anchor="w",
            )
            sub_lbl.pack(fill="x")

            is_selected = target.process_name in selected
            action_txt = ("Will duck" if self.cfg.get("audio_mode") == "duck" else "Will mute") if is_selected else "Select"
            action_bg = COLORS["accent_soft"] if is_selected else COLORS["pill_idle"]
            action_fg = COLORS["accent_text"] if is_selected else COLORS["subtext"]
            action_lbl = tk.Label(
                row,
                text=action_txt,
                bg=action_bg,
                fg=action_fg,
                font=("Segoe UI", 8, "bold"),
                padx=9,
                pady=3,
                width=10,
            )
            action_lbl.pack(side="right", padx=(10, 0))

            if target.currently_muted:
                badge_txt, badge_bg, badge_fg = "Muted", COLORS["red_bg"], COLORS["red"]
            elif target.has_audio:
                badge_txt, badge_bg, badge_fg = "Audio active", COLORS["green_bg"], COLORS["green_text"]
            else:
                badge_txt, badge_bg, badge_fg = "Window only", COLORS["pill_idle"], COLORS["subtext"]
            badge_lbl = tk.Label(
                row,
                text=badge_txt,
                bg=badge_bg,
                fg=badge_fg,
                font=("Segoe UI", 8, "bold"),
                padx=9,
                pady=3,
                width=13,
            )
            badge_lbl.pack(side="right", padx=(8, 0))

            def toggle_row(_e: Any, p: str = target.process_name, v: tk.BooleanVar = var) -> None:
                v.set(not v.get())
                self._on_app_checkbox_toggle(p, v)

            for widget in (row, text_col, top_line, name_lbl, proc_lbl, sub_lbl, badge_lbl, action_lbl):
                widget.bind("<Button-1>", toggle_row)

    # ---- Discord Auto-Deafen Page ----

    def _on_toggle_auto_deafen(self) -> None:
        enabled = bool(self.auto_deafen_var.get())
        with self.cfg_lock:
            self.cfg["auto_deafen"] = enabled
        self._save_cfg()
        self._update_sidebar_badges()
        self._update_status_indicators()
        self._apply_mute_state(self.current_want_mute, force_reapply=True)

    def _begin_hotkey_capture(
        self,
        button: tk.Button,
        base_bg: str,
        restore_text: str,
        on_captured: Callable[[dict[str, Any]], None],
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        if self.recording_hotkey:
            self._finish_hotkey_capture(cancelled=True)
        self.recording_hotkey = True
        self._hk_capture_state = (button, base_bg, restore_text, on_cancel)
        button.configure(text="Press keys (Esc cancels)...", bg=COLORS["amber"], fg="#111827")
        button._base_bg = COLORS["amber"]  # type: ignore[attr-defined]
        self.root.focus_force()

        def on_key(event: tk.Event) -> str:
            if event.keysym == "Escape":
                self._finish_hotkey_capture(cancelled=True)
                return "break"
            parsed = parse_tk_hotkey(event)
            if parsed is None:
                return "break"
            self._finish_hotkey_capture(cancelled=False)
            on_captured(parsed)
            return "break"

        self._hk_bind_id = self.root.bind("<KeyPress>", on_key)

    def _finish_hotkey_capture(self, cancelled: bool = False) -> None:
        self.recording_hotkey = False
        if hasattr(self, "_hk_bind_id"):
            try:
                self.root.unbind("<KeyPress>", self._hk_bind_id)
            except Exception:
                pass
        state = self._hk_capture_state
        self._hk_capture_state = None
        if state is not None:
            button, base_bg, restore_text, on_cancel = state
            button.configure(text=restore_text, bg=base_bg, fg=COLORS["text"])
            button._base_bg = base_bg  # type: ignore[attr-defined]
            if cancelled and on_cancel is not None:
                on_cancel()

    def _start_hotkey_record(self) -> None:
        def on_captured(parsed: dict[str, Any]) -> None:
            with self.cfg_lock:
                self.cfg["deafen_hotkey"] = parsed
            self._save_cfg()
            self.hotkey_var.set(parsed["label"])
            self._update_sidebar_badges()
            self._update_status_indicators()

        self._begin_hotkey_capture(
            button=self.record_hk_btn,
            base_bg=COLORS["accent"],
            restore_text="Record Hotkey",
            on_captured=on_captured,
        )

    def _test_hotkey(self) -> None:
        hk = self.cfg.get("deafen_hotkey")
        if not hk:
            messagebox.showinfo("No Hotkey Set", "Click 'Record Hotkey' and press your Discord Toggle Deafen keybind first.")
            return
        threading.Thread(target=lambda: send_hotkey(hk), daemon=True).start()

    def _clear_hotkey(self) -> None:
        with self.cfg_lock:
            prev_hk = self.cfg.get("deafen_hotkey")
            self.cfg["deafen_hotkey"] = None
        self._save_cfg()
        if self.discord_ctrl.deafened_by_us and prev_hk:
            with self.apply_lock:
                self.discord_ctrl.restore(prev_hk)
        self.hotkey_var.set("Not set")
        self._update_sidebar_badges()
        self._update_status_indicators()

    def _show_update_banner(self, info: UpdateInfo) -> None:
        self.update_banner_lbl.configure(
            text=f"GD Quiet v{info.latest_version} is available (current: v{info.current_version}) — update automatically?"
        )
        if not self.update_banner.winfo_manager():
            self.update_banner.pack(fill="x", pady=(0, 12))
            try:
                self.root.bell()
            except Exception:
                pass

    def _dismiss_update_banner(self) -> None:
        if self._pending_update is not None:
            self._dismissed_update_version = self._pending_update.latest_version
        self.update_banner.pack_forget()

    def _on_update_banner_install(self) -> None:
        if self._pending_update is not None and self._pending_update.update_available:
            self._prompt_and_install_update(self._pending_update)
        else:
            self._check_updates_async(silent=False)

    def _schedule_periodic_update_check(self) -> None:
        if self._closing:
            return
        if not self._updating and self.cfg.get("auto_check_updates", True):
            self._check_updates_async(silent=True)
        if not self._closing:
            self._periodic_update_id = self.root.after(300000, self._schedule_periodic_update_check)

    def _on_update_button_click(self) -> None:
        if self._updating:
            return
        if self._pending_update is not None and self._pending_update.update_available:
            self._prompt_and_install_update(self._pending_update)
        else:
            self._check_updates_async(silent=False)

    def _check_updates_async(self, silent: bool = True) -> None:
        if self._updating or self._closing:
            return
        if not silent:
            self.update_btn.configure(state="disabled", text="Checking...")
            self.footer_error.configure(text="")

        def worker() -> None:
            try:
                with self.cfg_lock:
                    include_prereleases = bool(self.cfg.get("beta_updates", False))
                info = check_for_update(
                    current_version=APP_VERSION,
                    include_prereleases=include_prereleases,
                )
                if not self._closing:
                    self.root.after(0, lambda i=info, s=silent: self._on_update_checked(i, s))
            except Exception as exc:
                if not self._closing:
                    self.root.after(0, lambda err=str(exc), s=silent: self._on_update_check_failed(err, s))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_checked(self, info: UpdateInfo, silent: bool) -> None:
        if self._closing:
            return
        if not self._updating:
            self.update_btn.configure(state="normal")
        if info.update_available:
            self._pending_update = info
            self.update_btn.configure(
                text=f"Install v{info.latest_version} Update",
                bg=COLORS["cyan_dark"],
                fg=COLORS["text"],
            )
            self.update_btn._base_bg = COLORS["cyan_dark"]  # type: ignore[attr-defined]
            self.update_btn._hover_bg = COLORS["cyan"]  # type: ignore[attr-defined]
            self.footer_error.configure(text=f"Update v{info.latest_version} ready!", fg=COLORS["cyan"])
            if not silent or info.latest_version != self._dismissed_update_version:
                self._show_update_banner(info)
            if not silent:
                self._prompt_and_install_update(info)
        else:
            self._pending_update = None
            self.update_banner.pack_forget()
            self.update_btn.configure(text="Check for Updates", bg=COLORS["card_alt"], fg=COLORS["text"])
            self.update_btn._base_bg = COLORS["card_alt"]  # type: ignore[attr-defined]
            self.update_btn._hover_bg = COLORS["hover"]  # type: ignore[attr-defined]
            if not silent:
                self.footer_error.configure(text=f"Up to date (v{APP_VERSION})", fg=COLORS["green"])
                messagebox.showinfo("Up to Date", f"You are already running the latest version (v{APP_VERSION}).")

    def _on_update_check_failed(self, err: str, silent: bool) -> None:
        if self._closing:
            return
        if not self._updating:
            self.update_btn.configure(state="normal", text="Check for Updates")
        if not silent:
            self.footer_error.configure(text=f"Update check failed: {err}", fg=COLORS["amber"])

    def _prompt_and_install_update(self, info: UpdateInfo) -> None:
        if not is_packaged_executable():
            if messagebox.askyesno(
                "Update Available",
                f"GD Quiet v{info.latest_version} is available (running from Python source v{info.current_version}).\n\nOpen the GitHub release page?",
            ):
                webbrowser.open(info.release_url or RELEASES_PAGE_URL)
            return

        is_inst = bool(getattr(info, "is_installer", False))
        prompt_action = (
            "Download and open the Setup Wizard now?"
            if is_inst
            else "Download and install now? GD Quiet will restart automatically."
        )
        if not messagebox.askyesno(
            "Install Update",
            f"GD Quiet v{info.latest_version} is available (you have v{info.current_version}).\n\n{prompt_action}",
        ):
            return

        self._updating = True
        self.update_btn.configure(state="disabled", text="Downloading 0%...")
        self.update_banner_btn.configure(state="disabled", text="Updating...")
        self.footer_error.configure(text=f"Downloading v{info.latest_version}...", fg=COLORS["cyan"])

        def worker() -> None:
            try:
                target_exe = current_executable_path()

                def on_prog(done: int, total: int) -> None:
                    pct_txt = f"{int(done * 100 / total)}%" if total > 0 else f"{done // 1024} KB"
                    if self._closing:
                        return
                    try:
                        self.root.after(0, lambda t=pct_txt: self.update_btn.configure(text=f"Downloading {t}..."))
                    except Exception:
                        pass

                staged = download_update(info, target_exe, progress_cb=on_prog)
                try:
                    btn_msg = "Launching Setup..." if is_inst else "Restarting..."
                    self.root.after(0, lambda msg=btn_msg: self.update_btn.configure(text=msg))
                except Exception:
                    pass
                launch_update_swap_and_restart(staged, target_exe, is_installer=is_inst)
                self.root.after(0, self._finish_update_and_exit)
            except UpdateError as exc:
                self.root.after(0, lambda err=str(exc): self._on_update_install_failed(err))
            except Exception as exc:
                self.root.after(0, lambda err=str(exc): self._on_update_install_failed(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_install_failed(self, err: str) -> None:
        if self._closing:
            return
        self._updating = False
        self.update_btn.configure(state="normal", text="Retry Update")
        self.update_banner_btn.configure(state="normal", text="Update Now")
        self.footer_error.configure(text=f"Update failed: {err}", fg=COLORS["amber"])
        messagebox.showerror("Update Failed", err)

    def _finish_update_and_exit(self) -> None:
        self._on_close()

    def _cleanup_sync(self) -> None:
        if hasattr(self, "obs_overlay_server") and self.obs_overlay_server:
            self.obs_overlay_server.stop()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            finally:
                self.tray_icon = None
        if hasattr(self, "global_hotkey") and self.global_hotkey:
            self.global_hotkey.stop()
        if hasattr(self, "watcher") and self.watcher:
            self.watcher.stop()
        with self._apply_cond:
            self._apply_stop = True
            self._apply_cond.notify_all()
        if hasattr(self, "_apply_thread") and self._apply_thread.is_alive():
            self._apply_thread.join(timeout=0.5)
        with self.apply_lock:
            self.audio_ctrl.restore()
            self.discord_ctrl.restore(self.cfg.get("deafen_hotkey"))

    def _cancel_after(self, attr_name: str) -> None:
        after_id = getattr(self, attr_name, None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
            setattr(self, attr_name, None)

    def _on_close(self, force_exit: bool = False) -> None:
        if (
            not force_exit
            and self.tray_icon is not None
            and bool(self.cfg.get("close_to_tray", False))
        ):
            self.root.withdraw()
            return
        self._closing = True
        self._cancel_delay_pick()
        for timer_attr in ("_startup_update_id", "_periodic_update_id", "_test_tick_id"):
            self._cancel_after(timer_attr)
        self._cleanup_sync()
        self._save_cfg()
        self.root.destroy()


def main() -> None:
    if "--self-check" in sys.argv:
        ocr = WinRtPercentOcr()
        try:
            assert ocr.available(), "Windows.Media.Ocr engine unavailable"
        finally:
            ocr.close()
        _ = list_mute_targets(include_background_audio=False)
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("gdquiet.desktop.app")
    except Exception:
        pass

    if "--tk" in sys.argv:
        root = tk.Tk()
        GDQuietApp(root)
        root.mainloop()
        return

    from web_ui import run_web_app

    run_web_app()


if __name__ == "__main__":
    main()
