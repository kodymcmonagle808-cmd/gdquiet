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
from screen_reader import (
    ScreenPercentWatcher,
    ScreenReading,
    ScreenTestSession,
    WinRtPercentOcr,
    find_window_client_rect,
    get_virtual_screen_bounds,
    normalize_level_name,
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
    "bg": "#090d16",
    "sidebar": "#0e1422",
    "card": "#141c2e",
    "card_alt": "#1b263d",
    "hover": "#24324f",
    "border": "#253352",
    "input": "#0b101b",
    "text": "#f8fafc",
    "subtext": "#94a3b8",
    "accent": "#6366f1",
    "accent_hover": "#4f46e5",
    "cyan": "#38bdf8",
    "cyan_dark": "#0284c7",
    "green": "#22c55e",
    "green_bg": "#14532d",
    "red": "#ef4444",
    "red_bg": "#7f1d1d",
    "amber": "#f59e0b",
    "amber_bg": "#78350f",
    "pill_idle": "#334155",
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


class GDQuietApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("GD Quiet")
        self.root.geometry("980x680")
        self.root.minsize(880, 610)
        self.root.configure(bg=COLORS["bg"])
        self._window_icon_photo: ImageTk.PhotoImage | None = None
        self._sidebar_logo_photo: ImageTk.PhotoImage | None = None
        self._apply_window_icons()

        self.cfg_lock = threading.RLock()
        self.cfg = load_config()

        self.audio_ctrl = AudioMuteController()
        self.discord_ctrl = DiscordDeafenController(
            toggle_cooldown_s=float(self.cfg.get("discord_cooldown_ms", 180)) / 1000.0
        )
        self.apply_lock = threading.Lock()
        self._apply_cond = threading.Condition()
        self._pending_mute: bool | None = None
        self._force_reapply = False
        self._apply_stop = False
        self._apply_thread = threading.Thread(target=self._apply_worker, daemon=True)
        self._apply_thread.start()
        self.reading_lock = threading.Lock()
        self.test_lock = threading.RLock()
        self.test_session = ScreenTestSession(duration_s=float(self.cfg.get("test_duration_s", 30.0)))

        self.current_want_mute = False
        self.recording_hotkey = False
        self._hk_capture_state: tuple[tk.Button, str, str, Callable[[], None] | None] | None = None
        self.global_hotkey = GlobalHotkeyWatcher(on_trigger=self._on_global_watch_toggle)
        self._pending_update: UpdateInfo | None = None
        self._dismissed_update_version: str = ""
        self._pending_level_raw = ""
        self._pending_level_display = ""
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

    def _make_card(self, parent: tk.Widget, padx: int = 18, pady: int = 15) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=COLORS["card"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=padx,
            pady=pady,
        )

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
        sidebar = tk.Frame(shell, bg=COLORS["sidebar"], width=242, padx=14, pady=16)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=COLORS["sidebar"])
        brand.pack(fill="x", pady=(0, 16))
        logo_cv = tk.Canvas(brand, width=42, height=42, bg=COLORS["sidebar"], highlightthickness=0)
        logo_cv.pack(side="left", padx=(0, 10))
        self._draw_logo(logo_cv)

        brand_txt = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_txt.pack(side="left", fill="x")
        tk.Label(brand_txt, text="GD Quiet", bg=COLORS["sidebar"], fg=COLORS["text"], font=("Segoe UI", 15, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            brand_txt,
            text=f"v{APP_VERSION} • Screen muter",
            bg=COLORS["sidebar"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        ).pack(anchor="w")

        # Live Sidebar Status Card
        status_box = tk.Frame(
            sidebar,
            bg=COLORS["card"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=12,
            pady=12,
        )
        status_box.pack(fill="x", pady=(0, 16))

        st_row = tk.Frame(status_box, bg=COLORS["card"])
        st_row.pack(fill="x")
        self.status_dot = tk.Canvas(st_row, width=10, height=10, bg=COLORS["card"], highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self._dot_id = self.status_dot.create_oval(1, 1, 9, 9, fill=COLORS["green"], outline="")

        self.sidebar_status_lbl = tk.Label(
            st_row,
            text="WATCHING",
            bg=COLORS["card"],
            fg=COLORS["green"],
            font=("Segoe UI", 9, "bold"),
        )
        self.sidebar_status_lbl.pack(side="left")

        self.sidebar_pct_lbl = tk.Label(
            status_box,
            text="--%",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Consolas", 22, "bold"),
        )
        self.sidebar_pct_lbl.pack(anchor="w", pady=(4, 4))

        self.sidebar_pct_bar = tk.Canvas(
            status_box,
            height=6,
            bg=COLORS["input"],
            highlightthickness=0,
        )
        self.sidebar_pct_bar.pack(fill="x", pady=(0, 6))

        self.sidebar_thresh_lbl = tk.Label(
            status_box,
            text=f"Mutes past {float(self.cfg.get('mute_at_percent', 50.0)):.0f}%",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        )
        self.sidebar_thresh_lbl.pack(anchor="w")

        self.sidebar_deaf_lbl = tk.Label(
            status_box,
            text="Audio & Discord: Clear",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        )
        self.sidebar_deaf_lbl.pack(anchor="w", pady=(2, 0))

        # Navigation Tabs
        nav_wrap = tk.Frame(sidebar, bg=COLORS["sidebar"])
        nav_wrap.pack(fill="x", expand=True, anchor="n")

        nav_items = [
            ("dashboard", "Overview & 30s Test"),
            ("apps", "Apps to Mute"),
            ("discord", "Discord Auto-Deafen"),
            ("capture", "Screen Capture"),
            ("settings", "Settings"),
        ]
        for key, label in nav_items:
            row = tk.Frame(nav_wrap, bg=COLORS["sidebar"])
            row.pack(fill="x", pady=3)
            indicator = tk.Frame(row, bg=COLORS["sidebar"], width=4)
            indicator.pack(side="left", fill="y", padx=(0, 4))
            btn = tk.Button(
                row,
                text=label,
                command=lambda k=key: self._show_page(k),
                bg=COLORS["sidebar"],
                fg=COLORS["subtext"],
                activebackground=COLORS["card"],
                activeforeground=COLORS["text"],
                relief="flat",
                bd=0,
                anchor="w",
                cursor="hand2",
                font=("Segoe UI", 10, "bold"),
                padx=10,
                pady=9,
            )
            btn.pack(side="left", fill="x", expand=True)
            btn.bind(
                "<Enter>",
                lambda _e, k=key, r=row, b=btn: (
                    r.configure(bg=COLORS["card_alt"]),
                    b.configure(bg=COLORS["card_alt"], fg=COLORS["text"]),
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

        # Sidebar bottom controls
        self.watch_toggle_btn = self._make_button(
            sidebar,
            "Pause Watching" if self.cfg.get("watch_enabled", True) else "Resume Watching",
            self._toggle_watching,
            bg=COLORS["card_alt"],
            pady=8,
        )
        self.watch_toggle_btn.pack(fill="x", pady=(8, 4))

        self.reset_mute_btn = self._make_button(
            sidebar,
            "Unmute & Undeafen All",
            self._force_restore_all,
            bg=COLORS["card_alt"],
            pady=6,
            font=("Segoe UI", 8, "bold"),
        )
        self.reset_mute_btn.pack(fill="x", pady=(0, 4))

        self.update_btn = self._make_button(
            sidebar,
            "Check for Updates",
            self._on_update_button_click,
            bg=COLORS["card_alt"],
            pady=6,
            font=("Segoe UI", 8, "bold"),
        )
        self.update_btn.pack(fill="x", pady=(0, 2))

        self.footer_error = tk.Label(
            sidebar,
            text="",
            bg=COLORS["sidebar"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
            wraplength=200,
            justify="left",
        )
        self.footer_error.pack(anchor="w", pady=(2, 0))

        # Divider
        tk.Frame(shell, bg=COLORS["border"], width=1).pack(side="left", fill="y")

        # Right Main Content Area
        self.main_area = tk.Frame(shell, bg=COLORS["bg"], padx=22, pady=18)
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

        self.level_detection_prompt = tk.Frame(
            self._banner_slot,
            bg="#33270f",
            highlightthickness=1,
            highlightbackground=COLORS["amber"],
            padx=12,
            pady=8,
        )
        self.level_detection_prompt_label = tk.Label(
            self.level_detection_prompt,
            text="",
            bg="#33270f",
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        self.level_detection_prompt_label.pack(side="left", fill="x", expand=True)
        self._make_button(
            self.level_detection_prompt,
            "Correct",
            self._edit_level_detection_candidate,
            bg=COLORS["card_alt"],
            padx=10,
            pady=4,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left", padx=(8, 6))
        self._make_button(
            self.level_detection_prompt,
            "Yes",
            self._confirm_level_detection_candidate,
            bg=COLORS["green"],
            fg="#052e16",
            hover_bg="#4ade80",
            padx=12,
            pady=4,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left", padx=(0, 6))
        self._make_button(
            self.level_detection_prompt,
            "Later",
            self._dismiss_level_detection_candidate,
            bg=COLORS["card_alt"],
            padx=10,
            pady=4,
            font=("Segoe UI", 8),
        ).pack(side="left")

        self.level_correction_row = tk.Frame(self._banner_slot, bg=COLORS["bg"])
        self.level_correction_entry = tk.Entry(
            self.level_correction_row,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            font=("Segoe UI", 9),
        )
        self.level_correction_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self._make_button(
            self.level_correction_row,
            "Save correction",
            self._save_level_detection_correction,
            bg=COLORS["accent"],
            padx=12,
            pady=4,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left", padx=(8, 0))
        self.level_correction_row.pack_forget()
        self.level_detection_prompt.pack_forget()

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
                row.configure(bg=COLORS["card"])
                ind.configure(bg=COLORS["cyan"])
                btn.configure(bg=COLORS["card"], fg=COLORS["text"])
            else:
                row.configure(bg=COLORS["sidebar"])
                ind.configure(bg=COLORS["sidebar"])
                btn.configure(bg=COLORS["sidebar"], fg=COLORS["subtext"])

    def _build_dashboard_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["dashboard"] = page

        tk.Label(page, text="Overview", bg=COLORS["bg"], fg=COLORS["text"], font=("Segoe UI", 17, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            page,
            text="Control when apps mute and run a 30-second test to verify screen reading.",
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 14))
        self.level_detection_status_lbl = tk.Label(
            page,
            text=(
                "Level detection beta is on; waiting for a visible pause-screen title."
                if self.cfg.get("level_detection_beta", False)
                else "Level detection beta is off."
            ),
            bg=COLORS["bg"],
            fg=COLORS["amber"] if self.cfg.get("level_detection_beta", False) else COLORS["subtext"],
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.level_detection_status_lbl.pack(anchor="w", pady=(0, 8))

        # Card 1: Threshold + quick summary
        trig_card = self._make_card(page)
        trig_card.pack(fill="x", pady=(0, 14))

        t_top = tk.Frame(trig_card, bg=COLORS["card"])
        t_top.pack(fill="x")
        tk.Label(
            t_top,
            text="Mute Apps & Discord Past",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")

        self.unmute_100_var = tk.BooleanVar(value=bool(self.cfg.get("unmute_at_100", True)))
        tk.Checkbutton(
            t_top,
            text="Unmute at 100% (level complete)",
            variable=self.unmute_100_var,
            command=self._on_toggle_unmute_100,
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            selectcolor=COLORS["input"],
            activebackground=COLORS["card"],
            activeforeground=COLORS["text"],
            font=("Segoe UI", 9),
        ).pack(side="right")

        pct_row = tk.Frame(trig_card, bg=COLORS["card"])
        pct_row.pack(fill="x", pady=(10, 12))

        self.percent_var = tk.DoubleVar(value=float(self.cfg.get("mute_at_percent", 50.0)))
        self.percent_scale = tk.Scale(
            pct_row,
            from_=0,
            to=100,
            resolution=1,
            orient="horizontal",
            variable=self.percent_var,
            showvalue=False,
            bg=COLORS["card"],
            fg=COLORS["text"],
            troughcolor=COLORS["input"],
            activebackground=COLORS["cyan"],
            highlightthickness=0,
            length=340,
            command=self._on_scale_move,
        )
        self.percent_scale.pack(side="left", padx=(0, 12))

        self.percent_entry_var = tk.StringVar(value=f"{self.percent_var.get():.0f}")
        tk.Entry(
            pct_row,
            textvariable=self.percent_entry_var,
            width=5,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            font=("Consolas", 13, "bold"),
            justify="center",
        ).pack(side="left", padx=(0, 4), ipady=4)
        tk.Label(pct_row, text="%", bg=COLORS["card"], fg=COLORS["subtext"], font=("Segoe UI", 12, "bold")).pack(
            side="left", padx=(0, 12)
        )
        self.save_pct_btn = self._make_button(
            pct_row,
            "Apply %",
            self._save_mute_percent,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
        )
        self.save_pct_btn.pack(side="left")

        # Global Hotkey to Toggle % Checks On/Off
        watch_hk_row = tk.Frame(trig_card, bg=COLORS["card"])
        watch_hk_row.pack(fill="x", pady=(0, 12))

        tk.Label(
            watch_hk_row,
            text="Toggle % Checks Hotkey:",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 8))

        watch_hk = self.cfg.get("watch_toggle_hotkey")
        watch_hk_text = watch_hk.get("label", "Not set") if isinstance(watch_hk, dict) else "Not set"
        self.watch_hk_var = tk.StringVar(value=watch_hk_text)
        tk.Label(
            watch_hk_row,
            textvariable=self.watch_hk_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            font=("Consolas", 9, "bold"),
            padx=10,
            pady=4,
            width=18,
            anchor="w",
        ).pack(side="left", padx=(0, 8))

        self.record_watch_hk_btn = self._make_button(
            watch_hk_row,
            "Record Hotkey",
            self._start_watch_hotkey_record,
            bg=COLORS["card_alt"],
            padx=10,
            pady=4,
            font=("Segoe UI", 8, "bold"),
        )
        self.record_watch_hk_btn.pack(side="left", padx=(0, 6))
        self._make_button(
            watch_hk_row,
            "Clear",
            self._clear_watch_hotkey,
            padx=8,
            pady=4,
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(0, 10))

        self.watch_hk_status_lbl = tk.Label(
            watch_hk_row,
            text="Inactive",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8, "bold"),
        )
        self.watch_hk_status_lbl.pack(side="left")

        # Summary Chips Row
        chips = tk.Frame(trig_card, bg=COLORS["card"])
        chips.pack(fill="x")

        self.summary_apps_btn = self._make_button(
            chips,
            "Apps: 0 selected",
            lambda: self._show_page("apps"),
            padx=12,
            pady=5,
            font=("Segoe UI", 8, "bold"),
        )
        self.summary_apps_btn.pack(side="left", padx=(0, 8))

        self.summary_discord_btn = self._make_button(
            chips,
            "Discord Deafen: Off",
            lambda: self._show_page("discord"),
            padx=12,
            pady=5,
            font=("Segoe UI", 8, "bold"),
        )
        self.summary_discord_btn.pack(side="left", padx=(0, 8))

        self.summary_capture_btn = self._make_button(
            chips,
            "Capture: Auto (GeometryDash.exe)",
            lambda: self._show_page("capture"),
            padx=12,
            pady=5,
            font=("Segoe UI", 8, "bold"),
        )
        self.summary_capture_btn.pack(side="left", padx=(0, 8))

        self._make_button(
            chips,
            "Pick % Spot",
            self._start_region_pick,
            bg=COLORS["card_alt"],
            padx=10,
            pady=5,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left")

        # Card 2: 30-Second Screen Test
        test_card = self._make_card(page)
        test_card.pack(fill="both", expand=True)

        tc_top = tk.Frame(test_card, bg=COLORS["card"])
        tc_top.pack(fill="x")

        tc_title = tk.Frame(tc_top, bg=COLORS["card"])
        tc_title.pack(side="left", fill="x", expand=True)
        tk.Label(
            tc_title,
            text="30-Second Screen Test",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w")
        tk.Label(
            tc_title,
            text="Play any GD level for 30 seconds — we'll show every % detected (nothing will be muted during the test).",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))

        self.test_btn = self._make_button(
            tc_top,
            "Start 30s Test",
            self._toggle_screen_test,
            bg=COLORS["cyan"],
            fg="#082f49",
            hover_bg="#7dd3fc",
            padx=16,
            pady=8,
        )
        self.test_btn.pack(side="right", padx=(10, 0))

        # Progress bar + metrics row
        prog_row = tk.Frame(test_card, bg=COLORS["card"])
        prog_row.pack(fill="x", pady=(12, 8))

        self.test_bar_canvas = tk.Canvas(
            prog_row,
            height=10,
            bg=COLORS["input"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self.test_bar_canvas.pack(side="left", fill="x", expand=True, padx=(0, 12))

        self.test_timer_lbl = tk.Label(
            prog_row,
            text="Ready (30s)",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Consolas", 10, "bold"),
            width=14,
            anchor="e",
        )
        self.test_timer_lbl.pack(side="right")

        # Live Chart + Percentages Seen Feed
        body_row = tk.Frame(test_card, bg=COLORS["card"])
        body_row.pack(fill="both", expand=True, pady=(4, 8))

        chart_wrap = tk.Frame(
            body_row,
            bg=COLORS["input"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=8,
            pady=8,
        )
        chart_wrap.pack(side="left", fill="both", expand=True, padx=(0, 12))

        ch_hdr = tk.Frame(chart_wrap, bg=COLORS["input"])
        ch_hdr.pack(fill="x", pady=(0, 4))
        self.test_live_pct_lbl = tk.Label(
            ch_hdr,
            text="Live Read: --%",
            bg=COLORS["input"],
            fg=COLORS["text"],
            font=("Consolas", 11, "bold"),
        )
        self.test_live_pct_lbl.pack(side="left")
        self.test_stats_lbl = tk.Label(
            ch_hdr,
            text="Peak: --%  •  Reads: 0",
            bg=COLORS["input"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        )
        self.test_stats_lbl.pack(side="right")

        self.test_chart = tk.Canvas(chart_wrap, bg=COLORS["input"], highlightthickness=0, height=175)
        self.test_chart.pack(fill="both", expand=True)
        self.test_chart.bind("<Configure>", lambda _e: self._draw_test_chart())

        # Milestones list on the right
        feed_wrap = tk.Frame(
            body_row,
            bg=COLORS["input"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            width=215,
            padx=8,
            pady=8,
        )
        feed_wrap.pack(side="right", fill="y")
        feed_wrap.pack_propagate(False)

        tk.Label(
            feed_wrap,
            text="Percentages Seen",
            bg=COLORS["input"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        self.test_listbox = tk.Listbox(
            feed_wrap,
            bg=COLORS["input"],
            fg=COLORS["cyan"],
            selectbackground=COLORS["card_alt"],
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Consolas", 9),
        )
        self.test_listbox.pack(fill="both", expand=True)

        # Result Banner
        self.test_verdict_lbl = tk.Label(
            test_card,
            text="Tip: Start a level in Geometry Dash and press Start 30s Test to check detection.",
            bg=COLORS["card_alt"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
            padx=12,
            pady=8,
            anchor="w",
            justify="left",
        )
        self.test_verdict_lbl.pack(fill="x")

    def _build_apps_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["apps"] = page

        hdr = tk.Frame(page, bg=COLORS["bg"])
        hdr.pack(fill="x", pady=(0, 12))
        tk.Label(hdr, text="Apps to Mute", bg=COLORS["bg"], fg=COLORS["text"], font=("Segoe UI", 17, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            hdr,
            text="Select only the windows/apps you want muted when passing your %. Geometry Dash is never muted.",
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))

        card = self._make_card(page, padx=14, pady=12)
        card.pack(fill="both", expand=True)

        toolbar = tk.Frame(card, bg=COLORS["card"])
        toolbar.pack(fill="x", pady=(0, 10))

        self.app_search_var = tk.StringVar()
        self.app_search_var.trace_add("write", lambda *_a: self._render_apps_list())
        search_box = tk.Entry(
            toolbar,
            textvariable=self.app_search_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            font=("Segoe UI", 9),
            width=26,
        )
        search_box.pack(side="left", ipady=5, padx=(0, 8))

        self.bg_audio_var = tk.BooleanVar(value=bool(self.cfg.get("show_background_audio", False)))
        tk.Checkbutton(
            toolbar,
            text="Show background audio",
            variable=self.bg_audio_var,
            command=self._on_toggle_bg_audio,
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            selectcolor=COLORS["input"],
            activebackground=COLORS["card"],
            activeforeground=COLORS["text"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(4, 0))

        self._make_button(toolbar, "Clear All", self._clear_selected_apps, padx=10, pady=5).pack(side="right")
        self._make_button(toolbar, "Refresh Windows", self._refresh_windows, padx=10, pady=5).pack(
            side="right", padx=(0, 8)
        )

        list_wrap = tk.Frame(card, bg=COLORS["input"], highlightthickness=1, highlightbackground=COLORS["border"])
        list_wrap.pack(fill="both", expand=True)

        self.win_canvas = tk.Canvas(list_wrap, bg=COLORS["input"], highlightthickness=0)
        self.win_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.win_canvas.yview)
        self.win_list_frame = tk.Frame(self.win_canvas, bg=COLORS["input"])

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

    def _build_discord_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["discord"] = page

        tk.Label(
            page,
            text="Discord Auto-Deafen",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            page,
            text="Automatically press your Discord Toggle Deafen keybind past your % and undeafen when you die or exit.",
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 12))

        # Card 1: Hotkey Setup Controls
        deaf_card = self._make_card(page, pady=12)
        deaf_card.pack(fill="x", pady=(0, 12))

        deaf_hdr = tk.Frame(deaf_card, bg=COLORS["card"])
        deaf_hdr.pack(fill="x")
        tk.Label(
            deaf_hdr,
            text="Toggle Deafen Hotkey",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left")

        self.discord_state_badge = tk.Label(
            deaf_hdr,
            text="Disabled",
            bg=COLORS["pill_idle"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8, "bold"),
            padx=8,
            pady=2,
        )
        self.discord_state_badge.pack(side="right")

        tk.Label(
            deaf_card,
            text="Set a keybind here and match it to Discord → System → Keybinds → Toggle Deafen (auto-undeafens on death, 100%, or exit).",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

        self.auto_deafen_var = tk.BooleanVar(value=bool(self.cfg.get("auto_deafen", False)))
        tk.Checkbutton(
            deaf_card,
            text="Enable Discord Auto-Deafen",
            variable=self.auto_deafen_var,
            command=self._on_toggle_auto_deafen,
            bg=COLORS["card"],
            fg=COLORS["text"],
            selectcolor=COLORS["input"],
            activebackground=COLORS["card"],
            activeforeground=COLORS["text"],
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 8))

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
            font=("Consolas", 11, "bold"),
            padx=14,
            pady=7,
            width=22,
            anchor="w",
        ).pack(side="left", padx=(0, 10))

        self.record_hk_btn = self._make_button(
            hk_row,
            "Record Hotkey",
            self._start_hotkey_record,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
        )
        self.record_hk_btn.pack(side="left", padx=(0, 8))
        self._make_button(hk_row, "Test Hotkey", self._test_hotkey).pack(side="left", padx=(0, 8))
        self._make_button(hk_row, "Clear", self._clear_hotkey).pack(side="left")

        # Card 2: Interactive Discord Deafen Setup Tutorial
        guide_card = self._make_card(page, pady=12)
        guide_card.pack(fill="both", expand=True)

        guide_hdr = tk.Frame(guide_card, bg=COLORS["card"])
        guide_hdr.pack(fill="x", pady=(0, 8))

        tk.Label(
            guide_hdr,
            text="How to Set Up Discord Auto-Deafen",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"),
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
                "In Discord, click the cog in the bottom-left, scroll down under Experience, click System, and the Keybinds menu is there.",
                None,
            ),
            (
                "2",
                "Create a 'Toggle Deafen' Keybind in Discord",
                "Click Add a Keybind, set Action to Toggle Deafen (not Toggle Mute), click Record Keybind, and press an unused combo like Ctrl+Shift+D or F8. Avoid Space, Up, W, or Esc.",
                None,
            ),
            (
                "3",
                "Match & Enable Here in GD Quiet",
                "Check 'Enable Discord Auto-Deafen' above, click Record Hotkey, and press that exact same keybind.",
                "step3",
            ),
            (
                "4",
                "Test Before Starting a Level",
                "Join a Discord voice channel and click Test Hotkey above to confirm your headphone icon toggles Deafened on/off. Start each GD run undeafened so death/100% restores it cleanly.",
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
                pady=6,
            )
            row.pack(fill="x", pady=(0, 6))

            num_lbl = tk.Label(
                row,
                text=num,
                bg=COLORS["cyan_dark"],
                fg=COLORS["text"],
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
                wraplength=540,
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
                "Want to mute YouTube, Spotify, or browsers instead?",
                "Open the 'Apps to Mute' tab and check those windows directly — GD Quiet mutes them silently via Windows audio without needing any keybinds.",
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
                fg=COLORS["cyan"],
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                box,
                text=desc,
                bg=COLORS["card_alt"],
                fg=COLORS["subtext"],
                font=("Segoe UI", 8),
                wraplength=580,
                justify="left",
                anchor="w",
            ).pack(fill="x", pady=(3, 0))

        self._show_discord_guide_tab("steps")

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

        tk.Label(
            page,
            text="Screen Capture",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            page,
            text="Choose which game window to watch and preview what GD Quiet sees.",
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 14))

        cap_card = self._make_card(page)
        cap_card.pack(fill="x", pady=(0, 14))

        row1 = tk.Frame(cap_card, bg=COLORS["card"])
        row1.pack(fill="x", pady=(0, 12))
        tk.Label(row1, text="Target Window:", bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 9, "bold")).pack(
            side="left", padx=(0, 8)
        )

        self.target_proc_var = tk.StringVar(value=str(self.cfg.get("target_process") or "geometrydash.exe"))
        self.target_combo = ttk.Combobox(
            row1,
            textvariable=self.target_proc_var,
            state="normal",
            width=32,
            style="Dark.TCombobox",
        )
        self.target_combo.pack(side="left", padx=(0, 8))
        self.target_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_target_proc_change())
        self.target_combo.bind("<Return>", lambda _e: self._on_target_proc_change())
        self._make_button(row1, "Refresh", self._refresh_target_windows, padx=10, pady=4).pack(side="left")

        presets_row = tk.Frame(cap_card, bg=COLORS["card"])
        presets_row.pack(fill="x", pady=(0, 10))
        tk.Label(
            presets_row,
            text="Quick % Spot:",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 8))
        self._make_button(
            presets_row,
            "Auto Top 10%",
            self._reset_auto_region,
            padx=9,
            pady=4,
            font=("Segoe UI", 8, "bold"),
        ).pack(side="left", padx=(0, 6))
        for p_label, p_rx, p_ry, p_rw, p_rh in PERCENT_PRESETS:
            self._make_button(
                presets_row,
                p_label,
                lambda lbl=p_label, rx=p_rx, ry=p_ry, rw=p_rw, rh=p_rh: self._apply_region_preset(
                    lbl, rx, ry, rw, rh
                ),
                padx=9,
                pady=4,
                font=("Segoe UI", 8, "bold"),
            ).pack(side="left", padx=(0, 6))

        row2 = tk.Frame(cap_card, bg=COLORS["card"])
        row2.pack(fill="x")
        self._make_button(
            row2,
            "Drag Box on Screen",
            self._start_region_pick,
            bg=COLORS["cyan"],
            fg="#082f49",
            hover_bg="#7dd3fc",
        ).pack(side="left", padx=(0, 8))
        self.delay_pick_btn = self._make_button(
            row2,
            "Pick with 3s Delay",
            self._start_region_pick_delayed,
            padx=10,
            pady=6,
            font=("Segoe UI", 8, "bold"),
        )
        self.delay_pick_btn.pack(side="left", padx=(0, 12))
        self.region_mode_label = tk.Label(
            row2,
            text=self._describe_region_mode(),
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        )
        self.region_mode_label.pack(side="left")

        # Live preview card
        prev_card = self._make_card(page)
        prev_card.pack(fill="both", expand=True)

        tk.Label(
            prev_card,
            text="Live Capture Preview",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        prev_box = tk.Frame(
            prev_card,
            bg=COLORS["input"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=14,
            pady=14,
        )
        prev_box.pack(fill="x", pady=(0, 10))

        self.preview_label = tk.Label(
            prev_box,
            text="Waiting for capture...",
            bg="#090d12",
            fg=COLORS["subtext"],
            width=44,
            height=5,
            font=("Segoe UI", 9),
        )
        self.preview_label.pack(pady=(0, 10))

        self.detected_pct_label = tk.Label(
            prev_box,
            text="Detected: --%",
            bg=COLORS["input"],
            fg=COLORS["text"],
            font=("Consolas", 14, "bold"),
        )
        self.detected_pct_label.pack(anchor="w")

        self.watch_status_label = tk.Label(
            prev_box,
            text="Waiting for Geometry Dash...",
            bg=COLORS["input"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        )
        self.watch_status_label.pack(anchor="w", pady=(2, 0))

        tk.Label(
            prev_card,
            text="For best accuracy, run Geometry Dash in Borderless or Windowed mode with 'Show Percentage' enabled.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        ).pack(anchor="w")

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

        hdr = tk.Frame(page, bg=COLORS["bg"])
        hdr.pack(fill="x", pady=(0, 10))
        tk.Label(hdr, text="Settings", bg=COLORS["bg"], fg=COLORS["text"], font=("Segoe UI", 17, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            hdr,
            text="Configure muting behavior, OCR timing, Discord cooldowns, window preferences, and data.",
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))

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
                font=("Segoe UI", 10, "bold"),
                anchor="w",
            )
            chk.pack(anchor="w")
            tk.Label(
                row,
                text=desc,
                bg=COLORS["card"],
                fg=COLORS["subtext"],
                font=("Segoe UI", 8),
                anchor="w",
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
            tk.Label(row, text=label, bg=COLORS["card"], fg=COLORS["text"], font=("Segoe UI", 9, "bold")).pack(
                side="left"
            )
            val = self.cfg.get(key, DEFAULT_CONFIG[key])
            sval = f"{int(round(float(val)))}" if is_int else f"{float(val):.2f}".rstrip("0").rstrip(".")
            var = tk.StringVar(value=sval)
            self._settings_num_vars[key] = (var, min_v, max_v, is_int)

            right = tk.Frame(row, bg=COLORS["card"])
            right.pack(side="right")
            entry = tk.Entry(
                right,
                textvariable=var,
                width=7,
                bg=COLORS["input"],
                fg=COLORS["text"],
                insertbackground=COLORS["text"],
                relief="flat",
                font=("Consolas", 10, "bold"),
                justify="center",
            )
            entry.pack(side="left", ipady=3, padx=(0, 6))
            tk.Label(right, text=unit, bg=COLORS["card"], fg=COLORS["subtext"], font=("Segoe UI", 8), width=6, anchor="w").pack(
                side="left", padx=(0, 6)
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
            self._make_button(right, "Apply", apply_num, bg=COLORS["card_alt"], padx=10, pady=3, font=("Segoe UI", 8, "bold")).pack(
                side="left"
            )

        # Card 1: Trigger & Muting Behavior
        c1 = self._make_card(self.settings_body, pady=12)
        c1.pack(fill="x", pady=(0, 12))
        tk.Label(c1, text="Trigger & Muting Behavior", bg=COLORS["card"], fg=COLORS["cyan"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w", pady=(0, 6)
        )
        add_number_row(
            c1,
            "mute_at_percent",
            "Mute Threshold Percent",
            "% (0-100)",
            0.0,
            100.0,
            is_int=False,
            on_apply=self._on_settings_percent_applied,
        )
        add_number_row(
            c1,
            "missing_timeout_s",
            "Unmute Delay After % Disappears (Death / Exit)",
            "sec",
            0.05,
            5.0,
            is_int=False,
        )
        add_number_row(
            c1,
            "remute_interval_s",
            "Re-Enforce Mute Interval (New Audio Streams)",
            "sec",
            0.1,
            5.0,
            is_int=False,
        )
        add_toggle(
            c1,
            "Unmute & Undeafen at 100% (Level Complete)",
            "Automatically restore audio and Discord when reaching 100%.",
            self.unmute_100_var,
            self._on_toggle_unmute_100,
        )
        add_toggle(
            c1,
            "Show Background Audio Processes",
            "Include apps without visible windows in the Apps to Mute list.",
            self.bg_audio_var,
            self._on_toggle_bg_audio,
        )
        self.sound_feedback_var = tk.BooleanVar(value=bool(self.cfg.get("sound_feedback", False)))
        add_toggle(
            c1,
            "Play Sound Chime on Mute / Unmute",
            "Plays a subtle Windows notification sound whenever GD Quiet mutes or unmutes.",
            self.sound_feedback_var,
            self._on_toggle_sound_feedback,
        )

        # Card 2: OCR & Screen Capture
        c2 = self._make_card(self.settings_body, pady=12)
        c2.pack(fill="x", pady=(0, 12))
        tk.Label(c2, text="OCR & Screen Capture", bg=COLORS["card"], fg=COLORS["cyan"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w", pady=(0, 6)
        )
        self.settings_watch_enabled_var = tk.BooleanVar(value=bool(self.cfg.get("watch_enabled", True)))
        add_toggle(
            c2,
            "Enable Screen % Watching",
            "Master toggle for capturing the screen and muting apps automatically.",
            self.settings_watch_enabled_var,
            self._on_settings_watch_toggle,
        )
        add_number_row(
            c2,
            "scan_interval_playing_ms",
            "Scan Interval While Playing",
            "ms",
            5.0,
            500.0,
            is_int=True,
        )
        add_number_row(
            c2,
            "scan_interval_idle_ms",
            "Scan Interval in Menus / Idle",
            "ms",
            10.0,
            1000.0,
            is_int=True,
        )
        add_number_row(
            c2,
            "preview_interval_ms",
            "Live Capture Preview Refresh Interval",
            "ms",
            50.0,
            2000.0,
            is_int=True,
        )
        add_number_row(
            c2,
            "test_duration_s",
            "Screen Test Duration",
            "sec",
            5.0,
            120.0,
            is_int=True,
            on_apply=self._on_settings_test_duration_applied,
        )

        # Card 3: Discord & Hotkeys
        c3 = self._make_card(self.settings_body, pady=12)
        c3.pack(fill="x", pady=(0, 12))
        tk.Label(c3, text="Discord & Hotkeys", bg=COLORS["card"], fg=COLORS["cyan"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w", pady=(0, 6)
        )
        add_toggle(
            c3,
            "Enable Discord Auto-Deafen",
            "Send your Discord Toggle Deafen keybind when crossing the mute threshold.",
            self.auto_deafen_var,
            self._on_toggle_auto_deafen,
        )
        add_number_row(
            c3,
            "discord_cooldown_ms",
            "Discord Hotkey Toggle Cooldown",
            "ms",
            0.0,
            2000.0,
            is_int=True,
            on_apply=lambda v: self.discord_ctrl.set_cooldown_ms(v),
        )

        # Card 4: Window & Updates
        c4 = self._make_card(self.settings_body, pady=12)
        c4.pack(fill="x", pady=(0, 12))
        tk.Label(c4, text="Window & Updates", bg=COLORS["card"], fg=COLORS["cyan"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w", pady=(0, 6)
        )
        self.always_on_top_var = tk.BooleanVar(value=bool(self.cfg.get("always_on_top", False)))
        add_toggle(
            c4,
            "Always on Top",
            "Keep the GD Quiet window floating above other windows.",
            self.always_on_top_var,
            self._on_toggle_always_on_top,
        )
        self.start_minimized_var = tk.BooleanVar(value=bool(self.cfg.get("start_minimized", False)))
        add_toggle(
            c4,
            "Start Minimized",
            "Minimize GD Quiet to the taskbar automatically when launched.",
            self.start_minimized_var,
            self._on_toggle_start_minimized,
        )
        self.auto_updates_var = tk.BooleanVar(value=bool(self.cfg.get("auto_check_updates", True)))
        add_toggle(
            c4,
            "Automatically Check for Updates",
            "Check GitHub Releases on startup and every 5 minutes for GDQuiet-Setup.exe updates.",
            self.auto_updates_var,
            self._on_toggle_auto_updates,
        )
        self.beta_updates_var = tk.BooleanVar(value=bool(self.cfg.get("beta_updates", False)))
        add_toggle(
            c4,
            "Beta Updates (Opt In)",
            "Allow prerelease versions to appear in the updater. Disabled by default.",
            self.beta_updates_var,
            self._on_toggle_beta_updates,
        )

        # Experimental level detection is deliberately opt-in and disabled by default.
        c5 = self._make_card(self.settings_body, pady=12)
        c5.pack(fill="x", pady=(0, 12))
        tk.Label(
            c5,
            text="Experimental Features (Beta)",
            bg=COLORS["card"],
            fg=COLORS["amber"],
            font=("Segoe UI", 11, "bold"),
        ).pack(
            anchor="w", pady=(0, 6)
        )
        self.level_detection_var = tk.BooleanVar(value=bool(self.cfg.get("level_detection_beta", False)))
        add_toggle(
            c5,
            "BETA: Detect level names (risky)",
            "Experimental OCR scans about once per second while screen watching is active and only reads a title on the pause screen. It never pauses or controls Geometry Dash. Misreads are expected; corrections are stored locally, and screenshots are not saved or uploaded.",
            self.level_detection_var,
            self._on_toggle_level_detection,
        )

        # Card 6: Config & Data
        c6 = self._make_card(self.settings_body, pady=12)
        c6.pack(fill="x", pady=(0, 12))
        tk.Label(c6, text="Config & Data", bg=COLORS["card"], fg=COLORS["cyan"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w", pady=(0, 6)
        )
        tk.Label(
            c6,
            text=f"Config file: {config_path()}",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Consolas", 8),
        ).pack(anchor="w", pady=(0, 8))
        btn_row = tk.Frame(c6, bg=COLORS["card"])
        btn_row.pack(fill="x")
        self._make_button(
            btn_row,
            "Open Config Folder",
            self._open_config_folder,
            bg=COLORS["card_alt"],
            padx=12,
            pady=6,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 8))
        self._make_button(
            btn_row,
            "Reset All Settings to Defaults",
            self._on_reset_defaults,
            bg=COLORS["red_bg"],
            hover_bg=COLORS["red"],
            padx=12,
            pady=6,
            font=("Segoe UI", 9, "bold"),
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
            self._apply_mute_state(False)
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
        with self.cfg_lock:
            self.cfg["beta_updates"] = bool(self.beta_updates_var.get())
        self._save_cfg()
        self._check_updates_async(silent=True)

    def _on_toggle_level_detection(self) -> None:
        enabled = bool(self.level_detection_var.get())
        with self.cfg_lock:
            self.cfg["level_detection_beta"] = enabled
        self._save_cfg()
        if enabled:
            self.level_detection_status_lbl.configure(
                text="Level detection beta is on; waiting for a visible pause-screen title.",
                fg=COLORS["amber"],
            )
        else:
            self.level_detection_status_lbl.configure(text="Level detection beta is off.", fg=COLORS["subtext"])
            self._dismiss_level_detection_candidate()

    def _show_level_detection_candidate(self, raw_name: str) -> None:
        if self._closing:
            return
        with self.cfg_lock:
            if not self.cfg.get("level_detection_beta", False):
                return
            corrections = self.cfg.get("level_name_corrections")
            corrections = dict(corrections) if isinstance(corrections, dict) else {}

        key = normalize_level_name(raw_name)
        if not key:
            return
        learned_name = corrections.get(key)
        display_name = str(learned_name or raw_name).strip()

        self._pending_level_raw = raw_name
        self._pending_level_display = display_name
        prefix = "Learned match" if learned_name else "Detected level"
        self.level_detection_prompt_label.configure(
            text=f"{prefix}: {display_name}. Is this correct?"
        )
        self.level_correction_row.pack_forget()
        self.level_detection_prompt.pack(fill="x", pady=(0, 8))
        self.level_detection_status_lbl.configure(
            text=f"Possible level detected: {display_name} (awaiting confirmation).",
            fg=COLORS["amber"],
        )

    def _confirm_level_detection_candidate(self) -> None:
        self._save_level_detection_feedback(self._pending_level_display)

    def _edit_level_detection_candidate(self) -> None:
        self.level_correction_entry.delete(0, "end")
        self.level_correction_entry.insert(0, self._pending_level_display)
        self.level_correction_row.pack(fill="x", pady=(0, 8))
        self.level_correction_entry.focus_set()

    def _save_level_detection_correction(self) -> None:
        corrected = self.level_correction_entry.get().strip()
        if corrected:
            self._save_level_detection_feedback(corrected)
        else:
            self.level_detection_prompt_label.configure(text="Enter the correct level name, or choose Later.")

    def _save_level_detection_feedback(self, accepted_name: str) -> None:
        key = normalize_level_name(self._pending_level_raw)
        accepted_name = accepted_name.strip()
        if not key or not accepted_name:
            return

        with self.cfg_lock:
            corrections = self.cfg.get("level_name_corrections")
            learned = dict(corrections) if isinstance(corrections, dict) else {}
            learned[key] = accepted_name
            self.cfg["level_name_corrections"] = learned
        self._save_cfg()
        self.level_detection_status_lbl.configure(
            text=f"Last confirmed level: {accepted_name} (saved on this device).",
            fg=COLORS["green"],
        )
        self._dismiss_level_detection_candidate()

    def _dismiss_level_detection_candidate(self) -> None:
        self._pending_level_raw = ""
        self._pending_level_display = ""
        self.level_detection_prompt.pack_forget()
        self.level_correction_row.pack_forget()

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
        self._save_cfg()

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
        self.auto_updates_var.set(bool(self.cfg["auto_check_updates"]))
        self.beta_updates_var.set(bool(self.cfg["beta_updates"]))
        self.level_detection_var.set(bool(self.cfg["level_detection_beta"]))
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
        self.level_detection_status_lbl.configure(text="Level detection beta is off.", fg=COLORS["subtext"])
        self._dismiss_level_detection_candidate()
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
                    lambda name=reading.level_name_candidate: self._show_level_detection_candidate(name),
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
        effective_mute = False if (testing or not watch_enabled) else reading.should_mute
        now_m = time.monotonic()
        if effective_mute != self.current_want_mute:
            self.current_want_mute = effective_mute
            if effective_mute:
                self._last_audio_enforce_at = now_m
            self._apply_mute_state(effective_mute)
            if sound_feedback:
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
        with self.cfg_lock:
            auto_deaf = bool(self.cfg.get("auto_deafen", False))
            hk = self.cfg.get("deafen_hotkey")

        if testing:
            color, txt = COLORS["cyan"], "TESTING (30s)"
        elif not self.cfg.get("watch_enabled", True):
            color, txt = COLORS["subtext"], "PAUSED"
        elif apps_muted or deafened:
            color, txt = COLORS["red"], "MUTED"
        else:
            color, txt = COLORS["green"], "WATCHING"

        self.status_dot.itemconfigure(self._dot_id, fill=color)
        self.sidebar_status_lbl.configure(text=txt, fg=color)

        deaf_lbl = getattr(self, "sidebar_deaf_lbl", None)
        if deaf_lbl is not None:
            if deafened and apps_muted:
                deaf_lbl.configure(text="Apps & Discord: Muted", fg=COLORS["red"])
            elif deafened:
                deaf_lbl.configure(text="Discord: Deafened", fg=COLORS["red"])
            elif apps_muted:
                deaf_lbl.configure(text="Apps: Muted", fg=COLORS["red"])
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

        self.sidebar_thresh_lbl.configure(text=f"Mutes past {pct:.0f}%")
        if "apps" in self._nav_buttons:
            self._nav_buttons["apps"][2].configure(text=f"Apps to Mute ({n_apps})")

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
        self.sidebar_thresh_lbl.configure(text=f"Mutes past {pct:.0f}%")

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

    def _apply_worker(self) -> None:
        last_applied: bool | None = None
        while True:
            with self._apply_cond:
                while not self._apply_stop and (
                    self._pending_mute is None
                    or (self._pending_mute == last_applied and not self._force_reapply)
                ):
                    self._apply_cond.wait(timeout=0.5)
                    if (
                        not self._apply_stop
                        and self._pending_mute is None
                        and last_applied is False
                        and self.audio_ctrl.is_muted
                    ):
                        self._pending_mute = False
                        self._force_reapply = True
                if self._apply_stop:
                    return
                target_mute = bool(self._pending_mute)
                self._pending_mute = None
                self._force_reapply = False

            with self.apply_lock:
                with self.cfg_lock:
                    selected = list(self.cfg.get("selected_apps") or [])
                    hotkey = self.cfg.get("deafen_hotkey")
                    auto_deafen = bool(self.cfg.get("auto_deafen", False))
                self.audio_ctrl.apply(selected, target_mute)
                self.discord_ctrl.set_deafened(
                    want_deaf=target_mute,
                    hotkey=hotkey,
                    enabled=auto_deafen,
                )
                last_applied = target_mute
            try:
                self.root.after(0, self._update_status_indicators)
            except Exception:
                pass

    def _apply_mute_state(self, want_mute: bool, force_reapply: bool = False) -> None:
        with self._apply_cond:
            self.current_want_mute = want_mute
            self._pending_mute = want_mute
            if force_reapply:
                self._force_reapply = True
            self._apply_cond.notify_all()

    def _force_restore_all(self) -> None:
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
            row_bg = COLORS["input"] if idx % 2 == 0 else "#111826"
            row = tk.Frame(self.win_list_frame, bg=row_bg, padx=12, pady=8, cursor="hand2")
            row.pack(fill="x")

            var = tk.BooleanVar(value=(target.process_name in selected))
            self.window_vars[target.process_name] = var

            chk = tk.Checkbutton(
                row,
                variable=var,
                command=lambda p=target.process_name, v=var: self._on_app_checkbox_toggle(p, v),
                bg=row_bg,
                fg=COLORS["text"],
                selectcolor=COLORS["card"],
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
                fg=COLORS["subtext"],
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
                fg=COLORS["subtext"],
                font=("Segoe UI", 8),
                anchor="w",
            )
            sub_lbl.pack(fill="x")

            if target.currently_muted:
                badge_txt, badge_bg = "Muted", COLORS["red_bg"]
            elif target.has_audio:
                badge_txt, badge_bg = "Audio active", COLORS["green_bg"]
            else:
                badge_txt, badge_bg = "Window", COLORS["pill_idle"]
            badge_lbl = tk.Label(
                row,
                text=badge_txt,
                bg=badge_bg,
                fg=COLORS["text"],
                font=("Segoe UI", 8, "bold"),
                padx=9,
                pady=3,
            )
            badge_lbl.pack(side="right", padx=(8, 0))

            def toggle_row(_e: Any, p: str = target.process_name, v: tk.BooleanVar = var) -> None:
                v.set(not v.get())
                self._on_app_checkbox_toggle(p, v)

            for widget in (row, text_col, top_line, name_lbl, proc_lbl, sub_lbl, badge_lbl):
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

    def _on_close(self) -> None:
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

    root = tk.Tk()
    GDQuietApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
