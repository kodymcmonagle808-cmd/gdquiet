from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import config  # noqa: E402
import discord_hotkey  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
