from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import mss
import psutil
from PIL import Image, ImageChops, ImageOps
import winrt.windows.graphics.imaging as wgi
import winrt.windows.media.ocr as wmo
import winrt.windows.storage.streams as wss

user32 = ctypes.windll.user32
try:
    dwmapi = ctypes.windll.dwmapi
except OSError:
    dwmapi = None

DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

# Maps white-ish HUD score (0..255) to dark ink (0..255, inverted for OCR).
_WHITE_LUT = [255 if v < 110 else max(0, 255 - int((v - 110) * 2.2)) for v in range(256)]
_LUMA_LUT = [255 if v < 165 else max(0, 255 - int((v - 165) * 2.8)) for v in range(256)]

_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%")


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def get_virtual_screen_bounds() -> tuple[int, int, int, int]:
    left = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    top = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    return left, top, max(1, width), max(1, height)


def _is_cloaked(hwnd: int) -> bool:
    if dwmapi is None:
        return False
    cloaked = wintypes.DWORD(0)
    res = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_CLOAKED),
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return res == 0 and cloaked.value != 0


def find_window_client_rect(process_name: str) -> tuple[int, int, int, int] | None:
    target = (process_name or "").strip().lower()
    if not target:
        return None

    matching_pids: set[int] = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == target:
                matching_pids.add(int(proc.info["pid"]))
        except Exception:
            continue

    if not matching_pids:
        return None

    best_rect: tuple[int, int, int, int] | None = None
    best_area = 0

    def callback(hwnd: int, _lparam: int) -> bool:
        nonlocal best_rect, best_area
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if _is_cloaked(hwnd):
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in matching_pids:
            return True

        cr = RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(cr)):
            return True
        w = int(cr.right - cr.left)
        h = int(cr.bottom - cr.top)
        if w < 120 or h < 120:
            return True

        pt = POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return True

        area = w * h
        if area > best_area:
            best_area = area
            best_rect = (int(pt.x), int(pt.y), w, h)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return best_rect


def compute_capture_rect(
    watch_mode: str,
    target_process: str,
    custom_region: dict[str, Any] | None,
) -> tuple[dict[str, int] | None, str]:
    """Return (mss monitor dict, human status label) for the current watch target."""
    if watch_mode == "custom_region" and isinstance(custom_region, dict):
        kind = custom_region.get("kind")
        if kind == "window_rel":
            proc = str(custom_region.get("process") or target_process or "geometrydash.exe")
            rect = find_window_client_rect(proc)
            if rect is None:
                return None, f"Waiting for {proc} window..."
            wx, wy, ww, wh = rect
            left = wx + int(round(float(custom_region["rx"]) * ww))
            top = wy + int(round(float(custom_region["ry"]) * wh))
            width = max(24, int(round(float(custom_region["rw"]) * ww)))
            height = max(18, int(round(float(custom_region["rh"]) * wh)))
            return {"left": left, "top": top, "width": width, "height": height}, proc

        if kind == "screen":
            left = int(custom_region.get("left", 0))
            top = int(custom_region.get("top", 0))
            width = max(24, int(custom_region.get("width", 200)))
            height = max(18, int(custom_region.get("height", 80)))
            return {"left": left, "top": top, "width": width, "height": height}, "Custom screen region"

    proc = (target_process or "geometrydash.exe").strip()
    rect = find_window_client_rect(proc)
    if rect is None:
        return None, f"Waiting for {proc} window..."

    wx, wy, ww, wh = rect
    # Scan only the top 10% of the game window.
    left = wx
    top = wy
    width = max(80, ww)
    height = max(24, int(wh * 0.10))
    return {"left": left, "top": top, "width": width, "height": height}, proc


