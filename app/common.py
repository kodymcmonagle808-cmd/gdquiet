"""Helpers shared by the GD Quiet app: % region presets, the drag-a-box region
picker, bundled-resource lookup, OCR diagnostics text, and the fade curve."""

from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk
from typing import Any

import mss
from PIL import Image, ImageEnhance, ImageTk

from screen_reader import ScreenReading, get_virtual_screen_bounds


PERCENT_PRESETS: tuple[tuple[str, float, float, float, float], ...] = (
    ("Top Center", 0.34, 0.00, 0.32, 0.11),
    ("Top Left", 0.00, 0.00, 0.28, 0.11),
    ("Top Right", 0.72, 0.00, 0.28, 0.11),
    ("Bottom Center", 0.34, 0.88, 0.32, 0.12),
)


# Status colours for OCR diagnostics text.
COLORS = {
    "green": "#22c55e",
    "red": "#ef4444",
    "amber": "#facc15",
    "subtext": "#a1a1aa",
}


class RegionSelectorOverlay:
    """Fullscreen screenshot overlay letting the user drag a box over the % text."""

    def __init__(self, parent: tk.Tk, on_selected: Any, on_cancel: Any) -> None:
        self.parent = parent
        self.on_selected = on_selected
        self.on_cancel = on_cancel
        self.vx, self.vy, self.vw, self.vh = get_virtual_screen_bounds()

        with mss.MSS() as sct:
            shot = sct.grab({"left": self.vx, "top": self.vy, "width": self.vw, "height": self.vh})
            raw_img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        dimmed = ImageEnhance.Brightness(raw_img).enhance(0.55)
        self._photo = ImageTk.PhotoImage(dimmed)
        self._start: tuple[int, int] | None = None
        self._rect_id: int | None = None
        self._size_id: int | None = None

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{self.vw}x{self.vh}+{self.vx}+{self.vy}")
        self.win.configure(cursor="crosshair")

        self.canvas = tk.Canvas(self.win, width=self.vw, height=self.vh, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self.canvas.create_rectangle(
            self.vw // 2 - 350, 14, self.vw // 2 + 350, 52,
            fill="#0b0f17", outline="#38bdf8", width=1,
        )
        self.canvas.create_text(
            self.vw // 2,
            33,
            text="Drag a box tightly around the % number  •  Release to save  •  Esc to cancel",
            fill="#f8fafc",
            font=("Segoe UI", 11, "bold"),
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.win.bind("<Escape>", self._cancel)
        self.win.focus_force()

    def _on_press(self, event: tk.Event) -> None:
        self._start = (int(event.x), int(event.y))
        if self._rect_id is not None:
            self.canvas.delete(self._rect_id)
        if self._size_id is not None:
            self.canvas.delete(self._size_id)
        self._rect_id = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="#38bdf8",
            width=2,
        )
        self._size_id = self.canvas.create_text(
            event.x + 8,
            max(64, event.y - 12),
            text="0×0 px",
            fill="#38bdf8",
            anchor="w",
            font=("Consolas", 10, "bold"),
        )

    def _on_drag(self, event: tk.Event) -> None:
        if not self._start or self._rect_id is None:
            return
        x0, y0 = self._start
        x1, y1 = int(event.x), int(event.y)
        self.canvas.coords(self._rect_id, x0, y0, x1, y1)
        if self._size_id is not None:
            self.canvas.coords(self._size_id, min(x0, x1), max(64, min(y0, y1) - 12))
            self.canvas.itemconfigure(self._size_id, text=f"{abs(x1 - x0)}×{abs(y1 - y0)} px")

    def _on_release(self, event: tk.Event) -> None:
        if not self._start:
            self._cancel()
            return
        x1, y1 = self._start
        x2, y2 = int(event.x), int(event.y)
        left = self.vx + min(x1, x2)
        top = self.vy + min(y1, y2)
        width = abs(x2 - x1)
        height = abs(y2 - y1)
        self.win.destroy()
        if width < 12 or height < 10:
            self.on_cancel()
            return
        self.on_selected(left, top, width, height)

    def _cancel(self, _event: Any = None) -> None:
        try:
            self.win.destroy()
        except Exception:
            pass
        self.on_cancel()


def _resource_path(rel_path: str) -> Path:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(str(meipass)) / rel_path)
    app_dir = Path(__file__).resolve().parent
    candidates.append(app_dir / rel_path)
    candidates.append(app_dir.parent / rel_path)
    for cand in candidates:
        if cand.exists():
            return cand
    return candidates[0]


def format_ocr_diagnostics(reading: ScreenReading) -> tuple[str, str]:
    raw_text = " ".join(reading.raw_text.split())
    if len(raw_text) > 100:
        raw_text = raw_text[:97] + "..."
    if reading.percent is not None:
        return f"OCR valid • {reading.scan_ms:.0f} ms • raw: {raw_text or '(empty)'}", COLORS["green"]
    if reading.is_playing or reading.consecutive_missed_reads > 0:
        text = (
            f"No valid % in {reading.consecutive_missed_reads} consecutive scans • "
            f"{reading.scan_ms:.0f} ms • raw: {raw_text or '(empty)'}"
        )
        if reading.is_playing and reading.consecutive_missed_reads >= 8:
            color = COLORS["red"]
        elif reading.consecutive_missed_reads >= 8:
            color = COLORS["amber"]
        else:
            color = COLORS["amber"] if reading.is_playing else COLORS["subtext"]
        return text, color
    return (
        f"Capture status • {reading.status_text} • raw: {raw_text or '(empty)'}",
        COLORS["subtext"],
    )


def fade_volume_for_percent(
    percent: float | None,
    threshold: float,
    mode: str,
    end_volume: float = 0.0,
) -> float | None:
    if percent is None or mode not in {"slow", "fast"} or threshold <= 0 or percent >= threshold:
        return None

    fade_width = 15.0 if mode == "slow" else 5.0
    fade_start = max(0.0, threshold - fade_width)
    if percent <= fade_start:
        return None

    progress = min(1.0, (percent - fade_start) / (threshold - fade_start))
    end = max(0.0, min(100.0, end_volume))
    return 100.0 - progress * (100.0 - end)
