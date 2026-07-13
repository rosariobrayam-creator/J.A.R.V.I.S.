"""The J.A.R.V.I.S. brain: a streaming agentic loop over the Claude API.

Maintains conversation history across turns and handles tool calls until the
model finishes its response. Text is streamed to the console as it arrives —
the same hook a TTS engine will attach to in Phase 2.
"""

from __future__ import annotations

from typing import Callable

import anthropic

from .config import MAX_TOKENS, MODEL
from .personality import SYSTEM_PROMPT
from .tools import TOOL_SCHEMAS, execute_tool

MAX_TOOL_ROUNDS = 10


class JarvisBrain:
    def __init__(self, on_text: Callable[[str], None] | None = None):
        self.client = anthropic.Anthropic()
        self.messages: list[dict] = []
        # Called with each streamed text chunk; defaults to printing to stdout.
        self.on_text = on_text or (lambda chunk: print(chunk, end="", flush=True))

    def ask(self, user_input: str) -> str:
        """Send one user turn, run tools as needed, return the full reply text."""
        self.messages.append({"role": "user", "content": user_input})
        reply_parts: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            with self.client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                thinking={"type": "adaptive"},
                tools=TOOL_SCHEMAS,
                messages=self.messages,
            ) as stream:
                for text in stream.text_stream:
                    reply_parts.append(text)
                    self.on_text(text)
                response = stream.get_final_message()

            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                note = "I'm afraid I can't assist with that request."
                reply_parts.append(note)
                self.on_text(note)
                break

            if response.stop_reason == "pause_turn":
                continue  # server-side pause; re-send to resume

            if response.stop_reason != "tool_use":
                break  # end_turn (or max_tokens) — we're done

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    self.messages.append({"role": "user", "content": tool_results})
                    result, is_error = execute_tool(block.name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                            "is_error": is_error,
                        }
                    )
            

        return "".join(reply_parts)

    def reset(self) -> None:
        self.messages.clear()
