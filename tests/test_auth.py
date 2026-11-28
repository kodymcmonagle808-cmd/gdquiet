from __future__ import annotations

import base64
import hashlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

import auth  # noqa: E402

FAKE_URL = "https://example-project.supabase.co"
FAKE_KEY = "sb_publishable_test"


def fake_session(expires_in: float = 3600.0, name: str = "kody") -> dict:
    return {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": time.time() + expires_in,
        "user": {
            "id": "user-uuid",
            "user_metadata": {
                "name": f"{name}#0",
                "full_name": name,
                "custom_claims": {"global_name": "Kody"},
                "avatar_url": "https://cdn.discordapp.com/avatars/1/a.png",
                "provider_id": "1234",
            },
        },
    }


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session_path = Path(self.tmp.name) / "session.bin"
        settings = patch.object(auth, "supabase_settings", return_value=(FAKE_URL, FAKE_KEY))
        settings.start()
        self.addCleanup(settings.stop)

    def test_pkce_challenge_is_s256_of_verifier(self) -> None:
        verifier, challenge = auth.make_pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)

    def test_authorize_url_targets_discord_with_local_callback(self) -> None:
        url = auth.build_authorize_url(FAKE_URL, "challenge123")
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", f"{FAKE_URL}/auth/v1/authorize")
        self.assertEqual(query["provider"], ["discord"])
        self.assertEqual(query["redirect_to"], ["http://127.0.0.1:47819/auth/callback"])
        self.assertEqual(query["code_challenge"], ["challenge123"])
        self.assertEqual(query["code_challenge_method"], ["s256"])

    def test_user_from_session_prefers_discord_global_name(self) -> None:
        user = auth.user_from_session(fake_session())
        self.assertEqual(user["display_name"], "Kody")
        self.assertEqual(user["username"], "kody")
        self.assertEqual(user["discord_id"], "1234")
        self.assertTrue(user["avatar_url"].startswith("https://cdn.discordapp.com/"))

    def test_session_file_is_dpapi_encrypted_and_round_trips(self) -> None:
        session = fake_session()
        auth.save_session(self.session_path, session)
        raw = self.session_path.read_bytes()
        self.assertNotIn(b"refresh-1", raw)
        self.assertEqual(auth.load_session(self.session_path)["refresh_token"], "refresh-1")
        self.session_path.write_bytes(b"garbage")
        self.assertIsNone(auth.load_session(self.session_path))

    def test_full_sign_in_via_local_callback(self) -> None:
        opened: list[str] = []

        def fake_browser(url: str) -> None:
            opened.append(url)

            def hit_callback() -> None:
                time.sleep(0.1)
                with urllib.request.urlopen(f"{auth.CALLBACK_URL}?code=the-code", timeout=5) as resp:
                    self.assertIn(b"Signed in", resp.read())

            threading.Thread(target=hit_callback, daemon=True).start()

        exchanged: dict[str, str] = {}

        def fake_exchange(url: str, key: str, code: str, verifier: str) -> dict:
            exchanged.update(url=url, key=key, code=code, verifier=verifier)
            return fake_session()

        ctl = auth.DiscordAuth(self.session_path, open_browser=fake_browser)
        with patch.object(auth, "exchange_code", side_effect=fake_exchange):
            ctl.start_sign_in()
            self.assertEqual(ctl.to_state()["status"], "waiting")
            self.assertTrue(wait_for(lambda: ctl.to_state()["status"] == "signed_in"))

        self.assertEqual(exchanged["code"], "the-code")
        challenge = urllib.parse.parse_qs(urllib.parse.urlparse(opened[0]).query)["code_challenge"][0]
        expected = base64.urlsafe_b64encode(hashlib.sha256(exchanged["verifier"].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)
        state = ctl.to_state()
        self.assertEqual(state["user"]["display_name"], "Kody")
        self.assertEqual(state["message"], "Signed in as Kody.")

        # Remembered across restarts.
        again = auth.DiscordAuth(self.session_path)
        again.load()
        self.assertEqual(again.to_state()["status"], "signed_in")

        with patch.object(auth, "_post_json") as post:
            again.sign_out()
            self.assertTrue(wait_for(lambda: post.called))
        self.assertEqual(again.to_state()["status"], "signed_out")
        self.assertFalse(self.session_path.exists())

    def test_sign_in_reports_provider_error_and_cancel_releases_port(self) -> None:
        def denied_browser(_url: str) -> None:
            def hit() -> None:
                time.sleep(0.1)
                urllib.request.urlopen(
                    f"{auth.CALLBACK_URL}?error=access_denied&error_description=User+denied", timeout=5
                ).read()

            threading.Thread(target=hit, daemon=True).start()

        ctl = auth.DiscordAuth(self.session_path, open_browser=denied_browser)
        ctl.start_sign_in()
        self.assertTrue(wait_for(lambda: ctl.to_state()["status"] == "signed_out"))
        self.assertIn("User denied", ctl.to_state()["message"])

        # Cancel, then immediately retry: the second attempt must get the port.
        ctl2 = auth.DiscordAuth(self.session_path, open_browser=lambda _u: None)
        ctl2.start_sign_in()
        time.sleep(0.2)
        ctl2.cancel_sign_in()
        self.assertEqual(ctl2.to_state()["status"], "signed_out")
        ctl2._open_browser = denied_browser
        ctl2.start_sign_in()
        self.assertTrue(wait_for(lambda: "User denied" in ctl2.to_state()["message"]))

    def test_expired_session_refreshes_and_rejected_refresh_signs_out(self) -> None:
        auth.save_session(self.session_path, fake_session(expires_in=-10))
        refreshed = fake_session()
        refreshed["access_token"] = "access-2"
        ctl = auth.DiscordAuth(self.session_path)
        with patch.object(auth, "refresh_session", return_value=refreshed):
            ctl.load()
            self.assertTrue(wait_for(lambda: ctl.access_token() == "access-2"))
        self.assertEqual(auth.load_session(self.session_path)["access_token"], "access-2")

        auth.save_session(self.session_path, fake_session(expires_in=-10))
        ctl = auth.DiscordAuth(self.session_path)
        with patch.object(auth, "refresh_session", side_effect=auth.AuthError("Invalid Refresh Token")):
            with patch.object(threading.Thread, "start"):
                ctl.load()
            self.assertIsNone(ctl.access_token())
        self.assertEqual(ctl.to_state()["status"], "signed_out")
        self.assertFalse(self.session_path.exists())

        # Network trouble keeps the session.
        auth.save_session(self.session_path, fake_session(expires_in=-10))
        ctl = auth.DiscordAuth(self.session_path)
        with patch.object(auth, "refresh_session", side_effect=auth.AuthError("Could not reach Supabase: offline")):
            with patch.object(threading.Thread, "start"):
                ctl.load()
            self.assertIsNone(ctl.access_token())
        self.assertEqual(ctl.to_state()["status"], "signed_in")

    def test_unconfigured_build_disables_sign_in(self) -> None:
        with patch.object(auth, "supabase_settings", return_value=("", "")):
            ctl = auth.DiscordAuth(self.session_path)
            ctl.start_sign_in()
            state = ctl.to_state()
        self.assertFalse(state["configured"])
        self.assertEqual(state["status"], "signed_out")
        self.assertIn("isn't configured", state["message"])


if __name__ == "__main__":
    unittest.main()
