"""J.A.R.V.I.S. voice output: Edge-TTS synthesis + sounddevice playback.

Free Microsoft neural voice. Playback reports a live amplitude level via
callback so the UI's neural net can spike in sync with actual speech.
"""

from __future__ import annotations

import asyncio
import io
import re
import threading
from typing import Callable

import av
import edge_tts
import numpy as np
import sounddevice as sd

from ..config import VOICE, VOICE_PITCH, VOICE_RATE

_BLOCK = 2048  # frames per playback block (~85ms at 24kHz)


def _speechify(text: str) -> str:
    """Make model output sound natural when read aloud: drop markdown
    artifacts, don't read URLs or email addresses character by character."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # code blocks
    text = re.sub(r"[*_#`]+", "", text)  # md emphasis/headers
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)  # bullets
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"\b([a-zA-Z0-9._%+-]+)@[a-zA-Z0-9.-]+\.[a-z]{2,}\b", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def synthesize(text: str) -> tuple[np.ndarray, int]:
    """Return (float32 mono samples, sample_rate) for the given text."""

    async def _collect() -> bytes:
        communicate = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    mp3_bytes = asyncio.run(_collect())
    if not mp3_bytes:
        raise RuntimeError("Edge-TTS returned no audio")

    container = av.open(io.BytesIO(mp3_bytes))
    audio_stream = container.streams.audio[0]
    sample_rate = audio_stream.rate
    frames = []
    for frame in container.decode(audio_stream):
        arr = frame.to_ndarray()  # (channels, samples)
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32) / np.iinfo(arr.dtype).max
        frames.append(arr.mean(axis=0) if arr.ndim > 1 else arr)
    container.close()
    samples = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
    return samples, sample_rate


class Speaker:
    """Plays synthesized speech; interruptible; reports amplitude while talking."""

    def __init__(
        self,
        on_level: Callable[[float], None] | None = None,
        on_state: Callable[[bool], None] | None = None,
    ):
        self.on_level = on_level or (lambda level: None)
        self.on_state = on_state or (lambda speaking: None)
        self._stop = threading.Event()
        self.is_speaking = False

    def speak(self, text: str) -> None:
        """Blocking: synthesize and play. Call stop() from another thread to cut off."""
        text = _speechify(text)
        if not text:
            return
        samples, rate = synthesize(text)
        self._stop.clear()
        self.is_speaking = True
        self.on_state(True)
        try:
            with sd.OutputStream(
                samplerate=rate, channels=1, dtype="float32", blocksize=_BLOCK
            ) as stream:
                for start in range(0, len(samples), _BLOCK):
                    if self._stop.is_set():
                        break
                    block = samples[start : start + _BLOCK]
                    # RMS of this block -> UI spike intensity (0..~1)
                    self.on_level(float(np.sqrt(np.mean(block**2))) * 4)
                    stream.write(block.reshape(-1, 1))
        finally:
            self.is_speaking = False
            self.on_level(0.0)
            self.on_state(False)

    def stop(self) -> None:
        self._stop.set()
