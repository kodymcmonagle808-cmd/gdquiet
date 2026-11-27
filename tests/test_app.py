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
import profiles  # noqa: E402
import screen_reader  # noqa: E402
import updater  # noqa: E402
import window_audio  # noqa: E402


class FakeVolume:
    def __init__(self, muted: int = 0, volume: float = 1.0) -> None:
        self.muted = muted
        self.volume = volume

    def GetMute(self) -> int:
        return self.muted

    def SetMute(self, value: int, _guid) -> None:
        self.muted = int(value)

    def GetMasterVolume(self) -> float:
        return self.volume

    def SetMasterVolume(self, value: float, _guid) -> None:
        self.volume = float(value)


class FakeSession:
    def __init__(
        self,
        name: str,
        pid: int,
        muted: int = 0,
        instance_id: str = "",
        volume: float = 1.0,
    ) -> None:
        self.Process = SimpleNamespace(name=lambda: name, pid=pid)
        self.SimpleAudioVolume = FakeVolume(muted, volume)
        self.InstanceIdentifier = instance_id


class GDQuietTests(unittest.TestCase):
    def setUp(self) -> None:
        import gdquiet

        tray_start_patch = patch.object(gdquiet.GDQuietApp, "_start_tray_icon")
        tray_start_patch.start()
        self.addCleanup(tray_start_patch.stop)

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
            self.assertEqual(ctrl.muted_session_count, 1)

            ctrl.apply(["chrome.exe", "spotify.exe"], mute=False)
            self.assertEqual(chrome.SimpleAudioVolume.muted, 0)
            self.assertEqual(spotify_already_muted.SimpleAudioVolume.muted, 1)
            self.assertEqual(discord.SimpleAudioVolume.muted, 0)
            self.assertEqual(ctrl.muted_session_count, 0)

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

    def test_audio_ducking_restores_original_volume_and_preserves_manual_mutes(self) -> None:
        chrome = FakeSession("chrome.exe", 101, volume=0.8)
        manually_muted = FakeSession("spotify.exe", 202, muted=1, volume=0.6)
        ctrl = window_audio.AudioMuteController()
        with patch.object(
            window_audio.AudioUtilities,
            "GetAllSessions",
            return_value=[chrome, manually_muted],
        ):
            ctrl.apply(["chrome.exe", "spotify.exe"], mute=True, duck_to=25.0)
            self.assertEqual(chrome.SimpleAudioVolume.volume, 0.25)
            self.assertEqual(manually_muted.SimpleAudioVolume.volume, 0.6)
            self.assertEqual(manually_muted.SimpleAudioVolume.muted, 1)
            self.assertTrue(ctrl.is_ducking)
            self.assertFalse(ctrl.is_muted)

            ctrl.apply(["chrome.exe", "spotify.exe"], mute=True, duck_to=40.0)
            self.assertEqual(chrome.SimpleAudioVolume.volume, 0.4)
            ctrl.apply(["chrome.exe", "spotify.exe"], mute=True, duck_to=90.0)
            self.assertEqual(chrome.SimpleAudioVolume.volume, 0.8)

            ctrl.apply([], mute=False)
            self.assertEqual(chrome.SimpleAudioVolume.volume, 0.8)
            self.assertEqual(manually_muted.SimpleAudioVolume.muted, 1)
            self.assertFalse(ctrl.is_ducking)

    def test_audio_fade_ramp_restores_before_threshold_mute(self) -> None:
        spotify = FakeSession("spotify.exe", 111, volume=0.8)
        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[spotify]):
            for target in (70.0, 40.0, 10.0):
                ctrl.apply(["spotify.exe"], mute=True, duck_to=target)
            self.assertEqual(spotify.SimpleAudioVolume.volume, 0.1)

            ctrl.apply(["spotify.exe"], mute=True)
            self.assertEqual(spotify.SimpleAudioVolume.volume, 0.8)
            self.assertEqual(spotify.SimpleAudioVolume.muted, 1)

            ctrl.restore()
            self.assertEqual(spotify.SimpleAudioVolume.volume, 0.8)
            self.assertEqual(spotify.SimpleAudioVolume.muted, 0)

    def test_fade_in_volume_respects_speed_threshold_and_action_volume(self) -> None:
        import gdquiet

        fade = gdquiet.fade_volume_for_percent
        self.assertIsNone(fade(30.0, 50.0, "slow"))
        self.assertIsNone(fade(35.0, 50.0, "slow"))
        self.assertEqual(fade(42.5, 50.0, "slow"), 50.0)
        self.assertEqual(fade(50.0, 50.0, "slow"), None)
        self.assertIsNone(fade(44.0, 50.0, "fast"))
        self.assertEqual(fade(47.5, 50.0, "fast"), 50.0)
        self.assertEqual(fade(47.5, 50.0, "fast", end_volume=20.0), 60.0)
        self.assertIsNone(fade(None, 50.0, "slow"))

    def test_screen_reading_fades_before_threshold_then_applies_mute(self) -> None:
        import threading
        import gdquiet

        app = gdquiet.GDQuietApp.__new__(gdquiet.GDQuietApp)
        app._closing = False
        app.cfg_lock = threading.RLock()
        app.cfg = {
            "watch_enabled": True,
            "remute_interval_s": 0.4,
            "sound_feedback": False,
            "fade_in_mode": "slow",
            "mute_at_percent": 50.0,
            "audio_mode": "mute",
            "ducking_volume": 20.0,
        }
        app._tray_audio_override = None
        app.test_lock = threading.Lock()
        app.test_session = SimpleNamespace(active=False)
        app.current_want_mute = False
        app._current_fade_volume = None
        app._last_audio_enforce_at = 0.0
        app.apply_requests: list[tuple[bool, float | None]] = []
        app._apply_mute_state = lambda muted, **kwargs: app.apply_requests.append(
            (muted, kwargs.get("fade_volume"))
        )
        app.reading_lock = threading.Lock()
        app._ui_reading_pending = False
        app.root = SimpleNamespace(after=lambda *_args: None)

        app._on_screen_reading(
            screen_reader.ScreenReading(
                percent=42.5,
                raw_text="42.5%",
                should_mute=False,
                is_playing=True,
                status_text="Watching",
                preview_image=None,
            )
        )
        app._on_screen_reading(
            screen_reader.ScreenReading(
                percent=50.0,
                raw_text="50%",
                should_mute=True,
                is_playing=True,
                status_text="Watching",
                preview_image=None,
            )
        )
        self.assertEqual(app.apply_requests, [(False, 50.0), (True, None)])

    def test_audio_mode_switch_restores_volume_before_muting(self) -> None:
        spotify = FakeSession("spotify.exe", 111, volume=0.7)
        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[spotify]):
            ctrl.apply(["spotify.exe"], mute=True, duck_to=20.0)
            self.assertEqual(spotify.SimpleAudioVolume.volume, 0.2)
            ctrl.apply(["spotify.exe"], mute=True)
            self.assertEqual(spotify.SimpleAudioVolume.volume, 0.7)
            self.assertEqual(spotify.SimpleAudioVolume.muted, 1)
            ctrl.restore()
            self.assertEqual(spotify.SimpleAudioVolume.muted, 0)

    def test_audio_duck_restore_retries_after_volume_api_failure(self) -> None:
        spotify = FakeSession("spotify.exe", 111, volume=0.7)
        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[spotify]):
            ctrl.apply(["spotify.exe"], mute=True, duck_to=20.0)

        attempts = 0

        def restore_volume(value: float, _guid: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("WASAPI temporarily unavailable")
            spotify.SimpleAudioVolume.volume = value

        with (
            patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[spotify]),
            patch.object(spotify.SimpleAudioVolume, "SetMasterVolume", side_effect=restore_volume),
        ):
            ctrl.restore()
            self.assertEqual(spotify.SimpleAudioVolume.volume, 0.2)
            self.assertTrue(ctrl.is_ducking)
            ctrl.restore()
            self.assertEqual(spotify.SimpleAudioVolume.volume, 0.7)
            self.assertFalse(ctrl.is_ducking)

    def test_level_profile_round_trip_is_local_and_copies_capture_settings(self) -> None:
        config_data = {
            "mute_at_percent": 67.5,
            "selected_apps": ["Spotify.exe", "chrome.exe"],
            "watch_mode": "custom_region",
            "target_process": "geometrydash.exe",
            "custom_region": {"kind": "screen", "left": 2, "top": 3, "width": 40, "height": 20},
            "auto_deafen": True,
        }
        profile = profiles.build_level_profile("Stereo Madness", config_data)
        self.assertEqual(profiles.profile_key("stereo madness"), "stereomadness")
        self.assertEqual(profile["selected_apps"], ["chrome.exe", "spotify.exe"])
        self.assertEqual(profile["custom_region"], config_data["custom_region"])

        updated = profiles.apply_level_profile({"auto_deafen": False}, profile)
        self.assertTrue(updated["auto_deafen"] is False)
        self.assertEqual(updated["mute_at_percent"], 67.5)
        self.assertEqual(updated["custom_region"], config_data["custom_region"])
        profile["custom_region"]["left"] = 99
        self.assertEqual(updated["custom_region"]["left"], 2)

    def test_confirmed_level_profile_saves_and_reapplies_settings(self) -> None:
        import threading
        import gdquiet

        class FakeLabel:
            def __init__(self) -> None:
                self.options: dict[str, object] = {}

            def configure(self, **kwargs: object) -> None:
                self.options.update(kwargs)

        app = gdquiet.GDQuietApp.__new__(gdquiet.GDQuietApp)
        app.cfg_lock = threading.RLock()
        app.cfg = dict(config.DEFAULT_CONFIG)
        app.cfg["level_profiles"] = {}
        app._active_level_name = ""
        app._active_level_key = ""
        app.current_want_mute = False
        app.active_level_profile_lbl = FakeLabel()
        app._save_cfg = lambda: None
        app._sync_level_profile_widgets = lambda: None

        app._activate_level_profile("Stereo Madness", "stereomadness")
        self.assertIn("stereomadness", app.cfg["level_profiles"])
        app.cfg["mute_at_percent"] = 72.0
        app._save_active_level_profile()
        app.cfg["mute_at_percent"] = 40.0
        app._activate_level_profile("Stereo Madness", "stereomadness")
        self.assertEqual(app.cfg["mute_at_percent"], 72.0)

    def test_ocr_diagnostics_flag_missed_reads_and_show_raw_text(self) -> None:
        import gdquiet

        valid = screen_reader.ScreenReading(
            percent=47.5,
            raw_text=" 47.5% ",
            should_mute=False,
            is_playing=True,
            status_text="Watching",
            preview_image=None,
            scan_ms=12.4,
        )
        text, color = gdquiet.format_ocr_diagnostics(valid)
        self.assertIn("12 ms", text)
        self.assertIn("47.5%", text)
        self.assertEqual(color, gdquiet.COLORS["green"])

        missed = screen_reader.ScreenReading(
            percent=None,
            raw_text="garbled text",
            should_mute=False,
            is_playing=True,
            status_text="Watching",
            preview_image=None,
            scan_ms=8.1,
            consecutive_missed_reads=9,
        )
        text, color = gdquiet.format_ocr_diagnostics(missed)
        self.assertIn("9 consecutive scans", text)
        self.assertIn("garbled text", text)
        self.assertEqual(color, gdquiet.COLORS["red"])

        missed_menu = screen_reader.ScreenReading(
            percent=None,
            raw_text="",
            should_mute=False,
            is_playing=False,
            status_text="Watching • no % visible",
            preview_image=None,
            consecutive_missed_reads=10,
        )
        text, color = gdquiet.format_ocr_diagnostics(missed_menu)
        self.assertIn("10 consecutive scans", text)
        self.assertEqual(color, gdquiet.COLORS["amber"])

    def test_tray_action_can_force_unmute_and_then_mute(self) -> None:
        import threading
        import gdquiet

        app = gdquiet.GDQuietApp.__new__(gdquiet.GDQuietApp)
        app.current_want_mute = True
        app._tray_audio_override = None
        app.audio_ctrl = SimpleNamespace(has_active_effect=True)
        app._apply_cond = threading.Condition()
        app._pending_mute = None
        app._force_reapply = False
        app._update_status_indicators = lambda: None

        app._toggle_tray_mute()
        self.assertFalse(app._tray_audio_override)
        self.assertFalse(app.current_want_mute)
        app._toggle_tray_mute()
        self.assertTrue(app._tray_audio_override)
        self.assertTrue(app.current_want_mute)

    def test_default_config_excludes_supabase_keys(self) -> None:
        self.assertNotIn("supabase_url", config.DEFAULT_CONFIG)
        self.assertNotIn("supabase_anon_key", config.DEFAULT_CONFIG)
        self.assertNotIn("session", config.DEFAULT_CONFIG)
        self.assertFalse(config.DEFAULT_CONFIG["level_detection_beta"])
        self.assertEqual(config.DEFAULT_CONFIG["level_name_corrections"], {})

    def test_level_name_correction_is_saved_and_reused_locally(self) -> None:
        import threading
        import gdquiet

        class FakeWidget:
            def __init__(self) -> None:
                self.options: dict[str, object] = {}

            def configure(self, **kwargs: object) -> None:
                self.options.update(kwargs)

            def pack(self, **_kwargs: object) -> None:
                pass

            def pack_forget(self) -> None:
                pass

        app = gdquiet.GDQuietApp.__new__(gdquiet.GDQuietApp)
        app.cfg_lock = threading.RLock()
        app.cfg = {"level_detection_beta": True, "level_name_corrections": {}}
        app._pending_level_raw = "mis-read!"
        app._pending_level_display = "mis-read!"
        app._closing = False
        app._save_cfg = lambda: None
        app.level_detection_status_lbl = FakeWidget()
        app.level_detection_prompt_label = FakeWidget()
        app.level_detection_prompt = FakeWidget()
        app.level_correction_row = FakeWidget()

        app._save_level_detection_feedback("Correct Name")
        self.assertEqual(app.cfg["level_name_corrections"], {"misread": "Correct Name"})

        app._show_level_detection_candidate("MIS READ")
        self.assertEqual(
            app.level_detection_prompt_label.options["text"],
            "Learned match: Correct Name. Is this correct?",
        )
        self.assertEqual(config.DEFAULT_CONFIG["level_name_corrections"], {})

    def test_live_window_enumeration_runs(self) -> None:
        targets = window_audio.list_mute_targets(include_background_audio=True)
        self.assertIsInstance(targets, list)
        for t in targets:
            self.assertNotEqual(t.process_name, "geometrydash.exe")

    def test_compute_capture_rect_uses_top_10_percent(self) -> None:
        with patch.object(screen_reader, "find_window_client_rect", return_value=(100, 50, 1920, 1080)):
            rect, label = screen_reader.compute_capture_rect("auto_window", "geometrydash.exe", None)
            self.assertEqual(label, "geometrydash.exe")
            self.assertEqual(rect, {"left": 100, "top": 50, "width": 1920, "height": 129})

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

    def test_level_name_detection_requires_pause_menu_text(self) -> None:
        self.assertIsNone(screen_reader.extract_level_name_from_pause_text("Stereo Madness\n67%"))
        self.assertEqual(
            screen_reader.extract_level_name_from_pause_text(
                "Stereo Madness\nAttempts: 12\nResume\nRestart\nPractice"
            ),
            "Stereo Madness",
        )
        self.assertIsNone(
            screen_reader.extract_level_name_from_pause_text("Paused\nResume\nRestart\nPractice\nAttempts: 12")
        )

    def test_level_name_tracker_requires_stable_candidate_and_rearms(self) -> None:
        tracker = screen_reader.LevelNameTracker()
        self.assertIsNone(tracker.update("Stereo Madness"))
        self.assertEqual(tracker.update("stereo madness"), "stereo madness")
        self.assertIsNone(tracker.update("Stereo Madness"))
        self.assertIsNone(tracker.update(None))
        self.assertIsNone(tracker.update("Stereo Madness"))
        self.assertEqual(tracker.update("Stereo Madness"), "Stereo Madness")
        self.assertIsNone(tracker.update("Deadlocked"))
        self.assertEqual(tracker.update("Deadlocked"), "Deadlocked")
        self.assertIsNone(tracker.update("Stereo Madness"))
        self.assertEqual(tracker.update("Stereo Madness"), "Stereo Madness")

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

    def test_percent_debouncer_keeps_threshold_confirmation_across_brief_ocr_gap(self) -> None:
        deb = screen_reader.PercentStateDebouncer(missing_timeout_s=0.6)
        self.assertEqual(deb.update(20.0, 50.0, True, now=1.0), (False, True))
        self.assertEqual(deb.update(75.0, 50.0, True, now=1.1), (False, True))
        self.assertEqual(deb.update(None, 50.0, True, now=1.2), (False, True))
        self.assertEqual(deb.update(76.0, 50.0, True, now=1.3), (True, True))

    def test_percent_debouncer_reports_unmuted_after_missing_timeout(self) -> None:
        deb = screen_reader.PercentStateDebouncer(missing_timeout_s=0.6)
        self.assertEqual(deb.update(20.0, 50.0, True, now=1.0), (False, True))
        self.assertEqual(deb.update(51.0, 50.0, True, now=1.1), (False, True))
        self.assertEqual(deb.update(52.0, 50.0, True, now=1.2), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=1.8), (False, False))

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

                with app.cfg_lock:
                    app.cfg["audio_mode"] = "duck"
                app._update_sidebar_badges()
                self.assertEqual(app.sidebar_thresh_lbl.cget("text"), "Ducks at 50%")
                self.assertEqual(app._nav_buttons["apps"][2].cget("text"), "Apps to Duck (0)")
                self.assertEqual(app.apps_page_heading_lbl.cget("text"), "Apps to Duck")
                self.assertEqual(app.reset_mute_btn.cget("text"), "Restore Audio & Undeafen")
                with app.cfg_lock:
                    app.cfg["audio_mode"] = "mute"
                app._update_sidebar_badges()

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

    def test_discord_restore_uses_hotkey_that_deafened(self) -> None:
        original_hotkey = {"modifiers": ["ctrl", "shift"], "vk": ord("D"), "label": "Ctrl + Shift + D"}
        changed_hotkey = {"modifiers": ["alt"], "vk": ord("F"), "label": "Alt + F"}
        ctrl = discord_hotkey.DiscordDeafenController(toggle_cooldown_s=0)

        with patch.object(discord_hotkey, "send_hotkey", return_value=True) as mock_send:
            ctrl.set_deafened(True, original_hotkey, True)
            ctrl.set_deafened(False, changed_hotkey, True)

        self.assertEqual(mock_send.call_args_list[0].args[0], original_hotkey)
        self.assertEqual(mock_send.call_args_list[1].args[0], original_hotkey)
        self.assertFalse(ctrl.deafened_by_us)

    def test_discord_restore_uses_stored_hotkey_when_cleared_or_disabled(self) -> None:
        original_hotkey = {"modifiers": ["ctrl", "shift"], "vk": ord("D"), "label": "Ctrl + Shift + D"}
        ctrl = discord_hotkey.DiscordDeafenController(toggle_cooldown_s=0)

        with patch.object(discord_hotkey, "send_hotkey", return_value=True) as mock_send:
            ctrl.set_deafened(True, original_hotkey, True)
            ctrl.set_deafened(False, None, False)

        self.assertEqual(mock_send.call_args_list[1].args[0], original_hotkey)
        self.assertFalse(ctrl.deafened_by_us)

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
        self.assertTrue(updater.is_newer_version("v4.0.0-beta.1", "3.1.8"))
        self.assertTrue(updater.is_newer_version("v4.0.0-beta.2", "v4.0.0-beta.1"))
        self.assertTrue(updater.is_newer_version("v4.0.0", "v4.0.0-beta.1"))
        self.assertTrue(updater.is_newer_version("v4.0.0", "v4.0.0-beta.2"))
        self.assertFalse(updater.is_newer_version("v4.0.0-beta.1", "v4.0.0"))

        # Setup Wizard asset should be selected cleanly and marked as an installer
        only_setup = updater._select_exe_asset(
            [{"name": "GDQuiet-Setup.exe", "browser_download_url": "https://example.com/setup.exe"}]
        )
        self.assertIsNotNone(only_setup)
        self.assertEqual(only_setup["name"], "GDQuiet-Setup.exe")
        chosen = updater._select_exe_asset(
            [
                {"name": "GDQuiet.exe", "browser_download_url": "https://example.com/GDQuiet.exe"},
                {"name": "GDQuiet-Setup.exe", "browser_download_url": "https://example.com/setup.exe"},
            ]
        )
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["name"], "GDQuiet-Setup.exe")

        payload = b"MZ" + b"\x90" * 512
        sha = hashlib.sha256(payload).hexdigest()
        release_json = json.dumps(
            {
                "tag_name": "v9.9.9",
                "html_url": "https://github.com/kodymcmonagle808-cmd/gdquiet/releases/tag/v9.9.9",
                "body": "Test release",
                "assets": [
                    {
                        "name": "GDQuiet-Setup.exe",
                        "browser_download_url": "https://github.com/kodymcmonagle808-cmd/gdquiet/releases/download/v9.9.9/GDQuiet-Setup.exe",
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
        self.assertTrue(info.is_installer)

        stable_release = {
            "tag_name": "v3.1.8",
            "html_url": "https://github.com/kodymcmonagle808-cmd/gdquiet/releases/tag/v3.1.8",
            "body": "Stable",
            "assets": [],
            "draft": False,
            "prerelease": False,
        }
        beta_release = {
            "tag_name": "v4.0.0-beta.1",
            "html_url": "https://github.com/kodymcmonagle808-cmd/gdquiet/releases/tag/v4.0.0-beta.1",
            "body": "Beta",
            "assets": json.loads(release_json)["assets"],
            "draft": False,
            "prerelease": True,
        }
        with patch("urllib.request.urlopen", return_value=FakeResp(json.dumps([beta_release, stable_release]).encode())):
            beta_info = updater.check_for_update(current_version="3.1.8", include_prereleases=True)
        self.assertEqual(beta_info.latest_version, "4.0.0-beta.1")
        self.assertTrue(beta_info.update_available)

        with tempfile.TemporaryDirectory() as tmp:
            target_exe = Path(tmp) / "GDQuiet.exe"
            with patch("urllib.request.urlopen", return_value=FakeResp(payload)):
                staged = updater.download_update(info, target_exe)
            try:
                self.assertTrue(staged.is_file())
                self.assertEqual(staged.name, "GDQuiet-Setup-9.9.9.exe")
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
                    is_installer=True,
                )
                with patch("urllib.request.urlopen", return_value=FakeResp(payload)):
                    with self.assertRaises(updater.UpdateError):
                        updater.download_update(bad_info, target_exe)

                with patch("subprocess.Popen") as mock_popen:
                    script_path = updater.launch_update_swap_and_restart(
                        staged, target_exe, current_pid=99999, is_installer=True
                    )
                    mock_popen.assert_called_once()
                    popen_cmd = mock_popen.call_args.args[0]
                    self.assertEqual(popen_cmd[0], "powershell.exe")
                    self.assertTrue(script_path.is_file())
                    self.assertEqual(script_path.suffix, ".ps1")
                    script_text = script_path.read_text(encoding="utf-8")
                    self.assertIn("Start-Process", script_text)
                    self.assertIn("-WindowStyle Normal -Wait", script_text)
                    script_path.unlink(missing_ok=True)
            finally:
                staged.unlink(missing_ok=True)

            stale = target_exe.with_name(".GDQuiet.exe.3.0.7.update")
            stale.write_bytes(b"stale")
            updater.cleanup_stale_update_files(target_exe)
            self.assertFalse(stale.exists())

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

    def test_logo_and_icon_assets_load_in_app(self) -> None:
        import tkinter as tk
        from PIL import Image
        import gdquiet

        repo_root = Path(__file__).resolve().parent.parent
        logo_path = repo_root / "logo.png"
        ico_path = repo_root / "app" / "assets" / "gdquiet.ico"
        self.assertTrue(logo_path.is_file())
        self.assertTrue(ico_path.is_file())
        with Image.open(logo_path) as im:
            self.assertEqual(im.size, (256, 256))
            self.assertEqual(im.mode, "RGBA")

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
                self.assertIsNotNone(app._sidebar_logo_photo)
                self.assertIsNotNone(app._window_icon_photo)
                self.assertFalse(app.beta_updates_var.get())
                self.assertFalse(app.level_detection_var.get())
                self.assertIn("settings", app._pages)
                self.assertIn("settings", app._nav_buttons)
                app._show_page("settings")
                self.assertEqual(app._active_page, "settings")
                app._on_close()
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_audio_restore_and_expanded_terms(self) -> None:
        fake_session = FakeSession("spotify.exe", 111, muted=0, instance_id="inst1")
        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[fake_session]):
            ctrl.apply(["spotify.exe"], True)
            self.assertEqual(fake_session.SimpleAudioVolume.muted, 1)
            self.assertTrue(ctrl.is_muted)
            ctrl.restore()
            self.assertEqual(fake_session.SimpleAudioVolume.muted, 0)
            self.assertFalse(ctrl.is_muted)

        terms_path = Path(__file__).resolve().parent.parent / "installer" / "TERMS.txt"
        self.assertTrue(terms_path.is_file())
        terms_text = terms_path.read_text(encoding="utf-8")
        self.assertGreater(len(terms_text), 3000)

    def test_audio_restore_keeps_tracking_when_session_enumeration_fails(self) -> None:
        fake_session = FakeSession("spotify.exe", 111, muted=0, instance_id="inst1")
        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[fake_session]):
            ctrl.apply(["spotify.exe"], True)
        with patch.object(
            window_audio.AudioUtilities,
            "GetAllSessions",
            side_effect=[RuntimeError("WASAPI unavailable"), [fake_session]],
        ):
            ctrl.restore()
            self.assertEqual(fake_session.SimpleAudioVolume.muted, 1)
            self.assertTrue(ctrl.is_muted)
            ctrl.restore()
            self.assertEqual(fake_session.SimpleAudioVolume.muted, 0)
            self.assertFalse(ctrl.is_muted)

    def test_audio_restore_keeps_tracking_when_session_unmute_fails(self) -> None:
        fake_session = FakeSession("spotify.exe", 111, muted=0, instance_id="inst1")
        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[fake_session]):
            ctrl.apply(["spotify.exe"], True)

        attempts = 0

        def fail_first_unmute(value: int, _guid: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("WASAPI temporarily unavailable")
            fake_session.SimpleAudioVolume.muted = value

        with (
            patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[fake_session]),
            patch.object(fake_session.SimpleAudioVolume, "SetMute", side_effect=fail_first_unmute),
        ):
            ctrl.restore()
            self.assertEqual(fake_session.SimpleAudioVolume.muted, 1)
            self.assertTrue(ctrl.is_muted)
            ctrl.restore()
            self.assertEqual(fake_session.SimpleAudioVolume.muted, 0)
            self.assertFalse(ctrl.is_muted)

    def test_apply_worker_retries_failed_audio_restore(self) -> None:
        import threading
        import time
        import gdquiet

        class FakeAudioController:
            def __init__(self) -> None:
                self.attempts = 0
                self.is_muted = True

            @property
            def has_active_effect(self) -> bool:
                return self.is_muted

            def apply(self, _selected: list[str], mute: bool, duck_to: float | None = None) -> None:
                if not mute:
                    self.attempts += 1
                    if self.attempts >= 2:
                        self.is_muted = False

        class FakeDiscordController:
            def set_deafened(self, **_kwargs: object) -> None:
                pass

        app = gdquiet.GDQuietApp.__new__(gdquiet.GDQuietApp)
        app._apply_cond = threading.Condition()
        app._pending_mute = False
        app._pending_fade_volume = None
        app._force_reapply = True
        app._apply_stop = False
        app.apply_lock = threading.Lock()
        app.cfg_lock = threading.RLock()
        app.cfg = {"selected_apps": [], "deafen_hotkey": None, "auto_deafen": False}
        app.audio_ctrl = FakeAudioController()
        app.discord_ctrl = FakeDiscordController()
        app.root = SimpleNamespace(after=lambda *_args: None)
        worker = threading.Thread(target=app._apply_worker, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 2.0
            while app.audio_ctrl.is_muted and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(app.audio_ctrl.is_muted)
            self.assertEqual(app.audio_ctrl.attempts, 2)
        finally:
            with app._apply_cond:
                app._apply_stop = True
                app._apply_cond.notify_all()
            worker.join(timeout=1.0)

    def test_web_ui_has_no_presets_and_wires_actions(self) -> None:
        import web_ui

        repo_root = Path(__file__).resolve().parent.parent
        html = (repo_root / "app" / "ui" / "index.html").read_text(encoding="utf-8")
        js = (repo_root / "app" / "ui" / "app.js").read_text(encoding="utf-8")

        # Ensure mockup preset values were removed from the HTML/JS templates
        for preset_marker in (
            "@Jacob",
            "Extreme Demon Gacha",
            "GEODE PRE-BUILD",
            "Kody McMonagle",
            "4 apps selected",
            "59% THRESHOLD",
            "v4.0.0",
        ):
            self.assertNotIn(preset_marker, html)
            self.assertNotIn(preset_marker, js)

        with (
            patch.object(web_ui, "load_config", return_value=dict(config.DEFAULT_CONFIG)),
            patch.object(web_ui, "save_config"),
            patch.object(web_ui, "list_mute_targets", return_value=[]),
            patch.object(web_ui, "list_open_window_processes", return_value=["geometrydash.exe"]),
            patch.object(web_ui, "ScreenPercentWatcher") as mock_watcher,
        ):
            app = web_ui.GDQuietWebApp()
            try:
                mock_watcher.return_value.start.assert_called_once()
                api = web_ui.WebApi(app)
                st = api.get_state()
                self.assertEqual(st["cfg"]["mute_at_percent"], 50.0)
                self.assertEqual(st["cfg"]["selected_apps"], [])
                self.assertIsNone(st["cfg"]["deafen_hotkey"])
                self.assertIsNone(st["cfg"]["watch_toggle_hotkey"])
                self.assertEqual(st["app_version"], updater.APP_VERSION)

                st = api.set_threshold(72)
                self.assertEqual(st["cfg"]["mute_at_percent"], 72.0)

                st = api.toggle_app("discord.exe")
                self.assertEqual(st["cfg"]["selected_apps"], ["discord.exe"])

                st = api.set_region_preset("Top Left")
                self.assertEqual(st["cfg"]["watch_mode"], "custom_region")
                self.assertEqual(st["region_description"], "Preset: Top Left")

                st = api.toggle_screen_test()
                self.assertTrue(st["test"]["active"])
                st = api.toggle_screen_test()
                self.assertFalse(st["test"]["active"])
                self.assertTrue(st["test"]["completed"])

                st = api.reset_defaults()
                self.assertEqual(st["cfg"]["mute_at_percent"], config.DEFAULT_CONFIG["mute_at_percent"])
                self.assertEqual(st["cfg"]["selected_apps"], [])
            finally:
                app._cleanup_sync()

    def test_downgrade_to_stable_from_beta_and_rate_limit_fallback(self) -> None:
        import io
        import json
        import urllib.error
        import web_ui

        self.assertTrue(updater.is_prerelease_version("4.4.0-beta.1"))
        self.assertFalse(updater.is_prerelease_version("4.3.2"))

        stable_payload = {
            "tag_name": "v4.3.2",
            "html_url": "https://github.com/kodymcmonagle808-cmd/gdquiet/releases/tag/v4.3.2",
            "body": "Stable",
            "assets": [
                {
                    "name": "GDQuiet-Setup.exe",
                    "browser_download_url": "https://github.com/kodymcmonagle808-cmd/gdquiet/releases/download/v4.3.2/GDQuiet-Setup.exe",
                    "size": 1024,
                }
            ],
        }

        class FakeResp(io.BytesIO):
            def __init__(self, data: bytes) -> None:
                super().__init__(data)
                self.headers = {"Content-Length": str(len(data))}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        with patch("urllib.request.urlopen", return_value=FakeResp(json.dumps(stable_payload).encode())):
            stable_info = updater.check_latest_stable_release(current_version="4.4.0-beta.1")
        self.assertEqual(stable_info.latest_version, "4.3.2")
        self.assertFalse(stable_info.update_available)
        self.assertTrue(stable_info.is_downgrade)

        # Verify HTTP 403 rate-limit fallback for stable releases
        http_403 = urllib.error.HTTPError(
            updater.RELEASES_API_URL, 403, "rate limit exceeded", {}, None
        )
        web_json = json.dumps({"tag_name": "v4.3.2"}).encode()
        with patch("urllib.request.urlopen", side_effect=[http_403, FakeResp(web_json)]):
            fallback_info = updater.check_for_update(current_version="4.0.0", include_prereleases=False)
        self.assertTrue(fallback_info.update_available)
        self.assertEqual(fallback_info.latest_version, "4.3.2")
        self.assertTrue(fallback_info.download_url.endswith("/v4.3.2/GDQuiet-Setup.exe"))

        with (
            patch.object(web_ui, "APP_VERSION", "4.4.0-beta.1"),
            patch.object(web_ui, "load_config", return_value=dict(config.DEFAULT_CONFIG, beta_updates=False)),
            patch.object(web_ui, "save_config"),
            patch.object(web_ui, "list_mute_targets", return_value=[]),
            patch.object(web_ui, "list_open_window_processes", return_value=["geometrydash.exe"]),
            patch.object(web_ui, "ScreenPercentWatcher"),
            patch.object(web_ui, "check_for_update", return_value=stable_info),
            patch.object(web_ui, "check_latest_stable_release", return_value=stable_info),
            patch("webbrowser.open") as mock_open,
        ):
            app = web_ui.GDQuietWebApp()
            try:
                app._check_updates_sync(silent=False)
                st = app.get_state()
                self.assertTrue(st["is_beta_build"])
                self.assertEqual(st["latest_stable"]["version"], "4.3.2")
                self.assertTrue(st["latest_stable"]["is_downgrade"])
                self.assertTrue(st["show_downgrade_banner"])

                st = app.downgrade_to_stable()
                mock_open.assert_called_once_with(stable_info.release_url)
                self.assertIn("4.3.2", st["footer_message"])
            finally:
                app._cleanup_sync()


if __name__ == "__main__":
    unittest.main()
