import atexit
from http.server import BaseHTTPRequestHandler, HTTPServer
from pycaw.pycaw import AudioUtilities

GD_NAME = 'geometrydash.exe'
PORT = 8765
muted = False
gd_seen = False

def apply(mute):
    global gd_seen
    gd_found = False
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return gd_found

    for s in sessions:
        try:
            if s.Process and s.Process.name().lower() == GD_NAME:
                gd_found = True
                continue
            s.SimpleAudioVolume.SetMute(1 if mute else 0, None)
        except Exception:
            pass

    if gd_found:
        gd_seen = True
    return gd_found

atexit.register(apply, False)

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        global muted, gd_seen
        muted = (self.path == '/mute')
        if not muted:
            gd_seen = False
        apply(muted)
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *a):
        pass

if __name__ == '__main__':
    srv = HTTPServer(('127.0.0.1', PORT), H)
    srv.timeout = 1
    try:
        while True:
            srv.handle_request()
            if muted:
                if not apply(True) and gd_seen:
                    muted = False
                    gd_seen = False
                    apply(False)
    finally:
        apply(False)
