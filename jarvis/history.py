"""Conversation transcript: append-only JSONL log at the project root.

Every exchange (voice or text mode) lands in history.jsonl; the UI's
transcript panel loads the recent tail on startup and gets new entries
pushed live.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import HISTORY_RETENTION_DAYS

HISTORY_PATH = Path(__file__).resolve().parents[1] / "history.jsonl"

_lock = threading.Lock()


def prune(retention_days: float = HISTORY_RETENTION_DAYS) -> int:
    """Drop entries older than the retention window. Called once at startup;
    returns how many entries were removed."""
    cutoff = time.time() - retention_days * 86400
    with _lock:
        try:
            lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        except Exception:
            return 0
        kept = []
        for line in lines:
            try:
                if json.loads(line).get("ts", 0) >= cutoff:
                    kept.append(line)
            except Exception:
                continue  # malformed lines age out with the prune
        if len(kept) == len(lines):
            return 0
        try:
            HISTORY_PATH.write_text(
                "".join(l + "\n" for l in kept), encoding="utf-8"
            )
        except Exception:
            return 0
        return len(lines) - len(kept)


def log(role: str, text: str) -> dict:
    """Append one entry ('user' or 'jarvis') and return it for UI push."""
    entry = {"ts": time.time(), "role": role, "text": text}
    try:
        with _lock, HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # a failed disk write shouldn't break the conversation
    return entry


def recent(limit: int = 100) -> list[dict]:
    try:
        lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
