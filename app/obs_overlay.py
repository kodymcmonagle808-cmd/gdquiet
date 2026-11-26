from __future__ import annotations

import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Callable
from urllib.parse import urlparse


OVERLAY_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GD Quiet OBS Overlay</title>
<style>
:root{color-scheme:dark;font-family:Inter,"Segoe UI",sans-serif}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:transparent;color:#f1f5f9}
.panel{max-width:520px;padding:18px;border:1px solid #293a56;border-radius:16px;
background:linear-gradient(145deg,rgba(21,31,51,.97),rgba(12,20,36,.97));
box-shadow:0 12px 36px rgba(0,0,0,.32)}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{font-size:12px;font-weight:800;letter-spacing:.11em;color:#a0aec0}
.state{display:flex;align-items:center;gap:7px;padding:6px 10px;border-radius:999px;
background:#163b2b;color:#86efac;font-size:12px;font-weight:800}
.dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.state.muted{background:#461f2a;color:#fca5a5}
.state.paused{background:#25324a;color:#cbd5e1}
.reading{display:flex;align-items:baseline;gap:8px;margin:18px 0 12px}
.percent{font-size:48px;line-height:1;font-weight:800;letter-spacing:-.06em}
.reading-label{font-size:12px;color:#a0aec0}
.meter{height:7px;border-radius:8px;background:#0c1424;overflow:hidden}
.fill{height:100%;width:0;background:linear-gradient(90deg,#38bdf8,#818cf8);transition:width .2s}
.details{display:flex;justify-content:space-between;gap:10px;margin-top:13px;font-size:12px;color:#a0aec0}
.details strong{color:#f1f5f9}
.controls{display:flex;align-items:center;gap:12px;margin-top:18px;padding-top:14px;border-top:1px solid #293a56}
button{border:0;border-radius:9px;padding:9px 12px;background:#293a56;color:#f1f5f9;
font:inherit;font-size:12px;font-weight:700;cursor:pointer}
button:hover{background:#3a4d6c}
button.primary{background:#6d78e8}
button.primary:hover{background:#818cf8}
.threshold{flex:1;min-width:100px;accent-color:#38bdf8}
.threshold-label{white-space:nowrap;font-size:12px;color:#a0aec0}
.error{display:none;margin-top:10px;color:#fca5a5;font-size:11px}
</style>
</head>
<body>
<main class="panel">
  <div class="top">
    <div class="brand">GD QUIET <span style="color:#38bdf8">/</span> LIVE</div>
    <div id="state" class="state"><span class="dot"></span><span id="state-text">Connecting</span></div>
  </div>
  <div class="reading">
    <div id="percent" class="percent">--%</div>
    <div class="reading-label">Geometry Dash progress</div>
  </div>
  <div class="meter"><div id="fill" class="fill"></div></div>
  <div class="details">
    <span>Threshold <strong id="threshold-text">--%</strong></span>
    <span id="watch-text">Connecting…</span>
  </div>
  <div class="controls">
    <button id="watch-button" class="primary" type="button">Pause watching</button>
    <span class="threshold-label">Mute at <strong id="slider-value">--%</strong></span>
    <input id="threshold" class="threshold" type="range" min="0" max="100" step="1" value="50" aria-label="Mute threshold">
  </div>
  <div id="error" class="error"></div>
</main>
<script>
const byId=id=>document.getElementById(id);
let watching=true;
let threshold=50;
let requestPending=false;
async function refresh(){
  try{
    const response=await fetch('/api/status',{cache:'no-store'});
    if(!response.ok)throw new Error('Status unavailable');
    const data=await response.json();
    watching=Boolean(data.watching);
    threshold=Number(data.threshold)||0;
    const pct=Number.isFinite(data.percent)?Math.max(0,Math.min(100,data.percent)):null;
    byId('percent').textContent=pct===null?'--%':`${pct.toFixed(1)}%`;
    byId('fill').style.width=pct===null?'0%':`${pct}%`;
    byId('threshold-text').textContent=`${threshold.toFixed(0)}%`;
    if(document.activeElement!==byId('threshold')){
      byId('threshold').value=String(threshold);
      byId('slider-value').textContent=`${threshold.toFixed(0)}%`;
    }
    byId('watch-button').textContent=watching?'Pause watching':'Resume watching';
    byId('watch-text').textContent=data.testing?'Screen test running':(watching?'Watching %':'Watching paused');
    const state=byId('state');
    state.className='state'+(data.muted?' muted':(!watching?' paused':''));
    byId('state-text').textContent=data.muted?'MUTED':(!watching?'PAUSED':'CLEAR');
    byId('error').style.display='none';
  }catch(error){
    byId('error').textContent='GD Quiet is not responding. Check that the OBS overlay is enabled in the app.';
    byId('error').style.display='block';
    byId('state').className='state paused';
    byId('state-text').textContent='OFFLINE';
  }
}
async function control(payload){
  if(requestPending)return;
  requestPending=true;
  try{
    const response=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(!response.ok)throw new Error('Could not update GD Quiet');
    await refresh();
  }catch(error){
    byId('error').textContent='Control failed. Make sure GD Quiet is running.';
    byId('error').style.display='block';
  }finally{requestPending=false;}
}
byId('watch-button').addEventListener('click',()=>control({action:'watch',enabled:!watching}));
byId('threshold').addEventListener('input',event=>byId('slider-value').textContent=`${event.target.value}%`);
byId('threshold').addEventListener('change',event=>control({action:'threshold',value:Number(event.target.value)}));
refresh();
setInterval(refresh,500);
</script>
</body>
</html>
"""


class OverlayServer:
    """Local-only HTTP server for the OBS browser-source status overlay."""

    def __init__(
        self,
        get_status: Callable[[], dict[str, Any]],
        on_control: Callable[[dict[str, Any]], bool],
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("The OBS overlay server must bind to loopback.")
        self.host = host
        self.port = int(port)
        self._get_status = get_status
        self._on_control = on_control
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def url(self) -> str:
        if self._server is None:
            return f"http://{self.host}:{self.port}/"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> str:
        if self.running:
            return self.url
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GDQuietOverlay"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'",
                )
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, status: int, data: dict[str, Any]) -> None:
                self._send(status, json.dumps(data, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

            def _is_local_host(self) -> bool:
                hostname = urlparse("//" + self.headers.get("Host", "")).hostname
                return hostname in {"127.0.0.1", "localhost"}

            def do_GET(self) -> None:
                if not self._is_local_host():
                    self._send_json(403, {"error": "Local requests only"})
                    return
                if self.path == "/":
                    self._send(200, OVERLAY_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if self.path == "/api/status":
                    try:
                        self._send_json(200, owner._get_status())
                    except (TypeError, ValueError):
                        self._send_json(500, {"error": "Status unavailable"})
                    return
                self._send_json(404, {"error": "Not found"})

            def do_POST(self) -> None:
                if not self._is_local_host() or self.path != "/api/control":
                    self._send_json(403, {"error": "Local control endpoint only"})
                    return
                origin = self.headers.get("Origin")
                if origin:
                    parsed_origin = urlparse(origin)
                    if (
                        parsed_origin.scheme != "http"
                        or parsed_origin.hostname not in {"127.0.0.1", "localhost"}
                        or parsed_origin.netloc.lower() != self.headers.get("Host", "").lower()
                    ):
                        self._send_json(403, {"error": "Cross-origin controls are not allowed"})
                        return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 2048:
                        raise ValueError("Invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or not owner._valid_control(payload):
                        raise ValueError("Invalid control request")
                except (ValueError, json.JSONDecodeError):
                    self._send_json(400, {"error": "Invalid control request"})
                    return
                try:
                    accepted = owner._on_control(payload)
                except Exception:
                    self._send_json(500, {"error": "Could not apply control"})
                    return
                if not accepted:
                    self._send_json(503, {"error": "GD Quiet is closing"})
                    return
                self._send_json(202, {"accepted": True})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, name="GDQuiet-OBS-Overlay", daemon=True)
        self._thread.start()
        return self.url

    @staticmethod
    def _valid_control(payload: dict[str, Any]) -> bool:
        if payload.get("action") == "watch":
            return set(payload) == {"action", "enabled"} and isinstance(payload.get("enabled"), bool)
        if payload.get("action") == "threshold":
            value = payload.get("value")
            if (
                set(payload) != {"action", "value"}
                or not isinstance(value, (int, float))
                or isinstance(value, bool)
            ):
                return False
            try:
                return math.isfinite(float(value)) and 0 <= value <= 100
            except (OverflowError, TypeError, ValueError):
                return False
        return False

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
