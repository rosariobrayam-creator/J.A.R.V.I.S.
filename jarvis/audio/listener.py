"""J.A.R.V.I.S. ears: always-listening wake word + command recording + local STT.

Two entry points:
- wait_for_command()   — block until "hey jarvis" + a command (cold start)
- wait_for_followup(s) — after Jarvis speaks, listen a few seconds for a reply
                         with no wake word needed (conversational flow)

Everything runs locally. Mute is a hard gate: frames are discarded before any
processing while muted.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd
from openwakeword.model import Model as WakeModel

from .. import game
from ..config import (
    GAME_PAUSE_SECONDS,
    MAX_COMMAND_SECONDS,
    PAUSE_SECONDS,
    WHISPER_THREADS,
)

SAMPLE_RATE = 16000
CHUNK = 1280  # 80 ms — the frame size openWakeWord expects
WAKE_THRESHOLD = 0.5
MIN_COMMAND_SECONDS = 0.6
FOLLOWUP_SPEECH_RMS = 450.0  # int16 RMS that counts as "user started talking"
WHISPER_MODEL = "base.en"  # ~75 MB, quick on CPU; bump to "small.en" for accuracy
STREAM_REFRESH_S = 300  # recycle the standby stream this often; a Windows audio
#                         stream can wedge silently (device switch, driver nap)
#                         and then reads nothing forever with no error


class _Reopen(Exception):
    """Internal: the standby stream should be closed and reopened."""


def _pause_seconds() -> float:
    """How much quiet ends an utterance. Shorter while gaming: the questions
    are short, and the wait is charged against a mission timer."""
    return GAME_PAUSE_SECONDS if game.is_game_mode() else PAUSE_SECONDS


def _chime(freq: int = 880, ms: int = 120) -> None:
    t = np.linspace(0, ms / 1000, int(SAMPLE_RATE * ms / 1000), dtype=np.float32)
    tone = 0.25 * np.sin(2 * np.pi * freq * t) * np.linspace(1, 0, len(t))
    sd.play(tone, SAMPLE_RATE, blocking=True)


class Listener:
    def __init__(
        self,
        on_level: Callable[[float], None] | None = None,
        on_state: Callable[[str], None] | None = None,
    ):
        self.on_level = on_level or (lambda level: None)
        self.on_state = on_state or (lambda state: None)
        self.muted = threading.Event()
        self._shutdown = threading.Event()
        self.wake_model = WakeModel(
            wakeword_models=["hey_jarvis"], inference_framework="onnx"
        )
        self._whisper = None  # lazy — first load downloads the model
        # Timestamps for the per-turn latency log. speech_ended_at is the
        # moment the user actually stopped talking, which is what they're
        # counting from — not when we noticed, PAUSE_SECONDS later.
        self.speech_ended_at = 0.0
        self.transcribed_at = 0.0

    # -- controls -----------------------------------------------------------

    def set_muted(self, muted: bool) -> None:
        if muted:
            self.muted.set()
        else:
            self.muted.clear()
        self.on_state("muted" if muted else "idle")

    def shutdown(self) -> None:
        self._shutdown.set()

    # -- STT ----------------------------------------------------------------

    def _get_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel

            self._whisper = WhisperModel(
                WHISPER_MODEL,
                device="cpu",
                compute_type="int8",
                cpu_threads=WHISPER_THREADS,
            )
        return self._whisper

    def transcribe(self, audio_int16: np.ndarray) -> str:
        audio = audio_int16.astype(np.float32) / 32768.0
        segments, _info = self._get_whisper().transcribe(audio, language="en")
        return " ".join(seg.text.strip() for seg in segments).strip()

    # -- internals ------------------------------------------------------------

    def _rms(self, mono: np.ndarray) -> float:
        return float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))

    def _record_until_silence(
        self, stream: sd.InputStream, preroll: list[np.ndarray]
    ) -> np.ndarray | None:
        """Record from an open stream until the speaker goes quiet."""
        self.on_state("listening")
        recorded: list[np.ndarray] = list(preroll)
        silence = _pause_seconds()
        silence_needed = int(silence * SAMPLE_RATE / CHUNK)
        max_chunks = int(MAX_COMMAND_SECONDS * SAMPLE_RATE / CHUNK)
        min_chunks = int(MIN_COMMAND_SECONDS * SAMPLE_RATE / CHUNK)
        quiet_run = 0
        noise_floor = 250.0  # int16 RMS; adapts upward in noisy rooms

        for _ in range(max_chunks):
            if self._shutdown.is_set() or self.muted.is_set():
                return None
            frames, _ = stream.read(CHUNK)
            mono = frames[:, 0]
            recorded.append(mono.copy())
            rms = self._rms(mono)
            self.on_level(rms / 3000)
            noise_floor = min(noise_floor * 1.001, 800.0)
            if rms < max(noise_floor, 300.0):
                quiet_run += 1
                if quiet_run >= silence_needed and len(recorded) > min_chunks:
                    # They stopped talking when the quiet started, not now.
                    self.speech_ended_at = time.monotonic() - silence
                    break
            else:
                quiet_run = 0
        else:
            self.speech_ended_at = time.monotonic()  # hit the hard cap mid-word
        return np.concatenate(recorded)

    def _finish(self, audio: np.ndarray | None) -> str | None:
        """Chime, transcribe, reset — shared tail for both listen modes."""
        if audio is None:
            return None
        _chime()
        self.on_state("thinking")
        self.on_level(0.0)
        text = self.transcribe(audio)
        self.transcribed_at = time.monotonic()
        self.wake_model.reset()
        return text or None

    # -- public listening modes ------------------------------------------------

    def wait_for_command(self) -> str | None:
        """Block until the wake word is heard and a command is transcribed.

        The input stream is recycled after every mute→unmute, on device
        errors, and every few minutes of standby — a stale stream otherwise
        leaves Jarvis deaf with no error to show for it."""
        self.on_state("muted" if self.muted.is_set() else "idle")
        while not self._shutdown.is_set():
            try:
                audio = self._standby_capture()
            except _Reopen:
                continue
            except Exception:
                time.sleep(1.0)  # device hiccup — reopen and carry on
                continue
            if audio is None:
                return None  # shutdown, or mute cut the recording short
            return self._finish(audio)
        return None

    def _standby_capture(self) -> np.ndarray | None:
        """One stream lifetime: listen for the wake word, then record the
        command. Raises _Reopen when the stream should be recycled."""
        was_muted = self.muted.is_set()
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK
        ) as stream:
            self.wake_model.reset()
            opened = time.monotonic()
            while not self._shutdown.is_set():
                frames, _ = stream.read(CHUNK)
                mono = frames[:, 0]
                if self.muted.is_set():
                    was_muted = True
                    self.on_level(0.0)
                    continue
                if was_muted:
                    raise _Reopen  # just unmuted — fresh stream, fresh model state
                if time.monotonic() - opened > STREAM_REFRESH_S:
                    raise _Reopen  # periodic refresh; revives wedged devices
                self.on_level(self._rms(mono) / 3000)
                scores = self.wake_model.predict(mono)
                if scores.get("hey_jarvis", 0) >= WAKE_THRESHOLD:
                    return self._record_until_silence(stream, preroll=[])
        return None

    def wake_interrupt_monitor(self, stop_event: threading.Event) -> bool:
        """Barge-in watcher: run wake-word detection until stop_event is set.

        Runs on its own input stream while Jarvis is speaking. Returns True
        the instant "hey jarvis" is heard (caller cuts the speech), False if
        the speech finished first.
        """
        self.wake_model.reset()
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK
            ) as stream:
                while not stop_event.is_set() and not self._shutdown.is_set():
                    frames, _ = stream.read(CHUNK)
                    if self.muted.is_set():
                        continue
                    scores = self.wake_model.predict(frames[:, 0])
                    if scores.get("hey_jarvis", 0) >= WAKE_THRESHOLD:
                        return True
        except Exception:
            return False  # no mic contention drama — barge-in just goes dark
        return False

    def record_command(self) -> str | None:
        """Record a command right now — the wake word was already heard
        (barge-in). Chimes first so the user knows Jarvis is listening."""
        if self.muted.is_set() or self._shutdown.is_set():
            return None
        _chime(988, 90)
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK
        ) as stream:
            audio = self._record_until_silence(stream, preroll=[])
        return self._finish(audio)

    def wait_for_followup(self, window_seconds: float = 3.0) -> str | None:
        """After Jarvis speaks: listen briefly for a reply, no wake word needed.

        Returns a transcript if the user starts talking within the window,
        else None (caller drops back to wake-word mode).
        """
        if self.muted.is_set() or self._shutdown.is_set():
            return None
        self.on_state("listening")

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK
        ) as stream:
            preroll: list[np.ndarray] = []
            voiced_run = 0
            audio = None
            for _ in range(int(window_seconds * SAMPLE_RATE / CHUNK)):
                if self._shutdown.is_set() or self.muted.is_set():
                    return None
                frames, _ = stream.read(CHUNK)
                mono = frames[:, 0]
                preroll.append(mono.copy())
                if len(preroll) > 8:  # keep ~0.6s so the first word isn't clipped
                    preroll.pop(0)
                rms = self._rms(mono)
                self.on_level(rms / 3000)
                # two consecutive voiced chunks = real speech, not a pop/echo
                voiced_run = voiced_run + 1 if rms >= FOLLOWUP_SPEECH_RMS else 0
                if voiced_run >= 2:
                    audio = self._record_until_silence(stream, preroll=preroll)
                    break
        if audio is None:
            self.on_level(0.0)
            return None
        return self._finish(audio)
