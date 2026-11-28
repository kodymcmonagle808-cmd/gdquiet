"""GD Quiet entry point (bundled as GDQuiet.exe).

The app itself lives in web_ui.py (pywebview + app/ui). `--self-check` is used
by the build to confirm OCR and audio enumeration work in the packaged exe.
"""

from __future__ import annotations

import ctypes
import sys

from screen_reader import WinRtPercentOcr
from window_audio import list_mute_targets

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


def main() -> None:
    if "--self-check" in sys.argv:
        ocr = WinRtPercentOcr()
        try:
            assert ocr.available(), "Windows.Media.Ocr engine unavailable"
        finally:
            ocr.close()
        _ = list_mute_targets(include_background_audio=False)
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("gdquiet.desktop.app")
    except Exception:
        pass

    from web_ui import run_web_app

    run_web_app()


if __name__ == "__main__":
    main()
