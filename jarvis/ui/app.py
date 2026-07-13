"""pywebview shell for the J.A.R.V.I.S. UI.

The window hosts web/index.html. Python pushes state in via evaluate_js;
the page calls back through the exposed js_api (mute toggle).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import webview

HTML_PATH = Path(__file__).resolve().parent / "web" / "index.html"


class _JsApi:
    """Methods callable from JavaScript via window.pywebview.api."""

    def __init__(self) -> None:
        self.on_mute_toggle: Callable[[], bool] | None = None
        self.on_restart: Callable[[], None] | None = None

    def toggle_mute(self) -> bool:
        if self.on_mute_toggle is not None:
            return self.on_mute_toggle()
        return False

    def restart(self) -> None:
        if self.on_restart is not None:
            self.on_restart()


class JarvisUI:
    def __init__(self) -> None:
        self.api = _JsApi()
        self.window = webview.create_window(
            "J.A.R.V.I.S.",
            HTML_PATH.as_uri(),
            js_api=self.api,
            width=920,
            height=620,
            background_color="#05080f",
        )

    def run(self, on_ready: Callable[[], None]) -> None:
        """Blocks on the UI main loop; on_ready fires once the window is up."""
        webview.start(on_ready, private_mode=True)

    # -- push helpers (safe to call from any thread) --------------------------

    def _js(self, code: str) -> None:
        try:
            self.window.evaluate_js(code)
        except Exception:
            pass  # window closing/closed — never let UI pushes kill the pipeline

    def set_mic_level(self, level: float) -> None:
        self._js(f"jarvisUI.setMicLevel({min(1.0, max(0.0, level)):.3f})")

    def set_speak_level(self, level: float) -> None:
        self._js(f"jarvisUI.setSpeakLevel({min(1.0, max(0.0, level)):.3f})")

    def set_speaking(self, speaking: bool) -> None:
        self._js(f"jarvisUI.setSpeaking({str(speaking).lower()})")

    def set_state(self, text: str) -> None:
        self._js(f"jarvisUI.setState({json.dumps(text)})")

    def set_user_text(self, text: str) -> None:
        self._js(f"jarvisUI.setUser({json.dumps(text)})")

    def set_reply_text(self, text: str) -> None:
        self._js(f"jarvisUI.setReply({json.dumps(text)})")

    def set_muted(self, muted: bool) -> None:
        self._js(f"jarvisUI.setMuted({str(muted).lower()})")

    def set_weather(self, weather: dict | None) -> None:
        self._js(f"jarvisUI.setWeather({json.dumps(weather)})")

    def set_emails(self, payload: dict) -> None:
        self._js(f"jarvisUI.setEmails({json.dumps(payload)})")

    def set_system(self, text: str) -> None:
        self._js(f"jarvisUI.setSystem({json.dumps(text)})")

    def set_history(self, entries: list[dict]) -> None:
        self._js(f"jarvisUI.setHistory({json.dumps(entries)})")

    def add_history(self, entry: dict) -> None:
        self._js(f"jarvisUI.addHistory({json.dumps(entry)})")

    def close(self) -> None:
        """Close the window (ends the app — webview.start returns)."""
        try:
            self.window.destroy()
        except Exception:
            pass
