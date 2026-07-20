"""Rolling screen replay + proactive screen watch.

The replay buffer is a background thread that snapshots the primary monitor
every REPLAY_INTERVAL seconds into a RAM ring buffer holding the last
REPLAY_SECONDS — Jarvis's short-term visual memory. It starts and stops with
game mode and powers the review_recent_screen MCP tool ("what just happened
on my screen?").

Watch mode sits on top: while enabled, frames ~1.2s apart are compared and
the alert handler fires so the voice loop can look at the screen and speak
unprompted. Three trigger paths, so it works on card tables AND live action:
"settled" (scene changed then settled — a hand dealt, a result screen),
"spike" (a sudden large change — a crash, a kill banner), and "heartbeat"
(sustained motion that never settles — driving, firefights — with no look in
a while). Detection is a cheap changed-pixel fraction on a tiny grayscale
thumbnail; the model decides whether the moment merits a comment.
"""

from __future__ import annotations

import io
import threading
import time
from collections import deque
from typing import Callable

from mss import mss
from PIL import Image, ImageChops

from . import game
from .config import (
    REPLAY_INTERVAL,
    REPLAY_JPEG_QUALITY,
    REPLAY_SECONDS,
    REPLAY_WIDTH,
    WATCH_COOLDOWN,
    WATCH_HEARTBEAT,
    WATCH_SETTLE,
    WATCH_SPIKE,
    WATCH_TRIGGER,
)

_THUMB_SIZE = (64, 36)  # change detection runs on this, so it costs nothing
_PIXEL_DELTA = 14  # a thumbnail pixel counts as "changed" past this (0-255)
# WATCH_TRIGGER/WATCH_SETTLE were tuned when frames were 1.2s apart. Now that
# capture runs at 5 fps, consecutive frames are too similar to ever cross those
# thresholds — so detection compares each thumb against the one ~1.2s back,
# keeping the effective cadence (and the tuned thresholds) unchanged.
_DETECT_SPACING = 1.2

_lock = threading.Lock()
_frames: deque[tuple[float, bytes]] = deque(
    maxlen=max(2, round(REPLAY_SECONDS / REPLAY_INTERVAL))
)
_running = threading.Event()
_thread: threading.Thread | None = None

_watch_enabled = False
_watch_focus = ""
_alert_handler: Callable[[str, str], None] | None = None
_alert_busy = threading.Lock()  # one call-out analysis in flight at a time
_alert_reason = ""  # why the pending call-out fired; written under _alert_busy


# ---------------------------------------------------------------------------
# Replay buffer
# ---------------------------------------------------------------------------


def replay_running() -> bool:
    return _running.is_set()


def start_replay() -> None:
    global _thread
    if _running.is_set():
        return
    # A just-stopped capture thread can sleep through one more interval; wait
    # it out so a quick off/on never leaves two loops running.
    old = _thread
    if old is not None and old.is_alive():
        old.join(timeout=REPLAY_INTERVAL + 2.0)
    with _lock:
        if _running.is_set():
            return
        _running.set()
        _thread = threading.Thread(
            target=_capture_loop, daemon=True, name="jarvis-replay"
        )
        _thread.start()


def stop_replay() -> None:
    with _lock:
        _running.clear()
        _frames.clear()


def recent_frames(seconds: float, max_frames: int) -> list[tuple[float, bytes]]:
    """Frames from the last `seconds`, evenly sampled down to `max_frames`.

    Returns (age_in_seconds, jpeg_bytes) pairs, oldest first, newest always
    included.
    """
    now = time.time()
    with _lock:
        window = [(now - ts, jpg) for ts, jpg in _frames if now - ts <= seconds]
    if len(window) > max_frames > 1:
        last = len(window) - 1
        picks = sorted({round(i * last / (max_frames - 1)) for i in range(max_frames)})
        window = [window[i] for i in picks]
    return window


def _changed_fraction(prev: Image.Image, cur: Image.Image) -> float:
    hist = ImageChops.difference(prev, cur).histogram()
    total = sum(hist)
    return sum(hist[_PIXEL_DELTA + 1 :]) / total if total else 0.0


