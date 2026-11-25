from __future__ import annotations

import atexit
import ctypes
from datetime import datetime, timezone
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from config import load_config, resolve_supabase_credentials, save_config
from discord_hotkey import DiscordDeafenController, parse_tk_hotkey, send_hotkey
from supabase_client import AccountSyncWorker, SupabaseClient, SupabaseError
from window_audio import (
    AudioMuteController,
    MuteTarget,
    is_geometry_dash_running,
    list_mute_targets,
)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


COLORS = {
    "bg": "#0f1419",
    "card": "#171f2b",
    "card_alt": "#1d2736",
    "border": "#293548",
    "input": "#0d1218",
    "text": "#f1f5f9",
    "subtext": "#94a3b8",
    "accent": "#5865f2",
    "accent_hover": "#4752c4",
    "green": "#22c55e",
    "red": "#ef4444",
    "amber": "#f59e0b",
    "pill_idle": "#334155",
}


def _parse_iso_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


class GDQuietApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("GD Quiet")
        self.root.geometry("760x780")
        self.root.minsize(680, 680)
        self.root.configure(bg=COLORS["bg"])

        self.cfg = load_config()
        url, anon_key = resolve_supabase_credentials(self.cfg)
        self.supabase = SupabaseClient(url, anon_key)

        self.audio_ctrl = AudioMuteController()
        self.discord_ctrl = DiscordDeafenController()
        self.apply_lock = threading.Lock()
        self.watcher: AccountSyncWorker | None = None

        self.account: dict[str, Any] | None = None
        self.current_want_mute = False
        self.last_telemetry_at = 0.0
        self.recording_hotkey = False
        self.window_vars: dict[str, tk.BooleanVar] = {}

        self._setup_styles()
        self._build_ui()
        self._refresh_windows()
        self._restore_session_async()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        atexit.register(self._cleanup_sync)
        self.root.after(1000, self._watchdog_tick)

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TScrollbar", background=COLORS["card_alt"], troughcolor=COLORS["input"])

    def _make_button(
        self,
        parent: tk.Widget,
        text: str,
        command: Any,
        bg: str = COLORS["card_alt"],
        fg: str = COLORS["text"],
        active_bg: str | None = None,
        padx: int = 12,
        pady: int = 6,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg or bg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            padx=padx,
            pady=pady,
        )

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=COLORS["bg"], padx=18, pady=16)
        outer.pack(fill="both", expand=True)

        # Header
        header = tk.Frame(outer, bg=COLORS["bg"])
        header.pack(fill="x", pady=(0, 12))

        title_col = tk.Frame(header, bg=COLORS["bg"])
        title_col.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_col,
            text="GD Quiet",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_col,
            text="Mute selected open windows & deafen Discord past your GD percent",
            bg=COLORS["bg"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(anchor="w")

        right_hdr = tk.Frame(header, bg=COLORS["bg"])
        right_hdr.pack(side="right")

        self.status_pill = tk.Label(
            right_hdr,
            text="SIGNED OUT",
            bg=COLORS["pill_idle"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=5,
        )
        self.status_pill.pack(side="left", padx=(0, 8))

        self.settings_btn = self._make_button(right_hdr, "Supabase Settings", self._open_settings_dialog)
        self.settings_btn.pack(side="left")

        # Card 1: Discord / Supabase Account
        self.account_card = tk.Frame(
            outer,
            bg=COLORS["card"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=14,
            pady=12,
        )
        self.account_card.pack(fill="x", pady=(0, 12))

        acc_top = tk.Frame(self.account_card, bg=COLORS["card"])
        acc_top.pack(fill="x")
        tk.Label(
            acc_top,
            text="GD Quiet Account (Discord via Supabase)",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")

        self.auth_btn_frame = tk.Frame(acc_top, bg=COLORS["card"])
        self.auth_btn_frame.pack(side="right")

        self.login_btn = self._make_button(
            self.auth_btn_frame,
            "Sign in with Discord",
            self._start_discord_login,
            bg=COLORS["accent"],
            active_bg=COLORS["accent_hover"],
        )
        self.logout_btn = self._make_button(
            self.auth_btn_frame,
            "Sign Out",
            self._sign_out,
        )

        self.config_warn_label = tk.Label(
            self.account_card,
            text="",
            bg=COLORS["card"],
            fg=COLORS["amber"],
            font=("Segoe UI", 9),
            justify="left",
            wraplength=690,
        )

        # Signed-in details container
        self.account_details = tk.Frame(self.account_card, bg=COLORS["card"])

        id_row = tk.Frame(self.account_details, bg=COLORS["card"])
        id_row.pack(fill="x", pady=(10, 8))

        left_id = tk.Frame(id_row, bg=COLORS["card"])
        left_id.pack(side="left", fill="x", expand=True)
        self.discord_user_label = tk.Label(
            left_id,
            text="Signed in as —",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        )
        self.discord_user_label.pack(anchor="w")
        tk.Label(
            left_id,
            text="Enter this Account Number in Geode → GD Quiet settings:",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(2, 0))

        right_id = tk.Frame(id_row, bg=COLORS["input"], padx=10, pady=6)
        right_id.pack(side="right")
        self.account_num_var = tk.StringVar(value="--------")
        tk.Label(
            right_id,
            textvariable=self.account_num_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            font=("Consolas", 14, "bold"),
        ).pack(side="left", padx=(0, 8))
        self.copy_btn = self._make_button(
            right_id,
            "Copy #",
            self._copy_account_number,
            bg=COLORS["accent"],
            active_bg=COLORS["accent_hover"],
            padx=10,
            pady=3,
        )
        self.copy_btn.pack(side="left")

        # Mute threshold row
        pct_row = tk.Frame(self.account_details, bg=COLORS["card"])
        pct_row.pack(fill="x", pady=(4, 6))
        tk.Label(
            pct_row,
            text="Mute at %:",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left", padx=(0, 8))

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
            highlightthickness=0,
            length=280,
            command=self._on_scale_move,
        )
        self.percent_scale.pack(side="left", padx=(0, 8))

        self.percent_entry_var = tk.StringVar(value=f"{self.percent_var.get():.0f}")
        self.percent_entry = tk.Entry(
            pct_row,
            textvariable=self.percent_entry_var,
            width=6,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief="flat",
            font=("Consolas", 10),
            justify="center",
        )
        self.percent_entry.pack(side="left", padx=(0, 6))
        tk.Label(pct_row, text="%", bg=COLORS["card"], fg=COLORS["subtext"], font=("Segoe UI", 9)).pack(
            side="left", padx=(0, 10)
        )
        self.save_pct_btn = self._make_button(pct_row, "Save %", self._save_mute_percent, padx=10, pady=4)
        self.save_pct_btn.pack(side="left")

        self.telemetry_label = tk.Label(
            self.account_details,
            text="Waiting for Geometry Dash level progress...",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        )
        self.telemetry_label.pack(anchor="w", pady=(6, 0))

        self.signed_out_hint = tk.Label(
            self.account_card,
            text="Sign in with Discord via Supabase to generate your GD Quiet Account Number and sync live level progress.",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
            wraplength=690,
            justify="left",
        )

        # Card 2: Open Windows to Mute
        win_card = tk.Frame(
            outer,
            bg=COLORS["card"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=14,
            pady=12,
        )
        win_card.pack(fill="both", expand=True, pady=(0, 12))

        win_hdr = tk.Frame(win_card, bg=COLORS["card"])
        win_hdr.pack(fill="x", pady=(0, 8))
        tk.Label(
            win_hdr,
            text="Open Windows to Mute",
            bg=COLORS["card"],
            fg=COLORS["text"],
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left")

        win_tools = tk.Frame(win_hdr, bg=COLORS["card"])
        win_tools.pack(side="right")

        self.bg_audio_var = tk.BooleanVar(value=bool(self.cfg.get("show_background_audio", False)))
        tk.Checkbutton(
            win_tools,
            text="Include background audio",
            variable=self.bg_audio_var,
            command=self._on_toggle_bg_audio,
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            selectcolor=COLORS["input"],
            activebackground=COLORS["card"],
            activeforeground=COLORS["text"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(0, 8))

        self._make_button(win_tools, "Refresh", self._refresh_windows, padx=10, pady=4).pack(
            side="left", padx=(0, 6)
        )
        self._make_button(win_tools, "Clear All", self._clear_selected_apps, padx=10, pady=4).pack(
            side="left"
        )

        list_wrap = tk.Frame(win_card, bg=COLORS["input"], highlightthickness=1, highlightbackground=COLORS["border"])
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
            lambda e: self.win_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

        # Card 3: Discord Deafen Hotkey
        deafen_card = tk.Frame(
            outer,
            bg=COLORS["card"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            padx=14,
            pady=12,
        )
        deafen_card.pack(fill="x")

        deaf_top = tk.Frame(deafen_card, bg=COLORS["card"])
        deaf_top.pack(fill="x")

        self.auto_deafen_var = tk.BooleanVar(value=bool(self.cfg.get("auto_deafen", False)))
        tk.Checkbutton(
            deaf_top,
            text="Auto-Deafen Discord when muted (uses your Discord Toggle Deafen keybind)",
            variable=self.auto_deafen_var,
            command=self._on_toggle_auto_deafen,
            bg=COLORS["card"],
            fg=COLORS["text"],
            selectcolor=COLORS["input"],
            activebackground=COLORS["card"],
            activeforeground=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        hk_row = tk.Frame(deafen_card, bg=COLORS["card"])
        hk_row.pack(fill="x", pady=(8, 0))

        tk.Label(
            hk_row,
            text="Deafen Hotkey:",
            bg=COLORS["card"],
            fg=COLORS["subtext"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 8))

        hk = self.cfg.get("deafen_hotkey")
        hk_text = hk.get("label", "Not set") if isinstance(hk, dict) else "Not set"
        self.hotkey_var = tk.StringVar(value=hk_text)
        tk.Label(
            hk_row,
            textvariable=self.hotkey_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            font=("Consolas", 10, "bold"),
            padx=12,
            pady=5,
            width=22,
            anchor="w",
        ).pack(side="left", padx=(0, 8))

        self.record_hk_btn = self._make_button(hk_row, "Record Hotkey", self._start_hotkey_record)
        self.record_hk_btn.pack(side="left", padx=(0, 6))
        self._make_button(hk_row, "Test", self._test_hotkey).pack(side="left", padx=(0, 6))
        self._make_button(hk_row, "Clear", self._clear_hotkey).pack(side="left")

        self.footer_error = tk.Label(
            outer,
            text="",
            bg=COLORS["bg"],
            fg=COLORS["amber"],
            font=("Segoe UI", 8),
        )
        self.footer_error.pack(anchor="w", pady=(8, 0))

        self._render_auth_state()

    def _render_auth_state(self) -> None:
        self.config_warn_label.pack_forget()
        self.account_details.pack_forget()
        self.signed_out_hint.pack_forget()
        self.login_btn.pack_forget()
        self.logout_btn.pack_forget()

        if not self.supabase.is_configured():
            self.config_warn_label.configure(
                text="Supabase is not configured yet. Click 'Supabase Settings' to enter your URL & anon key locally, or add SUPABASE_URL & SUPABASE_ANON_KEY as GitHub repository secrets when releasing."
            )
            self.config_warn_label.pack(anchor="w", pady=(8, 4))

        if self.account:
            self.logout_btn.pack(side="left")
            name = self.account.get("discord_username") or "Discord User"
            self.discord_user_label.configure(text=f"Signed in as {name}")
            self.account_num_var.set(str(self.account.get("account_number") or "--------"))
            self.account_details.pack(fill="x")
            self._update_status_pill()
        else:
            self.login_btn.pack(side="left")
            self.signed_out_hint.pack(anchor="w", pady=(8, 2))
            self._update_status_pill()

    def _update_status_pill(self) -> None:
        if not self.account:
            self.status_pill.configure(text="SIGNED OUT", bg=COLORS["pill_idle"])
        elif self.audio_ctrl.is_muted or self.discord_ctrl.deafened_by_us:
            self.status_pill.configure(text="MUTED", bg=COLORS["red"])
        else:
            self.status_pill.configure(text="UNMUTED", bg=COLORS["green"])

    def _on_scale_move(self, _val: str) -> None:
        self.percent_entry_var.set(f"{self.percent_var.get():.0f}")

    def _copy_account_number(self) -> None:
        code = self.account_num_var.get().strip()
        if not code or code == "--------":
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        self.copy_btn.configure(text="Copied!")
        self.root.after(1500, lambda: self.copy_btn.configure(text="Copy #"))

    def _open_settings_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Supabase Configuration")
        dlg.geometry("500x250")
        dlg.configure(bg=COLORS["bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        frame = tk.Frame(dlg, bg=COLORS["bg"], padx=16, pady=14)
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
            text="Supabase Anon / Publishable Key",
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
            self.cfg["supabase_url"] = url_var.get().strip().rstrip("/")
            self.cfg["supabase_anon_key"] = key_var.get().strip()
            save_config(self.cfg)
            new_url, new_key = resolve_supabase_credentials(self.cfg)
            self.supabase = SupabaseClient(new_url, new_key)
            dlg.destroy()
            self._render_auth_state()
            if self.cfg.get("session"):
                self._restore_session_async()

        btns = tk.Frame(frame, bg=COLORS["bg"])
        btns.pack(fill="x")
        self._make_button(btns, "Save", save_and_close, bg=COLORS["accent"], active_bg=COLORS["accent_hover"]).pack(
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
                "Enter your Supabase Project URL and Anon Key in 'Supabase Settings' first (or release with GitHub repo secrets).",
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
        self.cfg["session"] = session
        if "mute_at_percent" in account:
            pct = float(account["mute_at_percent"])
            self.cfg["mute_at_percent"] = pct
            self.percent_var.set(pct)
            self.percent_entry_var.set(f"{pct:.0f}")
        save_config(self.cfg)

        self.account = account
        self._render_auth_state()
        self._start_watcher()
        self._handle_account_row(account)

    def _sign_out(self) -> None:
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self._apply_mute_state(False)
        self.account = None
        self.cfg["session"] = None
        save_config(self.cfg)
        self._render_auth_state()

    def _start_watcher(self) -> None:
        if self.watcher:
            self.watcher.stop()
        self.watcher = AccountSyncWorker(
            client=self.supabase,
            get_session=lambda: self.cfg.get("session"),
            on_session_refreshed=self._on_session_refreshed,
            on_account_update=lambda row: self.root.after(0, lambda r=row: self._handle_account_row(r)),
            on_error=lambda err: self.root.after(0, lambda e=err: self.footer_error.configure(text=e)),
        )
        self.watcher.start()

    def _on_session_refreshed(self, session: dict[str, Any]) -> None:
        self.cfg["session"] = session
        save_config(self.cfg)

    def _save_mute_percent(self) -> None:
        try:
            raw = float(self.percent_entry_var.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid Percent", "Enter a number between 0 and 100.")
            return
        pct = max(0.0, min(100.0, raw))
        self.percent_var.set(pct)
        self.percent_entry_var.set(f"{pct:.0f}")
        self.cfg["mute_at_percent"] = pct
        save_config(self.cfg)

        session = self.cfg.get("session")
        if not session:
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
        self.save_pct_btn.configure(state="normal", text="Save %")
        self.account = account
        self.footer_error.configure(text="")

    def _on_percent_save_error(self, err: str) -> None:
        self.save_pct_btn.configure(state="normal", text="Save %")
        self.footer_error.configure(text=f"Could not save percent: {err}")

    def _handle_account_row(self, row: dict[str, Any]) -> None:
        self.account = row
        is_playing = bool(row.get("is_playing"))
        pct = float(row.get("current_percent") or 0.0)
        threshold = float(row.get("mute_at_percent") or self.cfg.get("mute_at_percent", 50.0))
        level_name = str(row.get("level_name") or "").strip()
        updated_epoch = _parse_iso_ts(row.get("updated_at"))
        is_fresh = updated_epoch is None or (datetime.now(timezone.utc).timestamp() - updated_epoch) <= 15.0

        want_mute = bool(row.get("should_mute")) and is_playing and is_fresh and is_geometry_dash_running()
        if is_playing:
            self.last_telemetry_at = datetime.now(timezone.utc).timestamp()
            lvl = f'"{level_name}"' if level_name else "Level"
            state_txt = "MUTED" if want_mute else "UNMUTED"
            self.telemetry_label.configure(
                text=f"Playing {lvl} • {pct:.1f}% (mutes at {threshold:.0f}%) • {state_txt}",
                fg=COLORS["red"] if want_mute else COLORS["green"],
            )
        else:
            self.telemetry_label.configure(
                text="Waiting for Geometry Dash level progress...",
                fg=COLORS["subtext"],
            )

        self.footer_error.configure(text="")
        self._apply_mute_state(want_mute)

    def _apply_mute_state(self, want_mute: bool) -> None:
        def worker() -> None:
            with self.apply_lock:
                self.current_want_mute = want_mute
                selected = self.cfg.get("selected_apps") or []
                self.audio_ctrl.apply(selected, want_mute)
                self.discord_ctrl.set_deafened(
                    want_deaf=want_mute,
                    hotkey=self.cfg.get("deafen_hotkey"),
                    enabled=bool(self.cfg.get("auto_deafen", False)),
                )
            self.root.after(0, self._update_status_pill)

        threading.Thread(target=worker, daemon=True).start()

    def _watchdog_tick(self) -> None:
        if self.current_want_mute:
            stale = (
                self.last_telemetry_at > 0
                and (datetime.now(timezone.utc).timestamp() - self.last_telemetry_at) > 15.0
            )
            if stale or not is_geometry_dash_running():
                self._apply_mute_state(False)
                if self.account:
                    self.telemetry_label.configure(
                        text="Geometry Dash closed or timed out — unmuted.",
                        fg=COLORS["subtext"],
                    )
        self.root.after(1000, self._watchdog_tick)

    def _on_toggle_bg_audio(self) -> None:
        self.cfg["show_background_audio"] = bool(self.bg_audio_var.get())
        save_config(self.cfg)
        self._refresh_windows()

    def _clear_selected_apps(self) -> None:
        self.cfg["selected_apps"] = []
        save_config(self.cfg)
        for var in self.window_vars.values():
            var.set(False)
        if self.current_want_mute:
            self._apply_mute_state(True)

    def _on_app_checkbox_toggle(self, proc_name: str, var: tk.BooleanVar) -> None:
        selected = {p.lower() for p in (self.cfg.get("selected_apps") or []) if p}
        if var.get():
            selected.add(proc_name.lower())
        else:
            selected.discard(proc_name.lower())
        self.cfg["selected_apps"] = sorted(selected)
        save_config(self.cfg)

        if self.current_want_mute:
            self._apply_mute_state(True)

    def _refresh_windows(self) -> None:
        for child in self.win_list_frame.winfo_children():
            child.destroy()
        self.window_vars.clear()

        include_bg = bool(self.bg_audio_var.get())
        targets: list[MuteTarget] = list_mute_targets(include_background_audio=include_bg)
        selected = {p.lower() for p in (self.cfg.get("selected_apps") or [])}

        if not targets:
            tk.Label(
                self.win_list_frame,
                text="No open windows detected. Click Refresh after opening YouTube, Discord, Spotify, etc.",
                bg=COLORS["input"],
                fg=COLORS["subtext"],
                font=("Segoe UI", 9),
                padx=12,
                pady=14,
            ).pack(anchor="w")
            return

        for idx, target in enumerate(targets):
            row_bg = COLORS["input"] if idx % 2 == 0 else "#121923"
            row = tk.Frame(self.win_list_frame, bg=row_bg, padx=10, pady=7)
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
            chk.pack(side="left", padx=(0, 6))

            text_col = tk.Frame(row, bg=row_bg)
            text_col.pack(side="left", fill="x", expand=True)

            top_line = tk.Frame(text_col, bg=row_bg)
            top_line.pack(fill="x")
            tk.Label(
                top_line,
                text=target.display_name,
                bg=row_bg,
                fg=COLORS["text"],
                font=("Segoe UI", 9, "bold"),
            ).pack(side="left")
            tk.Label(
                top_line,
                text=f" ({target.process_name})",
                bg=row_bg,
                fg=COLORS["subtext"],
                font=("Consolas", 8),
            ).pack(side="left")

            if target.window_titles:
                preview = " • ".join(target.window_titles[:2])
                if len(preview) > 78:
                    preview = preview[:75] + "..."
            else:
                preview = "Background audio session (no open window)"
            tk.Label(
                text_col,
                text=preview,
                bg=row_bg,
                fg=COLORS["subtext"],
                font=("Segoe UI", 8),
                anchor="w",
            ).pack(fill="x")

            if target.currently_muted:
                badge_txt, badge_bg = "Muted", COLORS["red"]
            elif target.has_audio:
                badge_txt, badge_bg = "Audio active", "#166534"
            else:
                badge_txt, badge_bg = "Window", COLORS["pill_idle"]
            tk.Label(
                row,
                text=badge_txt,
                bg=badge_bg,
                fg=COLORS["text"],
                font=("Segoe UI", 8, "bold"),
                padx=8,
                pady=2,
            ).pack(side="right", padx=(8, 0))

    def _on_toggle_auto_deafen(self) -> None:
        enabled = bool(self.auto_deafen_var.get())
        self.cfg["auto_deafen"] = enabled
        save_config(self.cfg)
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
        self.root.focus_force()

        def on_key(event: tk.Event) -> str:
            if event.keysym == "Escape":
                self._stop_hotkey_record()
                return "break"
            parsed = parse_tk_hotkey(event)
            if parsed is None:
                return "break"
            self.cfg["deafen_hotkey"] = parsed
            save_config(self.cfg)
            self.hotkey_var.set(parsed["label"])
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
        self.record_hk_btn.configure(text="Record Hotkey", bg=COLORS["card_alt"], fg=COLORS["text"])

    def _test_hotkey(self) -> None:
        hk = self.cfg.get("deafen_hotkey")
        if not hk:
            messagebox.showinfo("No Hotkey Set", "Click 'Record Hotkey' and press your Discord Toggle Deafen keybind first.")
            return
        threading.Thread(target=lambda: send_hotkey(hk), daemon=True).start()

    def _clear_hotkey(self) -> None:
        self.cfg["deafen_hotkey"] = None
        save_config(self.cfg)
        self.hotkey_var.set("Not set")

    def _cleanup_sync(self) -> None:
        if self.watcher:
            self.watcher.stop()
        with self.apply_lock:
            self.audio_ctrl.restore()
            self.discord_ctrl.restore(self.cfg.get("deafen_hotkey"))

    def _on_close(self) -> None:
        self._cleanup_sync()
        save_config(self.cfg)
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    GDQuietApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
