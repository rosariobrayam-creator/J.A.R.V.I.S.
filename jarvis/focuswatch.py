"""Foreground-focus watchdog: catches whatever steals input focus from a game.

A game only receives keyboard and mouse input while its window holds the
foreground, and several Jarvis tools legitimately foreground something else
(launching an app, opening the browser, starting Spotify). Tools run in the
gap between the user's question and the spoken reply — so a focus steal lands,
from the player's chair, as "my inputs died right before Jarvis talked".

While game mode is on, this polls GetForegroundWindow and records every
change with a timestamp: a [focus] console line as it happens, and the recent
list via the get_focus_changes tool, so "did something take focus?" has an
answer instead of a guess.
"""

from __future__ import annotations

import ctypes
import datetime
import sys
import threading
import time
from collections import deque

import psutil

from . import game

_POLL_SECONDS = 0.25  # quick enough to catch a brief steal; GetForegroundWindow is cheap

_lock = threading.Lock()
_events: deque[str] = deque(maxlen=120)
_running = threading.Event()
_thread: threading.Thread | None = None


def _foreground() -> tuple[int, str, str]:
    """(pid, process name, window title) of the current foreground window."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    try:
        name = psutil.Process(pid.value).name()
    except Exception:
        pass
    return pid.value, name, buf.value


def _watch() -> None:
    prev_pid: int | None = None
    while _running.is_set():
        try:
            pid, proc, title = _foreground()
            if pid != prev_pid:
                stamp = datetime.datetime.now().strftime("%H:%M:%S")
                line = f"{stamp}  focus -> {proc or 'unknown'} ('{title or 'untitled'}')"
                with _lock:
                    _events.append(line)
                if sys.stdout is not None:  # windowless (pythonw) has no console
                    print(f"[focus] {line}", flush=True)
                prev_pid = pid
        except Exception:
            pass  # a flaky read must never kill the watchdog
        time.sleep(_POLL_SECONDS)


def start_watch() -> None:
    global _thread
    if _running.is_set():
        return
    old = _thread
    if old is not None and old.is_alive():
        old.join(timeout=1.0)
    _running.set()
    _thread = threading.Thread(target=_watch, daemon=True, name="jarvis-focuswatch")
    _thread.start()


def stop_watch() -> None:
    # Events are kept after game mode ends so "what stole focus?" can still be
    # answered about the session that just finished.
    _running.clear()


def get_focus_changes() -> str:
    with _lock:
        events = list(_events)
    if not events:
        if _running.is_set():
            return (
                "No focus changes recorded yet this session — the game has "
                "held input focus the whole time it's been watched."
            )
        return (
            "No focus changes recorded. The focus watchdog runs while game "
            "mode is on; turn game mode on to start tracking."
        )
    header = (
        "Foreground-focus changes recorded during game mode (a game only "
        "receives inputs while it holds focus — anything else in this list "
        "was stealing the user's inputs at that moment), oldest first:"
    )
    tail = "" if _running.is_set() else "\n(The watchdog is currently stopped — game mode is off.)"
    return header + "\n" + "\n".join(events) + tail


def _on_game_mode(on: bool) -> None:
    if on:
        start_watch()
    else:
        stop_watch()


game.on_game_mode(_on_game_mode)
