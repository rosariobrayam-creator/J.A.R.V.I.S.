"""Free brain backend: routes through headless Claude Code on your subscription.

Shells out to `claude -p` with a session-resume for conversation continuity and
the local MCP server for tool access. No API key required.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from . import game
from .config import (
    BRAIN_MODEL,
    BRAIN_THINKING,
    GAME_BRAIN_MODEL,
    WATCH_SESSION_MAX_LOOKS,
)
from .personality import GAME_PROMPT, SYSTEM_PROMPT

MCP_CONFIG = Path(__file__).resolve().parent.parent / "mcp-config.json"
TURN_TIMEOUT = 240  # seconds; first turns can be slow
MAX_TURNS = 15
QUICK_TIMEOUT = 90  # watch-mode call-outs are worthless if they arrive late

# The watcher keeps its own resumed session, separate from the conversation:
# it remembers what it already said (no repeated lines, callbacks to earlier
# moments) and later call-outs prefill mostly from prompt cache. Only ever
# touched from the single watch-alert thread (replay's _alert_busy lock).
_watch_session_id: str | None = None
_watch_looks = 0


def reset_watch_session() -> None:
    """Forget the watcher's commentary session (watch toggled, or context cap)."""
    global _watch_session_id, _watch_looks
    _watch_session_id = None
    _watch_looks = 0


def has_watch_session() -> bool:
    """True while a watcher session is live — callers send a short follow-up
    prompt instead of re-sending the full persona."""
    return _watch_session_id is not None


def _cli_env() -> dict[str, str]:
    """Environment for the `claude` subprocess.

    The CLI reads ~/.claude/settings.json, which is tuned for interactive
    coding — so without this, whatever effort level is set there decides how
    long Jarvis takes to answer out loud. MAX_THINKING_TOKENS=0 turns the
    reasoning pass off for spoken replies regardless of that file.
    """
    env = os.environ.copy()
    if not BRAIN_THINKING:
        env["MAX_THINKING_TOKENS"] = "0"
    return env


def quick_look_stream(
    prompt: str,
    model: str = "haiku",
    on_delta: Callable[[str], None] | None = None,
    on_event: Callable[[str], None] | None = None,
) -> str:
    """Fast turn for watch-mode call-outs: screen tools only, no persona
    system prompt (the watcher persona rides in the first user prompt so it
    lands in session history once). Resumes the watcher session when one is
    live. With on_delta, reply text streams out as it's generated so speech
    can start on the first sentence. on_event marks stream structure the
    caller needs to gate speech: "tool_use" when a tool call starts (any text
    so far was narration, not the reply) and "block_stop" when a text block
    completes. Returns the final reply text; raises on any failure (the
    watcher treats failures as 'say nothing')."""
    global _watch_session_id, _watch_looks
    on_delta = on_delta or (lambda piece: None)
    on_event = on_event or (lambda kind: None)
    exe = shutil.which("claude")
    if exe is None:
        raise RuntimeError("Claude Code CLI not found on PATH.")
    # Every look drags ~10-15k image tokens into the session; retire it before
    # it grows past what a fast call-out can afford to prefill.
    if _watch_looks >= WATCH_SESSION_MAX_LOOKS:
        reset_watch_session()
    cmd = [
        exe,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model", model,
        "--mcp-config", str(MCP_CONFIG),
        "--strict-mcp-config",
        "--allowedTools", "mcp__jarvis",
        "--max-turns", "6",
    ]
    if _watch_session_id:
        cmd += ["--resume", _watch_session_id]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW,
        env=_cli_env(),
    )
    stderr_parts: list[str] = []
    drain = threading.Thread(
        target=lambda: stderr_parts.append(proc.stderr.read() or ""),
        daemon=True, name="quick-look-stderr",
    )
    drain.start()
    timed_out = threading.Event()

    def _deadline() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(QUICK_TIMEOUT, _deadline)
    timer.start()

    result: dict | None = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "stream_event":
                event = data.get("event") or {}
                if event.get("type") == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        on_event("tool_use")
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        on_delta(delta["text"])
                elif event.get("type") == "content_block_stop":
                    on_event("block_stop")
            elif kind == "result":
                result = data
        proc.wait()
    finally:
        timer.cancel()
    drain.join(timeout=2.0)

    if timed_out.is_set():
        raise RuntimeError(f"quick_look timed out after {QUICK_TIMEOUT}s.")
    if result is None:
        detail = ((stderr_parts and stderr_parts[0]) or "").strip()[-300:]
        raise RuntimeError(f"quick_look failed (exit {proc.returncode}): {detail}")
    text = (result.get("result") or "").strip()
    if result.get("is_error"):
        # A broken session id (expired, corrupted) would otherwise wedge every
        # future call-out; drop it so the next look starts clean.
        reset_watch_session()
        raise RuntimeError(f"quick_look error: {text[:300]}")
    _watch_session_id = result.get("session_id", _watch_session_id)
    _watch_looks += 1
    return text


