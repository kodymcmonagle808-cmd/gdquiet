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
:root{
  color-scheme:dark;
  --bg:#1e1f22;
  --bar:#18191c;
  --sunken:#141518;
  --ctrl:#2e3037;
  --ctrl-hover:#373a42;
  --line:#31333a;
  --line2:#3e414a;
  --ink:#eceef2;
  --muted:#a1a6b3;
  --faint:#757a87;
  --accent:#4c7dd9;
  --accent-hover:#5b8be4;
  --green:#4ea864;
  --amber:#d4a23b;
  --red:#d95b57;
  --mono:"Cascadia Mono",Consolas,ui-monospace,monospace;
  font-family:"Segoe UI Variable","Segoe UI",system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;padding:12px;background:transparent;color:var(--ink)}
.panel{max-width:520px;background:var(--bg);border:1px solid var(--line2);border-radius:2px;overflow:hidden}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 12px;background:var(--bar);border-bottom:1px solid var(--line)}
.brand{font-size:11.5px;font-weight:600;letter-spacing:.04em;color:var(--muted)}
.state{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:600;color:var(--green)}
.dot{width:6px;height:6px;border-radius:50%;background:currentColor}
.state.muted{color:var(--red)}
.state.paused{color:var(--amber)}
.body{padding:14px 14px 12px}
.reading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:10px}
.percent{font-family:var(--mono);font-size:40px;line-height:1;font-weight:600;font-variant-numeric:tabular-nums;color:var(--ink)}
.reading-label{font-size:12px;color:var(--faint)}
.meter{position:relative;height:6px;border-radius:1px;background:var(--sunken);border:1px solid var(--line);overflow:hidden}
.fill{height:100%;width:0;background:var(--accent);transition:width .16s linear}
.panel.is-muted .fill{background:var(--red)}
.details{display:flex;justify-content:space-between;gap:10px;margin-top:9px;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.details strong{color:var(--ink);font-weight:600}
.controls{display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--bar);border-top:1px solid var(--line)}
button{border:1px solid var(--line2);border-radius:2px;height:26px;padding:0 10px;background:var(--ctrl);color:var(--ink);
font:inherit;font-size:12px;font-weight:500;cursor:pointer;white-space:nowrap}
button:hover{background:var(--ctrl-hover)}
button.primary{background:var(--accent);border-color:rgba(255,255,255,.08);color:#fff}
button.primary:hover{background:var(--accent-hover)}
.threshold-label{white-space:nowrap;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.threshold-label strong{color:var(--ink);font-weight:600}
.threshold{-webkit-appearance:none;appearance:none;flex:1;min-width:90px;height:16px;background:transparent;cursor:pointer}
.threshold::-webkit-slider-runnable-track{height:3px;border-radius:1px;background:var(--line2)}
.threshold::-webkit-slider-thumb{-webkit-appearance:none;width:9px;height:14px;border-radius:2px;background:#eceef2;border:1px solid var(--line2);margin-top:-5.5px}
.error{display:none;padding:7px 12px;border-top:1px solid var(--line);background:#2b1b1c;color:#efb0ae;font-size:11.5px}
</style>
</head>
<body>
<main id="panel" class="panel">
  <div class="top">
    <div class="brand">GD QUIET</div>
    <div id="state" class="state"><span class="dot"></span><span id="state-text">Connecting</span></div>
  </div>
  <div class="body">
    <div class="reading">
      <div id="percent" class="percent">--%</div>
      <div class="reading-label">Geometry Dash progress</div>
    </div>
    <div class="meter"><div id="fill" class="fill"></div></div>
    <div class="details">
      <span>Threshold: <strong id="threshold-text">--%</strong></span>
      <span id="watch-text">Connecting...</span>
    </div>
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
    byId('panel').classList.toggle('is-muted',Boolean(data.muted));
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
    byId('state-text').textContent=data.muted?'Muted':(!watching?'Paused':'Clear');
    byId('error').style.display='none';
  }catch(error){
    byId('error').textContent='GD Quiet is not responding. Check that the OBS overlay is enabled in the app.';
    byId('error').style.display='block';
    byId('state').className='state paused';
    byId('state-text').textContent='Offline';
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