def preprocess_for_ocr(img: Image.Image, luma_fallback: bool = False) -> Image.Image:
    rgb = img.convert("RGB")
    if luma_fallback:
        ink = rgb.convert("L").point(_LUMA_LUT)
    else:
        r, g, b = rgb.split()
        mn = ImageChops.darker(ImageChops.darker(r, g), b)
        mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
        sat = ImageChops.subtract(mx, mn)
        ink = ImageChops.subtract(mn, sat).point(_WHITE_LUT)

    scale = 3 if rgb.height < 80 else (2 if rgb.height < 150 else 1)
    if scale > 1:
        ink = ink.resize((ink.width * scale, ink.height * scale), Image.Resampling.LANCZOS)
    return ImageOps.expand(ink, border=24, fill=255).convert("RGBA")


def parse_percent_from_text(text: str, last_percent: float | None = None) -> float | None:
    if not text:
        return None
    norm = text.replace("\n", " ").replace("\r", " ")
    # Windows OCR commonly reads bold outlined '%' as '0/0' or 'o/0'.
    norm = re.sub(r"[0oO°]\s*/\s*[0oO]", "%", norm)

    candidates: list[float] = []
    for match in _PERCENT_RE.finditer(norm):
        raw = match.group(1).replace(",", ".")
        try:
            val = float(raw)
        except ValueError:
            continue
        if val > 100.0 and "." not in raw:
            if len(raw) == 3:
                val = val / 10.0
            elif len(raw) == 4:
                val = val / 100.0
        if 0.0 <= val <= 100.0:
            candidates.append(round(val, 2))

    if not candidates:
        return None
    if len(candidates) == 1 or last_percent is None:
        return candidates[0]
    return min(candidates, key=lambda c: abs(c - last_percent))


