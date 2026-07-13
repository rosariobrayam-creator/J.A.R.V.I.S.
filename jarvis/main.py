"""J.A.R.V.I.S. text chat REPL — Phase 1 entry point.

Run with:  python -m jarvis.main
"""

from __future__ import annotations

import sys

import anthropic

from . import history
from .config import BACKEND

BANNER = r"""
     ____  ___    ____ _    __ ____ _____
 __ / /  |/  /   / __ \ |  / //  _// ___/
/ // / /|_/ /   / /_/ / | / / / /  \__ \
\___/_/  /_/ (_)_/ |_/| |/ /_/ /_ ___/ /
                      |___//___(_)____/
        Just A Rather Very Intelligent System — v0.1 (text mode)
        Type your request. 'reset' clears memory, 'exit' quits.
"""


def _create_brain():
    if BACKEND == "claude-code":
        from .claude_code_brain import ClaudeCodeBrain
        from .mcp_server import start_in_background

        start_in_background()
        return ClaudeCodeBrain()
    from .brain import JarvisBrain

    return JarvisBrain()


def main() -> None:
    # Windows consoles default to a legacy codepage; Jarvis speaks in UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    print(BANNER)
    history.prune()
    brain = _create_brain()

    # Opening line, in character, without an API call
    print("J.A.R.V.I.S.: At your service, sir. How may I help?\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nJ.A.R.V.I.S.: Powering down. Goodbye, sir.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "bye"):
            print("J.A.R.V.I.S.: Powering down. Goodbye, sir.")
            break
        if user_input.lower() == "reset":
            brain.reset()
            print("J.A.R.V.I.S.: Memory wiped. A fresh start, then.\n")
            continue

        print("J.A.R.V.I.S.: ", end="", flush=True)
        try:
            history.log("user", user_input)
            reply = brain.ask(user_input)
            if reply:
                history.log("jarvis", reply)
        except anthropic.AuthenticationError:
            print(
                "\n[Error] Invalid or missing API key. Set the ANTHROPIC_API_KEY "
                "environment variable or run 'ant auth login'."
            )
        except anthropic.RateLimitError:
            print("\n[Error] Rate limited — give it a moment and try again.")
        except anthropic.APIStatusError as e:
            print(f"\n[Error] API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            print("\n[Error] Network problem — check your connection.")
        except (RuntimeError, TimeoutError) as e:
            print(f"\n[Error] {e}")
        print("\n")


if __name__ == "__main__":
    sys.exit(main())
