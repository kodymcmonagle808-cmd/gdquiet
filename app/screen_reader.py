from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable

import mss
import psutil
from PIL import Image, ImageChops, ImageFilter, ImageOps
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
# Threshold at 110 to catch subtly tinted or off-white HUD text.
_WHITE_LUT = [255 if v < 110 else max(0, 255 - int((v - 110) * 2.2)) for v in range(256)]
_LUMA_LUT = [255 if v < 165 else max(0, 255 - int((v - 165) * 2.8)) for v in range(256)]
_DARK_INK_MASK = [255 if v < 245 else 0 for v in range(256)]

_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%")
# Default box for the Eclipse level-name label: the left 30% of the game window.
DEFAULT_LEVEL_LABEL_REGION: dict[str, Any] = {"kind": "window_rel", "rx": 0.0, "ry": 0.0, "rw": 0.30, "rh": 1.0}

# Other Eclipse labels that can share the left side of the screen with the level name.
# A line is treated as a stat when it starts with one of these words and contains a number.
_LABEL_STAT_RE = re.compile(
    r"^(?:fps|cps|tps|attempts?|att|from|noclip|accuracy|deaths?|jumps?|clicks?|session|time|clock|"
    r"best|run|frames?|ping|speed|progress|level\s*id|id|cheat\w*|startpos|checkpoints?)\b",
    re.IGNORECASE,
)
_LABEL_STAT_WORDS = frozenset(
    {"fps", "cps", "tps", "attempt", "attempts", "from", "noclip", "accuracy", "deaths", "jumps",
     "clicks", "session", "best", "run", "frames", "progress", "checkpoints"}
)
_LABEL_ALWAYS_STAT_WORDS = frozenset({"attempt", "attempts"})
_LABEL_STATUS_WORDS = frozenset(
    {"testmode", "noclip", "cheating", "cheat", "safemode", "practice", "practicemode", "startpos", "paused"}
)

_WINDOW_CACHE_LOCK = threading.Lock()
_WINDOW_CACHE: dict[str, tuple[int, float]] = {}
_WINDOW_CACHE_TTL_S = 5.0


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


def compute_region_rect(
    region: dict[str, Any],
    target_process: str,
) -> tuple[dict[str, int] | None, str] | None:
    """Resolve a saved region ("window_rel" or "screen") to an mss monitor dict.

    Returns None when the region dict is not a recognised kind.
    """
    kind = region.get("kind")
    if kind == "window_rel":
        proc = str(region.get("process") or target_process or "geometrydash.exe")
        rect = find_window_client_rect(proc)
        if rect is None:
            return None, f"Waiting for {proc} window..."
        wx, wy, ww, wh = rect
        left = wx + int(round(float(region["rx"]) * ww))
        top = wy + int(round(float(region["ry"]) * wh))
        width = max(24, int(round(float(region["rw"]) * ww)))
        height = max(18, int(round(float(region["rh"]) * wh)))
        return {"left": left, "top": top, "width": width, "height": height}, proc

    if kind == "screen":
        left = int(region.get("left", 0))
        top = int(region.get("top", 0))
        width = max(24, int(region.get("width", 200)))
        height = max(18, int(region.get("height", 80)))
        return {"left": left, "top": top, "width": width, "height": height}, "Custom screen region"

    return None


def compute_capture_rect(
    watch_mode: str,
    target_process: str,
    custom_region: dict[str, Any] | None,
) -> tuple[dict[str, int] | None, str]:
    """Return (mss monitor dict, human status label) for the current watch target."""
    if watch_mode == "custom_region" and isinstance(custom_region, dict):
        result = compute_region_rect(custom_region, target_process)
        if result is not None:
            return result

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


