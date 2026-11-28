"""Discord sign-in through Supabase Auth (OAuth with PKCE).

Flow:
  1. Start a one-shot HTTP listener on 127.0.0.1:CALLBACK_PORT.
  2. Open the browser at Supabase's /authorize endpoint for the Discord provider.
  3. Supabase redirects back to the listener with ?code=...
  4. Exchange the code (plus our PKCE verifier) for a session.

The session is stored in %APPDATA%\\GDQuiet\\session.bin, encrypted with Windows
DPAPI so only the signed-in Windows user can read it.

The callback URL must be in Supabase → Authentication → URL Configuration →
Redirect URLs:  http://127.0.0.1:47819/auth/callback
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import supabase_config

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 47819
CALLBACK_PATH = "/auth/callback"
CALLBACK_URL = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
SIGN_IN_TIMEOUT_S = 300.0
REFRESH_MARGIN_S = 120.0
HTTP_TIMEOUT_S = 15.0


class AuthError(RuntimeError):
    """Raised when a Supabase auth request fails."""


def supabase_settings() -> tuple[str, str]:
    url = os.environ.get("GDQUIET_SUPABASE_URL") or supabase_config.SUPABASE_URL
    key = os.environ.get("GDQUIET_SUPABASE_KEY") or supabase_config.SUPABASE_KEY
    return (url or "").strip().rstrip("/"), (key or "").strip()


# ---------------------------------------------------------------------------
# PKCE + URL helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(supabase_url: str, code_challenge: str, redirect_to: str = CALLBACK_URL) -> str:
    query = urllib.parse.urlencode(
        {
            "provider": "discord",
            "redirect_to": redirect_to,
            "code_challenge": code_challenge,
            "code_challenge_method": "s256",
        }
    )
    return f"{supabase_url}/auth/v1/authorize?{query}"


def user_from_session(session: dict[str, Any]) -> dict[str, str]:
    """Extract the Discord identity Supabase stores in user_metadata."""
    user = session.get("user") or {}
    meta = user.get("user_metadata") or {}
    claims = meta.get("custom_claims") or {}
    username = str(meta.get("name") or meta.get("user_name") or "").strip()
    if username.endswith("#0"):
        username = username[:-2]
    display = str(claims.get("global_name") or meta.get("full_name") or username or "Discord user").strip()
    if display.endswith("#0"):
        display = display[:-2]
    return {
        "id": str(user.get("id") or ""),
        "discord_id": str(meta.get("provider_id") or meta.get("sub") or ""),
        "username": username,
        "display_name": display,
        "avatar_url": str(meta.get("avatar_url") or meta.get("picture") or ""),
    }


# ---------------------------------------------------------------------------
# DPAPI-protected session storage
# ---------------------------------------------------------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    if not fn(ctypes.byref(blob_in), None, None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise OSError(ctypes.GetLastError(), "DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def save_session(path: Path, session: dict[str, Any]) -> None:
    payload = _dpapi(json.dumps(session).encode("utf-8"), protect=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def load_session(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(_dpapi(path.read_bytes(), protect=False).decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


# ---------------------------------------------------------------------------
# Supabase REST calls
# ---------------------------------------------------------------------------


def _post_json(url: str, api_key: str, body: dict[str, Any], bearer: str | None = None) -> dict[str, Any]:
    headers = {"apikey": api_key, "Content-Type": "application/json", "User-Agent": "GDQuiet"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            err = json.loads(exc.read().decode("utf-8"))
            detail = str(err.get("error_description") or err.get("msg") or err.get("message") or err.get("error") or "")
        except Exception:
            pass
        raise AuthError(detail or f"Supabase returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"Could not reach Supabase: {exc.reason}") from exc
    return json.loads(raw.decode("utf-8")) if raw else {}


def _normalize_session(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("access_token") or not data.get("refresh_token"):
        raise AuthError("Supabase did not return a session")
    expires_at = data.get("expires_at")
    if not expires_at:
        expires_at = time.time() + float(data.get("expires_in") or 3600)
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": float(expires_at),
        "user": data.get("user") or {},
    }


def exchange_code(supabase_url: str, api_key: str, code: str, verifier: str) -> dict[str, Any]:
    data = _post_json(
        f"{supabase_url}/auth/v1/token?grant_type=pkce",
        api_key,
        {"auth_code": code, "code_verifier": verifier},
    )
    return _normalize_session(data)


def refresh_session(supabase_url: str, api_key: str, refresh_token: str) -> dict[str, Any]:
    data = _post_json(
        f"{supabase_url}/auth/v1/token?grant_type=refresh_token",
        api_key,
        {"refresh_token": refresh_token},
    )
    return _normalize_session(data)


# ---------------------------------------------------------------------------
# Local callback listener
# ---------------------------------------------------------------------------

_CALLBACK_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>GD Quiet</title>
<style>body{{margin:0;height:100vh;display:grid;place-items:center;background:#1a1b1e;color:#eceff4;
font:15px "Segoe UI",system-ui,sans-serif}}div{{text-align:center;max-width:420px;padding:24px}}
h1{{font-size:20px;margin:0 0 8px;color:{color}}}p{{color:#a5abb8;margin:0}}</style></head>
<body><div><h1>{title}</h1><p>{body}</p></div></body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] | None = None

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v}
        if params.get("code"):
            title, body, color = "Signed in", "You can close this tab and return to GD Quiet.", "#4fb06d"
        else:
            reason = params.get("error_description") or params.get("error") or "No sign-in code was returned."
            title, body, color = "Sign-in failed", html.escape(reason), "#d9625d"
        page = _CALLBACK_PAGE.format(title=title, body=body, color=color).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)
        type(self).result = params

    def log_message(self, *_args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Controller used by the app
# ---------------------------------------------------------------------------


class DiscordAuth:
    """Tracks the Discord sign-in state for the UI."""

    def __init__(
        self,
        session_path: Path,
        open_browser: Callable[[str], Any] = webbrowser.open,
    ) -> None:
        self._path = session_path
        self._open_browser = open_browser
        self._lock = threading.Lock()
        self._session: dict[str, Any] | None = None
        self._status = "signed_out"  # signed_out | waiting | signed_in
        self._message = ""
        self._cancel = threading.Event()
        self._attempt = 0
        self._sign_in_lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        url, key = supabase_settings()
        return bool(url and key)

    # ---- startup ----

    def load(self) -> None:
        """Load a saved session and refresh it in the background if it's close to expiring."""
        session = load_session(self._path)
        if session is None:
            return
        with self._lock:
            self._session = session
            self._status = "signed_in"
        if float(session.get("expires_at") or 0) - time.time() < REFRESH_MARGIN_S:
            threading.Thread(target=self._refresh_quietly, daemon=True).start()

    def _refresh_quietly(self) -> None:
        try:
            self.access_token()
        except Exception:
            pass

    def access_token(self) -> str | None:
        """Return a valid access token, refreshing it if needed. None when signed out."""
        # One refresh at a time: refresh tokens rotate, so concurrent refreshes would race.
        with self._refresh_lock:
            with self._lock:
                session = self._session
            if session is None:
                return None
            if float(session.get("expires_at") or 0) - time.time() >= REFRESH_MARGIN_S:
                return str(session["access_token"])
            url, key = supabase_settings()
            try:
                fresh = refresh_session(url, key, str(session["refresh_token"]))
            except AuthError as exc:
                # A rejected refresh token means the session is gone; network trouble does not.
                if "reach Supabase" not in str(exc):
                    self._clear(f"Signed out: {exc}")
                return None
            self._persist(fresh)
            with self._lock:
                if self._session is session:
                    self._session = fresh
            return str(fresh["access_token"])

    # ---- sign in / out ----

    def start_sign_in(self) -> None:
        if not self.configured:
            with self._lock:
                self._message = "Discord sign-in isn't configured in this build."
            return
        with self._lock:
            if self._status == "waiting":
                return
            self._attempt += 1
            attempt = self._attempt
            self._status = "waiting"
            self._message = "Finish signing in with Discord in your browser."
            self._cancel = threading.Event()
            cancel = self._cancel
        threading.Thread(target=self._sign_in_worker, args=(attempt, cancel), daemon=True).start()

    def cancel_sign_in(self) -> None:
        with self._lock:
            self._cancel.set()
            if self._status == "waiting":
                self._attempt += 1
                self._status = "signed_in" if self._session else "signed_out"
                self._message = "Sign-in cancelled."

    def _finish_attempt(self, attempt: int, session: dict[str, Any] | None, message: str) -> None:
        with self._lock:
            if attempt != self._attempt:
                return
            if session is not None:
                self._session = session
            self._status = "signed_in" if self._session else "signed_out"
            self._message = message

    def _sign_in_worker(self, attempt: int, cancel: threading.Event) -> None:
        # A cancelled attempt can hold the port for up to one poll interval; wait for it.
        if not self._sign_in_lock.acquire(timeout=3.0):
            self._finish_attempt(attempt, None, "A previous sign-in is still closing. Try again.")
            return
        try:
            self._run_sign_in(attempt, cancel)
        finally:
            self._sign_in_lock.release()

    def _run_sign_in(self, attempt: int, cancel: threading.Event) -> None:
        if cancel.is_set():
            return
        url, key = supabase_settings()
        verifier, challenge = make_pkce_pair()
        handler = type("_Handler", (_CallbackHandler,), {"result": None})
        try:
            server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), handler)
        except OSError:
            self._finish_attempt(
                attempt, None, f"Port {CALLBACK_PORT} is in use, so sign-in can't receive the reply. Close the program using it and try again."
            )
            return
        server.timeout = 0.5
        try:
            self._open_browser(build_authorize_url(url, challenge))
            deadline = time.monotonic() + SIGN_IN_TIMEOUT_S
            while handler.result is None:
                if cancel.is_set():
                    return
                if time.monotonic() > deadline:
                    self._finish_attempt(attempt, None, "Sign-in timed out. Try again.")
                    return
                server.handle_request()
        finally:
            server.server_close()

        params = handler.result or {}
        code = params.get("code")
        if not code:
            reason = params.get("error_description") or params.get("error") or "No sign-in code was returned."
            self._finish_attempt(attempt, None, f"Sign-in failed: {reason}")
            return
        try:
            session = exchange_code(url, key, code, verifier)
        except AuthError as exc:
            self._finish_attempt(attempt, None, f"Sign-in failed: {exc}")
            return
        self._persist(session)
        name = user_from_session(session)["display_name"]
        self._finish_attempt(attempt, session, f"Signed in as {name}.")

    def sign_out(self) -> None:
        with self._lock:
            session = self._session
        if session is not None:
            url, key = supabase_settings()

            def revoke() -> None:
                try:
                    _post_json(f"{url}/auth/v1/logout", key, {}, bearer=str(session.get("access_token") or ""))
                except Exception:
                    pass

            if url and key:
                threading.Thread(target=revoke, daemon=True).start()
        self._clear("Signed out.")

    def _clear(self, message: str) -> None:
        with self._lock:
            self._session = None
            self._status = "signed_out"
            self._message = message
        try:
            self._path.unlink(missing_ok=True)
        except Exception:
            pass

    def _persist(self, session: dict[str, Any]) -> None:
        try:
            save_session(self._path, session)
        except Exception:
            # Sign-in still works for this run; it just won't be remembered.
            pass

    # ---- UI state ----

    def to_state(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
            status = self._status
            message = self._message
        return {
            "configured": self.configured,
            "status": status,
            "message": message,
            "user": user_from_session(session) if session else None,
        }