def _capture_loop() -> None:
    global _alert_reason
    # Thumbs from the last _DETECT_SPACING seconds; [0] is the comparison frame.
    thumb_history: deque[Image.Image] = deque(
        maxlen=max(2, round(_DETECT_SPACING / REPLAY_INTERVAL))
    )
    changing = False
    last_alert = 0.0
    with mss() as sct:
        while _running.is_set():
            t0 = time.monotonic()
            try:
                shot = sct.grab(sct.monitors[1])
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            except Exception:
                time.sleep(REPLAY_INTERVAL)
                continue
            if img.width > REPLAY_WIDTH:
                img = img.resize(
                    (REPLAY_WIDTH, round(img.height * REPLAY_WIDTH / img.width)),
                    Image.BILINEAR,  # replay frames favor speed over polish
                )
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=REPLAY_JPEG_QUALITY)
            with _lock:
                _frames.append((time.time(), buf.getvalue()))

            thumb = img.resize(_THUMB_SIZE).convert("L")
            if _watch_enabled and len(thumb_history) == thumb_history.maxlen:
                frac = _changed_fraction(thumb_history[0], thumb)
                now = time.monotonic()
                cooled = now - last_alert >= WATCH_COOLDOWN
                reason = ""
                if cooled and frac >= WATCH_SPIKE:
                    # Crash, kill banner, result screen slamming in: react now,
                    # and swallow the settle that follows the same event.
                    reason = "spike"
                    changing = False
                elif not changing and frac >= WATCH_TRIGGER:
                    changing = True
                elif changing and frac <= WATCH_SETTLE:
                    changing = False
                    if cooled:
                        reason = "settled"
                elif frac >= WATCH_TRIGGER and now - last_alert >= WATCH_HEARTBEAT:
                    # Continuous action (driving, a firefight) never settles;
                    # check in anyway and let the model judge the moment.
                    reason = "heartbeat"
                if (
                    reason
                    and _alert_handler is not None
                    and _alert_busy.acquire(blocking=False)
                ):
                    last_alert = now
                    _alert_reason = reason
                    threading.Thread(
                        target=_run_alert, daemon=True, name="jarvis-watch-alert"
                    ).start()
            else:
                changing = False
            thumb_history.append(thumb)

            time.sleep(max(0.05, REPLAY_INTERVAL - (time.monotonic() - t0)))


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------


def set_alert_handler(handler: Callable[[str, str], None]) -> None:
    """The voice loop registers here; the handler gets (watch focus, trigger
    reason) and is expected to look at the screen and speak. Called from a
    worker thread, never concurrently with itself."""
    global _alert_handler
    _alert_handler = handler


def _run_alert() -> None:
    try:
        handler = _alert_handler
        if handler is not None:
            handler(_watch_focus, _alert_reason)
    except Exception:
        pass  # a failed call-out should never kill the watcher
    finally:
        _alert_busy.release()


def set_watch(enabled: bool, focus: str = "") -> str:
    global _watch_enabled, _watch_focus
    # Toggling watch starts a fresh commentary session either way: the watcher
    # shouldn't "remember" a table it left an hour ago.
    from .claude_code_brain import reset_watch_session

    reset_watch_session()
    if enabled:
        _watch_focus = focus.strip()
        _watch_enabled = True
        start_replay()
        return (
            f"Watch mode on — focus: {_watch_focus or 'general gameplay commentary'}. "
            "The screen is being recorded into the rolling replay, and Jarvis "
            "will look and speak up on his own when something happens — a big "
            "moment fires instantly, quieter changes when they settle, and "
            "during nonstop action he checks in periodically (voice mode "
            "only). Each call-out takes a few seconds of analysis before it's "
            "spoken."
        )
    _watch_enabled = False
    _watch_focus = ""
    if not game.is_game_mode():
        stop_replay()
    return "Watch mode off. No more automatic call-outs."


def watch_status() -> str:
    if _watch_enabled:
        return (
            "Watch mode is on — focus: "
            f"{_watch_focus or 'general gameplay commentary'}."
        )
    if replay_running():
        return (
            "Watch mode is off, but the rolling screen replay is recording "
            f"(last {REPLAY_SECONDS // 60} minutes available via review_recent_screen)."
        )
    return "Watch mode is off and the screen replay is not recording."


# Replay is the game-session visual memory: recording follows game mode, and
# ending the session also silences the watcher — nobody wants spontaneous
# call-outs after they've left the table.
def _on_game_mode(on: bool) -> None:
    if on:
        start_replay()
    else:
        set_watch(False)


game.on_game_mode(_on_game_mode)
