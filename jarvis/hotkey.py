"""Global hotkey that works in games.

The `keyboard` library registers a low-level Windows keyboard hook — and those
receive nothing while an elevated window (game launchers, anti-cheat) has
focus, which is exactly when the mute key matters most. Polling
GetAsyncKeyState has no such blind spot: it reads key state directly, needs no
elevation, and DirectInput/raw-input games still update it.

Combos are written "m+n" — every named key must be held at the same time.
The callback fires once per press; holding the keys doesn't repeat it.
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Callable

_POLL_SECONDS = 0.03  # ~33 checks/s; negligible CPU, feels instant

_VK_NAMED = {
    "pause": 0x13, "scroll lock": 0x91, "num lock": 0x90, "caps lock": 0x14,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "backspace": 0x08, "esc": 0x1B,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "page up": 0x21, "page down": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},  # f1..f24
}


def _vk(name: str) -> int:
    name = name.strip().lower()
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name in _VK_NAMED:
        return _VK_NAMED[name]
    raise ValueError(f"unknown key name '{name}'")


def parse_combo(combo: str) -> list[int]:
    """'m+n' -> [0x4D, 0x4E]. Raises ValueError on anything unrecognized."""
    keys = [_vk(part) for part in combo.split("+") if part.strip()]
    if not keys:
        raise ValueError(f"empty hotkey combo '{combo}'")
    return keys


def _is_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def watch_combo(combo: str, callback: Callable[[], None]) -> threading.Thread:
    """Fire callback whenever all keys in `combo` become pressed together.

    Parses eagerly (so a config typo surfaces at startup), then polls on a
    daemon thread forever. Callback exceptions are swallowed — a UI hiccup
    must never kill the watcher.
    """
    keys = parse_combo(combo)

    def _poll() -> None:
        was_down = False
        while True:
            down = all(_is_down(k) for k in keys)
            if down and not was_down:
                try:
                    callback()
                except Exception:
                    pass
            was_down = down
            time.sleep(_POLL_SECONDS)

    thread = threading.Thread(target=_poll, daemon=True, name=f"hotkey-{combo}")
    thread.start()
    return thread
