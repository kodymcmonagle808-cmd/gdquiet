from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import config  # noqa: E402
import discord_hotkey  # noqa: E402
import screen_reader  # noqa: E402
import updater  # noqa: E402
import window_audio  # noqa: E402


class FakeVolume:
    def __init__(self, muted: int = 0) -> None:
        self.muted = muted

    def GetMute(self) -> int:
        return self.muted

    def SetMute(self, value: int, _guid) -> None:
        self.muted = int(value)


class FakeSession:
    def __init__(self, name: str, pid: int, muted: int = 0, instance_id: str = "") -> None:
        self.Process = SimpleNamespace(name=lambda: name, pid=pid)
        self.SimpleAudioVolume = FakeVolume(muted)
        self.InstanceIdentifier = instance_id


class GDQuietTests(unittest.TestCase):
    def test_input_struct_size_matches_win32_abi(self) -> None:
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(discord_hotkey.INPUT), expected)

    def test_parse_tk_hotkey(self) -> None:
        mod_only = SimpleNamespace(keysym="Control_L", state=0x0004, keycode=17)
        self.assertIsNone(discord_hotkey.parse_tk_hotkey(mod_only))

        combo = SimpleNamespace(keysym="d", state=0x0005, keycode=68)
        parsed = discord_hotkey.parse_tk_hotkey(combo)
        self.assertEqual(
            parsed,
            {"modifiers": ["ctrl", "shift"], "vk": ord("D"), "label": "Ctrl + Shift + D"},
        )

    def test_audio_mute_preserves_manual_mutes(self) -> None:
        chrome = FakeSession("chrome.exe", 101, muted=0)
        spotify_already_muted = FakeSession("spotify.exe", 202, muted=1)
        discord = FakeSession("discord.exe", 303, muted=0)
        sessions = [chrome, spotify_already_muted, discord]

        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=sessions):
            ctrl.apply(["chrome.exe", "spotify.exe"], mute=True)
            self.assertEqual(chrome.SimpleAudioVolume.muted, 1)
            self.assertEqual(spotify_already_muted.SimpleAudioVolume.muted, 1)
            self.assertEqual(discord.SimpleAudioVolume.muted, 0)

            ctrl.apply(["chrome.exe", "spotify.exe"], mute=False)
            self.assertEqual(chrome.SimpleAudioVolume.muted, 0)
            self.assertEqual(spotify_already_muted.SimpleAudioVolume.muted, 1)
            self.assertEqual(discord.SimpleAudioVolume.muted, 0)

    def test_audio_mute_unchecks_while_muted(self) -> None:
        chrome = FakeSession("chrome.exe", 101, muted=0)
        discord = FakeSession("discord.exe", 303, muted=0)
        sessions = [chrome, discord]

        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=sessions):
            ctrl.apply(["chrome.exe", "discord.exe"], mute=True)
            self.assertEqual(chrome.SimpleAudioVolume.muted, 1)
            self.assertEqual(discord.SimpleAudioVolume.muted, 1)

            ctrl.apply(["chrome.exe"], mute=True)
            self.assertEqual(chrome.SimpleAudioVolume.muted, 1)
            self.assertEqual(discord.SimpleAudioVolume.muted, 0)

    def test_default_config_excludes_supabase_keys(self) -> None:
        self.assertNotIn("supabase_url", config.DEFAULT_CONFIG)
        self.assertNotIn("supabase_anon_key", config.DEFAULT_CONFIG)
        self.assertNotIn("session", config.DEFAULT_CONFIG)

    def test_live_window_enumeration_runs(self) -> None:
        targets = window_audio.list_mute_targets(include_background_audio=True)
        self.assertIsInstance(targets, list)
        for t in targets:
            self.assertNotEqual(t.process_name, "geometrydash.exe")

    def test_compute_capture_rect_uses_top_10_percent(self) -> None:
        with patch.object(screen_reader, "find_window_client_rect", return_value=(100, 50, 1920, 1080)):
            rect, label = screen_reader.compute_capture_rect("auto_window", "geometrydash.exe", None)
            self.assertEqual(label, "geometrydash.exe")
            self.assertEqual(rect, {"left": 100, "top": 50, "width": 1920, "height": 108})

    def test_parse_percent_variants(self) -> None:
        self.assertEqual(screen_reader.parse_percent_from_text("50%"), 50.0)
        self.assertEqual(screen_reader.parse_percent_from_text("50.24%"), 50.24)
        self.assertEqual(screen_reader.parse_percent_from_text("490/0"), 49.0)
        self.assertEqual(screen_reader.parse_percent_from_text("1000/0"), 100.0)
        self.assertEqual(screen_reader.parse_percent_from_text("542%"), 54.2)
        self.assertEqual(screen_reader.parse_percent_from_text("O%"), 0.0)
        self.assertEqual(screen_reader.parse_percent_from_text("o%"), 0.0)
        self.assertEqual(screen_reader.parse_percent_from_text("00/0"), 0.0)
        self.assertIsNone(screen_reader.parse_percent_from_text("NO%"))
        self.assertIsNone(screen_reader.parse_percent_from_text("Attempt 42"))

    def test_winrt_ocr_reads_rendered_hud_percentages(self) -> None:
        ocr = screen_reader.WinRtPercentOcr()
        self.assertTrue(ocr.available())
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 28)
            for label, expected in [("8%", 8.0), ("49%", 49.0), ("50.24%", 50.24), ("100%", 100.0)]:
                bg = Image.new("RGBA", (280, 75), (40, 140, 220, 255))
                d = ImageDraw.Draw(bg)
                d.rectangle([20, 10, 250, 65], fill=(20, 200, 90, 255))
                d.text(
                    (85, 16),
                    label,
                    font=font,
                    fill=(255, 255, 255, 255),
                    stroke_width=2,
                    stroke_fill=(0, 0, 0, 255),
                )
                pct, _raw = ocr.read_percent(bg)
                self.assertEqual(pct, expected)

            # Blank frames should short-circuit before hitting OCR
            blank = Image.new("RGB", (1920, 108), (20, 40, 80))
            self.assertEqual(screen_reader._prepare_ocr_gray(blank, luma_fallback=False), (None, None))

            # Wide 1080p top-10% strip (center, far-left, and colored HUD) + watcher ROI tracking
            watcher = screen_reader.ScreenPercentWatcher(lambda: {}, lambda _r: None)
            wide_cases = [
                ((920, 16), "54.32%", (255, 255, 255), 54.32),
                ((40, 16), "19%", (255, 255, 255), 19.0),
                ((920, 16), "61.50%", (255, 240, 90), 61.5),
            ]
            x_frac = None
            prefer_luma = False
            for pos, label, color, expected in wide_cases:
                strip = Image.new("RGB", (1920, 108), (30, 110, 200))
                d = ImageDraw.Draw(strip)
                d.text(pos, label, font=font, fill=color, stroke_width=2, stroke_fill=(0, 0, 0))
                pct, _txt, x_frac, prefer_luma = watcher._scan_image(
                    ocr,
                    strip,
                    auto_roi=True,
                    last_x_frac=x_frac,
                    prefer_luma=prefer_luma,
                )
                self.assertEqual(pct, expected)
        finally:
            ocr.close()

    def test_percent_debouncer_transitions(self) -> None:
        deb = screen_reader.PercentStateDebouncer(missing_timeout_s=0.6)
        # Smooth ramp across threshold mutes immediately
        self.assertEqual(deb.update(45.0, 50.0, True, now=1.0), (False, True))
        self.assertEqual(deb.update(50.2, 50.0, True, now=1.1), (True, True))
        # Dropping below threshold unmutes immediately
        self.assertEqual(deb.update(0.0, 50.0, True, now=1.2), (False, True))
        # Sudden jump from 0 -> 88 requires 2 consecutive frames to ignore 1-frame OCR glitch
        self.assertEqual(deb.update(88.0, 50.0, True, now=1.3), (False, True))
        self.assertEqual(deb.update(12.0, 50.0, True, now=1.4), (False, True))
        # Two consecutive high frames mute
        self.assertEqual(deb.update(60.0, 50.0, True, now=1.5), (False, True))
        self.assertEqual(deb.update(61.0, 50.0, True, now=1.6), (True, True))
        # Reaching 100% unmutes if enabled
        self.assertEqual(deb.update(100.0, 50.0, True, now=1.7), (False, True))
        # Missing percent past timeout unmutes
        self.assertEqual(deb.update(62.0, 50.0, True, now=2.0), (False, True))
        self.assertEqual(deb.update(63.0, 50.0, True, now=2.1), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=2.2), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=2.8), (False, False))

    def test_screen_test_session_records_milestones_and_finishes(self) -> None:
        sess = screen_reader.ScreenTestSession(duration_s=30.0)
        sess.start(now=100.0)
        self.assertTrue(sess.active)
        self.assertFalse(sess.record(0.0, now=101.0))
        self.assertFalse(sess.record(12.5, now=105.0))
        self.assertFalse(sess.record(12.8, now=105.2))
        self.assertFalse(sess.record(48.2, now=115.0))
        self.assertTrue(sess.record(50.0, now=130.5))
        self.assertFalse(sess.active)
        self.assertTrue(sess.completed)
        passed, headline, _detail = sess.summary()
        self.assertTrue(passed)
        self.assertIn("48.20%", headline)
        self.assertGreaterEqual(len(sess.milestones), 3)

    def test_gui_stop_screen_test_early_does_not_deadlock(self) -> None:
        import tkinter as tk
        import gdquiet

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                patch.object(gdquiet, "load_config", return_value=dict(config.DEFAULT_CONFIG)),
                patch.object(gdquiet, "save_config"),
                patch.object(gdquiet, "list_mute_targets", return_value=[]),
                patch.object(gdquiet, "list_open_window_processes", return_value=[]),
                patch.object(gdquiet, "ScreenPercentWatcher") as mock_watcher,
            ):
                app = gdquiet.GDQuietApp(root)
                mock_watcher.return_value.start.assert_called_once()

                # Start 30s test, simulate readings, then stop early (previously deadlocked!)
                app._toggle_screen_test()
                self.assertTrue(app.test_session.active)
                app._on_screen_reading(
                    screen_reader.ScreenReading(
                        percent=24.5,
                        raw_text="24.5%",
                        should_mute=False,
                        is_playing=True,
                        status_text="Watching",
                        preview_image=None,
                    )
                )
                app._toggle_screen_test()
                self.assertFalse(app.test_session.active)
                self.assertTrue(app.test_session.completed)
                self.assertEqual(app.test_btn.cget("text"), "Start 30s Test")

                # Also verify natural expiration path in _test_ui_tick does not deadlock
                app._toggle_screen_test()
                app.test_session.start_time -= 31.0
                app._test_ui_tick()
                self.assertFalse(app.test_session.active)
                self.assertTrue(app.test_session.completed)

                # Exercise page tabs & watcher toggle
                self.assertEqual(app._nav_buttons["discord"][2].cget("text"), "Discord Auto-Deafen")
                self.assertFalse(hasattr(app, "supabase"))
                self.assertFalse(hasattr(app, "login_btn"))
                self.assertTrue(hasattr(app, "record_hk_btn"))
                for page_key in ("apps", "discord", "capture", "dashboard"):
                    app._show_page(page_key)
                app._show_discord_guide_tab("fixes")
                app._show_discord_guide_tab("steps")
                self.assertEqual(app.tutorial_step3_badge.cget("text"), "Needs hotkey")
                with app.cfg_lock:
                    app.cfg["auto_deafen"] = True
                    app.cfg["deafen_hotkey"] = {"modifiers": ["ctrl", "shift"], "vk": ord("D"), "label": "Ctrl + Shift + D"}
                app._update_sidebar_badges()
                self.assertEqual(app.tutorial_step3_badge.cget("text"), "Ready (Ctrl + Shift + D)")
                app._toggle_watching()
                app._toggle_watching()
                app._save_mute_percent()
                app._on_close()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_death_after_passing_percent_always_undeafens(self) -> None:
        import time

        # 1. Passing threshold then dying to 0% unmutes immediately; missing-% past 0.35s also unmutes
        deb = screen_reader.PercentStateDebouncer()
        self.assertEqual(deb.update(48.0, 50.0, True, now=1.0), (False, True))
        self.assertEqual(deb.update(55.0, 50.0, True, now=1.1), (True, True))
        self.assertEqual(deb.update(0.0, 50.0, True, now=1.2), (False, True))

        self.assertEqual(deb.update(49.0, 50.0, True, now=2.0), (False, True))
        self.assertEqual(deb.update(64.0, 50.0, True, now=2.1), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=2.2), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=2.6), (False, False))

        # 2. DiscordDeafenController enforces cooldown before toggling back to undeafen
        press_times: list[float] = []
        ctrl = discord_hotkey.DiscordDeafenController(toggle_cooldown_s=0.05)
        hk = {"modifiers": ["ctrl", "shift"], "vk": ord("D"), "label": "Ctrl + Shift + D"}
        with patch.object(discord_hotkey, "send_hotkey", side_effect=lambda _h: press_times.append(time.monotonic()) or True):
            ctrl.set_deafened(True, hk, True)
            self.assertTrue(ctrl.deafened_by_us)
            ctrl.set_deafened(False, hk, True)
            self.assertFalse(ctrl.deafened_by_us)
        self.assertEqual(len(press_times), 2)
        self.assertGreaterEqual(press_times[1] - press_times[0], 0.04)

    def test_gui_pass_percent_then_death_and_force_restore(self) -> None:
        import time
        import tkinter as tk
        import gdquiet

        root = tk.Tk()
        root.withdraw()
        hotkey = {"modifiers": ["ctrl", "shift"], "vk": ord("D"), "label": "Ctrl + Shift + D"}
        cfg = dict(config.DEFAULT_CONFIG, auto_deafen=True, deafen_hotkey=hotkey)
        try:
            with (
                patch.object(gdquiet, "load_config", return_value=cfg),
                patch.object(gdquiet, "save_config"),
                patch.object(gdquiet, "list_mute_targets", return_value=[]),
                patch.object(gdquiet, "list_open_window_processes", return_value=[]),
                patch.object(gdquiet, "ScreenPercentWatcher"),
                patch("discord_hotkey.send_hotkey", return_value=True) as mock_hotkey,
                patch("window_audio.AudioUtilities.GetAllSessions", return_value=[]),
            ):
                app = gdquiet.GDQuietApp(root)
                app.discord_ctrl._toggle_cooldown_s = 0.02

                # Cross threshold -> should deafen Discord
                app._on_screen_reading(
                    screen_reader.ScreenReading(
                        percent=62.0,
                        raw_text="62%",
                        should_mute=True,
                        is_playing=True,
                        status_text="Watching",
                        preview_image=None,
                        scan_ms=9.4,
                    )
                )
                deadline = time.monotonic() + 2.0
                while not app.discord_ctrl.deafened_by_us and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(app.discord_ctrl.deafened_by_us)

                # Die -> should undeafen reliably via serialized worker
                app._on_screen_reading(
                    screen_reader.ScreenReading(
                        percent=0.0,
                        raw_text="0%",
                        should_mute=False,
                        is_playing=True,
                        status_text="Watching",
                        preview_image=None,
                        scan_ms=8.1,
                    )
                )
                deadline = time.monotonic() + 2.0
                while app.discord_ctrl.deafened_by_us and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(app.discord_ctrl.deafened_by_us)
                self.assertEqual(mock_hotkey.call_count, 2)

                # Pass again and trigger failsafe Unmute & Undeafen button
                app._on_screen_reading(
                    screen_reader.ScreenReading(
                        percent=75.0,
                        raw_text="75%",
                        should_mute=True,
                        is_playing=True,
                        status_text="Watching",
                        preview_image=None,
                        scan_ms=7.8,
                    )
                )
                deadline = time.monotonic() + 2.0
                while not app.discord_ctrl.deafened_by_us and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(app.discord_ctrl.deafened_by_us)

                app._force_restore_all()
                deadline = time.monotonic() + 2.0
                while app.discord_ctrl.deafened_by_us and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(app.discord_ctrl.deafened_by_us)
                self.assertEqual(mock_hotkey.call_count, 4)

                app._on_close()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_global_watch_toggle_hotkey(self) -> None:
        import threading
        import tkinter as tk
        import gdquiet

        triggered = threading.Event()

        watcher = discord_hotkey.GlobalHotkeyWatcher(on_trigger=triggered.set)
        hk = {"modifiers": ["ctrl", "shift", "alt"], "vk": 0x7A, "label": "Ctrl + Shift + Alt + F11"}
        try:
            ok, msg = watcher.set_hotkey(hk)
            self.assertTrue(ok, msg)
            self.assertEqual(msg, "Active (Global)")
            # Inject WM_HOTKEY directly into the registered thread message queue
            discord_hotkey.user32.PostThreadMessageW(watcher._thread_id, discord_hotkey.WM_HOTKEY, watcher.HOTKEY_ID, 0)
            self.assertTrue(triggered.wait(timeout=1.5))

            with patch.object(discord_hotkey.user32, "RegisterHotKey", return_value=0), patch.object(
                discord_hotkey.ctypes, "get_last_error", return_value=discord_hotkey.ERROR_HOTKEY_ALREADY_REGISTERED
            ):
                ok_conflict, msg_conflict = watcher.set_hotkey(hk)
                self.assertFalse(ok_conflict)
                self.assertEqual(msg_conflict, "In use by another app")
        finally:
            watcher.stop()

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                patch.object(gdquiet, "load_config", return_value=dict(config.DEFAULT_CONFIG)),
                patch.object(gdquiet, "save_config"),
                patch.object(gdquiet, "list_mute_targets", return_value=[]),
                patch.object(gdquiet, "list_open_window_processes", return_value=[]),
                patch.object(gdquiet, "ScreenPercentWatcher"),
            ):
                app = gdquiet.GDQuietApp(root)
                with patch.object(app.global_hotkey, "set_hotkey", return_value=(True, "Active (Global)")):
                    with app.cfg_lock:
                        app.cfg["watch_toggle_hotkey"] = {"modifiers": ["ctrl", "shift"], "vk": 0x78, "label": "Ctrl + Shift + F9"}
                    app._apply_watch_toggle_hotkey()
                    self.assertEqual(app.watch_hk_var.get(), "Ctrl + Shift + F9")
                    self.assertEqual(app.watch_hk_status_lbl.cget("text"), "Active (Global)")
                    self.assertEqual(app.watch_toggle_btn.cget("text"), "Pause Watching (Ctrl + Shift + F9)")

                    app._toggle_watching()
                    self.assertFalse(app.cfg["watch_enabled"])
                    self.assertEqual(app.watch_toggle_btn.cget("text"), "Resume Watching (Ctrl + Shift + F9)")

                    app._toggle_watching()
                    self.assertTrue(app.cfg["watch_enabled"])
                    self.assertEqual(app.watch_toggle_btn.cget("text"), "Pause Watching (Ctrl + Shift + F9)")

                with patch.object(app.global_hotkey, "set_hotkey", return_value=(False, "Inactive")):
                    app._clear_watch_hotkey()
                    self.assertIsNone(app.cfg["watch_toggle_hotkey"])
                    self.assertEqual(app.watch_hk_var.get(), "Not set")
                    self.assertEqual(app.watch_toggle_btn.cget("text"), "Pause Watching")

                app._on_close()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_audio_mute_chrome_multiple_sessions_same_pid_and_reenforce(self) -> None:
        tab1_manual = FakeSession("chrome.exe", 404, muted=1, instance_id="chrome-tab-1")
        tab2_active = FakeSession("chrome.exe", 404, muted=0, instance_id="chrome-tab-2")
        ctrl = window_audio.AudioMuteController()

        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[tab1_manual, tab2_active]):
            ctrl.apply(["chrome.exe"], mute=True)
            self.assertEqual(tab1_manual.SimpleAudioVolume.muted, 1)
            self.assertEqual(tab2_active.SimpleAudioVolume.muted, 1)

        # Chrome spawns a 3rd audio stream mid-run under the same PID -> re-enforce catches it
        tab3_new = FakeSession("chrome.exe", 404, muted=0, instance_id="chrome-tab-3")
        with patch.object(
            window_audio.AudioUtilities, "GetAllSessions", return_value=[tab1_manual, tab2_active, tab3_new]
        ):
            ctrl.apply(["chrome.exe"], mute=True)
            self.assertEqual(tab3_new.SimpleAudioVolume.muted, 1)

            ctrl.apply(["chrome.exe"], mute=False)
            self.assertEqual(tab1_manual.SimpleAudioVolume.muted, 1)
            self.assertEqual(tab2_active.SimpleAudioVolume.muted, 0)
            self.assertEqual(tab3_new.SimpleAudioVolume.muted, 0)

    def test_debouncer_ignores_one_frame_dip_but_unmutes_at_0_and_2_frames(self) -> None:
        deb = screen_reader.PercentStateDebouncer()
        self.assertEqual(deb.update(48.0, 50.0, True, now=1.0), (False, True))
        self.assertEqual(deb.update(62.0, 50.0, True, now=1.1), (True, True))
        # Single-frame OCR dip from 62% -> 12% -> 63% must not blip audio unmuted
        self.assertEqual(deb.update(12.0, 50.0, True, now=1.15), (True, True))
        self.assertEqual(deb.update(63.0, 50.0, True, now=1.20), (True, True))
        # 2 consecutive frames < threshold unmute smoothly
        self.assertEqual(deb.update(14.0, 50.0, True, now=1.25), (True, True))
        self.assertEqual(deb.update(15.0, 50.0, True, now=1.30), (False, True))

    def test_updater_version_check_download_verify_and_script(self) -> None:
        import hashlib
        import io
        import json
        import tempfile

        self.assertTrue(updater.is_newer_version("v3.0.8", "3.0.7"))
        self.assertFalse(updater.is_newer_version("v3.0.7", "3.0.7"))
        self.assertFalse(updater.is_newer_version("3.0.6", "3.0.7"))

        payload = b"MZ" + b"\x90" * 512
        sha = hashlib.sha256(payload).hexdigest()
        release_json = json.dumps(
            {
                "tag_name": "v9.9.9",
                "html_url": "https://github.com/kodymcmonagle808-cmd/gdquiet/releases/tag/v9.9.9",
                "body": "Test release",
                "assets": [
                    {
                        "name": "GDQuiet.exe",
                        "browser_download_url": "https://github.com/kodymcmonagle808-cmd/gdquiet/releases/download/v9.9.9/GDQuiet.exe",
                        "size": len(payload),
                        "digest": f"sha256:{sha}",
                    }
                ],
            }
        ).encode("utf-8")

        class FakeResp(io.BytesIO):
            def __init__(self, data: bytes) -> None:
                super().__init__(data)
                self.headers = {"Content-Length": str(len(data))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        with patch("urllib.request.urlopen", return_value=FakeResp(release_json)):
            info = updater.check_for_update(current_version="3.0.7")
        self.assertTrue(info.update_available)
        self.assertEqual(info.latest_version, "9.9.9")
        self.assertEqual(info.sha256, sha)

        with tempfile.TemporaryDirectory() as tmp:
            target_exe = Path(tmp) / "GDQuiet.exe"
            with patch("urllib.request.urlopen", return_value=FakeResp(payload)):
                staged = updater.download_update(info, target_exe)
            self.assertTrue(staged.is_file())
            self.assertEqual(staged.read_bytes(), payload)

            bad_info = updater.UpdateInfo(
                current_version=info.current_version,
                latest_version=info.latest_version,
                tag_name=info.tag_name,
                update_available=True,
                download_url=info.download_url,
                release_url=info.release_url,
                asset_name=info.asset_name,
                size_bytes=len(payload),
                sha256="0" * 64,
                release_notes="",
            )
            with patch("urllib.request.urlopen", return_value=FakeResp(payload)):
                with self.assertRaises(updater.UpdateError):
                    updater.download_update(bad_info, target_exe)

            with patch("subprocess.Popen") as mock_popen:
                script_path = updater.launch_update_swap_and_restart(staged, target_exe, current_pid=99999)
                mock_popen.assert_called_once()
                popen_cmd = mock_popen.call_args.args[0]
                self.assertEqual(popen_cmd[0], "powershell.exe")
                self.assertTrue(script_path.is_file())
                self.assertEqual(script_path.suffix, ".ps1")
                script_text = script_path.read_text(encoding="utf-8")
                self.assertIn("Copy-Item", script_text)
                self.assertIn("Start-Process", script_text)
                script_path.unlink(missing_ok=True)
            updater.cleanup_stale_update_files(target_exe)
            self.assertFalse(staged.exists())

    def test_region_presets_and_capture_coords(self) -> None:
        import tkinter as tk
        import gdquiet

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                patch.object(gdquiet, "load_config", return_value=dict(config.DEFAULT_CONFIG)),
                patch.object(gdquiet, "save_config"),
                patch.object(gdquiet, "list_mute_targets", return_value=[]),
                patch.object(gdquiet, "list_open_window_processes", return_value=[]),
                patch.object(gdquiet, "ScreenPercentWatcher"),
            ):
                app = gdquiet.GDQuietApp(root)
                app._apply_region_preset("Top Left", 0.0, 0.0, 0.28, 0.11)
                self.assertEqual(app.cfg["watch_mode"], "custom_region")
                self.assertEqual(app._describe_region_mode(), "Preset: Top Left")
                self.assertEqual(app.summary_capture_btn.cget("text"), "Capture: Top Left (geometrydash.exe)")

                with patch.object(screen_reader, "find_window_client_rect", return_value=(100, 50, 1000, 500)):
                    rect, _label = screen_reader.compute_capture_rect(
                        app.cfg["watch_mode"],
                        app.cfg["target_process"],
                        app.cfg["custom_region"],
                    )
                    self.assertEqual(rect, {"left": 100, "top": 50, "width": 280, "height": 55})
                app._on_close()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_update_banner_shows_and_dismisses_and_pause_guard(self) -> None:
        import tkinter as tk
        import gdquiet

        root = tk.Tk()
        root.withdraw()
        try:
            with (
                patch.object(gdquiet, "load_config", return_value=dict(config.DEFAULT_CONFIG)),
                patch.object(gdquiet, "save_config"),
                patch.object(gdquiet, "list_mute_targets", return_value=[]),
                patch.object(gdquiet, "list_open_window_processes", return_value=[]),
                patch.object(gdquiet, "ScreenPercentWatcher"),
            ):
                app = gdquiet.GDQuietApp(root)
                self.assertEqual(app.update_banner.winfo_manager(), "")

                info1 = updater.UpdateInfo(
                    current_version="3.0.8",
                    latest_version="9.9.9",
                    tag_name="v9.9.9",
                    update_available=True,
                    download_url="https://example.com/GDQuiet.exe",
                    release_url="https://example.com",
                    asset_name="GDQuiet.exe",
                    size_bytes=1024,
                    sha256="a" * 64,
                    release_notes="",
                )
                app._on_update_checked(info1, silent=True)
                self.assertEqual(app.update_banner.winfo_manager(), "pack")
                self.assertIn("9.9.9", app.update_banner_lbl.cget("text"))
                self.assertEqual(app.update_btn.cget("text"), "Install v9.9.9 Update")

                app._dismiss_update_banner()
                self.assertEqual(app.update_banner.winfo_manager(), "")
                # Same version should stay dismissed on background re-check
                app._on_update_checked(info1, silent=True)
                self.assertEqual(app.update_banner.winfo_manager(), "")

                # Newer version shows the banner again
                info2 = updater.UpdateInfo(**{**info1.__dict__, "latest_version": "9.9.10", "tag_name": "v9.9.10"})
                app._on_update_checked(info2, silent=True)
                self.assertEqual(app.update_banner.winfo_manager(), "pack")
                self.assertIn("9.9.10", app.update_banner_lbl.cget("text"))

                # Pause watching guard ignores stale in-flight should_mute=True frame
                with app.cfg_lock:
                    app.cfg["watch_enabled"] = False
                app._on_screen_reading(
                    screen_reader.ScreenReading(
                        percent=75.0,
                        raw_text="75%",
                        should_mute=True,
                        is_playing=True,
                        status_text="Watching",
                        preview_image=None,
                    )
                )
                self.assertFalse(app.current_want_mute)

                app._on_close()
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
