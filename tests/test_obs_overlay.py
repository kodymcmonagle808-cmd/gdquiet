from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from obs_overlay import OverlayServer  # noqa: E402
import config  # noqa: E402


class ObsOverlayTests(unittest.TestCase):
    def test_obs_overlay_is_disabled_by_default(self) -> None:
        self.assertFalse(config.DEFAULT_CONFIG["obs_overlay_enabled"])

    def setUp(self) -> None:
        self.control_received: list[dict[str, object]] = []

        def on_control(payload: dict[str, object]) -> bool:
            self.control_received.append(payload)
            return True

        self.server = OverlayServer(
            get_status=lambda: {
                "watching": True,
                "muted": False,
                "percent": 47.5,
                "threshold": 60.0,
                "testing": False,
            },
            on_control=on_control,
            port=0,
        )
        self.url = self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def test_serves_overlay_and_live_status_on_loopback(self) -> None:
        with urlopen(self.url, timeout=2) as response:
            page = response.read().decode("utf-8")
            self.assertIn("GD QUIET", page)
            self.assertIn("Pause watching", page)
            self.assertIn("Content-Security-Policy", response.headers)

        with urlopen(self.url + "api/status", timeout=2) as response:
            status = json.loads(response.read())
        self.assertEqual(status["percent"], 47.5)
        self.assertEqual(status["threshold"], 60.0)
        self.assertEqual(self.server._server.server_address[0], "127.0.0.1")

    def test_accepts_valid_watch_and_threshold_controls(self) -> None:
        for payload in (
            {"action": "watch", "enabled": False},
            {"action": "threshold", "value": 35},
        ):
            request = Request(
                self.url + "api/control",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 202)

        self.assertEqual(len(self.control_received), 2)

    def test_rejects_invalid_or_cross_origin_controls(self) -> None:
        payloads = (
            {"action": "watch", "enabled": "false"},
            {"action": "threshold", "value": 101},
            {"action": "unmute_all"},
        )
        for payload in payloads:
            request = Request(
                self.url + "api/control",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 400)

        request = Request(
            self.url + "api/control",
            data=b'{"action":"watch","enabled":false}',
            headers={
                "Content-Type": "application/json",
                "Origin": "http://example.invalid",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(self.control_received, [])

        request = Request(
            self.url + "api/control",
            data=b'{"action":"watch","enabled":false}',
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:1234",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
