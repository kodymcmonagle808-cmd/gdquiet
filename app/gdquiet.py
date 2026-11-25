from __future__ import annotations

import atexit
import ctypes
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

import mss
from PIL import Image, ImageEnhance, ImageTk

from config import load_config, resolve_supabase_credentials, save_config
from discord_hotkey import DiscordDeafenController, parse_tk_hotkey, send_hotkey
from screen_reader import (
    ScreenPercentWatcher,
    ScreenReading,
    ScreenTestSession,
    find_window_client_rect,
    get_virtual_screen_bounds,
)
from supabase_client import SupabaseClient
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


COLORS = {
    "bg": "#0b0f17",
    "sidebar": "#101622",
    "card": "#161e2e",
    "card_alt": "#1d283c",
    "hover": "#233048",
    "border": "#26334d",
    "input": "#0c111b",
    "text": "#f8fafc",
    "subtext": "#94a3b8",
    "accent": "#5865f2",
    "accent_hover": "#4752c4",
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

        with mss.mss() as sct:
            shot = sct.grab({"left": self.vx, "top": self.vy, "width": self.vw, "height": self.vh})
            raw_img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        dimmed = ImageEnhance.Brightness(raw_img).enhance(0.55)
        self._photo = ImageTk.PhotoImage(dimmed)
        self._start: tuple[int, int] | None = None
        self._rect_id: int | None = None

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.vw}x{self.vh}+{self.vx}+{self.vy}")
        self.win.configure(cursor="crosshair")

        self.canvas = tk.Canvas(self.win, width=self.vw, height=self.vh, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self.canvas.create_rectangle(
            self.vw // 2 - 340, 14, self.vw // 2 + 340, 52,
            fill="#0b0f17", outline="#38bdf8", width=1,
        )
        self.canvas.create_text(
            self.vw // 2,
            33,
            text="Drag a box around the % number on screen  •  Press Esc to cancel",
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
        self._rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#38bdf8",
            width=2,
        )

    def _on_drag(self, event: tk.Event) -> None:
        if not self._start or self._rect_id is None:
            return
        self.canvas.coords(self._rect_id, self._start[0], self._start[1], event.x, event.y)

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


class GDQuietApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("GD Quiet")
        self.root.geometry("940x650")
        self.root.minsize(860, 590)
        self.root.configure(bg=COLORS["bg"])

        self.cfg_lock = threading.Lock()
        self.cfg = load_config()
        url, anon_key = resolve_supabase_credentials(self.cfg)
        self.supabase = SupabaseClient(url, anon_key)

        self.audio_ctrl = AudioMuteController()
        self.discord_ctrl = DiscordDeafenController()
        self.apply_lock = threading.Lock()
        self.reading_lock = threading.Lock()
        self.test_lock = threading.Lock()
        self.test_session = ScreenTestSession(duration_s=30.0)

        self.account: dict[str, Any] | None = None
        self.current_want_mute = False
        self.recording_hotkey = False
        self.window_vars: dict[str, tk.BooleanVar] = {}
        self._all_targets: list[MuteTarget] = []
        self._latest_reading: ScreenReading | None = None
        self._ui_reading_pending = False
        self._preview_photo_main: ImageTk.PhotoImage | None = None
        self._last_cloud_sync_at = 0.0
        self._last_cloud_mute: bool | None = None
        self._active_page = "dashboard"
        self._nav_buttons: dict[str, tuple[tk.Frame, tk.Frame, tk.Button]] = {}
        self._pages: dict[str, tk.Frame] = {}

        self._setup_styles()
        self._build_ui()
        self._show_page("dashboard")
        self._refresh_windows()
        self._refresh_target_windows()
        self._restore_session_async()

        self.watcher = ScreenPercentWatcher(
            get_settings=self._snapshot_cfg,
            on_reading=self._on_screen_reading,
        )
        self.watcher.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        atexit.register(self._cleanup_sync)
        self.root.after(150, self._test_ui_tick)

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
            "TScrollbar",
            background=COLORS["card_alt"],
            troughcolor=COLORS["input"],
            bordercolor=COLORS["input"],
            arrowcolor=COLORS["subtext"],
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground=COLORS["input"],
            background=COLORS["card_alt"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["text"],
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
        btn.bind("<Enter>", lambda _e, b=btn, c=hbg: b.configure(bg=c) if str(b["state"]) != "disabled" else None)
        btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=b._base_bg) if str(b["state"]) != "disabled" else None)  # type: ignore[attr-defined]
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

    def _draw_logo(self, canvas: tk.Canvas) -> None:
        canvas.create_rectangle(2, 2, 36, 36, fill=COLORS["accent"], outline="#818cf8", width=1)
        # Speaker silhouette
        canvas.create_rectangle(9, 14, 15, 24, fill="#f8fafc", outline="")
        canvas.create_polygon(15, 14, 24, 8, 24, 30, 15, 24, fill="#f8fafc", outline="")
        # Mute slash
        canvas.create_line(9, 9, 29, 29, fill=COLORS["red"], width=3, capstyle="round")

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=COLORS["bg"])
        shell.pack(fill="both", expand=True)

        # Left Sidebar
        sidebar = tk.Frame(shell, bg=COLORS["sidebar"], width=232, padx=14, pady=16)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=COLORS["sidebar"])
        brand.pack(fill="x", pady=(0, 16))
        logo_cv = tk.Canvas(brand, width=38, height=38, bg=COLORS["sidebar"], highlightthickness=0)
        logo_cv.pack(side="left", padx=(0, 10))
        self._draw_logo(logo_cv)

        brand_txt = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_txt.pack(side="left", fill="x")
        tk.Label(brand_txt, text="GD Quiet", bg=COLORS["sidebar"], fg=COLORS["text"], font=("Segoe UI", 15, "bold")).pack(
            anchor="w"
        )
        tk.Label(
            brand_txt,
            text="Screen-reading muter",
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
        self.sidebar_pct_lbl.pack(anchor="w", pady=(4, 0))

        self.sidebar_thresh_lbl = tk.Label(
            status_box,
            text=f"Mutes past {float(self.cfg.get('mute_at_percent', 50.0)):.0f}%",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        )
        self.sidebar_thresh_lbl.pack(anchor="w")

        # Navigation Tabs
        nav_wrap = tk.Frame(sidebar, bg=COLORS["sidebar"])
        nav_wrap.pack(fill="x", expand=True, anchor="n")

        nav_items = [
            ("dashboard", "Overview & 30s Test"),
            ("apps", "Apps to Mute"),
            ("discord", "Discord & Account"),
            ("capture", "Screen Capture"),
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
            self._nav_buttons[key] = (row, indicator, btn)

        # Sidebar bottom toggle
        self.watch_toggle_btn = self._make_button(
            sidebar,
            "Pause Watching" if self.cfg.get("watch_enabled", True) else "Resume Watching",
            self._toggle_watching,
            bg=COLORS["card_alt"],
            pady=8,
        )
        self.watch_toggle_btn.pack(fill="x", pady=(10, 4))

        self.footer_error = tk.Label(
            sidebar,
            text="",
            bg=COLORS["sidebar"],
            fg=COLORS["amber"],
            font=("Segoe UI", 8),
            wraplength=200,
            justify="left",
        )
        self.footer_error.pack(anchor="w", pady=(4, 0))

        # Divider
        tk.Frame(shell, bg=COLORS["border"], width=1).pack(side="left", fill="y")

        # Right Main Content Area
        self.main_area = tk.Frame(shell, bg=COLORS["bg"], padx=22, pady=18)
        self.main_area.pack(side="left", fill="both", expand=True)

        self._build_dashboard_page()
        self._build_apps_page()
        self._build_discord_page()
        self._build_capture_page()

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
        self.summary_capture_btn.pack(side="left")

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
        self.win_canvas.bind_all(
            "<MouseWheel>",
            lambda e: self.win_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            if self._active_page == "apps"
            else None,
        )

    def _build_discord_page(self) -> None:
        page = tk.Frame(self.main_area, bg=COLORS["bg"])
        self._pages["discord"] = page

        tk.Label(
            page,
            text="Discord & Account",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor="w")
        tk.Label(
            page,
            text="Auto-deafen Discord past your percentage and link your GD Quiet account.",
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 14))

        # Card A: Auto-Deafen Hotkey
        deaf_card = self._make_card(page)
        deaf_card.pack(fill="x", pady=(0, 14))

        tk.Label(
            deaf_card,
            text="Discord Auto-Deafen",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w")
        tk.Label(
            deaf_card,
            text="Automatically presses your Discord Toggle Deafen keybind when you cross your % and undeafens when you die or exit.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(2, 10))

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
        ).pack(anchor="w", pady=(0, 10))

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

        tk.Label(
            deaf_card,
            text="Tip: Set the same keybind in Discord → User Settings → Keybinds → Toggle Deafen (avoid Jump/Pause keys).",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(10, 0))

        # Card B: Supabase Discord Account
        acc_card = self._make_card(page)
        acc_card.pack(fill="x")

        acc_top = tk.Frame(acc_card, bg=COLORS["card"])
        acc_top.pack(fill="x")
        tk.Label(
            acc_top,
            text="GD Quiet Account (Discord via Supabase)",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left")

        btn_box = tk.Frame(acc_top, bg=COLORS["card"])
        btn_box.pack(side="right")

        self._make_button(btn_box, "Supabase Settings", self._open_settings_dialog, padx=10, pady=5).pack(
            side="right", padx=(8, 0)
        )
        self.login_btn = self._make_button(
            btn_box,
            "Sign in with Discord",
            self._start_discord_login,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
            pady=5,
        )
        self.logout_btn = self._make_button(btn_box, "Sign Out", self._sign_out, pady=5)

        self.account_details = tk.Frame(acc_card, bg=COLORS["card"])
        info_row = tk.Frame(self.account_details, bg=COLORS["card"])
        info_row.pack(fill="x", pady=(12, 0))

        self.discord_user_label = tk.Label(
            info_row,
            text="Signed in as —",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 10, "bold"),
        )
        self.discord_user_label.pack(side="left")

        id_badge = tk.Frame(info_row, bg=COLORS["input"], padx=10, pady=5)
        id_badge.pack(side="right")
        tk.Label(id_badge, text="Account #:", bg=COLORS["input"], fg=COLORS["subtext"], font=("Segoe UI", 8)).pack(
            side="left", padx=(0, 6)
        )
        self.account_num_var = tk.StringVar(value="--------")
        tk.Label(
            id_badge,
            textvariable=self.account_num_var,
            bg=COLORS["input"],
            fg=COLORS["cyan"],
            font=("Consolas", 12, "bold"),
        ).pack(side="left", padx=(0, 8))
        self.copy_btn = self._make_button(
            id_badge,
            "Copy",
            self._copy_account_number,
            bg=COLORS["accent"],
            hover_bg=COLORS["accent_hover"],
            padx=10,
            pady=3,
        )
        self.copy_btn.pack(side="left")

        self.signed_out_hint = tk.Label(
            acc_card,
            text="Sign in with Discord through Supabase to save your threshold and GD Quiet account number in the cloud.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
            wraplength=600,
            justify="left",
        )

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

        row2 = tk.Frame(cap_card, bg=COLORS["card"])
        row2.pack(fill="x")
        self._make_button(
            row2,
            "Pick Screen Region",
            self._start_region_pick,
            bg=COLORS["cyan"],
            fg="#082f49",
            hover_bg="#7dd3fc",
        ).pack(side="left", padx=(0, 10))
        self._make_button(row2, "Reset to Auto Top-Center", self._reset_auto_region).pack(side="left", padx=(0, 12))
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

    # ---- 30-Second Screen Test Logic ----

    def _toggle_screen_test(self) -> None:
        with self.test_lock:
            if self.test_session.active:
                self.test_session.stop()
                self._finish_screen_test_ui()
                return

            if not self.cfg.get("watch_enabled", True):
                with self.cfg_lock:
                    self.cfg["watch_enabled"] = True
                self._save_cfg()
                self.watch_toggle_btn.configure(text="Pause Watching")

            self.test_session.start()

        # Clear any active mute immediately when starting dry-run test
        if self.current_want_mute:
            self._apply_mute_state(False)

        self.test_listbox.delete(0, "end")
        self.test_btn.configure(text="Stop Test", bg=COLORS["red"], fg=COLORS["text"], hover_bg="#dc2626")
        self.test_btn._base_bg = COLORS["red"]  # type: ignore[attr-defined]
        self.test_verdict_lbl.configure(
            text="Test running — switch to Geometry Dash and play a level for 30 seconds!",
            bg=COLORS["card_alt"],
            fg=COLORS["cyan"],
        )
        self._update_status_indicators()

    def _test_ui_tick(self) -> None:
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
                self._finish_screen_test_ui_locked()

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

        self.root.after(150, self._test_ui_tick)

    def _finish_screen_test_ui(self) -> None:
        with self.test_lock:
            self._finish_screen_test_ui_locked()

    def _finish_screen_test_ui_locked(self) -> None:
        passed, headline, detail = self.test_session.summary()
        self.test_btn.configure(text="Start 30s Test", bg=COLORS["cyan"], fg="#082f49")
        self.test_btn._base_bg = COLORS["cyan"]  # type: ignore[attr-defined]
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
            if reg.get("kind") == "window_rel":
                return "Custom box (locked to window)"
            if reg.get("kind") == "screen":
                return f"Custom screen ({reg.get('width')}x{reg.get('height')})"
        return "Auto top-center"

    def _toggle_watching(self) -> None:
        new_val = not bool(self.cfg.get("watch_enabled", True))
        with self.cfg_lock:
            self.cfg["watch_enabled"] = new_val
        self._save_cfg()

        self.watch_toggle_btn.configure(text="Pause Watching" if new_val else "Resume Watching")
        if not new_val:
            self._apply_mute_state(False)
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

    def _start_region_pick(self) -> None:
        self.root.withdraw()
        self.root.after(220, self._open_region_overlay)

    def _open_region_overlay(self) -> None:
        RegionSelectorOverlay(
            self.root,
            on_selected=self._on_region_selected,
            on_cancel=self._on_region_cancelled,
        )

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
        just_finished = False
        with self.test_lock:
            testing = self.test_session.active
            if testing:
                just_finished = self.test_session.record(reading.percent)

        effective_mute = False if testing else reading.should_mute
        if effective_mute != self.current_want_mute:
            self._apply_mute_state(effective_mute)

        if not testing:
            self._maybe_sync_cloud_state(reading)

        with self.reading_lock:
            self._latest_reading = reading
            if not self._ui_reading_pending:
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
            self.detected_pct_label.configure(
                text=f"Detected: {reading.percent:.2f}%   (OCR: {reading.raw_text!r})",
                fg=COLORS["red"] if reading.should_mute else COLORS["green"],
            )
        else:
            self.sidebar_pct_lbl.configure(text="--%")
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

    def _maybe_sync_cloud_state(self, reading: ScreenReading) -> None:
        session = self.cfg.get("session")
        if not session or not self.supabase.is_configured() or not session.get("user_id"):
            return

        now = time.monotonic()
        flipped = self._last_cloud_mute != reading.should_mute
        if not flipped and (now - self._last_cloud_sync_at) < 2.0:
            return
        if not flipped and not reading.is_playing:
            return

        self._last_cloud_sync_at = now
        self._last_cloud_mute = reading.should_mute

        def worker() -> None:
            try:
                cur = dict(session)
                if time.time() >= float(cur.get("expires_at") or 0.0) and cur.get("refresh_token"):
                    cur = self.supabase.refresh_session(cur["refresh_token"])
                    with self.cfg_lock:
                        self.cfg["session"] = cur
                    self._save_cfg()
                self.supabase.report_own_state(
                    access_token=cur["access_token"],
                    user_id=cur["user_id"],
                    percent=reading.percent or 0.0,
                    is_playing=reading.is_playing,
                    should_mute=reading.should_mute,
                )
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _update_status_indicators(self) -> None:
        with self.test_lock:
            testing = self.test_session.active

        if testing:
            color, txt = COLORS["cyan"], "TESTING (30s)"
        elif not self.cfg.get("watch_enabled", True):
            color, txt = COLORS["subtext"], "PAUSED"
        elif self.audio_ctrl.is_muted or self.discord_ctrl.deafened_by_us:
            color, txt = COLORS["red"], "MUTED"
        else:
            color, txt = COLORS["green"], "WATCHING"

        self.status_dot.itemconfigure(self._dot_id, fill=color)
        self.sidebar_status_lbl.configure(text=txt, fg=color)

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

        reg_mode = "Custom" if self.cfg.get("watch_mode") == "custom_region" else "Auto"
        self.summary_capture_btn.configure(text=f"Capture: {reg_mode} ({proc})")

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
        self._update_sidebar_badges()

        session = self.cfg.get("session")
        if not session or not self.supabase.is_configured():
            return

        self.save_pct_btn.configure(state="disabled", text="Saving...")

        def worker() -> None:
            try:
                acct = self.supabase.update_mute_percent(session["access_token"], pct)
                self.root.after(0, lambda: self._on_percent_saved(acct))
            except Exception as exc:
                self.root.after(0, lambda err=str(exc): self._on_percent_save_error(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_percent_saved(self, account: dict[str, Any]) -> None:
        self.save_pct_btn.configure(state="normal", text="Apply %")
        self.account = account
        self.footer_error.configure(text="")

    def _on_percent_save_error(self, err: str) -> None:
        self.save_pct_btn.configure(state="normal", text="Apply %")
        self.footer_error.configure(text=f"Saved locally (cloud sync skipped: {err})")

    def _apply_mute_state(self, want_mute: bool) -> None:
        def worker() -> None:
            with self.apply_lock:
                self.current_want_mute = want_mute
                with self.cfg_lock:
                    selected = list(self.cfg.get("selected_apps") or [])
                    hotkey = self.cfg.get("deafen_hotkey")
                    auto_deafen = bool(self.cfg.get("auto_deafen", False))
                self.audio_ctrl.apply(selected, want_mute)
                self.discord_ctrl.set_deafened(
                    want_deaf=want_mute,
                    hotkey=hotkey,
                    enabled=auto_deafen,
                )
            try:
                self.root.after(0, self._update_status_indicators)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

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
            self._apply_mute_state(True)

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
            self._apply_mute_state(True)

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

    # ---- Discord & Supabase Account Page ----

    def _on_toggle_auto_deafen(self) -> None:
        enabled = bool(self.auto_deafen_var.get())
        with self.cfg_lock:
            self.cfg["auto_deafen"] = enabled
        self._save_cfg()
        self._update_sidebar_badges()
        if not enabled:
            threading.Thread(
                target=lambda: self.discord_ctrl.restore(self.cfg.get("deafen_hotkey")),
                daemon=True,
            ).start()

    def _start_hotkey_record(self) -> None:
        if self.recording_hotkey:
            return
        self.recording_hotkey = True
        self.record_hk_btn.configure(text="Press keys (Esc cancels)...", bg=COLORS["amber"], fg="#111827")
        self.record_hk_btn._base_bg = COLORS["amber"]  # type: ignore[attr-defined]
        self.root.focus_force()

        def on_key(event: tk.Event) -> str:
            if event.keysym == "Escape":
                self._stop_hotkey_record()
                return "break"
            parsed = parse_tk_hotkey(event)
            if parsed is None:
                return "break"
            with self.cfg_lock:
                self.cfg["deafen_hotkey"] = parsed
            self._save_cfg()
            self.hotkey_var.set(parsed["label"])
            self._update_sidebar_badges()
            self._stop_hotkey_record()
            return "break"

        self._hk_bind_id = self.root.bind("<KeyPress>", on_key)

    def _stop_hotkey_record(self) -> None:
        self.recording_hotkey = False
        if hasattr(self, "_hk_bind_id"):
            try:
                self.root.unbind("<KeyPress>", self._hk_bind_id)
            except Exception:
                pass
        self.record_hk_btn.configure(text="Record Hotkey", bg=COLORS["accent"], fg=COLORS["text"])
        self.record_hk_btn._base_bg = COLORS["accent"]  # type: ignore[attr-defined]

    def _test_hotkey(self) -> None:
        hk = self.cfg.get("deafen_hotkey")
        if not hk:
            messagebox.showinfo("No Hotkey Set", "Click 'Record Hotkey' and press your Discord Toggle Deafen keybind first.")
            return
        threading.Thread(target=lambda: send_hotkey(hk), daemon=True).start()

    def _clear_hotkey(self) -> None:
        with self.cfg_lock:
            self.cfg["deafen_hotkey"] = None
        self._save_cfg()
        self.hotkey_var.set("Not set")
        self._update_sidebar_badges()

    def _render_auth_state(self) -> None:
        self.account_details.pack_forget()
        self.signed_out_hint.pack_forget()
        self.login_btn.pack_forget()
        self.logout_btn.pack_forget()

        if self.account:
            self.logout_btn.pack(side="right")
            name = self.account.get("discord_username") or "Discord User"
            self.discord_user_label.configure(text=f"Signed in as {name}")
            self.account_num_var.set(str(self.account.get("account_number") or "--------"))
            self.account_details.pack(fill="x")
        else:
            self.login_btn.pack(side="right")
            self.signed_out_hint.pack(anchor="w", pady=(10, 0))

    def _copy_account_number(self) -> None:
        code = self.account_num_var.get().strip()
        if not code or code == "--------":
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        self.copy_btn.configure(text="Copied!")
        self.root.after(1500, lambda: self.copy_btn.configure(text="Copy"))

    def _open_settings_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Supabase Configuration")
        dlg.geometry("500x250")
        dlg.configure(bg=COLORS["bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        frame = tk.Frame(dlg, bg=COLORS["bg"], padx=18, pady=16)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="Local override (optional if already baked from GitHub repo secrets):",
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 10))

        cur_url, cur_key = resolve_supabase_credentials(self.cfg)

        tk.Label(frame, text="Supabase Project URL", bg=COLORS["bg"], fg=COLORS["text"], font=("Segoe UI", 9, "bold")).pack(
            anchor="w"
        )
        url_var = tk.StringVar(value=self.cfg.get("supabase_url") or cur_url)
        tk.Entry(
            frame,
            textvariable=url_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            font=("Consolas", 9),
        ).pack(fill="x", pady=(4, 10), ipady=5)

        tk.Label(
            frame,
            text="Supabase Publishable / Anon Key",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        key_var = tk.StringVar(value=self.cfg.get("supabase_anon_key") or cur_key)
        tk.Entry(
            frame,
            textvariable=key_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            font=("Consolas", 9),
        ).pack(fill="x", pady=(4, 14), ipady=5)

        def save_and_close() -> None:
            with self.cfg_lock:
                self.cfg["supabase_url"] = url_var.get().strip().rstrip("/")
                self.cfg["supabase_anon_key"] = key_var.get().strip()
            self._save_cfg()
            new_url, new_key = resolve_supabase_credentials(self.cfg)
            self.supabase = SupabaseClient(new_url, new_key)
            dlg.destroy()
            self._render_auth_state()
            if self.cfg.get("session"):
                self._restore_session_async()

        btns = tk.Frame(frame, bg=COLORS["bg"])
        btns.pack(fill="x")
        self._make_button(btns, "Save", save_and_close, bg=COLORS["accent"], hover_bg=COLORS["accent_hover"]).pack(
            side="right"
        )
        self._make_button(btns, "Cancel", dlg.destroy).pack(side="right", padx=(0, 8))

    def _restore_session_async(self) -> None:
        session = self.cfg.get("session")
        if not session or not self.supabase.is_configured():
            return

        def worker() -> None:
            try:
                cur = dict(session)
                if cur.get("refresh_token"):
                    cur = self.supabase.refresh_session(cur["refresh_token"])
                acct = self.supabase.get_or_create_account(cur["access_token"])
                self.root.after(0, lambda: self._on_signed_in(cur, acct))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _start_discord_login(self) -> None:
        if not self.supabase.is_configured():
            messagebox.showinfo(
                "Supabase Not Configured",
                "Enter your Supabase Project URL and Publishable/Anon Key in 'Supabase Settings' first.",
            )
            return

        self.login_btn.configure(state="disabled", text="Waiting for browser...")
        self.footer_error.configure(text="")

        def worker() -> None:
            try:
                session = self.supabase.sign_in_with_discord()
                acct = self.supabase.get_or_create_account(session["access_token"])
                self.root.after(0, lambda: self._on_signed_in(session, acct))
            except Exception as exc:
                self.root.after(0, lambda err=str(exc): self._on_login_error(err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_login_error(self, err: str) -> None:
        self.login_btn.configure(state="normal", text="Sign in with Discord")
        self.footer_error.configure(text=f"Sign-in failed: {err}")

    def _on_signed_in(self, session: dict[str, Any], account: dict[str, Any]) -> None:
        self.login_btn.configure(state="normal", text="Sign in with Discord")
        with self.cfg_lock:
            self.cfg["session"] = session
            if "mute_at_percent" in account:
                pct = float(account["mute_at_percent"])
                self.cfg["mute_at_percent"] = pct
                self.percent_var.set(pct)
                self.percent_entry_var.set(f"{pct:.0f}")
        self._save_cfg()

        self.account = account
        self._render_auth_state()
        self._update_sidebar_badges()

    def _sign_out(self) -> None:
        self.account = None
        with self.cfg_lock:
            self.cfg["session"] = None
        self._save_cfg()
        self._render_auth_state()

    def _cleanup_sync(self) -> None:
        if hasattr(self, "watcher") and self.watcher:
            self.watcher.stop()
        with self.apply_lock:
            self.audio_ctrl.restore()
            self.discord_ctrl.restore(self.cfg.get("deafen_hotkey"))

    def _on_close(self) -> None:
        self._cleanup_sync()
        self._save_cfg()
        self.root.destroy()


def main() -> None:
    if "--self-check" in sys.argv:
        from screen_reader import WinRtPercentOcr

        cfg = load_config()
        url, key = resolve_supabase_credentials(cfg)
        ocr = WinRtPercentOcr()
        try:
            assert ocr.available(), "Windows.Media.Ocr engine unavailable"
        finally:
            ocr.close()
        _ = list_mute_targets(include_background_audio=False)
        assert bool(url and key), "Supabase config missing"
        return

    root = tk.Tk()
    GDQuietApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
