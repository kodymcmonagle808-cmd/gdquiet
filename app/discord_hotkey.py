from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any, Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.windll.kernel32

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

HOTKEY_MOD_FLAGS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "shift": MOD_SHIFT,
}

MODIFIER_VKS = {
    "ctrl": 0x11,   # VK_CONTROL
    "shift": 0x10,  # VK_SHIFT
    "alt": 0x12,    # VK_MENU
}

EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24,  # PageUp, PageDown, End, Home
    0x25, 0x26, 0x27, 0x28,  # Arrow keys
    0x2D, 0x2E,              # Insert, Delete
    0x5B, 0x5C, 0x5D,        # Win, Apps
    0x6F,                    # Numpad Divide
    0x90,                    # NumLock
    0xA3, 0xA5,              # RControl, RMenu
}

KEYSYM_TO_VK: dict[str, tuple[int, str]] = {
    "BackSpace": (0x08, "Backspace"),
    "Tab": (0x09, "Tab"),
    "Return": (0x0D, "Enter"),
    "Pause": (0x13, "Pause"),
    "Caps_Lock": (0x14, "CapsLock"),
    "Escape": (0x1B, "Esc"),
    "space": (0x20, "Space"),
    "Prior": (0x21, "PageUp"),
    "Next": (0x22, "PageDown"),
    "End": (0x23, "End"),
    "Home": (0x24, "Home"),
    "Left": (0x25, "Left"),
    "Up": (0x26, "Up"),
    "Right": (0x27, "Right"),
    "Down": (0x28, "Down"),
    "Insert": (0x2D, "Insert"),
    "Delete": (0x2E, "Delete"),
    "KP_Add": (0x6B, "Num +"),
    "KP_Subtract": (0x6D, "Num -"),
    "KP_Multiply": (0x6A, "Num *"),
    "KP_Divide": (0x6F, "Num /"),
    "KP_Decimal": (0x6E, "Num ."),
    "minus": (0xBD, "-"),
    "equal": (0xBB, "="),
    "bracketleft": (0xDB, "["),
    "bracketright": (0xDD, "]"),
    "backslash": (0xDC, "\\"),
    "semicolon": (0xBA, ";"),
    "apostrophe": (0xDE, "'"),
    "comma": (0xBC, ","),
    "period": (0xBE, "."),
    "slash": (0xBF, "/"),
    "grave": (0xC0, "`"),
}
for _i in range(1, 25):
    KEYSYM_TO_VK[f"F{_i}"] = (0x70 + (_i - 1), f"F{_i}")

MODIFIER_KEYSYMS = {
    "Control_L", "Control_R",
    "Shift_L", "Shift_R",
    "Alt_L", "Alt_R",
    "Super_L", "Super_R",
    "Meta_L", "Meta_R",
}

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