def level_name_similarity(a: str, b: str) -> float:
    """0..1 similarity of two level names, ignoring case, spaces and punctuation."""
    ka, kb = normalize_level_name(a), normalize_level_name(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return SequenceMatcher(None, ka, kb).ratio()


@dataclass
class OcrLine:
    """One OCR line with the left/top of its first word, in source-image pixels."""

    text: str
    x: float = 0.0
    y: float = 0.0


_OCR_LETTER_FIXES = str.maketrans({"+": "t", "|": "l", "!": "l", "1": "l", "0": "o", "4": "a", "@": "a", "5": "s", "$": "s"})


def _first_token_letters(line: str) -> str:
    """Letters of the first whitespace-separated token, undoing common OCR swaps
    ("At+emptl" -> "attemptl", "c!ubstep" -> "clubstep")."""
    token = (line or "").strip().split(" ", 1)[0].casefold().translate(_OCR_LETTER_FIXES)
    return re.sub(r"[^a-z]", "", token)


def _fuzzy_prefix(letters: str, keyword: str, cutoff: float) -> bool:
    if not letters:
        return False
    if letters.startswith(keyword):
        return True
    if len(keyword) < 4 or len(letters) < 4:
        return False
    return SequenceMatcher(None, letters[: len(keyword) + 1], keyword).ratio() >= cutoff


def _is_label_stat_line(line: str) -> bool:
    normalized = normalize_level_name(line)
    if normalized in _LABEL_STATUS_WORDS or normalized in _LABEL_STAT_WORDS:
        return True
    if "%" in line:
        return True
    digits =len(re.findall(r"\d", line))
    letters_total = len(re.findall(r"[A-Za-z]", line))
    has_digit = digits > 0
    first = _first_token_letters(line)
    # "Attempt 180" is also drawn huge in the middle of the screen and often comes
    # back garbled ("Attematq 4)", "At+emptl", "AtternpU"), so match it loosely
    # even without digits.
    if first.startswith("at") and any(_fuzzy_prefix(first, kw, 0.6) for kw in _LABEL_ALWAYS_STAT_WORDS):
        return True
    if has_digit and (
        _LABEL_STAT_RE.search(line) or any(_fuzzy_prefix(first, kw, 0.75) for kw in _LABEL_STAT_WORDS)
    ):
        return True
    # "Label: 123" style stats, including OCR reading the colon as . or ; ("cess. 240").
    if re.search(r":\s*[\d\-]", line):
        return True
    if re.fullmatch(r"\W*[A-Za-z]{1,6}[.:;,]\s*[\d.,/\-]+\W*", line):
        return True
    # Counters such as "0/0/0 CPS" (OCR sometimes reads 0 as o/O).
    if re.match(r"^\W*[\doO]+\s*/\s*[\doO]+", line):
        return True
    if re.search(r"\b(?:cps|fps|tps)\b", line, re.IGNORECASE) and has_digit:
        return True
    # Mostly numbers ("cess. 240", "-58.550/0"), clocks (12:34:56) and counters.
    if digits >= 3 and digits >= letters_total:
        return True
    if re.fullmatch(r"[\d\s:.,/\-]+", line):
        return True
    if re.search(r"\d{1,2}:\d{2}", line):
        return True
    return False


def _clean_label_line(raw_line: str) -> str:
    line = re.sub(r"\s+", " ", raw_line).strip(" \t|•-")
    return re.sub(r"^level(?:\s+name)?\s*[:\-]\s*", "", line, flags=re.IGNORECASE).strip()


def _is_plausible_name(line: str) -> bool:
    normalized = normalize_level_name(line)
    if len(normalized) < 2 or len(line) > 60:
        return False
    letters = len(re.findall(r"[A-Za-z]", line))
    # Mostly symbols/noise ("{(@ &") is OCR garbage, not a name.
    return letters >= 2 and letters >= len(normalized) * 0.5


def pick_level_name(lines: list[OcrLine], align_tolerance: float = 10.0) -> str | None:
    """Pick the level name from OCR lines of the Eclipse label region.

    Eclipse stacks its labels with a shared left edge. When stat labels (FPS,
    attempts, CPS, %) are visible, only lines starting at that same left edge
    are considered, which filters out other overlays and in-game text. The
    first remaining line that isn't a stat is the level name. When the box was
    drawn tightly around just the level-name label, there are no stat lines
    and the first plausible line wins.
    """
    cleaned = [(OcrLine(_clean_label_line(line.text), line.x, line.y)) for line in lines]
    cleaned = [line for line in cleaned if line.text]
    stat_xs = sorted(line.x for line in cleaned if _is_label_stat_line(line.text) and re.search(r"[A-Za-z]", line.text))
    anchor_x: float | None = stat_xs[len(stat_xs) // 2] if stat_xs else None
    for line in cleaned:
        if anchor_x is not None and abs(line.x - anchor_x) > align_tolerance:
            continue
        if not _is_plausible_name(line.text) or _is_label_stat_line(line.text):
            continue
        return line.text
    return None


def extract_level_name_from_label_text(text: str) -> str | None:
    """Text-only version of pick_level_name (no position information)."""
    return pick_level_name([OcrLine(line) for line in (text or "").splitlines()])


def prepare_label_image(img: Image.Image) -> tuple[Image.Image, float]:
    """Sharpen and upscale the label crop for OCR. Eclipse labels are small and
    semi-transparent; Windows OCR reads them far more reliably this way.
    Returns (image, scale)."""
    scale = 3 if img.height <= 240 else 2
    sharp = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    big = sharp.resize((img.width * scale, img.height * scale), Image.Resampling.LANCZOS)
    return big, float(scale)


class LevelNameTracker:
    """Emits a level name once it shows up in 2 of the last 5 OCR scans.

    OCR of semi-transparent labels over a moving background is noisy, so reads
    count as the same name when they're close, missed reads don't break the
    run, and the most common spelling wins. A name that was already reported
    is not reported again until a different level shows up (or reset()).
    """

    SIMILAR = 0.8
    WINDOW = 5
    NEEDED = 2

    def __init__(self) -> None:
        self._recent: list[str] = []
        self._reported_text = ""

    def reset(self) -> None:
        self._recent = []
        self._reported_text = ""

    def update(self, candidate: str | None) -> str | None:
        text = (candidate or "").strip()
        self._recent.append(text if normalize_level_name(text) else "")
        self._recent = self._recent[-self.WINDOW :]
        if not self._recent[-1]:
            return None

        cluster = [r for r in self._recent if r and level_name_similarity(r, text) >= self.SIMILAR]
        if len(cluster) < self.NEEDED:
            return None
        counts: dict[str, int] = {}
        for r in cluster:
            counts[r] = counts.get(r, 0) + 1
        # Most common spelling; ties go to the most recent read.
        best = max(reversed(cluster), key=lambda r: counts[r])
        if self._reported_text and level_name_similarity(best, self._reported_text) >= self.SIMILAR:
            return None
        self._reported_text = best
        return best


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

    def recognize_lines(self, rgba_img: Image.Image) -> str:
        """Like recognize_text, but keeps each OCR line on its own line.

        OcrResult.text joins every line with spaces, which merges stacked labels
        (e.g. "FPS: 240" and the level name) into one string.
        """
        return "\n".join(line.text for line in self.recognize_line_boxes(rgba_img))

    def recognize_line_boxes(self, rgba_img: Image.Image, scale: float = 1.0) -> list[OcrLine]:
        """OCR lines with the position of each line's first word, divided by `scale`."""
        if self._engine is None:
            return []
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
        out: list[OcrLine] = []
        for line in getattr(res, "lines", None) or []:
            text = str(getattr(line, "text", "") or "").strip()
            if not text:
                continue
            x = y = 0.0
            try:
                rect = next(iter(line.words)).bounding_rect
                x, y = float(rect.x) / scale, float(rect.y) / scale
            except Exception:
                pass
            out.append(OcrLine(text, x, y))
        return out

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
        # Fallback: try raw RGBA OCR when preprocessing fails to find text.
        if not first_text and captured.width >= 16 and captured.height >= 8:
            raw_text = self.recognize_text(
                captured if captured.mode == "RGBA" else captured.convert("RGBA")
            )
            if raw_text:
                first_text = raw_text
                pct = parse_percent_from_text(raw_text, last_percent=last_percent)
                if pct is not None:
                    return pct, raw_text, None, prefer_luma
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
        self._low_streak = 0

    def reset(self) -> None:
        self.is_muted = False
        self.last_percent = None
        self.last_seen_at = 0.0
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
            if self.last_seen_at > 0:
                gap = ts - self.last_seen_at
                # Hold mute for at least 1s when already muted to ride out brief OCR gaps
                # without false-unmuting.  When not muted, use the shorter configured timeout.
                effective_timeout = max(self.missing_timeout_s, 1.0) if self.is_muted else self.missing_timeout_s
                if gap >= effective_timeout:
                    self.is_muted = False
                    self.last_percent = None
                    return False, False
                return self.is_muted, True
            return False, False

        self.last_seen_at = ts
        prev = self.last_percent
        self.last_percent = percent

        if unmute_at_100 and percent >= 99.95:
            self._low_streak = 0
            self.is_muted = False
            return False, True

        if percent < threshold:
            self._low_streak += 1
            # Require 2 consecutive below-threshold frames to unmute (avoids 1-frame OCR glitches including false 0%).
            if not self.is_muted or self._low_streak >= 2:
                self.is_muted = False
            return self.is_muted, True

        self._low_streak = 0
        # percent >= threshold: mute immediately (1 frame).  The 2-frame guard
        # is only used for *un*muting to avoid false unmutes from OCR glitches.
        self.is_muted = True

        return True, True


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
    level_label_raw: str | None = None


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
        self._level_lock = threading.Lock()
        self._level_pending_candidate: str | None = None
        self._level_pending_raw: str | None = None
        self._level_pending_error: str | None = None
        self._level_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._level_thread = threading.Thread(target=self._level_loop, daemon=True)
        self._level_thread.start()

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
    def _scan_level_label(
        sct: Any,
        ocr: WinRtPercentOcr,
        region: dict[str, Any] | None,
        target_process: str,
    ) -> tuple[str | None, str | None]:
        """OCR the Eclipse level-name label box.

        Returns (level name or None, raw OCR text). Raw text is None when the
        game window isn't available.
        """
        reg = region if isinstance(region, dict) and region.get("kind") else DEFAULT_LEVEL_LABEL_REGION
        resolved = compute_region_rect(reg, target_process)
        if resolved is None or resolved[0] is None:
            return None, None
        shot = sct.grab(resolved[0])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        big, scale = prepare_label_image(img)
        lines = ocr.recognize_line_boxes(big, scale=scale)
        raw = "\n".join(line.text for line in lines)
        tolerance = max(10.0, img.width * 0.02)
        return pick_level_name(lines, align_tolerance=tolerance), raw

    def _post_level_result(self, candidate: str | None, raw: str | None, error: str | None) -> None:
        with self._level_lock:
            if candidate:
                self._level_pending_candidate = candidate
            if raw is not None:
                self._level_pending_raw = raw
            self._level_pending_error = error

    def _take_level_result(self) -> tuple[str | None, str | None, str | None]:
        with self._level_lock:
            result = (self._level_pending_candidate, self._level_pending_raw, self._level_pending_error)
            self._level_pending_candidate = None
            self._level_pending_raw = None
            self._level_pending_error = None
        return result

    def _level_loop(self) -> None:
        """Reads the Eclipse level label about once a second on its own thread.

        It has its own OCR engine and screen grabber so the ~100 ms label scan
        never delays the percentage loop (and therefore never delays a mute).
        """
        ocr = WinRtPercentOcr()
        try:
            with mss.MSS() as sct:
                while not self._stop.is_set():
                    cfg = self._get_settings()
                    if (
                        not cfg.get("watch_enabled", True)
                        or not cfg.get("level_label_enabled", False)
                        or not ocr.available()
                    ):
                        self._level_name_tracker.reset()
                        self._stop.wait(0.5)
                        continue
                    target_process = str(cfg.get("target_process") or "geometrydash.exe")
                    try:
                        title, raw = self._scan_level_label(sct, ocr, cfg.get("level_label_region"), target_process)
                    except Exception as exc:
                        self._post_level_result(None, None, str(exc))
                    else:
                        if raw is None:
                            # Game window closed: re-arm so reopening the level reloads its profile.
                            self._level_name_tracker.reset()
                        self._post_level_result(self._level_name_tracker.update(title), raw, None)
                    self._stop.wait(1.0)
        finally:
            ocr.close()

    def _run(self) -> None:
        ocr = WinRtPercentOcr()
        try:
            with mss.MSS() as sct:
                last_preview_at = 0.0
                last_x_frac: float | None = None
                prefer_luma = False
                last_target_key: tuple[str, str] | None = None
                consecutive_missed_reads = 0
                last_good_rect: dict[str, int] | None = None
                last_good_rect_at = 0.0

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
                        last_good_rect = None
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
                        last_good_rect = None
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
                        watch_mode=watch_mode,
                        target_process=target_process,
                        custom_region=cfg.get("custom_region"),
                    )

                    if rect is None:
                        # Grace period: reuse the last known rect briefly to avoid
                        # flickering when window lookup hiccups for a frame or two.
                        now_grace = time.monotonic()
                        if last_good_rect is not None and (now_grace - last_good_rect_at) < 2.0:
                            rect = last_good_rect
                        else:
                            last_good_rect = None
                            last_x_frac = None
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

                    frame_t0 = time.perf_counter()
                    try:
                        shot = sct.grab(rect)
                        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                        last_good_rect = dict(rect)
                        last_good_rect_at = time.monotonic()
                    except Exception as exc:
                        last_good_rect = None
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
                    level_name_candidate, level_label_raw, level_detection_error = self._take_level_result()

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
                            level_label_raw=level_label_raw,
                        )
                    )
                    self._stop.wait(play_wait_s if is_playing else idle_wait_s)
        finally:
            ocr.close()
