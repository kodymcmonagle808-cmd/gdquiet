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
_DARK_INK_MASK = [255 if v < 245 else 0 for v in range(256)]

_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%")
_PAUSE_MENU_MARKER_RE = re.compile(r"\b(?:resume|restart|practice|normal mode|attempts?)\b", re.IGNORECASE)
_LEVEL_MENU_LABELS = {
    "attempt",
    "attempts",
    "best",
    "exit",
    "level complete",
    "normal mode",
    "paused",
    "practice",
    "resume",
    "restart",
    "settings",
}

_WINDOW_CACHE_LOCK = threading.Lock()
_WINDOW_CACHE: dict[str, tuple[int, float]] = {}
_WINDOW_CACHE_TTL_S = 2.0


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


def clear_window_cache() -> None:
    with _WINDOW_CACHE_LOCK:
        _WINDOW_CACHE.clear()


def _get_window_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if not hwnd or not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    if _is_cloaked(hwnd):
        return None

    cr = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(cr)):
        return None
    w = int(cr.right - cr.left)
    h = int(cr.bottom - cr.top)
    if w < 120 or h < 120:
        return None

    pt = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return (int(pt.x), int(pt.y), w, h)


def _window_belongs_to_process(hwnd: int, target_lower: str) -> bool:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False
    try:
        return psutil.Process(int(pid.value)).name().lower() == target_lower
    except Exception:
        return False


def find_window_client_rect(process_name: str) -> tuple[int, int, int, int] | None:
    target = (process_name or "").strip().lower()
    if not target:
        return None

    now = time.monotonic()
    with _WINDOW_CACHE_LOCK:
        cached = _WINDOW_CACHE.get(target)
    if cached is not None:
        cached_hwnd, cached_at = cached
        if (now - cached_at) < _WINDOW_CACHE_TTL_S and _window_belongs_to_process(cached_hwnd, target):
            rect = _get_window_client_rect(cached_hwnd)
            if rect is not None:
                return rect

    matching_pids: set[int] = set()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == target:
                matching_pids.add(int(proc.info["pid"]))
        except Exception:
            continue

    if not matching_pids:
        with _WINDOW_CACHE_LOCK:
            _WINDOW_CACHE.pop(target, None)
        return None

    best_rect: tuple[int, int, int, int] | None = None
    best_hwnd = 0
    best_area = 0

    def callback(hwnd: int, _lparam: int) -> bool:
        nonlocal best_rect, best_hwnd, best_area
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) not in matching_pids:
            return True

        rect = _get_window_client_rect(hwnd)
        if rect is None:
            return True

        area = rect[2] * rect[3]
        if area > best_area:
            best_area = area
            best_hwnd = hwnd
            best_rect = rect
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    with _WINDOW_CACHE_LOCK:
        if best_hwnd:
            _WINDOW_CACHE[target] = (best_hwnd, time.monotonic())
        else:
            _WINDOW_CACHE.pop(target, None)
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
    # Scan the top 12% of the game window (wider than 10% to avoid clipping large % text).
    left = wx
    top = wy
    width = max(80, ww)
    height = max(24, int(wh * 0.12))
    return {"left": left, "top": top, "width": width, "height": height}, proc


def _extract_ink(img: Image.Image, luma_fallback: bool = False) -> Image.Image:
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    if luma_fallback:
        return rgb.convert("L").point(_LUMA_LUT)
    r, g, b = rgb.split()
    mn = ImageChops.darker(ImageChops.darker(r, g), b)
    mx = ImageChops.lighter(ImageChops.lighter(r, g), b)
    sat = ImageChops.subtract(mx, mn)
    return ImageChops.subtract(mn, sat).point(_WHITE_LUT)


def _prepare_ocr_gray(
    img: Image.Image,
    luma_fallback: bool = False,
) -> tuple[Image.Image | None, tuple[int, int, int, int] | None]:
    """Extract high-contrast ink crop padded for WinRT OCR, skipping blank frames."""
    if img.width < 4 or img.height < 4:
        return None, None
    ink = _extract_ink(img, luma_fallback=luma_fallback)
    if ink.getextrema()[0] >= 245:
        return None, None
    bbox = ink.point(_DARK_INK_MASK).getbbox()
    if bbox is None:
        return None, None

    x0, y0, x1, y1 = bbox
    # Pad around the foreground ink so short strings like "8%" keep line context.
    # Use generous left padding (80px) to avoid clipping leading digits (e.g. "26%" -> "6%").
    cropped = ink.crop((max(0, x0 - 80), max(0, y0 - 20), min(ink.width, x1 + 60), min(ink.height, y1 + 20)))
    scale = 3 if cropped.height < 80 else (2 if cropped.height < 150 else 1)
    if scale > 1:
        cropped = cropped.resize((cropped.width * scale, cropped.height * scale), Image.Resampling.BILINEAR)
    return ImageOps.expand(cropped, border=24, fill=255), bbox


