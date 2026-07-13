"""Free brain backend: routes through headless Claude Code on your subscription.

Shells out to `claude -p` with a session-resume for conversation continuity and
the local MCP server for tool access. No API key required.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .personality import SYSTEM_PROMPT

MCP_CONFIG = Path(__file__).resolve().parent.parent / "mcp-config.json"
TURN_TIMEOUT = 240  # seconds; first turns can be slow
MAX_TURNS = 15


class ClaudeCodeBrain:
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

    def ask(self, user_input: str) -> str:
        cmd = [
            self.exe,
            "-p", user_input,
            "--output-format", "json",
            "--model", "sonnet",
            "--append-system-prompt", SYSTEM_PROMPT,
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
        )
        self._proc = proc
        try:
            stdout, stderr = proc.communicate(timeout=TURN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError(
                f"Claude took longer than {TURN_TIMEOUT}s to reply — try again."
            ) from None
        finally:
            self._proc = None
        if proc.returncode != 0:
            detail = (stderr or stdout or "").strip()[-500:]
            raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail}")

        data = json.loads(stdout)
        self.session_id = data.get("session_id", self.session_id)
        text = (data.get("result") or "").strip()
        if data.get("is_error"):
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
