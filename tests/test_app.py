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
import supabase_client  # noqa: E402
import window_audio  # noqa: E402


class FakeVolume:
    def __init__(self, muted: int = 0) -> None:
        self.muted = muted

    def GetMute(self) -> int:
        return self.muted

    def SetMute(self, value: int, _guid) -> None:
        self.muted = int(value)


class FakeSession:
    def __init__(self, name: str, pid: int, muted: int = 0) -> None:
        self.Process = SimpleNamespace(name=lambda: name, pid=pid)
        self.SimpleAudioVolume = FakeVolume(muted)


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

    def test_pkce_pair(self) -> None:
        verifier, challenge = supabase_client._pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotEqual(verifier, challenge)
        self.assertNotIn("=", challenge)

    def test_supabase_headers_support_publishable_and_jwt_keys(self) -> None:
        pub_client = supabase_client.SupabaseClient("https://x.supabase.co", "sb_publishable_abc123")
        self.assertNotIn("Authorization", pub_client._headers())
        self.assertEqual(pub_client._headers("user-jwt")["Authorization"], "Bearer user-jwt")

        jwt_client = supabase_client.SupabaseClient("https://x.supabase.co", "eyJ_legacy_anon")
        self.assertEqual(jwt_client._headers()["Authorization"], "Bearer eyJ_legacy_anon")

    def test_resolve_supabase_credentials_precedence(self) -> None:
        with patch.dict("os.environ", {"SUPABASE_URL": "https://env.supabase.co/", "SUPABASE_ANON_KEY": "env-key"}):
            url, key = config.resolve_supabase_credentials({"supabase_url": "", "supabase_anon_key": ""})
            self.assertEqual(url, "https://env.supabase.co")
            self.assertEqual(key, "env-key")

            url2, key2 = config.resolve_supabase_credentials(
                {"supabase_url": "https://cfg.supabase.co/", "supabase_anon_key": "cfg-key"}
            )
            self.assertEqual(url2, "https://cfg.supabase.co")
            self.assertEqual(key2, "cfg-key")

    def test_live_window_enumeration_runs(self) -> None:
        targets = window_audio.list_mute_targets(include_background_audio=True)
        self.assertIsInstance(targets, list)
        for t in targets:
            self.assertNotEqual(t.process_name, "geometrydash.exe")

    def test_parse_percent_variants(self) -> None:
        self.assertEqual(screen_reader.parse_percent_from_text("50%"), 50.0)
        self.assertEqual(screen_reader.parse_percent_from_text("50.24%"), 50.24)
        self.assertEqual(screen_reader.parse_percent_from_text("490/0"), 49.0)
        self.assertEqual(screen_reader.parse_percent_from_text("1000/0"), 100.0)
        self.assertEqual(screen_reader.parse_percent_from_text("542%"), 54.2)
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
                for page_key in ("apps", "discord", "capture", "dashboard"):
                    app._show_page(page_key)
                app._toggle_watching()
                app._toggle_watching()
                app._save_mute_percent()
                app._on_close()
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
