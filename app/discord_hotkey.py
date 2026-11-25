from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any

user32 = ctypes.windll.user32

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

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

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deafened_by_us = False

    @property
    def deafened_by_us(self) -> bool:
        with self._lock:
            return self._deafened_by_us

    def set_deafened(self, want_deaf: bool, hotkey: dict[str, Any] | None, enabled: bool) -> None:
        with self._lock:
            if not enabled or not hotkey:
                if self._deafened_by_us and hotkey:
                    send_hotkey(hotkey)
                    self._deafened_by_us = False
                return

            if want_deaf and not self._deafened_by_us:
                if send_hotkey(hotkey):
                    self._deafened_by_us = True
            elif not want_deaf and self._deafened_by_us:
                if send_hotkey(hotkey):
                    self._deafened_by_us = False

    def restore(self, hotkey: dict[str, Any] | None) -> None:
        with self._lock:
            if self._deafened_by_us and hotkey:
                send_hotkey(hotkey)
            self._deafened_by_us = False
