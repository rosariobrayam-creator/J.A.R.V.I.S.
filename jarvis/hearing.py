"""Game-audio hearing: rolling loopback capture + on-demand transcription.

The ears' twin to replay.py's eyes: while game mode is on, whatever the PC
plays through the speakers — game dialogue, mission briefings, sirens, radio
callouts — is captured via WASAPI loopback into a RAM ring buffer holding
the last HEARING_SECONDS. The hear_game_audio tool transcribes a slice of it
with a local Whisper so the brain can answer "what did they just say?".

Loopback hears everything the speakers play, Jarvis's own voice and music
included — transcripts say so, and the model is told to sort it out.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

from . import game
from .config import HEARING_SECONDS, WHISPER_THREADS

SAMPLE_RATE = 16000
_BLOCK = 4000  # 0.25 s per capture block
_STREAM_REFRESH_S = 300  # reopen periodically so a default-device switch
#                          (headset plugged in mid-session) gets picked up
_SILENCE_RMS = 0.0015  # float32 RMS below which a slice counts as silence
_WHISPER_MODEL = "base.en"  # same family as the mic listener; see note below

_lock = threading.Lock()
_frames: deque[tuple[float, np.ndarray]] = deque(
    maxlen=max(2, round(HEARING_SECONDS * SAMPLE_RATE / _BLOCK))
)
_running = threading.Event()
_thread: threading.Thread | None = None

# Own Whisper instance rather than sharing the mic listener's: a game-audio
# transcription must never queue behind (or delay) the latency-critical
# transcription of the user's own voice. base.en int8 is ~75 MB of RAM.
_whisper = None
_whisper_lock = threading.Lock()


def hearing_running() -> bool:
    return _running.is_set()


def start_hearing() -> None:
    global _thread
    if _running.is_set():
        return
    old = _thread
    if old is not None and old.is_alive():
        old.join(timeout=2.0)
    with _lock:
        if _running.is_set():
            return
        _running.set()
        _thread = threading.Thread(
            target=_capture_loop, daemon=True, name="jarvis-hearing"
        )
        _thread.start()


def stop_hearing() -> None:
    with _lock:
        _running.clear()
        _frames.clear()


def _capture_loop() -> None:
    # Imported here, not at module top: the first import initializes COM on
    # the importing thread (this one), and a missing package degrades to the
    # tool reporting hearing as unavailable instead of killing startup.
    import soundcard as sc

    while _running.is_set():
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(str(speaker.name), include_loopback=True)
            opened = time.monotonic()
            with mic.recorder(samplerate=SAMPLE_RATE, blocksize=_BLOCK) as rec:
                while _running.is_set():
                    if time.monotonic() - opened > _STREAM_REFRESH_S:
                        break  # reopen against the current default device
                    data = rec.record(numframes=_BLOCK)
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    with _lock:
                        _frames.append(
                            (time.time(), mono.astype(np.float32, copy=False))
                        )
        except Exception:
            time.sleep(2.0)  # no speakers / device vanished — retry quietly


def _transcribe(audio: np.ndarray) -> str:
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel

            _whisper = WhisperModel(
                _WHISPER_MODEL,
                device="cpu",
                compute_type="int8",
                cpu_threads=WHISPER_THREADS,
            )
        # vad_filter skips music and effects so only actual speech is decoded
        segments, _info = _whisper.transcribe(
            audio, language="en", vad_filter=True
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


def hear_recent(seconds: float = 20.0) -> str:
    """Transcribe the last `seconds` of the PC's audio output."""
    try:
        seconds = max(5.0, min(float(seconds), float(HEARING_SECONDS)))
    except (TypeError, ValueError):
        seconds = 20.0
    if not _running.is_set():
        start_hearing()
        return (
            "Game-audio capture wasn't running (it starts with game mode), so "
            "there's no recording of what already played. Listening now — "
            f"from here on the last {HEARING_SECONDS} seconds of speaker "
            "audio can be transcribed."
        )
    now = time.time()
    with _lock:
        have_any = bool(_frames)
        window = [f for ts, f in _frames if now - ts <= seconds]
    if not window:
        if have_any:
            return (
                f"Nothing has come through the speakers in the last "
                f"{round(seconds)} seconds."
            )
        return (
            "The audio capture just started and hasn't recorded anything "
            "yet — try again in a few seconds."
        )
    audio = np.concatenate(window)
    if float(np.sqrt(np.mean(audio**2))) < _SILENCE_RMS:
        return (
            f"The last {round(seconds)} seconds of game audio were "
            "essentially silent."
        )
    text = _transcribe(audio)
    if not text:
        return (
            f"No intelligible speech in the last {round(seconds)} seconds of "
            "game audio — sound was playing, but no words (music or effects)."
        )
    return (
        f"Transcript of the last {round(seconds)} seconds of the PC's audio "
        "output (everything the speakers played — game dialogue, plus "
        "possibly music lyrics or Jarvis's own voice; ignore anything that "
        f"is clearly not the game): {text}"
    )


# Hearing follows game mode the way the screen replay does: ears on when the
# session starts, off (and buffer dropped) when it ends.
def _on_game_mode(on: bool) -> None:
    if on:
        start_hearing()
    else:
        stop_hearing()


game.on_game_mode(_on_game_mode)