def preprocess_for_ocr(img: Image.Image, luma_fallback: bool = False) -> Image.Image:
    gray, _bbox = _prepare_ocr_gray(img, luma_fallback=luma_fallback)
    if gray is None:
        return Image.new("RGBA", (64, 64), (255, 255, 255, 255))
    return gray.convert("RGBA")


def parse_percent_from_text(text: str, last_percent: float | None = None) -> float | None:
    if not text:
        return None
    norm = text.replace("\n", " ").replace("\r", " ")
    # Windows OCR commonly reads bold outlined '%' as '0/0' or 'o/0', and '0%' as 'O%'.
    norm = re.sub(r"[0oO°]\s*/\s*[0oO]", "%", norm)
    norm = re.sub(r"(?<![A-Za-z0-9])[Oo](?=%)", "0", norm)

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


def normalize_level_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").casefold())


_LEVEL_MENU_LABELS_NORMALIZED = frozenset(normalize_level_name(label) for label in _LEVEL_MENU_LABELS)


def extract_level_name_from_pause_text(text: str) -> str | None:
    lines = [re.sub(r"\s+", " ", line).strip(" \t|:•-") for line in (text or "").splitlines()]
    if not any(_PAUSE_MENU_MARKER_RE.search(line) for line in lines):
        return None

    for line in lines:
        candidate = re.sub(r"^level(?:\s+name)?\s*[:\-]\s*", "", line, flags=re.IGNORECASE).strip()
        normalized = normalize_level_name(candidate)
        if (
            len(candidate) > 48
            or len(normalized) < 3
            or "%" in candidate
            or normalized in _LEVEL_MENU_LABELS_NORMALIZED
            or re.search(r"\b(?:attempts?|normal mode|practice|resume|restart)\b", candidate, re.IGNORECASE)
        ):
            continue
        if re.search(r"[A-Za-z]", candidate):
            return candidate
    return None


class LevelNameTracker:
    """Emits a pause-menu level title only after two matching OCR scans."""

    def __init__(self) -> None:
        self._candidate_key = ""
        self._candidate_text = ""
        self._candidate_count = 0
        self._reported_key = ""

    def reset(self) -> None:
        self._candidate_key = ""
        self._candidate_text = ""
        self._candidate_count = 0
        self._reported_key = ""

    def update(self, candidate: str | None) -> str | None:
        key = normalize_level_name(candidate or "")
        if not key:
            self.reset()
            return None

        if key == self._candidate_key:
            self._candidate_count += 1
            self._candidate_text = candidate or self._candidate_text
        else:
            self._candidate_key = key
            self._candidate_text = candidate or ""
            self._candidate_count = 1
            self._reported_key = ""

        if self._candidate_count >= 2 and self._reported_key != key:
            self._reported_key = key
            return self._candidate_text
        return None


