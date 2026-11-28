from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import common  # noqa: E402
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


@contextmanager
def web_app(cfg: dict | None = None, **extra_patches):
    """A GDQuietWebApp with config, audio enumeration, the screen watcher and the
    tray icon stubbed out. Yields (app, api)."""
    import web_ui

    with (
        patch.object(web_ui, "load_config", return_value=dict(cfg or config.DEFAULT_CONFIG)),
        patch.object(web_ui, "save_config"),
        patch.object(web_ui, "list_mute_targets", return_value=[]),
        patch.object(web_ui, "list_open_window_processes", return_value=["geometrydash.exe"]),
        patch.object(web_ui, "ScreenPercentWatcher"),
        patch.object(web_ui.DiscordAuth, "load"),
    ):
        app = web_ui.GDQuietWebApp()
        try:
            yield app, web_ui.WebApi(app)
        finally:
            app._cleanup_sync()


def reading(percent, should_mute, **kw) -> "screen_reader.ScreenReading":
    return screen_reader.ScreenReading(
        percent=percent,
        raw_text="" if percent is None else f"{percent}%",
        should_mute=should_mute,
        is_playing=percent is not None,
        status_text="Watching",
        preview_image=None,
        **kw,
    )


def wait_until(predicate, timeout: float = 2.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class GDQuietTests(unittest.TestCase):
    def setUp(self) -> None:
        import web_ui

        tray_start_patch = patch.object(web_ui.GDQuietWebApp, "_start_tray_icon")
        tray_start_patch.start()
        self.addCleanup(tray_start_patch.stop)

    def test_input_struct_size_matches_win32_abi(self) -> None:
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(discord_hotkey.INPUT), expected)

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
        fade = common.fade_volume_for_percent
        self.assertIsNone(fade(30.0, 50.0, "slow"))
        self.assertIsNone(fade(35.0, 50.0, "slow"))
        self.assertEqual(fade(42.5, 50.0, "slow"), 50.0)
        self.assertEqual(fade(50.0, 50.0, "slow"), None)
        self.assertIsNone(fade(44.0, 50.0, "fast"))
        self.assertEqual(fade(47.5, 50.0, "fast"), 50.0)
        self.assertEqual(fade(47.5, 50.0, "fast", end_volume=20.0), 60.0)
        self.assertIsNone(fade(None, 50.0, "slow"))

    def test_screen_reading_fades_before_threshold_then_applies_mute(self) -> None:
        cfg = dict(config.DEFAULT_CONFIG, fade_in_mode="slow", mute_at_percent=50.0, audio_mode="mute")
        with web_app(cfg) as (app, _api):
            requests: list[tuple[bool, float | None]] = []
            app._apply_mute_state = lambda muted, **kw: requests.append((muted, kw.get("fade_volume")))
            app._on_screen_reading(reading(42.5, False))
            app._on_screen_reading(reading(50.0, True))
            self.assertEqual(requests, [(False, 50.0), (True, None)])

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

    def test_ocr_diagnostics_flag_missed_reads_and_show_raw_text(self) -> None:
        valid = screen_reader.ScreenReading(
            percent=47.5,
            raw_text=" 47.5% ",
            should_mute=False,
            is_playing=True,
            status_text="Watching",
            preview_image=None,
            scan_ms=12.4,
        )
        text, color = common.format_ocr_diagnostics(valid)
        self.assertIn("12 ms", text)
        self.assertIn("47.5%", text)
        self.assertEqual(color, common.COLORS["green"])

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
        text, color = common.format_ocr_diagnostics(missed)
        self.assertIn("9 consecutive scans", text)
        self.assertIn("garbled text", text)
        self.assertEqual(color, common.COLORS["red"])

        missed_menu = screen_reader.ScreenReading(
            percent=None,
            raw_text="",
            should_mute=False,
            is_playing=False,
            status_text="Watching • no % visible",
            preview_image=None,
            consecutive_missed_reads=10,
        )
        text, color = common.format_ocr_diagnostics(missed_menu)
        self.assertIn("10 consecutive scans", text)
        self.assertEqual(color, common.COLORS["amber"])

    def test_default_config_excludes_supabase_keys(self) -> None:
        self.assertNotIn("supabase_url", config.DEFAULT_CONFIG)
        self.assertNotIn("supabase_anon_key", config.DEFAULT_CONFIG)
        self.assertNotIn("session", config.DEFAULT_CONFIG)
        self.assertFalse(config.DEFAULT_CONFIG["level_label_enabled"])
        self.assertIsNone(config.DEFAULT_CONFIG["level_label_region"])
        self.assertNotIn("level_detection_beta", config.DEFAULT_CONFIG)

    def test_load_config_drops_removed_level_guessing_keys(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"level_detection_beta": True, "level_name_corrections": {"a": "b"}, "mute_at_percent": 61}),
                encoding="utf-8",
            )
            with patch.object(config, "config_path", return_value=path):
                loaded = config.load_config()
        self.assertNotIn("level_detection_beta", loaded)
        self.assertNotIn("level_name_corrections", loaded)
        self.assertEqual(loaded["mute_at_percent"], 61)

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

    def test_level_name_from_eclipse_label_skips_stat_labels(self) -> None:
        extract = screen_reader.extract_level_name_from_label_text
        self.assertEqual(extract("Stereo Madness"), "Stereo Madness")
        self.assertEqual(extract("FPS: 240\nCPS: 4/9\nBloodbath by Riot\nAttempt 23\n12:41:07"), "Bloodbath by Riot")
        self.assertEqual(extract("Level: Deadlocked\nNoclip 98.5%"), "Deadlocked")
        self.assertEqual(extract("Testmode\nTime Machine"), "Time Machine")
        self.assertIsNone(extract("FPS 240\nAttempts: 12\n67%\n00:31"))
        self.assertIsNone(extract(""))

    def test_level_label_region_defaults_to_left_30_percent(self) -> None:
        with patch.object(screen_reader, "find_window_client_rect", return_value=(100, 50, 1920, 1080)):
            rect, _label = screen_reader.compute_region_rect(
                screen_reader.DEFAULT_LEVEL_LABEL_REGION, "geometrydash.exe"
            )
        self.assertEqual(rect, {"left": 100, "top": 50, "width": 576, "height": 1080})

    def test_level_name_tracker_requires_stable_candidate_and_ignores_gaps(self) -> None:
        tracker = screen_reader.LevelNameTracker()
        self.assertIsNone(tracker.update("Stereo Madness"))
        self.assertEqual(tracker.update("stereo madness"), "stereo madness")
        self.assertIsNone(tracker.update("Stereo Madness"))
        # A missed OCR read mid-level must not re-trigger the same level.
        self.assertIsNone(tracker.update(None))
        self.assertIsNone(tracker.update("Stereo Madness"))
        # Close misreads count as the same level.
        self.assertIsNone(tracker.update("Stere0 Madnes"))
        self.assertIsNone(tracker.update("Stereo Madness"))
        # A different level needs 2 reads; switching back is reported again.
        self.assertIsNone(tracker.update("Deadlocked"))
        self.assertEqual(tracker.update("Deadlocked"), "Deadlocked")
        self.assertIsNone(tracker.update("Deadlocked"))
        self.assertIsNone(tracker.update("Deadlocked"))
        self.assertIsNone(tracker.update("Deadlocked"))
        self.assertIsNone(tracker.update("Stereo Madness"))
        self.assertEqual(tracker.update("Stereo Madness"), "Stereo Madness")
        tracker.reset()
        self.assertIsNone(tracker.update("Stereo Madness"))
        self.assertEqual(tracker.update("Stereo Madness"), "Stereo Madness")

    def test_level_name_tracker_tolerates_gaps_and_picks_common_spelling(self) -> None:
        tracker = screen_reader.LevelNameTracker()
        self.assertIsNone(tracker.update("clubstep sii@ttpos"))
        self.assertIsNone(tracker.update(None))
        self.assertIsNone(tracker.update("Attempt garbage"))
        reported = tracker.update("clubstep startpos")
        # 2 of the last 5 reads agree, so it's reported despite the gap.
        self.assertIn(reported, {"clubstep startpos", "clubstep sii@ttpos"})
        tracker.reset()
        tracker.update("clubstep startpos")
        tracker.update("clubstep sii@ttpos")
        self.assertIsNone(tracker.update("clubstep startpos"))

    def test_pick_level_name_from_real_eclipse_overlay(self) -> None:
        # OCR lines (text, left x) from a real 1920x1080 frame: Eclipse labels at x~11,
        # another mod's friend list at x~74, and the huge in-game "Attempt 180" text.
        line = screen_reader.OcrLine
        frame = [
            line("FPS: 240", 12),
            line("Attematq 4)", 10.5),
            line("-5\ufffd.550/0", 0),
            line("Best run: 57.914951150/0", 12),
            line("0/0/0 CPS", 11),
            line("clubstep startpos", 11.5),
            line("bread", 74.5),
            line("Cous", 74.5),
            line("LIVE", 200),
        ]
        self.assertEqual(screen_reader.pick_level_name(frame, align_tolerance=11.5), "clubstep startpos")
        # Name unreadable this frame: don't fall back to another overlay's text.
        without_name = [ln for ln in frame if ln.text != "clubstep startpos"]
        self.assertIsNone(screen_reader.pick_level_name(without_name, align_tolerance=11.5))
        # Garbled big "Attempt" text is a stat, not a level name.
        for garbled in ("At+emptl", "AtternpU", "Atiternp", "Atternat", "At+ernztl"):
            self.assertIsNone(
                screen_reader.pick_level_name([line("FPS: 240", 12), line(garbled, 10)], align_tolerance=11.5),
                garbled,
            )
        for stat in ("Bestrun;A7v91-95.159/6", "cess. 240", "o/o/o\ufffdpS", "O/O/OCPS", "From: 57%"):
            self.assertIsNone(screen_reader.extract_level_name_from_label_text(stat), stat)
        # A box drawn tightly around just the label has no stat lines to align to.
        self.assertEqual(screen_reader.pick_level_name([line("clubstep startpos", 3)]), "clubstep startpos")

    def test_ocr_misreads_snap_to_saved_level(self) -> None:
        saved = {
            "clubstepstartpos": {"name": "clubstep startpos"},
            "deadlocked": {"name": "Deadlocked"},
        }
        self.assertEqual(profiles.find_matching_profile(saved, "clubstep sii@ttpos"), "clubstepstartpos")
        self.assertEqual(profiles.find_matching_profile(saved, "c!ubstep startpos"), "clubstepstartpos")
        self.assertEqual(profiles.find_matching_profile(saved, "DEADLOCKED"), "deadlocked")
        self.assertIsNone(profiles.find_matching_profile(saved, "Bloodbath"))
        self.assertIsNone(profiles.find_matching_profile(saved, ""))

    def test_winrt_ocr_reads_level_name_from_stacked_eclipse_labels(self) -> None:
        ocr = screen_reader.WinRtPercentOcr()
        try:
            if not ocr.available():
                self.skipTest("Windows OCR engine unavailable")
            try:
                font = ImageFont.truetype("arialbd.ttf", 26)
            except OSError:
                self.skipTest("Arial Bold unavailable")
            img = Image.new("RGB", (576, 400), (40, 70, 160))
            draw = ImageDraw.Draw(img)
            for i, text in enumerate(["FPS: 240", "CPS: 4/9", "Stereo Madness", "Attempt 12"]):
                draw.text((12, 12 + i * 36), text, font=font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0))
            raw = ocr.recognize_lines(img.convert("RGBA"))
            self.assertGreaterEqual(raw.count("\n"), 2, raw)
            self.assertEqual(screen_reader.extract_level_name_from_label_text(raw), "Stereo Madness")
        finally:
            ocr.close()

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
        # 0% death also needs 2 consecutive frames (avoids 1-frame OCR glitch)
        self.assertEqual(deb.update(0.0, 50.0, True, now=1.2), (True, True))
        self.assertEqual(deb.update(0.0, 50.0, True, now=1.25), (False, True))
        # Jump from 0 -> 88 now mutes instantly (muting is always 1-frame)
        self.assertEqual(deb.update(88.0, 50.0, True, now=1.3), (True, True))
        self.assertEqual(deb.update(12.0, 50.0, True, now=1.4), (True, True))
        # Already muted, stays muted
        self.assertEqual(deb.update(60.0, 50.0, True, now=1.5), (True, True))
        self.assertEqual(deb.update(61.0, 50.0, True, now=1.6), (True, True))
        # Reaching 100% unmutes if enabled
        self.assertEqual(deb.update(100.0, 50.0, True, now=1.7), (False, True))
        # Missing percent past timeout unmutes
        self.assertEqual(deb.update(62.0, 50.0, True, now=2.0), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=2.2), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=3.2), (False, False))

    def test_percent_debouncer_keeps_threshold_confirmation_across_brief_ocr_gap(self) -> None:
        deb = screen_reader.PercentStateDebouncer(missing_timeout_s=0.6)
        self.assertEqual(deb.update(20.0, 50.0, True, now=1.0), (False, True))
        # Above threshold mutes immediately now (1-frame)
        self.assertEqual(deb.update(75.0, 50.0, True, now=1.1), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=1.2), (True, True))
        self.assertEqual(deb.update(76.0, 50.0, True, now=1.3), (True, True))

    def test_percent_debouncer_reports_unmuted_after_missing_timeout(self) -> None:
        deb = screen_reader.PercentStateDebouncer(missing_timeout_s=0.6)
        self.assertEqual(deb.update(20.0, 50.0, True, now=1.0), (False, True))
        # Above threshold mutes immediately now (1-frame)
        self.assertEqual(deb.update(51.0, 50.0, True, now=1.1), (True, True))
        self.assertEqual(deb.update(52.0, 50.0, True, now=1.2), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=2.3), (False, False))

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

    def test_death_after_passing_percent_always_undeafens(self) -> None:
        import time

        # 1. Passing threshold then dying to 0% unmutes immediately; missing-% past 0.35s also unmutes
        deb = screen_reader.PercentStateDebouncer()
        self.assertEqual(deb.update(48.0, 50.0, True, now=1.0), (False, True))
        self.assertEqual(deb.update(55.0, 50.0, True, now=1.1), (True, True))
        self.assertEqual(deb.update(0.0, 50.0, True, now=1.2), (True, True))
        self.assertEqual(deb.update(0.0, 50.0, True, now=1.25), (False, True))

        self.assertEqual(deb.update(49.0, 50.0, True, now=2.0), (False, True))
        self.assertEqual(deb.update(64.0, 50.0, True, now=2.1), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=2.2), (True, True))
        self.assertEqual(deb.update(None, 50.0, True, now=3.2), (False, False))

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

    def test_pass_percent_then_death_and_force_restore(self) -> None:
        hotkey = {"modifiers": ["ctrl", "shift"], "vk": ord("D"), "label": "Ctrl + Shift + D"}
        cfg = dict(config.DEFAULT_CONFIG, auto_deafen=True, deafen_hotkey=hotkey)
        with (
            patch("discord_hotkey.send_hotkey", return_value=True) as mock_hotkey,
            patch("window_audio.AudioUtilities.GetAllSessions", return_value=[]),
            web_app(cfg) as (app, api),
        ):
            app.discord_ctrl._toggle_cooldown_s = 0.02

            # Cross the threshold: Discord is deafened.
            app._on_screen_reading(reading(62.0, True))
            self.assertTrue(wait_until(lambda: app.discord_ctrl.deafened_by_us))

            # Die: undeafened via the serialized worker.
            app._on_screen_reading(reading(0.0, False))
            self.assertTrue(wait_until(lambda: not app.discord_ctrl.deafened_by_us))
            self.assertEqual(mock_hotkey.call_count, 2)

            # Pass again, then the "Unmute & undeafen all" failsafe.
            app._on_screen_reading(reading(75.0, True))
            self.assertTrue(wait_until(lambda: app.discord_ctrl.deafened_by_us))
            api.force_restore_all()
            self.assertTrue(wait_until(lambda: not app.discord_ctrl.deafened_by_us))
            self.assertEqual(mock_hotkey.call_count, 4)

            # Paused watching ignores a stale should_mute frame.
            api.set_setting("watch_enabled", False)
            app._on_screen_reading(reading(75.0, True))
            self.assertFalse(app.current_want_mute)

    def test_global_watch_toggle_hotkey(self) -> None:
        import threading

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

        with web_app() as (app, api):
            spec = {"modifiers": ["ctrl", "shift"], "vk": 0x78, "label": "Ctrl + Shift + F9"}
            with patch.object(app.global_hotkey, "set_hotkey", return_value=(True, "Active (Global)")):
                st = api.set_watch_hotkey(spec)
                self.assertEqual(st["cfg"]["watch_toggle_hotkey"]["label"], "Ctrl + Shift + F9")
                self.assertEqual(st["watch_hotkey_status"], "Active (Global)")
                app._on_global_watch_toggle()
                self.assertFalse(app.cfg["watch_enabled"])
                app._on_global_watch_toggle()
                self.assertTrue(app.cfg["watch_enabled"])
            with patch.object(app.global_hotkey, "set_hotkey", return_value=(False, "Inactive")):
                st = api.clear_watch_hotkey()
                self.assertIsNone(st["cfg"]["watch_toggle_hotkey"])
                self.assertEqual(st["watch_hotkey_status"], "Inactive")

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
        with web_app() as (app, api):
            st = api.set_region_preset("Top Left")
            self.assertEqual(st["cfg"]["watch_mode"], "custom_region")
            self.assertEqual(st["region_description"], "Preset: Top Left")
            with patch.object(screen_reader, "find_window_client_rect", return_value=(100, 50, 1000, 500)):
                rect, _label = screen_reader.compute_capture_rect(
                    st["cfg"]["watch_mode"], st["cfg"]["target_process"], st["cfg"]["custom_region"]
                )
            self.assertEqual(rect, {"left": 100, "top": 50, "width": 280, "height": 55})
            st = api.set_region_preset("auto")
            self.assertEqual(st["cfg"]["watch_mode"], "auto_window")

    def test_update_banner_shows_and_dismisses(self) -> None:
        import web_ui

        info1 = updater.UpdateInfo(
            current_version="3.0.8",
            latest_version="9.9.9",
            tag_name="v9.9.9",
            update_available=True,
            download_url="https://example.com/GDQuiet-Setup.exe",
            release_url="https://example.com",
            asset_name="GDQuiet-Setup.exe",
            size_bytes=1024,
            sha256="a" * 64,
            release_notes="### What's New\n- Thing",
        )
        info2 = updater.UpdateInfo(**{**info1.__dict__, "latest_version": "9.9.10", "tag_name": "v9.9.10"})
        with web_app() as (app, api):
            self.assertFalse(api.get_state()["show_update_banner"])
            with (
                patch.object(web_ui, "check_for_update", return_value=info1),
                patch.object(app, "_refresh_latest_stable_locked"),
            ):
                app._check_updates_sync(silent=True)
            st = api.get_state()
            self.assertTrue(st["show_update_banner"])
            self.assertEqual(st["pending_update"]["latest_version"], "9.9.9")
            self.assertEqual(st["update_btn_text"], "Install v9.9.9 update")

            st = api.dismiss_update_banner()
            self.assertFalse(st["show_update_banner"])
            # Same version stays dismissed after a background re-check.
            with (
                patch.object(web_ui, "check_for_update", return_value=info1),
                patch.object(app, "_refresh_latest_stable_locked"),
            ):
                app._check_updates_sync(silent=True)
            self.assertFalse(api.get_state()["show_update_banner"])
            # A newer version shows it again.
            with (
                patch.object(web_ui, "check_for_update", return_value=info2),
                patch.object(app, "_refresh_latest_stable_locked"),
            ):
                app._check_updates_sync(silent=True)
            st = api.get_state()
            self.assertTrue(st["show_update_banner"])
            self.assertEqual(st["pending_update"]["latest_version"], "9.9.10")

    def test_logo_icon_and_ui_assets_exist(self) -> None:
        from PIL import Image

        repo_root = Path(__file__).resolve().parent.parent
        logo_path = repo_root / "logo.png"
        ico_path = repo_root / "app" / "assets" / "gdquiet.ico"
        self.assertTrue(logo_path.is_file())
        self.assertTrue(ico_path.is_file())
        with Image.open(logo_path) as im:
            self.assertEqual(im.size, (256, 256))
            self.assertEqual(im.mode, "RGBA")
        for rel in ("ui/index.html", "ui/app.js", "ui/styles.css", "assets/gdquiet.ico"):
            self.assertTrue(common._resource_path(rel).is_file(), rel)
        self.assertTrue(common._resource_path("logo.png").is_file())

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

    def test_audio_restore_forgets_sessions_of_closed_apps(self) -> None:
        fake_session = FakeSession("spotify.exe", 111, muted=0, instance_id="inst1")
        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[fake_session]):
            ctrl.apply(["spotify.exe"], True)
        self.assertTrue(ctrl.has_active_effect)
        # Spotify exits while muted: its session disappears from the list.
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[]):
            ctrl.restore()
        self.assertFalse(ctrl.has_active_effect)
        self.assertFalse(ctrl.is_muted)

    def test_audio_restore_keeps_unseen_session_if_listing_was_partial_and_pid_alive(self) -> None:
        fake_session = FakeSession("spotify.exe", 111, muted=0, instance_id="inst1")
        ctrl = window_audio.AudioMuteController()
        with patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[fake_session]):
            ctrl.apply(["spotify.exe"], True)

        class BrokenSession:
            @property
            def Process(self):  # noqa: N802 (pycaw attribute name)
                raise RuntimeError("process query failed")

        with (
            patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[BrokenSession()]),
            patch.object(window_audio.psutil, "pid_exists", return_value=True),
        ):
            ctrl.restore()
        # We couldn't identify every session and the app is still running, so keep tracking it.
        self.assertTrue(ctrl.is_muted)
        with (
            patch.object(window_audio.AudioUtilities, "GetAllSessions", return_value=[BrokenSession()]),
            patch.object(window_audio.psutil, "pid_exists", return_value=False),
        ):
            ctrl.restore()
        self.assertFalse(ctrl.is_muted)

    def test_apply_worker_retries_failed_audio_restore(self) -> None:
        class FakeAudioController:
            def __init__(self) -> None:
                self.attempts = 0
                self.is_muted = True
                self.is_ducking = False

            @property
            def has_active_effect(self) -> bool:
                return self.is_muted

            def restore(self) -> None:
                self.attempts += 1
                if self.attempts >= 2:
                    self.is_muted = False

            def apply(self, *_a, **_k) -> None:
                pass

            def set_duck_volume(self, *_a, **_k) -> None:
                pass

        with web_app() as (app, _api):
            fake = FakeAudioController()
            app.audio_ctrl = fake
            app._apply_mute_state(False)
            self.assertTrue(wait_until(lambda: not fake.is_muted))
            self.assertEqual(fake.attempts, 2)

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

    def test_web_ui_eclipse_level_label_loads_profiles_and_account_state(self) -> None:
        import web_ui

        with (
            patch.object(web_ui, "load_config", return_value=dict(config.DEFAULT_CONFIG)),
            patch.object(web_ui, "save_config"),
            patch.object(web_ui, "list_mute_targets", return_value=[]),
            patch.object(web_ui, "list_open_window_processes", return_value=["geometrydash.exe"]),
            patch.object(web_ui, "ScreenPercentWatcher"),
            patch.object(web_ui.DiscordAuth, "load"),
        ):
            app = web_ui.GDQuietWebApp()
            try:
                api = web_ui.WebApi(app)
                st = api.get_state()
                self.assertIn(st["account"]["status"], {"signed_out", "signed_in"})
                self.assertEqual(st["level_detection_status"], "Level label reading is off")

                def reading(name: str | None, raw: str = "") -> screen_reader.ScreenReading:
                    return screen_reader.ScreenReading(
                        percent=3.0,
                        raw_text="3%",
                        should_mute=False,
                        is_playing=True,
                        status_text="",
                        preview_image=None,
                        level_name_candidate=name,
                        level_label_raw=raw,
                    )

                # Ignored while the feature is off.
                app._on_screen_reading(reading("Stereo Madness"))
                self.assertEqual(app._active_level_key, "")

                api.set_setting("level_label_enabled", True)
                # Auto-add is off: an unknown level is shown but not saved.
                app._on_screen_reading(reading("Stereo Madness", "FPS 240\nStereo Madness"))
                st = api.get_state()
                self.assertEqual(st["level_label"]["active_level"], "Stereo Madness")
                self.assertIn("Stereo Madness", st["level_label"]["last_raw"])
                self.assertEqual(st["cfg"]["level_profiles"], {})

                api.add_level("Stereo Madness", 40)
                api.add_level("Deadlocked", 80)
                app._on_screen_reading(reading("Deadlocked"))
                api.save_setup()
                app._on_screen_reading(reading("Stereo Madness"))
                self.assertEqual(api.get_state()["cfg"]["mute_at_percent"], 40.0)
                app._on_screen_reading(reading("Deadlocked"))
                self.assertEqual(api.get_state()["cfg"]["mute_at_percent"], 80.0)

                # The label box is stored separately from the % region.
                app.cfg["level_label_region"] = {"kind": "window_rel", "rx": 0.01, "ry": 0.4, "rw": 0.2, "rh": 0.05}
                st = api.get_state()
                self.assertEqual(st["level_label"]["region_description"], "Custom box (also checks above and below it)")
                self.assertEqual(st["cfg"]["watch_mode"], "auto_window")
                st = api.reset_level_region()
                self.assertIsNone(st["cfg"]["level_label_region"])

                # Levels page: add a level by name, OCR misreads snap to it.
                st = api.add_level("clubstep startpos", "62")
                row = next(r for r in st["levels"] if r["key"] == "clubstepstartpos")
                self.assertEqual(row["threshold"], 62.0)
                self.assertTrue(row["enabled"])
                app._on_screen_reading(reading("clubstep sii@ttpos"))
                st = api.get_state()
                self.assertEqual(st["level_label"]["active_level"], "clubstep startpos")
                self.assertEqual(st["cfg"]["mute_at_percent"], 62.0)
                self.assertNotIn("clubstepsiiattpos", st["cfg"]["level_profiles"])

                # Switched-off levels are recognised but don't change settings.
                api.set_level_enabled("deadlocked", False)
                api.set_threshold(33)
                app._on_screen_reading(reading("Deadlocked"))
                st = api.get_state()
                self.assertEqual(st["level_label"]["active_level"], "Deadlocked")
                self.assertEqual(st["cfg"]["mute_at_percent"], 33.0)
                api.save_setup()
                self.assertFalse(api.get_state()["cfg"]["level_profiles"]["deadlocked"]["enabled"])
                st = api.set_level_enabled("deadlocked", True)
                self.assertEqual(st["cfg"]["mute_at_percent"], 33.0)

                st = api.set_level_threshold("clubstepstartpos", 70)
                self.assertEqual(st["cfg"]["level_profiles"]["clubstepstartpos"]["mute_at_percent"], 70.0)
                st = api.delete_level("deadlocked")
                self.assertNotIn("deadlocked", st["cfg"]["level_profiles"])
                self.assertEqual(st["level_label"]["active_level"], "")
                st = api.add_level("   ")
                self.assertEqual(st["footer_message"], "Type a level name first.")

                # Beta updates can't be switched on.
                st = api.set_setting("beta_updates", True)
                self.assertFalse(st["cfg"]["beta_updates"])
                self.assertIn("no beta updates", st["beta_updates_notice"])
            finally:
                app._cleanup_sync()

    def test_level_scanner_runs_while_muting_is_paused_and_skips_covered_game(self) -> None:
        import threading
        import time

        results: list[tuple] = []
        got = threading.Event()

        def on_level(candidate, raw, error, status="ok"):
            results.append((candidate, raw, error, status))
            if len(results) >= 3:
                got.set()

        cfg = {"watch_enabled": False, "level_label_enabled": True, "target_process": "geometrydash.exe"}
        watcher = screen_reader.ScreenPercentWatcher(
            get_settings=lambda: cfg, on_reading=lambda _r: None, on_level_result=on_level
        )
        scans = iter(
            [
                ("clubstep startpos", "clubstep startpos", "ok"),
                (None, None, "covered"),
                ("clubstep startpos", "clubstep startpos", "ok"),
            ]
        )

        class FakeMss:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        with (
            patch.object(screen_reader.mss, "MSS", FakeMss),
            patch.object(
                screen_reader.ScreenPercentWatcher,
                "_scan_level_label",
                staticmethod(lambda *_a: next(scans, (None, None, "no_window"))),
            ),
            patch.object(screen_reader.ScreenPercentWatcher, "_run", lambda self: None),
        ):
            orig_wait = watcher._stop.wait
            watcher._stop.wait = lambda t=None: orig_wait(0.01)
            watcher.start()
            try:
                self.assertTrue(got.wait(5.0))
            finally:
                watcher.stop()
                time.sleep(0.05)
        statuses = [r[3] for r in results[:3]]
        self.assertEqual(statuses, ["ok", "covered", "ok"])
        # The covered scan didn't break the run: the second good read reports the level.
        self.assertIsNone(results[0][0])
        self.assertEqual(results[2][0], "clubstep startpos")

    def test_scan_level_label_refuses_covered_game_window(self) -> None:
        rect = {"left": 0, "top": 0, "width": 200, "height": 40}
        with (
            patch.object(screen_reader, "compute_region_rect", return_value=(rect, "geometrydash.exe")),
            patch.object(screen_reader, "region_shows_process", return_value=False),
        ):
            name, raw, status = screen_reader.ScreenPercentWatcher._scan_level_label(
                None, None, None, "geometrydash.exe"
            )
        self.assertEqual((name, raw, status), (None, None, "covered"))
        with patch.object(screen_reader, "compute_region_rect", return_value=(None, "Waiting")):
            self.assertEqual(
                screen_reader.ScreenPercentWatcher._scan_level_label(None, None, None, "geometrydash.exe"),
                (None, None, "no_window"),
            )

    def test_web_ui_level_detection_status_and_test_button(self) -> None:
        import web_ui
        from PIL import Image as PILImage

        cfg = dict(config.DEFAULT_CONFIG)
        cfg["watch_enabled"] = False
        cfg["level_label_enabled"] = True
        with (
            patch.object(web_ui, "load_config", return_value=cfg),
            patch.object(web_ui, "save_config"),
            patch.object(web_ui, "list_mute_targets", return_value=[]),
            patch.object(web_ui, "list_open_window_processes", return_value=["geometrydash.exe"]),
            patch.object(web_ui, "ScreenPercentWatcher"),
            patch.object(web_ui.DiscordAuth, "load"),
        ):
            app = web_ui.GDQuietWebApp()
            try:
                api = web_ui.WebApi(app)
                api.add_level("clubstep startpos", 70)

                # Results from the label thread load profiles even with muting paused.
                app._on_level_result(None, None, None, "no_window")
                self.assertEqual(api.get_state()["level_label"]["scan_status"], "Waiting for Geometry Dash to open")
                app._on_level_result(None, None, None, "covered")
                self.assertIn("covering Geometry Dash", api.get_state()["level_label"]["scan_status"])
                app._on_level_result("slubStep Startpos", "slubStep Startpos", None, "ok")
                st = api.get_state()
                self.assertEqual(st["level_label"]["active_level"], "clubstep startpos")
                self.assertEqual(st["cfg"]["mute_at_percent"], 70.0)
                # The level is switched on, so detecting it turned watching back on.
                self.assertTrue(st["cfg"]["watch_enabled"])
                self.assertNotIn("muting is paused", st["level_label"]["scan_status"])

                img =PILImage.new("RGB", (271, 61), (20, 120, 40))
                with patch.object(
                    web_ui,
                    "probe_level_label",
                    return_value=screen_reader.LevelLabelProbe("c!ubstep startpos", "c!ubstep startpos", img),
                ):
                    st = api.test_level_detection()
                test = st["level_label"]["test"]
                self.assertTrue(test["ok"])
                self.assertEqual(test["matched_name"], "clubstep startpos")
                self.assertTrue(test["matched_enabled"])
                self.assertTrue(test["preview"].startswith("data:image/png;base64,"))

                with patch.object(
                    web_ui,
                    "probe_level_label",
                    return_value=screen_reader.LevelLabelProbe(None, "", None, "Another window is covering Geometry Dash"),
                ):
                    st = api.test_level_detection()
                self.assertFalse(st["level_label"]["test"]["ok"])
                self.assertIn("covering", st["level_label"]["test"]["problem"])
            finally:
                app._cleanup_sync()

    def test_level_id_parsing_from_eclipse_label(self) -> None:
        split = screen_reader.split_level_id
        self.assertEqual(split("clubstep startpos (12345678)"), ("clubstep startpos", "12345678"))
        # OCR reading 1 as I/l and dropping the opening bracket.
        self.assertEqual(split("clubstep startpos (I2345678)"), ("clubstep startpos", "12345678"))
        self.assertEqual(split("clubstep startpos 12345678)"), ("clubstep startpos", "12345678"))
        self.assertEqual(split("Sky (00123)"), ("Sky", "123"))
        # A number that's part of the name isn't an ID.
        self.assertEqual(split("Bloodbath 2"), ("Bloodbath 2", None))
        self.assertEqual(split("clubstep startpos"), ("clubstep startpos", None))

        pick = screen_reader.extract_level_name_from_label_text
        # Short names with long IDs must not be mistaken for stats.
        self.assertEqual(pick("FPS: 240\nSky (98765432)"), "Sky (98765432)")
        self.assertEqual(pick("0/0/0 CPS\nclubstep startpos (128)"), "clubstep startpos (128)")
        # A lone short bracketed number is noise.
        self.assertIsNone(pick("(2)"))

        same = screen_reader.same_level_label
        self.assertTrue(same("Startpos (111)", "c!ubstep (111)"))  # same ID, name ignored
        self.assertFalse(same("clubstep startpos (111)", "clubstep startpos (222)"))  # same name, different ID
        self.assertTrue(same("clubstep startpos", "clubstep sii@ttpos"))  # no IDs: names

    def test_level_id_profiles_match_by_id_only(self) -> None:
        saved = {
            "id:111": {"name": "clubstep startpos", "level_id": "111"},
            "id:222": {"name": "clubstep startpos", "level_id": "222"},
            "deadlocked": {"name": "Deadlocked"},
        }
        find = profiles.find_matching_profile
        self.assertEqual(find(saved, "clubstep startpos (222)"), "id:222")
        self.assertEqual(find(saved, "totally misread name (111)"), "id:111")
        # Unknown ID: never fall back to a same-named level.
        self.assertIsNone(find(saved, "clubstep startpos (333)"))
        self.assertIsNone(find(saved, "Deadlocked (444)"))
        # Name-only labels still match by name.
        self.assertEqual(find(saved, "DEADLOCKED"), "deadlocked")
        self.assertEqual(profiles.level_key("x", "00123"), "id:123")
        self.assertEqual(profiles.level_key("Stereo Madness", None), "stereomadness")

    def test_web_ui_levels_with_same_name_are_kept_apart_by_id(self) -> None:
        import web_ui

        cfg = dict(config.DEFAULT_CONFIG)
        cfg["level_label_enabled"] = True
        with (
            patch.object(web_ui, "load_config", return_value=cfg),
            patch.object(web_ui, "save_config"),
            patch.object(web_ui, "list_mute_targets", return_value=[]),
            patch.object(web_ui, "list_open_window_processes", return_value=["geometrydash.exe"]),
            patch.object(web_ui, "ScreenPercentWatcher"),
            patch.object(web_ui.DiscordAuth, "load"),
        ):
            app = web_ui.GDQuietWebApp()
            try:
                api = web_ui.WebApi(app)
                api.add_level("clubstep startpos", 60, "111")
                st = api.add_level("clubstep startpos", 85, "222")
                rows = {r["key"]: r for r in st["levels"]}
                self.assertEqual(set(rows), {"id:111", "id:222"})
                self.assertEqual(rows["id:222"]["level_id"], "222")
                self.assertEqual(rows["id:222"]["name"], "clubstep startpos")
                # "Name (ID)" typed in the name box works too.
                st = api.add_level("Sky (98765432)", 40)
                self.assertIn("id:98765432", st["cfg"]["level_profiles"])
                st = api.add_level("Sky", 40, "12ab")
                self.assertEqual(st["footer_message"], "The level ID can only contain numbers.")

                app._on_level_result("clubstep startpos (222)", "clubstep startpos (222)", None, "ok")
                st = api.get_state()
                self.assertEqual(st["level_label"]["active_level"], "clubstep startpos (222)")
                self.assertEqual(st["cfg"]["mute_at_percent"], 85.0)
                app._on_level_result("clubstep startpos (111)", "clubstep startpos (111)", None, "ok")
                self.assertEqual(api.get_state()["cfg"]["mute_at_percent"], 60.0)

                # A new ID with a known name becomes its own level, keyed by ID (auto-add on).
                api.set_setting("level_auto_add", True)
                api.set_threshold(33)
                app._on_level_result("clubstep startpos (333)", "clubstep startpos (333)", None, "ok")
                st = api.get_state()
                added = st["cfg"]["level_profiles"]["id:333"]
                self.assertEqual((added["name"], added["level_id"]), ("clubstep startpos", "333"))
                self.assertFalse(added["enabled"])
                self.assertIsNone(added["mute_at_percent"])
                self.assertEqual(st["cfg"]["mute_at_percent"], 33.0)
                self.assertEqual(st["cfg"]["level_profiles"]["id:111"]["mute_at_percent"], 60.0)

                # Saving keeps the clean name and the ID.
                api.save_setup()
                saved = api.get_state()["cfg"]["level_profiles"]["id:333"]
                self.assertEqual((saved["name"], saved["level_id"], saved["mute_at_percent"]), ("clubstep startpos", "333", 33.0))
            finally:
                app._cleanup_sync()

    def test_web_ui_enabled_level_turns_watching_on_and_auto_add(self) -> None:
        import web_ui

        cfg = dict(config.DEFAULT_CONFIG)
        cfg["level_label_enabled"] = True
        cfg["watch_enabled"] = False
        cfg["mute_at_percent"] = 50.0
        with (
            patch.object(web_ui, "load_config", return_value=cfg),
            patch.object(web_ui, "save_config"),
            patch.object(web_ui, "list_mute_targets", return_value=[]),
            patch.object(web_ui, "list_open_window_processes", return_value=["geometrydash.exe"]),
            patch.object(web_ui, "ScreenPercentWatcher"),
            patch.object(web_ui.DiscordAuth, "load"),
        ):
            app = web_ui.GDQuietWebApp()
            try:
                api = web_ui.WebApi(app)
                api.add_level("clubstep startpos", 72, "12345556")
                api.set_level_enabled("id:12345556", False)

                # Matched but switched off: watching stays paused, % unchanged.
                app._on_level_result("clubstep startpos (12345556)", "", None, "ok")
                st = api.get_state()
                self.assertFalse(st["cfg"]["watch_enabled"])
                self.assertEqual(st["cfg"]["mute_at_percent"], 50.0)

                # Switching on the level you're playing starts watching with its %.
                st = api.set_level_enabled("id:12345556", True)
                self.assertTrue(st["cfg"]["watch_enabled"])
                self.assertEqual(st["cfg"]["mute_at_percent"], 72.0)

                # Detecting a switched-on level by ID turns watching on.
                api.set_setting("watch_enabled", False)
                api.set_threshold(10)
                app._on_level_result("something else (999)", "", None, "ok")
                app._on_level_result("misread name (12345556)", "", None, "ok")
                st = api.get_state()
                self.assertTrue(st["cfg"]["watch_enabled"])
                self.assertEqual(st["cfg"]["mute_at_percent"], 72.0)
                self.assertIn("Watching turned on", st["footer_message"])
                # Auto-add was off, so the unknown level wasn't saved.
                self.assertNotIn("id:999", st["cfg"]["level_profiles"])

                # Auto-add: new levels start switched off with an empty %.
                api.set_setting("level_auto_add", True)
                app._on_level_result("Bloodbath (10565740)", "", None, "ok")
                st = api.get_state()
                row = next(r for r in st["levels"] if r["key"] == "id:10565740")
                self.assertEqual((row["name"], row["level_id"]), ("Bloodbath", "10565740"))
                self.assertFalse(row["enabled"])
                self.assertIsNone(row["threshold"])
                self.assertEqual(row["app_count"], 0)
                self.assertEqual(st["cfg"]["mute_at_percent"], 72.0)

                # Switched on with no % set: watching starts but the current % is kept.
                api.set_setting("watch_enabled", False)
                st = api.set_level_enabled("id:10565740", True)
                self.assertTrue(st["cfg"]["watch_enabled"])
                self.assertEqual(st["cfg"]["mute_at_percent"], 72.0)
                st = api.set_level_threshold("id:10565740", "65")
                self.assertEqual(st["cfg"]["mute_at_percent"], 65.0)
                st = api.set_level_threshold("id:10565740", "")
                self.assertIsNone(next(r for r in st["levels"] if r["key"] == "id:10565740")["threshold"])
            finally:
                app._cleanup_sync()

    def test_apply_level_profile_skips_empty_threshold(self) -> None:
        cfg = {"mute_at_percent": 55.0, "selected_apps": ["spotify.exe"]}
        auto = profiles.new_auto_added_profile("Bloodbath", "10565740")
        self.assertEqual(profiles.apply_level_profile(cfg, auto), cfg)
        self.assertEqual(profiles.apply_level_profile(cfg, {"mute_at_percent": "70"})["mute_at_percent"], 70.0)

    def test_leaving_a_level_pauses_watching_that_a_level_turned_on(self) -> None:
        cfg = dict(config.DEFAULT_CONFIG, level_label_enabled=True, watch_enabled=False)
        with web_app(cfg) as (app, api):
            api.add_level("clubstep startpos", 70, "111")
            api.add_level("Off level", 40, "222")
            api.set_level_enabled("id:222", False)

            app._on_level_result("clubstep startpos (111)", "", None, "ok")
            self.assertTrue(app.cfg["watch_enabled"])

            # Switched-off level: watching goes back to paused.
            app._on_level_result("Off level (222)", "", None, "ok")
            self.assertFalse(app.cfg["watch_enabled"])
            self.assertIn("isn't switched on", api.get_state()["footer_message"])

            # Unknown level also pauses it.
            app._on_level_result("clubstep startpos (111)", "", None, "ok")
            self.assertTrue(app.cfg["watch_enabled"])
            app._on_level_result("Never seen (333)", "", None, "ok")
            self.assertFalse(app.cfg["watch_enabled"])

            # Closing the game pauses it too.
            app._on_level_result("clubstep startpos (111)", "", None, "ok")
            self.assertTrue(app.cfg["watch_enabled"])
            app._on_level_result(None, None, None, "no_window")
            self.assertFalse(app.cfg["watch_enabled"])

            # Switching off the level being played pauses it.
            app._on_level_result("clubstep startpos (111)", "", None, "ok")
            self.assertTrue(app.cfg["watch_enabled"])
            api.set_level_enabled("id:111", False)
            self.assertFalse(app.cfg["watch_enabled"])

    def test_manual_watching_is_never_auto_paused(self) -> None:
        cfg = dict(config.DEFAULT_CONFIG, level_label_enabled=True, watch_enabled=True)
        with web_app(cfg) as (app, api):
            api.add_level("A", 70, "111")
            # Watching was already on (user's choice): leaving levels never pauses it.
            app._on_level_result("A (111)", "", None, "ok")
            app._on_level_result("Unknown (999)", "", None, "ok")
            self.assertTrue(app.cfg["watch_enabled"])

            # A level turns it on, then the user takes over: no auto-pause after that.
            api.set_setting("watch_enabled", False)
            app._on_level_result("A (111)", "", None, "ok")
            self.assertTrue(app.cfg["watch_enabled"])
            api.toggle_watching()
            api.toggle_watching()
            app._on_level_result("Unknown 2 (998)", "", None, "ok")
            self.assertTrue(app.cfg["watch_enabled"])

    def test_per_level_app_picker(self) -> None:
        cfg = dict(config.DEFAULT_CONFIG, level_label_enabled=True, selected_apps=["chrome.exe"])
        with web_app(cfg) as (app, api):
            api.add_level("A", 70, "111")
            row = next(r for r in api.get_state()["levels"] if r["key"] == "id:111")
            self.assertTrue(row["has_apps"])
            self.assertEqual(row["apps"], ["chrome.exe"])

            st = api.set_level_apps("id:111", ["Spotify.exe", "discord.exe", " "])
            row = next(r for r in st["levels"] if r["key"] == "id:111")
            self.assertEqual(row["apps"], ["discord.exe", "spotify.exe"])
            self.assertEqual(st["cfg"]["selected_apps"], ["chrome.exe"])  # not the active level

            # Applied immediately to the level you're playing.
            app._on_level_result("A (111)", "", None, "ok")
            self.assertEqual(app.cfg["selected_apps"], ["discord.exe", "spotify.exe"])
            st = api.set_level_apps("id:111", ["obs64.exe"])
            self.assertEqual(st["cfg"]["selected_apps"], ["obs64.exe"])

            # "Use my current apps": the level stops changing the selection.
            st = api.set_level_apps("id:111", None)
            row = next(r for r in st["levels"] if r["key"] == "id:111")
            self.assertFalse(row["has_apps"])
            self.assertNotIn("selected_apps", st["cfg"]["level_profiles"]["id:111"])
            api.toggle_app("vlc.exe")
            app._on_level_result("Other (5)", "", None, "ok")
            app._on_level_result("A (111)", "", None, "ok")
            self.assertIn("vlc.exe", app.cfg["selected_apps"])

    def test_synced_levels_replace_local_only_when_unchanged(self) -> None:
        with web_app() as (app, api):
            api.add_level("A", 70, "111")
            expected = app._get_level_profiles()
            new = {**expected, "id:222": {"name": "B", "level_id": "222", "enabled": True, "mute_at_percent": 40.0}}
            self.assertTrue(app._replace_level_profiles(new, expected))
            self.assertIn("id:222", api.get_state()["cfg"]["level_profiles"])
            # Stale snapshot: refused.
            self.assertFalse(app._replace_level_profiles({}, expected))
            self.assertIn("level_sync", api.get_state())

    def test_label_box_is_a_hint_and_the_closest_label_line_wins(self) -> None:
        line = screen_reader.OcrLine
        lines = [
            line("FPS: 240", 11, 80, 20),
            line("Stereo Madness (1)", 11, 200, 20),
            line("0/0/0 CPS", 11, 260, 20),
            line("Back On Track (2)", 11, 300, 20),
        ]
        # Without a box the top-most label wins; with one, the line nearest it.
        self.assertEqual(screen_reader.pick_level_name(lines, 11.5), "Stereo Madness (1)")
        self.assertEqual(screen_reader.pick_level_name(lines, 11.5, prefer_y=330), "Back On Track (2)")
        self.assertEqual(screen_reader.pick_level_name(lines, 11.5, prefer_y=150), "Stereo Madness (1)")

    def test_level_label_scan_area_stretches_custom_box(self) -> None:
        box = {"kind": "window_rel", "rx": 0.00261, "ry": 0.26574, "rw": 0.14122, "rh": 0.05648}
        with patch.object(screen_reader, "find_window_client_rect", return_value=(0, 0, 1920, 1080)):
            area, prefer_y = screen_reader.level_label_scan_area(box, "geometrydash.exe")
            # Full window height, at least 35% of the width, starting at the box.
            self.assertEqual(area, {"left": 5, "top": 0, "width": 672, "height": 1080})
            self.assertAlmostEqual(prefer_y, 287 + 61 / 2, delta=1)
            # No custom box: the default left 30%, top-most label wins.
            area, prefer_y = screen_reader.level_label_scan_area(None, "geometrydash.exe")
            self.assertEqual(area, {"left": 0, "top": 0, "width": 576, "height": 1080})
            self.assertIsNone(prefer_y)
        with patch.object(screen_reader, "find_window_client_rect", return_value=None):
            self.assertEqual(screen_reader.level_label_scan_area(box, "geometrydash.exe"), (None, None))

    def test_label_lines_not_drawn_by_the_game_are_dropped(self) -> None:
        from PIL import Image

        class FakeShot:
            size = (100, 400)
            bgra = bytes(100 * 400 * 4)

        class FakeSct:
            def grab(self, _area):
                return FakeShot()

        class FakeOcr:
            def recognize_line_boxes(self, _img, scale=1.0):
                return [
                    screen_reader.OcrLine("clubstep startpos (5)", 10, 100, 20),
                    screen_reader.OcrLine("Cloudy", 10, 380, 20),  # taskbar weather widget
                ]

        area = {"left": 0, "top": 0, "width": 100, "height": 400}
        with patch.object(screen_reader, "point_shows_process", side_effect=lambda x, y, *_a: y < 300):
            lines, img = screen_reader.read_level_label_lines(FakeSct(), FakeOcr(), area, "geometrydash.exe")
        self.assertEqual([l.text for l in lines], ["clubstep startpos (5)"])
        self.assertIsInstance(img, Image.Image)

    def test_winrt_ocr_finds_label_after_the_stack_moves(self) -> None:
        from PIL import ImageFilter

        ocr = screen_reader.WinRtPercentOcr()
        try:
            if not ocr.available():
                self.skipTest("Windows OCR engine unavailable")
            try:
                font = ImageFont.truetype("arialbd.ttf", 26)
            except OSError:
                self.skipTest("Arial Bold unavailable")
            box_center_y = 317.5  # where the user drew their box
            for shift in (0, -38, 38, -76):
                img = Image.new("RGB", (560, 720), (60, 20, 70)).filter(ImageFilter.GaussianBlur(1))
                draw = ImageDraw.Draw(img)
                labels = ["FPS: 240", "Attempt 12", "7.98%", "Best run: 0.00-2.71%", "1/4/2 CPS", "Back On Track (2)"]
                for i, text in enumerate(labels):
                    draw.text((10, 120 + shift + i * 38), text, font=font, fill=(235, 225, 240))
                big, scale = screen_reader.prepare_label_image(img)
                lines = ocr.recognize_line_boxes(big, scale=scale)
                self.assertEqual(
                    screen_reader.pick_level_name(lines, align_tolerance=11.2, prefer_y=box_center_y),
                    "Back On Track (2)",
                    f"shift {shift}: {[l.text for l in lines]}",
                )
        finally:
            ocr.close()

    def test_web_ui_turns_off_saved_beta_updates_on_start(self) -> None:
        import web_ui

        cfg = dict(config.DEFAULT_CONFIG)
        cfg["beta_updates"] = True
        with (
            patch.object(web_ui, "load_config", return_value=cfg),
            patch.object(web_ui, "save_config") as save,
            patch.object(web_ui, "list_mute_targets", return_value=[]),
            patch.object(web_ui, "list_open_window_processes", return_value=["geometrydash.exe"]),
            patch.object(web_ui, "ScreenPercentWatcher"),
            patch.object(web_ui.DiscordAuth, "load"),
        ):
            app = web_ui.GDQuietWebApp()
            try:
                self.assertFalse(app.cfg["beta_updates"])
                self.assertTrue(save.called)
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
