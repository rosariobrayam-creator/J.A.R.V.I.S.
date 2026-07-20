"""Gaming copilot support: game mode + per-game persistent notes.

Game mode makes Jarvis a polite background process while a game has the PC:
the whole process drops to below-normal CPU priority and registered listeners
(the UI) dial their update rate down. The MCP server runs in this same
process, so the tool call in tools.py mutates this module's state directly.

Notes live as one markdown file per game under game_data/ at the project
root — grind routines, unlocks, goals — so they survive restarts and let the
brain build on what the user told it last week.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable

import psutil

GAME_DATA_DIR = Path(__file__).resolve().parents[1] / "game_data"

_lock = threading.Lock()
_enabled = False
_current_game = ""
_listeners: list[Callable[[bool], None]] = []


# ---------------------------------------------------------------------------
# Game mode
# ---------------------------------------------------------------------------


def on_game_mode(callback: Callable[[bool], None]) -> None:
    """Register a listener called with True/False when game mode toggles."""
    _listeners.append(callback)


def is_game_mode() -> bool:
    return _enabled


def current_game() -> str:
    return _current_game


def _set_process_priority(low: bool) -> None:
    try:
        psutil.Process().nice(
            psutil.BELOW_NORMAL_PRIORITY_CLASS if low else psutil.NORMAL_PRIORITY_CLASS
        )
    except Exception:
        pass  # priority is best-effort; never fail the toggle over it


def set_game_mode(enabled: bool, game: str = "") -> str:
    global _enabled, _current_game
    with _lock:
        _enabled = enabled
        _current_game = game.strip() if enabled else ""
        _set_process_priority(enabled)
    for cb in _listeners:
        try:
            cb(enabled)
        except Exception:
            pass
    if enabled:
        title = f" for {_current_game}" if _current_game else ""
        return (
            f"Game mode on{title}: Jarvis dropped to background CPU priority "
            "and reduced UI updates so the game runs smoothly. Voice commands "
            "still work exactly as before."
        )
    return "Game mode off: normal priority and UI updates restored."


# ---------------------------------------------------------------------------
# Per-game notes (grind routines, goals, unlocks, ...)
# ---------------------------------------------------------------------------


def _slug(game: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", game.lower()).strip("-")
    return s or "unknown-game"


def _notes_path(game: str) -> Path:
    return GAME_DATA_DIR / f"{_slug(game)}.md"


def save_game_notes(game: str, content: str, mode: str = "append") -> str:
    if not game.strip():
        return "Error: 'game' must name the game these notes are for."
    if not content.strip():
        return "Error: 'content' is empty."
    if mode not in ("append", "replace"):
        return f"Error: unknown mode '{mode}' (use 'append' or 'replace')."
    GAME_DATA_DIR.mkdir(exist_ok=True)
    path = _notes_path(game)
    text = content.strip() + "\n"
    if mode == "append" and path.exists():
        path.write_text(
            path.read_text(encoding="utf-8").rstrip() + "\n\n" + text,
            encoding="utf-8",
        )
    else:
        path.write_text(f"# {game.strip()}\n\n{text}", encoding="utf-8")
    return f"Saved notes for {game.strip()} ({mode})."


def read_game_notes(game: str = "") -> str:
    if not game.strip():
        games = sorted(p.stem for p in GAME_DATA_DIR.glob("*.md"))
        if not games:
            return "No game notes saved yet."
        return "Games with saved notes: " + ", ".join(games)
    path = _notes_path(game)
    if not path.exists():
        games = sorted(p.stem for p in GAME_DATA_DIR.glob("*.md"))
        hint = f" Games with notes: {', '.join(games)}." if games else ""
        return f"No notes saved for '{game.strip()}' yet.{hint}"
    return path.read_text(encoding="utf-8")