class WinRtPercentOcr:
    """Thread-affine Windows.Media.Ocr recognizer for fast in-memory crops."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._engine = wmo.OcrEngine.try_create_from_user_profile_languages()

    def available(self) -> bool:
        return self._engine is not None

    def recognize_gray(self, gray_img: Image.Image) -> str:
        if self._engine is None:
            return ""
        img = gray_img if gray_img.mode == "L" else gray_img.convert("L")
        dw = wss.DataWriter()
        dw.write_bytes(img.tobytes())
        ibuf = dw.detach_buffer()
        sb = wgi.SoftwareBitmap.create_copy_from_buffer(
            ibuf,
            wgi.BitmapPixelFormat.GRAY8,
            img.width,
            img.height,
        )
        res = self._loop.run_until_complete(self._engine.recognize_async(sb))
        return str(getattr(res, "text", "") or "").strip()

    def recognize_text(self, rgba_img: Image.Image) -> str:
        if self._engine is None:
            return ""
        if rgba_img.mode == "L":
            return self.recognize_gray(rgba_img)
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

    def read_percent_with_meta(
        self,
        captured: Image.Image,
        last_percent: float | None = None,
        prefer_luma: bool = False,
    ) -> tuple[float | None, str, tuple[int, int, int, int] | None, bool]:
        first_text = ""
        for use_luma in (prefer_luma, not prefer_luma):
            gray, bbox = _prepare_ocr_gray(captured, luma_fallback=use_luma)
            if gray is None:
                continue
            text = self.recognize_gray(gray)
            if not first_text and text:
                first_text = text
            pct = parse_percent_from_text(text, last_percent=last_percent)
            if pct is not None:
                return pct, text, bbox, use_luma
        return None, first_text, None, prefer_luma

    def read_percent(
        self,
        captured: Image.Image,
        last_percent: float | None = None,
    ) -> tuple[float | None, str]:
        pct, text, _bbox, _used_luma = self.read_percent_with_meta(captured, last_percent=last_percent)
        return pct, text

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:
            pass


class PercentStateDebouncer:
    """Converts noisy frame-by-frame OCR readings into clean mute/unmute transitions."""

    def __init__(self, missing_timeout_s: float = 0.35) -> None:
        self.missing_timeout_s = missing_timeout_s
        self.is_muted = False
        self.last_percent: float | None = None
        self.last_seen_at = 0.0
        self._high_streak = 0
        self._low_streak = 0

    def reset(self) -> None:
        self.is_muted = False
        self.last_percent = None
        self.last_seen_at = 0.0
        self._high_streak = 0
        self._low_streak = 0

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
            self._low_streak = 0
            if self.last_seen_at > 0 and (ts - self.last_seen_at) >= self.missing_timeout_s:
                self.is_muted = False
                self.last_percent = None
                self._high_streak = 0
                return False, False
            return self.is_muted, (self.last_seen_at > 0 and (ts - self.last_seen_at) < self.missing_timeout_s)

        self.last_seen_at = ts
        prev = self.last_percent
        self.last_percent = percent

        if unmute_at_100 and percent >= 99.95:
            self._high_streak = 0
            self._low_streak = 0
            self.is_muted = False
            return False, True

        if percent < threshold:
            self._high_streak = 0
            self._low_streak += 1
            # Unmute right away at 0% death respawn; require 2 frames otherwise to ignore 1-frame OCR dips.
            if not self.is_muted or percent <= 0.5 or self._low_streak >= 2:
                self.is_muted = False
            return self.is_muted, True

        self._low_streak = 0
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
    scan_ms: float = 0.0
    level_name_candidate: str | None = None
    level_detection_error: str | None = None
    consecutive_missed_reads: int = 0


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
        self._level_name_tracker = LevelNameTracker()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _scan_image(
        self,
        ocr: WinRtPercentOcr,
        img: Image.Image,
        *,
        auto_roi: bool,
        last_x_frac: float | None,
        prefer_luma: bool,
    ) -> tuple[float | None, str, float | None, bool]:
        last_pct = self._debouncer.last_percent
        if auto_roi and img.width >= 700:
            band_w = max(480, int(img.width * 0.48))
            center_frac = 0.50 if last_x_frac is None else last_x_frac
            x0 = max(0, min(img.width - band_w, int(center_frac * img.width) - (band_w // 2)))
            x1 = min(img.width, x0 + band_w)
            roi = img.crop((x0, 0, x1, img.height))
            pct, raw_text, bbox, used_luma = ocr.read_percent_with_meta(
                roi,
                last_percent=last_pct,
                prefer_luma=prefer_luma,
            )
            if pct is not None and bbox is not None:
                hit_frac = (x0 + (bbox[0] + bbox[2]) * 0.5) / img.width
                return pct, raw_text, hit_frac, used_luma
            if x0 == 0 and x1 == img.width:
                return None, raw_text, last_x_frac, prefer_luma

        pct, raw_text, bbox, used_luma = ocr.read_percent_with_meta(
            img,
            last_percent=last_pct,
            prefer_luma=prefer_luma,
        )
        if pct is not None and bbox is not None:
            hit_frac = ((bbox[0] + bbox[2]) * 0.5) / max(1, img.width)
            return pct, raw_text, hit_frac, used_luma
        return None, raw_text, last_x_frac, prefer_luma

    @staticmethod
    def _scan_level_name(sct: Any, ocr: WinRtPercentOcr, process_name: str) -> str | None:
        rect = find_window_client_rect(process_name)
        if rect is None:
            return None
        x, y, width, height = rect
        # Capture only the top ~40% of the window where the pause menu title appears,
        # reducing OCR noise from gameplay elements and improving scan speed.
        scan_height = max(80, int(height * 0.40))
        shot = sct.grab({"left": x, "top": y, "width": width, "height": scan_height})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        text = ocr.recognize_text(img.convert("RGBA"))
        return extract_level_name_from_pause_text(text)

    def _run(self) -> None:
        ocr = WinRtPercentOcr()
        try:
            with mss.MSS() as sct:
                last_preview_at = 0.0
                last_level_scan_at = 0.0
                last_x_frac: float | None = None
                prefer_luma = False
                last_target_key: tuple[str, str] | None = None
                consecutive_missed_reads = 0

                while not self._stop.is_set():
                    cfg = self._get_settings()
                    enabled = bool(cfg.get("watch_enabled", True))
                    threshold = float(cfg.get("mute_at_percent", 50.0))
                    unmute_100 = bool(cfg.get("unmute_at_100", True))
                    watch_mode = str(cfg.get("watch_mode") or "auto_window")
                    target_process = str(cfg.get("target_process") or "geometrydash.exe")
                    self._debouncer.missing_timeout_s = max(
                        0.05, min(5.0, float(cfg.get("missing_timeout_s", 0.35)))
                    )
                    play_wait_s = max(0.005, min(0.5, float(cfg.get("scan_interval_playing_ms", 15)) / 1000.0))
                    idle_wait_s = max(0.01, min(1.0, float(cfg.get("scan_interval_idle_ms", 45)) / 1000.0))
                    preview_interval_s = max(0.05, min(2.0, float(cfg.get("preview_interval_ms", 250)) / 1000.0))
                    target_key = (watch_mode, target_process)
                    if target_key != last_target_key:
                        last_target_key = target_key
                        last_x_frac = None

                    if not enabled:
                        last_x_frac = None
                        self._debouncer.reset()
                        self._level_name_tracker.reset()
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
                        self._level_name_tracker.reset()
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
                        watch_mode=watch_mode,
                        target_process=target_process,
                        custom_region=cfg.get("custom_region"),
                    )

                    if rect is None:
                        last_x_frac = None
                        self._debouncer.reset()
                        self._level_name_tracker.reset()
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

                    frame_t0 = time.perf_counter()
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

                    pct, raw_text, last_x_frac, prefer_luma = self._scan_image(
                        ocr,
                        img,
                        auto_roi=(watch_mode != "custom_region"),
                        last_x_frac=last_x_frac,
                        prefer_luma=prefer_luma,
                    )
                    scan_ms = (time.perf_counter() - frame_t0) * 1000.0
                    should_mute, is_playing = self._debouncer.update(
                        pct,
                        threshold=threshold,
                        unmute_at_100=unmute_100,
                    )
                    consecutive_missed_reads = consecutive_missed_reads + 1 if pct is None else 0

                    now = time.monotonic()
                    level_name_candidate: str | None = None
                    level_detection_error: str | None = None
                    if cfg.get("level_detection_beta", False):
                        if now - last_level_scan_at >= 1.0:
                            last_level_scan_at = now
                            level_process = target_process
                            region = cfg.get("custom_region")
                            if (
                                watch_mode == "custom_region"
                                and isinstance(region, dict)
                                and region.get("kind") == "window_rel"
                            ):
                                level_process = str(region.get("process") or target_process)
                            try:
                                title = self._scan_level_name(sct, ocr, level_process)
                            except Exception as exc:
                                self._level_name_tracker.reset()
                                level_detection_error = str(exc)
                            else:
                                level_name_candidate = self._level_name_tracker.update(title)
                    else:
                        self._level_name_tracker.reset()

                    preview: Image.Image | None = None
                    if (now - last_preview_at) >= preview_interval_s:
                        last_preview_at = now
                        preview = img.copy()

                    if pct is not None:
                        active_word = "DUCKED" if cfg.get("audio_mode") == "duck" else "MUTED"
                        state = active_word if should_mute else "UNMUTED"
                        status = f"Watching {source_label} • {pct:.2f}% (threshold {threshold:.0f}%) • {state}"
                    elif is_playing:
                        active_word = "DUCKED" if cfg.get("audio_mode") == "duck" else "MUTED"
                        state = active_word if should_mute else "UNMUTED"
                        status = f"Watching {source_label} • % unreadable, holding {state}"
                    else:
                        status = f"Watching {source_label} • no % visible (in menus or paused) • UNMUTED"

                    self._on_reading(
                        ScreenReading(
                            percent=pct,
                            raw_text=raw_text,
                            should_mute=should_mute,
                            is_playing=is_playing,
                            status_text=status,
                            preview_image=preview,
                            scan_ms=scan_ms,
                            level_name_candidate=level_name_candidate,
                            level_detection_error=level_detection_error,
                            consecutive_missed_reads=consecutive_missed_reads,
                        )
                    )
                    self._stop.wait(play_wait_s if is_playing else idle_wait_s)
        finally:
            ocr.close()