class WinRtPercentOcr:
    """Thread-affine Windows.Media.Ocr recognizer for fast in-memory crops."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._engine = wmo.OcrEngine.try_create_from_user_profile_languages()

    def available(self) -> bool:
        return self._engine is not None

    def recognize_text(self, rgba_img: Image.Image) -> str:
        if self._engine is None:
            return ""
        img = rgba_img if rgba_img.mode == "RGBA" else rgba_img.convert("RGBA")
        dw = wss.DataWriter()
        dw.write_bytes(img.tobytes())
        ibuf = dw.detach_buffer()
        sb = wgi.SoftwareBitmap.create_copy_from_buffer(
            ibuf,
            wgi.BitmapPixelFormat.RGBA8,
            img.width,
            img.height,
        )
        res = self._loop.run_until_complete(self._engine.recognize_async(sb))
        return str(getattr(res, "text", "") or "").strip()

    def read_percent(
        self,
        captured: Image.Image,
        last_percent: float | None = None,
    ) -> tuple[float | None, str]:
        primary = preprocess_for_ocr(captured, luma_fallback=False)
        text = self.recognize_text(primary)
        pct = parse_percent_from_text(text, last_percent=last_percent)
        if pct is not None:
            return pct, text

        fallback = preprocess_for_ocr(captured, luma_fallback=True)
        fb_text = self.recognize_text(fallback)
        fb_pct = parse_percent_from_text(fb_text, last_percent=last_percent)
        if fb_pct is not None:
            return fb_pct, fb_text
        return None, text or fb_text

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:
            pass


class PercentStateDebouncer:
    """Converts noisy frame-by-frame OCR readings into clean mute/unmute transitions."""

    def __init__(self, missing_timeout_s: float = 0.65) -> None:
        self.missing_timeout_s = missing_timeout_s
        self.is_muted = False
        self.last_percent: float | None = None
        self.last_seen_at = 0.0
        self._high_streak = 0

    def reset(self) -> None:
        self.is_muted = False
        self.last_percent = None
        self.last_seen_at = 0.0
        self._high_streak = 0

    def update(
        self,
        percent: float | None,
        threshold: float,
        unmute_at_100: bool,
        now: float | None = None,
    ) -> tuple[bool, bool]:
        """Returns (should_mute, is_playing)."""
        ts = time.monotonic() if now is None else now

        if percent is None:
            self._high_streak = 0
            if self.last_seen_at > 0 and (ts - self.last_seen_at) >= self.missing_timeout_s:
                self.is_muted = False
                self.last_percent = None
                return False, False
            return self.is_muted, (self.last_seen_at > 0 and (ts - self.last_seen_at) < self.missing_timeout_s)

        self.last_seen_at = ts
        prev = self.last_percent
        self.last_percent = percent

        if unmute_at_100 and percent >= 99.95:
            self._high_streak = 0
            self.is_muted = False
            return False, True

        if percent < threshold:
            self._high_streak = 0
            self.is_muted = False
            return False, True

        # percent >= threshold: accept immediately on smooth progression, or 2 consecutive frames on a jump.
        smooth_cross = prev is not None and (threshold - 15.0) <= prev <= percent <= (prev + 15.0)
        self._high_streak += 1
        if self.is_muted or smooth_cross or self._high_streak >= 2:
            self.is_muted = True

        return self.is_muted, True


@dataclass
class ScreenReading:
    percent: float | None
    raw_text: str
    should_mute: bool
    is_playing: bool
    status_text: str
    preview_image: Image.Image | None


class ScreenTestSession:
    """Tracks a timed dry-run test window showing percentages seen on screen."""

    def __init__(self, duration_s: float = 30.0) -> None:
        self.duration_s = float(duration_s)
        self.active = False
        self.completed = False
        self.start_time = 0.0
        self.total_frames = 0
        self.valid_reads = 0
        self.peak_percent: float | None = None
        self.last_percent: float | None = None
        self.samples: list[tuple[float, float | None]] = []
        self.milestones: list[str] = []
        self._last_milestone_pct: float | None = None

    def start(self, now: float | None = None) -> None:
        ts = time.monotonic() if now is None else now
        self.active = True
        self.completed = False
        self.start_time = ts
        self.total_frames = 0
        self.valid_reads = 0
        self.peak_percent = None
        self.last_percent = None
        self.samples = []
        self.milestones = []
        self._last_milestone_pct = None

    def stop(self) -> None:
        if self.active:
            self.active = False
            self.completed = True

    def elapsed_seconds(self, now: float | None = None) -> float:
        if not self.active and not self.completed:
            return 0.0
        ts = time.monotonic() if now is None else now
        return max(0.0, min(self.duration_s, ts - self.start_time))

    def remaining_seconds(self, now: float | None = None) -> float:
        if not self.active:
            return 0.0
        return max(0.0, self.duration_s - self.elapsed_seconds(now))

    def record(self, percent: float | None, now: float | None = None) -> bool:
        """Record a reading; returns True if the session just finished its duration."""
        if not self.active:
            return False
        ts = time.monotonic() if now is None else now
        elapsed = ts - self.start_time
        if elapsed >= self.duration_s:
            self.active = False
            self.completed = True
            return True

        self.total_frames += 1
        self.samples.append((elapsed, percent))
        if percent is not None:
            self.valid_reads += 1
            self.last_percent = percent
            if self.peak_percent is None or percent > self.peak_percent:
                self.peak_percent = percent

            should_log = (
                self._last_milestone_pct is None
                or abs(percent - self._last_milestone_pct) >= 1.5
                or (percent == 0.0 and self._last_milestone_pct > 1.0)
            )
            if should_log:
                self._last_milestone_pct = percent
                self.milestones.append(f"[{elapsed:4.1f}s]  {percent:.2f}%")
        return False

    def summary(self) -> tuple[bool, str, str]:
        if self.valid_reads >= 3 and self.peak_percent is not None:
            return (
                True,
                f"Passed — GD Quiet can see your screen! (Peak: {self.peak_percent:.2f}%)",
                f"Captured {self.valid_reads} valid percentage readings across {self.total_frames} checks.",
            )
        if self.valid_reads > 0 and self.peak_percent is not None:
            return (
                False,
                f"Low detections ({self.valid_reads} read(s), peak {self.peak_percent:.2f}%)",
                "Try Windowed or Borderless mode in GD, or use Pick Screen Region in Screen Capture.",
            )
        return (
            False,
            "No percentage detected during test",
            "Check that GD is in Borderless/Windowed mode with 'Show Percentage' ON, or pick the % box manually.",
        )


class ScreenPercentWatcher:
    """Background loop capturing the GD window or screen region and running Windows OCR."""

    def __init__(
        self,
        get_settings: Callable[[], dict[str, Any]],
        on_reading: Callable[[ScreenReading], None],
    ) -> None:
        self._get_settings = get_settings
        self._on_reading = on_reading
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._debouncer = PercentStateDebouncer()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        ocr = WinRtPercentOcr()
        try:
            with mss.mss() as sct:
                last_preview_at = 0.0
                while not self._stop.is_set():
                    cfg = self._get_settings()
                    enabled = bool(cfg.get("watch_enabled", True))
                    threshold = float(cfg.get("mute_at_percent", 50.0))
                    unmute_100 = bool(cfg.get("unmute_at_100", True))

                    if not enabled:
                        self._debouncer.reset()
                        self._on_reading(
                            ScreenReading(
                                percent=None,
                                raw_text="",
                                should_mute=False,
                                is_playing=False,
                                status_text="Screen watcher paused",
                                preview_image=None,
                            )
                        )
                        self._stop.wait(0.25)
                        continue

                    if not ocr.available():
                        self._debouncer.reset()
                        self._on_reading(
                            ScreenReading(
                                percent=None,
                                raw_text="",
                                should_mute=False,
                                is_playing=False,
                                status_text="Windows OCR language pack unavailable",
                                preview_image=None,
                            )
                        )
                        self._stop.wait(1.0)
                        continue

                    rect, source_label = compute_capture_rect(
                        watch_mode=str(cfg.get("watch_mode") or "auto_window"),
                        target_process=str(cfg.get("target_process") or "geometrydash.exe"),
                        custom_region=cfg.get("custom_region"),
                    )

                    if rect is None:
                        self._debouncer.reset()
                        self._on_reading(
                            ScreenReading(
                                percent=None,
                                raw_text="",
                                should_mute=False,
                                is_playing=False,
                                status_text=source_label,
                                preview_image=None,
                            )
                        )
                        self._stop.wait(0.5)
                        continue

                    try:
                        shot = sct.grab(rect)
                        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    except Exception as exc:
                        self._debouncer.reset()
                        self._on_reading(
                            ScreenReading(
                                percent=None,
                                raw_text="",
                                should_mute=False,
                                is_playing=False,
                                status_text=f"Capture error: {exc}",
                                preview_image=None,
                            )
                        )
                        self._stop.wait(0.5)
                        continue

                    pct, raw_text = ocr.read_percent(img, last_percent=self._debouncer.last_percent)
                    should_mute, is_playing = self._debouncer.update(
                        pct,
                        threshold=threshold,
                        unmute_at_100=unmute_100,
                    )

                    now = time.monotonic()
                    preview: Image.Image | None = None
                    if (now - last_preview_at) >= 0.25:
                        last_preview_at = now
                        preview = img.copy()

                    if pct is not None:
                        state = "MUTED" if should_mute else "UNMUTED"
                        status = f"Watching {source_label} • {pct:.2f}% (mutes at {threshold:.0f}%) • {state}"
                    elif is_playing:
                        status = f"Watching {source_label} • reading..."
                    else:
                        status = f"Watching {source_label} • no % visible yet (in menus or paused)"

                    self._on_reading(
                        ScreenReading(
                            percent=pct,
                            raw_text=raw_text,
                            should_mute=should_mute,
                            is_playing=is_playing,
                            status_text=status,
                            preview_image=preview,
                        )
                    )
                    self._stop.wait(0.10)
        finally:
            ocr.close()