class ClaudeCodeBrain:
    # ask() can deliver text incrementally via on_delta while the model is
    # still generating — voice_main starts speaking off the first sentence.
    supports_streaming = True

    def __init__(self, on_text: Callable[[str], None] | None = None):
        exe = shutil.which("claude")
        if exe is None:
            raise RuntimeError(
                "Claude Code CLI not found on PATH. Install it or switch "
                "BACKEND to 'api' in jarvis/config.py."
            )
        self.exe = exe
        self.session_id: str | None = None
        self.on_text = on_text or (lambda chunk: print(chunk, end="", flush=True))
        self._proc: subprocess.Popen | None = None

    def ask(
        self, user_input: str, on_delta: Callable[[str], None] | None = None
    ) -> str:
        """One turn. With on_delta, reply text is streamed out as the model
        writes it (including short lead-ins before tool calls), so speech can
        begin seconds before the turn finishes. Returns the final reply text."""
        on_delta = on_delta or (lambda piece: None)
        # Game mode swaps in the faster model and the answer-first brief: on a
        # mission timer, finishing ~2s sooner is worth more than the nuance.
        in_game = game.is_game_mode()
        cmd = [
            self.exe,
            "-p", user_input,
            # stream-json + partial messages = text deltas as they're generated
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model", GAME_BRAIN_MODEL if in_game else BRAIN_MODEL,
            "--append-system-prompt",
            SYSTEM_PROMPT + (GAME_PROMPT if in_game else ""),
            "--mcp-config", str(MCP_CONFIG),
            "--strict-mcp-config",
            # WebSearch/WebFetch let Jarvis answer questions with live info;
            # opening the browser for the user is the mcp__jarvis search_web tool.
            "--allowedTools", "mcp__jarvis,WebSearch,WebFetch",
            "--max-turns", str(MAX_TURNS),
        ]
        if self.session_id:
            cmd += ["--resume", self.session_id]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            # Never pop a console window: when Jarvis runs windowless (pythonw
            # via the desktop shortcut), a console child would otherwise open a
            # cmd window over the user's game — and end up in screenshots.
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=_cli_env(),
        )
        self._proc = proc
        # Drain stderr on the side so a chatty CLI can't deadlock the pipe.
        stderr_parts: list[str] = []
        drain = threading.Thread(
            target=lambda: stderr_parts.append(proc.stderr.read() or ""),
            daemon=True, name="claude-stderr",
        )
        drain.start()
        timed_out = threading.Event()

        def _deadline() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(TURN_TIMEOUT, _deadline)
        timer.start()

        result: dict | None = None
        saw_delta = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "stream_event":
                    event = data.get("event") or {}
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            saw_delta = True
                            on_delta(delta["text"])
                    elif event.get("type") == "content_block_stop":
                        # Blocks/messages don't carry separating whitespace.
                        on_delta(" ")
                elif kind == "assistant" and not saw_delta:
                    # Older CLI without partial messages: whole blocks at a time.
                    for block in (data.get("message") or {}).get("content", []):
                        if block.get("type") == "text" and block.get("text"):
                            on_delta(block["text"] + " ")
                elif kind == "result":
                    result = data
            proc.wait()
        finally:
            timer.cancel()
            self._proc = None
        drain.join(timeout=2.0)

        if timed_out.is_set():
            raise RuntimeError(
                f"Claude took longer than {TURN_TIMEOUT}s to reply — try again."
            )
        if result is None:
            detail = ((stderr_parts and stderr_parts[0]) or "").strip()[-500:]
            raise RuntimeError(
                f"claude CLI failed (exit {proc.returncode}): {detail}"
            )
        self.session_id = result.get("session_id", self.session_id)
        text = (result.get("result") or "").strip()
        if result.get("is_error"):
            raise RuntimeError(f"claude CLI reported an error: {text[:500]}")
        self.on_text(text)
        return text

    def cancel(self) -> None:
        """Abandon the in-flight turn (user barged in while thinking). The
        killed turn never lands in the session, so the caller should fold the
        original question into its next ask."""
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def reset(self) -> None:
        self.session_id = None
