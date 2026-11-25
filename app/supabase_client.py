from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

try:
    from websockets.sync.client import connect as ws_connect
    HAS_WEBSOCKETS = True
except ImportError:
    ws_connect = None
    HAS_WEBSOCKETS = False


class SupabaseError(Exception):
    pass


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _request_json(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None and "Content-Type" not in (headers or {}):
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body)
            msg = parsed.get("msg") or parsed.get("error_description") or parsed.get("message") or err_body
        except Exception:
            msg = err_body or str(exc)
        raise SupabaseError(f"HTTP {exc.code}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise SupabaseError(f"Network error: {exc.reason}") from exc


class SupabaseClient:
    def __init__(self, url: str, anon_key: str) -> None:
        self.url = url.rstrip("/")
        self.anon_key = anon_key

    def is_configured(self) -> bool:
        return bool(self.url and self.anon_key)

    def _headers(self, access_token: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.anon_key,
            "Content-Type": "application/json",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        elif self.anon_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.anon_key}"
        return headers

    def exchange_pkce_code(self, auth_code: str, code_verifier: str) -> dict[str, Any]:
        data = _request_json(
            f"{self.url}/auth/v1/token?grant_type=pkce",
            method="POST",
            headers=self._headers(),
            payload={"auth_code": auth_code, "code_verifier": code_verifier},
        )
        return self._normalize_session(data)

    def refresh_session(self, refresh_token: str) -> dict[str, Any]:
        data = _request_json(
            f"{self.url}/auth/v1/token?grant_type=refresh_token",
            method="POST",
            headers=self._headers(),
            payload={"refresh_token": refresh_token},
        )
        return self._normalize_session(data)

    def fetch_user(self, access_token: str) -> dict[str, Any]:
        return _request_json(
            f"{self.url}/auth/v1/user",
            method="GET",
            headers=self._headers(access_token),
        )

    def get_or_create_account(self, access_token: str) -> dict[str, Any]:
        return _request_json(
            f"{self.url}/rest/v1/rpc/get_or_create_gdquiet_account",
            method="POST",
            headers=self._headers(access_token),
            payload={},
        )

    def update_mute_percent(self, access_token: str, percent: float) -> dict[str, Any]:
        return _request_json(
            f"{self.url}/rest/v1/rpc/update_gdquiet_mute_percent",
            method="POST",
            headers=self._headers(access_token),
            payload={"p_mute_at_percent": float(percent)},
        )

    def poll_account_state(self, access_token: str, user_id: str) -> dict[str, Any] | None:
        encoded_uid = urllib.parse.quote(user_id, safe="")
        rows = _request_json(
            f"{self.url}/rest/v1/gdquiet_accounts?user_id=eq.{encoded_uid}&select=*",
            method="GET",
            headers=self._headers(access_token),
            timeout=6.0,
        )
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    def _normalize_session(self, data: dict[str, Any]) -> dict[str, Any]:
        user = data.get("user") or {}
        expires_in = int(data.get("expires_in") or 3600)
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": time.time() + max(60, expires_in - 60),
            "user_id": user.get("id", ""),
        }

    def sign_in_with_discord(self, timeout_seconds: float = 180.0) -> dict[str, Any]:
        if not self.is_configured():
            raise SupabaseError("Supabase URL and Anon Key are not configured.")

        verifier, challenge = _pkce_pair()
        result_box: dict[str, Any] = {}
        done_event = threading.Event()

        srv: HTTPServer | None = None
        bound_port = 54321
        for candidate_port in (54321, 54322, 54323, 54324, 54325):
            try:
                class CallbackHandler(BaseHTTPRequestHandler):
                    def log_message(self, *args: Any) -> None:
                        pass

                    def _send_html(self, title: str, message: str, status: int = 200) -> None:
                        body = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body{{font-family:Segoe UI,sans-serif;background:#111827;color:#f3f4f6;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#1f2937;padding:28px 32px;border-radius:12px;max-width:460px;text-align:center;box-shadow:0 12px 32px rgba(0,0,0,.4)}}
h1{{margin:0 0 10px;font-size:20px}} p{{margin:0;color:#cbd5e1;font-size:14px;line-height:1.5}}
</style></head>
<body><div class="card"><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></div></body></html>"""
                        encoded = body.encode("utf-8")
                        self.send_response(status)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(encoded)))
                        self.end_headers()
                        self.wfile.write(encoded)

                    def do_GET(self) -> None:
                        parsed = urllib.parse.urlparse(self.path)
                        qs = urllib.parse.parse_qs(parsed.query)

                        if parsed.path == "/callback":
                            if "error" in qs:
                                desc = qs.get("error_description", qs["error"])[0]
                                result_box["error"] = desc
                                self._send_html("Sign-in Failed", desc, 400)
                                done_event.set()
                                return

                            if "code" in qs:
                                result_box["code"] = qs["code"][0]
                                self._send_html(
                                    "GD Quiet Signed In",
                                    "Discord authentication succeeded. You can close this browser tab and return to GDQuiet.exe.",
                                )
                                done_event.set()
                                return

                            # Fallback if implicit grant returned tokens in the URL hash (#access_token=...)
                            bridge_html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Completing Sign-in...</title></head>
<body style="font-family:Segoe UI,sans-serif;background:#111827;color:#f3f4f6;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<p id="msg">Completing Discord sign-in...</p>
<script>
const hash = window.location.hash ? window.location.hash.substring(1) : "";
if (hash) {
  window.location.replace("/fragment?" + hash);
} else {
  document.getElementById("msg").textContent = "No authorization code was received from Supabase.";
}
</script></body></html>"""
                            raw = bridge_html.encode("utf-8")
                            self.send_response(200)
                            self.send_header("Content-Type", "text/html; charset=utf-8")
                            self.send_header("Content-Length", str(len(raw)))
                            self.end_headers()
                            self.wfile.write(raw)
                            return

                        if parsed.path == "/fragment":
                            if "access_token" in qs and "refresh_token" in qs:
                                result_box["implicit"] = {
                                    "access_token": qs["access_token"][0],
                                    "refresh_token": qs["refresh_token"][0],
                                    "expires_in": int(qs.get("expires_in", ["3600"])[0]),
                                }
                                self._send_html(
                                    "GD Quiet Signed In",
                                    "Discord authentication succeeded. You can close this tab and return to GDQuiet.exe.",
                                )
                            else:
                                err = qs.get("error_description", ["Authentication failed."])[0]
                                result_box["error"] = err
                                self._send_html("Sign-in Failed", err, 400)
                            done_event.set()
                            return

                        self.send_response(404)
                        self.end_headers()

                srv = HTTPServer(("127.0.0.1", candidate_port), CallbackHandler)
                srv.timeout = 0.5
                bound_port = candidate_port
                break
            except OSError:
                continue

        if srv is None:
            raise SupabaseError("Could not bind a local callback port (54321-54325).")

        def serve_loop() -> None:
            deadline = time.time() + timeout_seconds
            while not done_event.is_set() and time.time() < deadline:
                srv.handle_request()

        thread = threading.Thread(target=serve_loop, daemon=True)
        thread.start()

        redirect_uri = f"http://127.0.0.1:{bound_port}/callback"
        params = urllib.parse.urlencode(
            {
                "provider": "discord",
                "redirect_to": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "s256",
            }
        )
        auth_url = f"{self.url}/auth/v1/authorize?{params}"
        webbrowser.open(auth_url)

        completed = done_event.wait(timeout=timeout_seconds)
        try:
            srv.server_close()
        except Exception:
            pass

        if not completed:
            raise SupabaseError("Discord sign-in timed out. Please try again.")
        if "error" in result_box:
            raise SupabaseError(result_box["error"])
        if "code" in result_box:
            return self.exchange_pkce_code(result_box["code"], verifier)
        if "implicit" in result_box:
            imp = result_box["implicit"]
            user = self.fetch_user(imp["access_token"])
            return {
                "access_token": imp["access_token"],
                "refresh_token": imp["refresh_token"],
                "expires_at": time.time() + max(60, int(imp["expires_in"]) - 60),
                "user_id": user.get("id", ""),
            }
        raise SupabaseError("No sign-in credentials returned.")


class AccountSyncWorker:
    """Watches gdquiet_accounts via Supabase Realtime WebSocket with HTTP polling fallback."""

    def __init__(
        self,
        client: SupabaseClient,
        get_session: Callable[[], dict[str, Any] | None],
        on_session_refreshed: Callable[[dict[str, Any]], None],
        on_account_update: Callable[[dict[str, Any]], None],
        on_error: Callable[[str], None],
    ) -> None:
        self._client = client
        self._get_session = get_session
        self._on_session_refreshed = on_session_refreshed
        self._on_account_update = on_account_update
        self._on_error = on_error
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._stop.clear()
        poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        poll_thread.start()
        self._threads = [poll_thread]
        if HAS_WEBSOCKETS:
            rt_thread = threading.Thread(target=self._realtime_loop, daemon=True)
            rt_thread.start()
            self._threads.append(rt_thread)

    def stop(self) -> None:
        self._stop.set()

    def _valid_session(self) -> dict[str, Any] | None:
        session = self._get_session()
        if not session or not self._client.is_configured():
            return None
        expires_at = float(session.get("expires_at") or 0.0)
        if time.time() >= expires_at and session.get("refresh_token"):
            try:
                refreshed = self._client.refresh_session(session["refresh_token"])
                self._on_session_refreshed(refreshed)
                return refreshed
            except Exception as exc:
                self._on_error(f"Session refresh failed: {exc}")
                return None
        return session

    def _poll_loop(self) -> None:
        last_playing = False
        while not self._stop.is_set():
            interval = 0.6 if last_playing else 2.0
            session = self._valid_session()
            if session and session.get("user_id"):
                try:
                    row = self._client.poll_account_state(
                        session["access_token"], session["user_id"]
                    )
                    if row:
                        last_playing = bool(row.get("is_playing"))
                        self._on_account_update(row)
                except Exception as exc:
                    self._on_error(str(exc))
            self._stop.wait(interval)

    def _realtime_loop(self) -> None:
        while not self._stop.is_set():
            session = self._valid_session()
            if not session or not session.get("user_id"):
                self._stop.wait(2.0)
                continue

            parsed = urllib.parse.urlparse(self._client.url)
            scheme = "wss" if parsed.scheme == "https" else "ws"
            ws_url = (
                f"{scheme}://{parsed.netloc}/realtime/v1/websocket"
                f"?apikey={urllib.parse.quote(self._client.anon_key, safe='')}&vsn=1.0.0"
            )
            user_id = session["user_id"]
            topic = f"realtime:public:gdquiet_accounts:user_id=eq.{user_id}"

            try:
                with ws_connect(ws_url, open_timeout=10, close_timeout=2) as ws:
                    join_msg = {
                        "topic": topic,
                        "event": "phx_join",
                        "payload": {
                            "config": {
                                "broadcast": {"self": False},
                                "presence": {"key": ""},
                                "postgres_changes": [
                                    {
                                        "event": "UPDATE",
                                        "schema": "public",
                                        "table": "gdquiet_accounts",
                                        "filter": f"user_id=eq.{user_id}",
                                    }
                                ],
                            },
                            "access_token": session["access_token"],
                        },
                        "ref": "1",
                    }
                    ws.send(json.dumps(join_msg))
                    last_hb = time.time()
                    ref_counter = 2

                    while not self._stop.is_set():
                        current = self._get_session()
                        if not current or current.get("user_id") != user_id:
                            break
                        if time.time() >= float(current.get("expires_at") or 0.0):
                            break

                        if time.time() - last_hb >= 25.0:
                            ws.send(
                                json.dumps(
                                    {
                                        "topic": "phoenix",
                                        "event": "heartbeat",
                                        "payload": {},
                                        "ref": str(ref_counter),
                                    }
                                )
                            )
                            ref_counter += 1
                            last_hb = time.time()

                        try:
                            raw = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue

                        msg = json.loads(raw)
                        if msg.get("event") == "postgres_changes":
                            record = (msg.get("payload") or {}).get("data", {}).get("record")
                            if isinstance(record, dict):
                                self._on_account_update(record)
            except Exception:
                self._stop.wait(3.0)