def _make_key_input(vk: int, key_up: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if key_up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFFFF
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(
        wVk=vk & 0xFFFF,
        wScan=scan,
        dwFlags=flags,
        time=0,
        dwExtraInfo=0,
    )
    return inp


def send_hotkey(hotkey: dict[str, Any]) -> bool:
    if not hotkey or "vk" not in hotkey:
        return False
    vk = int(hotkey["vk"])
    mods = [MODIFIER_VKS[m] for m in hotkey.get("modifiers", []) if m in MODIFIER_VKS]

    down_inputs = [_make_key_input(m_vk, key_up=False) for m_vk in mods]
    down_inputs.append(_make_key_input(vk, key_up=False))
    up_inputs = [_make_key_input(vk, key_up=True)]
    up_inputs.extend(_make_key_input(m_vk, key_up=True) for m_vk in reversed(mods))

    down_arr = (INPUT * len(down_inputs))(*down_inputs)
    sent_down = user32.SendInput(len(down_inputs), down_arr, ctypes.sizeof(INPUT))
    time.sleep(0.035)
    up_arr = (INPUT * len(up_inputs))(*up_inputs)
    sent_up = user32.SendInput(len(up_inputs), up_arr, ctypes.sizeof(INPUT))
    return sent_down > 0 and sent_up > 0


def parse_tk_hotkey(event: Any) -> dict[str, Any] | None:
    keysym = str(getattr(event, "keysym", "") or "")
    if not keysym or keysym in MODIFIER_KEYSYMS:
        return None

    state = int(getattr(event, "state", 0) or 0)
    modifiers: list[str] = []
    if state & 0x0004:
        modifiers.append("ctrl")
    if state & 0x0001:
        modifiers.append("shift")
    if state & 0x20000 or state & 0x0008:
        modifiers.append("alt")

    vk: int | None = None
    key_label: str = ""

    if keysym in KEYSYM_TO_VK:
        vk, key_label = KEYSYM_TO_VK[keysym]
    elif len(keysym) == 1 and keysym.isalnum():
        ch = keysym.upper()
        vk = ord(ch)
        key_label = ch
    elif keysym.startswith("KP_") and len(keysym) == 4 and keysym[3].isdigit():
        digit = int(keysym[3])
        vk = 0x60 + digit
        key_label = f"Num {digit}"
    else:
        keycode = int(getattr(event, "keycode", 0) or 0)
        if 8 <= keycode <= 254:
            vk = keycode
            key_label = keysym.upper()

    if vk is None:
        return None

    pretty_mods = [{"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt"}[m] for m in modifiers]
    label = " + ".join([*pretty_mods, key_label])
    return {
        "modifiers": modifiers,
        "vk": vk,
        "label": label,
    }


class DiscordDeafenController:
    """Tracks whether GDQuiet deafened Discord so unmute restores the prior state."""

    def __init__(self, toggle_cooldown_s: float = 0.18) -> None:
        self._lock = threading.Lock()
        self._deafened_by_us = False
        self._toggle_cooldown_s = float(toggle_cooldown_s)
        self._last_toggle_at = 0.0

    @property
    def deafened_by_us(self) -> bool:
        with self._lock:
            return self._deafened_by_us

    def _send_with_cooldown(self, hotkey: dict[str, Any]) -> bool:
        if self._last_toggle_at > 0.0 and self._toggle_cooldown_s > 0.0:
            remaining = self._toggle_cooldown_s - (time.monotonic() - self._last_toggle_at)
            if remaining > 0.0:
                time.sleep(remaining)
        sent = send_hotkey(hotkey)
        if sent:
            self._last_toggle_at = time.monotonic()
        return sent

    def set_deafened(self, want_deaf: bool, hotkey: dict[str, Any] | None, enabled: bool) -> None:
        with self._lock:
            if not enabled or not hotkey:
                if self._deafened_by_us and hotkey:
                    self._send_with_cooldown(hotkey)
                    self._deafened_by_us = False
                return

            if want_deaf and not self._deafened_by_us:
                if self._send_with_cooldown(hotkey):
                    self._deafened_by_us = True
            elif not want_deaf and self._deafened_by_us:
                if self._send_with_cooldown(hotkey):
                    self._deafened_by_us = False

    def restore(self, hotkey: dict[str, Any] | None) -> None:
        with self._lock:
            if self._deafened_by_us and hotkey:
                self._send_with_cooldown(hotkey)
            self._deafened_by_us = False


class GlobalHotkeyWatcher:
    """Listens globally via Win32 RegisterHotKey so a keybind works while GD is focused."""

    HOTKEY_ID = 1

    def __init__(self, on_trigger: Callable[[], None]) -> None:
        self._on_trigger = on_trigger
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._stop_requested = False

    def set_hotkey(self, hotkey: dict[str, Any] | None) -> tuple[bool, str]:
        """Register a new global hotkey, returning (active, status_message)."""
        with self._lock:
            self._stop_locked()
            if not hotkey or "vk" not in hotkey:
                return False, "Inactive"

            vk = int(hotkey["vk"])
            mods = MOD_NOREPEAT
            for name in hotkey.get("modifiers", []):
                mods |= HOTKEY_MOD_FLAGS.get(name, 0)

            ready = threading.Event()
            result: dict[str, Any] = {"ok": False, "msg": "Inactive"}
            self._stop_requested = False

            def worker() -> None:
                tid = int(kernel32.GetCurrentThreadId())
                self._thread_id = tid
                msg = wintypes.MSG()
                # Ensure the thread has a message queue before RegisterHotKey or PostThreadMessageW.
                user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

                ctypes.set_last_error(0)
                if not user32.RegisterHotKey(None, self.HOTKEY_ID, mods, vk):
                    err = ctypes.get_last_error()
                    if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                        result["msg"] = "In use by another app"
                    else:
                        result["msg"] = f"Unavailable (err {err})"
                    ready.set()
                    return

                result["ok"] = True
                result["msg"] = "Active (Global)"
                ready.set()

                try:
                    while not self._stop_requested:
                        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                        if ret <= 0:
                            break
                        if msg.message == WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                            try:
                                self._on_trigger()
                            except Exception:
                                pass
                finally:
                    user32.UnregisterHotKey(None, self.HOTKEY_ID)

            t = threading.Thread(target=worker, daemon=True)
            self._thread = t
            t.start()
            ready.wait(timeout=1.0)
            if not result["ok"]:
                t.join(timeout=0.3)
                self._thread = None
                self._thread_id = 0
            return bool(result["ok"]), str(result["msg"])

    def _stop_locked(self) -> None:
        self._stop_requested = True
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None
        self._thread_id = 0

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
