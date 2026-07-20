"""J.A.R.V.I.S. voice output: cloned movie voice with Edge-TTS fallback.

Primary voice is the local XTTS-v2 clone server (voiceclone_server.py) —
the actual movie J.A.R.V.I.S. timbre, cloned from voice_samples/jarvis.mp3.
If the server isn't up (still loading, not installed, crashed) the free
Edge-TTS neural voice takes over for that reply, so speech never blocks on
the clone stack. Game mode hands every reply to Edge-TTS deliberately, because
the clone needs VRAM the game is using — see _use_clone. Playback reports a
live amplitude level via callback so the UI's neural net can spike in sync
with actual speech.
"""

from __future__ import annotations

import asyncio
import io
import json
import queue
import re
import threading
import urllib.request
from typing import Callable, Iterable, Iterator

import av
import edge_tts
import numpy as np
import sounddevice as sd

from .. import game
from ..config import (
    CLONE_GAME_VRAM,
    GAME_VOICE_EDGE,
    VOICE,
    VOICE_CLONE,
    VOICE_CLONE_STREAM,
    VOICE_CLONE_TIMEOUT,
    VOICE_CLONE_URL,
    VOICE_PITCH,
    VOICE_RATE,
)

_BLOCK = 2048  # frames per playback block (~85ms at 24kHz)
_MAX_CHUNK = 240  # batch ceiling per synth request (matches the clone server's
#                   per-generation quality limit)
_LOOKAHEAD = 48  # audio chunks buffered ahead of playback. Chunks are ~0.4s of
#                  streamed audio, so this is ~20s of runway — enough that synth
#                  never starves playback, small enough to bound memory.
_CLONE_RATE = 24000  # XTTS output rate (the /tts stream is raw PCM16 at this)


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


def _sentence_stream(pieces: Iterable[str]) -> Iterator[str]:
    """Regroup an incoming stream of text fragments into synth-sized chunks.

    The first complete sentence is emitted alone and immediately — it gates
    time-to-first-audio, and a short sentence synthesizes fast. After that,
    whole sentences already in the buffer are batched up to _MAX_CHUNK per
    request for smoother prosody and less per-request overhead.
    """
    splitter = re.compile(r"(?<=[.!?…])\s+")
    buf = ""
    first = True
    for piece in pieces:
        if not piece:
            continue
        buf += piece
        while True:
            parts = splitter.split(buf)
            if len(parts) < 2:
                break
            *complete, buf = parts
            if first:
                yield complete[0]
                first = False
                buf = " ".join(complete[1:] + [buf])
                continue
            batch = ""
            taken = 0
            for s in complete:
                if batch and len(batch) + len(s) + 1 > _MAX_CHUNK:
                    break
                batch = f"{batch} {s}".strip()
                taken += 1
            buf = " ".join(complete[taken:] + [buf])
            yield batch
    if buf.strip():
        yield buf


def _decode_audio(source) -> tuple[np.ndarray, int]:
    """Decode any av-readable audio (bytes or file path) to float32 mono."""
    container = av.open(io.BytesIO(source) if isinstance(source, bytes) else source)
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


def _synthesize_edge(text: str) -> tuple[np.ndarray, int]:
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
    return _decode_audio(mp3_bytes)


def _synthesize_clone(text: str) -> tuple[np.ndarray, int]:
    req = urllib.request.Request(
        VOICE_CLONE_URL + "/tts",
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=VOICE_CLONE_TIMEOUT) as resp:
        wav_bytes = resp.read()
    if not wav_bytes:
        raise RuntimeError("clone server returned no audio")
    return _decode_audio(wav_bytes)


def _stream_clone(text: str) -> Iterator[tuple[np.ndarray, int]]:
    """Yield (samples, rate) as the clone server generates them.

    Raw PCM16 at _CLONE_RATE, no length header — the server closes the socket
    to mark the end. Raises before the first yield if the clone isn't
    available, so the caller can still fall back to Edge-TTS for this sentence;
    once audio has started, a mid-stream failure just ends the sentence (we're
    already speaking, and restarting it in a different voice would be worse).
    """
    req = urllib.request.Request(
        VOICE_CLONE_URL + "/tts",
        data=json.dumps({"text": text, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=VOICE_CLONE_TIMEOUT)
    rate = int(resp.headers.get("X-Sample-Rate", _CLONE_RATE))
    started = False
    try:
        while True:
            # Whole frames only; a split sample would click.
            raw = resp.read(_BLOCK * 2)
            if not raw:
                break
            if len(raw) % 2:
                raw += resp.read(1)
            started = True
            yield np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, rate
    finally:
        resp.close()
    if not started:
        raise RuntimeError("clone stream produced no audio")


def set_clone_vram_mode(game_mode: bool) -> None:
    """Tell the clone server how to treat the GPU as game mode flips.

    Out of game mode the model is always resident; in game mode it follows
    CLONE_GAME_VRAM, which parks it in system RAM so the game gets the ~2 GB
    back. That park is only free because GAME_VOICE_EDGE means the clone has
    nothing to say until gaming ends. Fire-and-forget: if the server isn't up,
    there's nothing to free anyway.
    """
    mode = CLONE_GAME_VRAM if game_mode else "resident"

    def _post() -> None:
        try:
            req = urllib.request.Request(
                VOICE_CLONE_URL + "/vram",
                data=json.dumps({"mode": mode}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass

    threading.Thread(target=_post, daemon=True, name="clone-vram-mode").start()


def _use_clone() -> bool:
    """Whether the cloned voice should carry this reply.

    Never while gaming: XTTS wants ~2 GB of VRAM the game already owns, and when
    it doesn't fit the driver spills the weights to system RAM instead of
    failing — measured at 5.2x slower than real time, which this module's
    play-on-arrival streaming turns into a word every couple of seconds. See
    GAME_VOICE_EDGE in config for the numbers.
    """
    return VOICE_CLONE and not (GAME_VOICE_EDGE and game.is_game_mode())


def synthesize(text: str) -> tuple[np.ndarray, int]:
    """Return (float32 mono samples, sample_rate) for the given text.

    Cloned voice first; a refused connection means the clone server isn't up
    (or still loading) and costs nothing, so falling back is effectively free.
    """
    if _use_clone():
        try:
            return _synthesize_clone(text)
        except Exception:
            pass  # any failure -> Edge-TTS carries the reply
    return _synthesize_edge(text)


# Keyed by (text, spoken-by-the-clone): an ack cached in one voice must never
# be replayed once the other voice is carrying the reply, or a turn opens in the
# movie voice and finishes in Edge-TTS.
_cache: dict[tuple[str, bool], tuple[np.ndarray, int]] = {}


def prewarm(lines: Iterable[str]) -> None:
    """Synthesize a fixed set of short lines now and keep the audio in memory.

    These are the instant acknowledgments ("One moment, sir.") spoken while the
    brain is still thinking. Synthesized live they were self-defeating: each one
    took the clone server's generation lock, so the real first sentence queued
    up behind the filler covering for it. Cached, they cost no GPU at all and
    play the instant they're picked. Runs in the background — an ack that isn't
    warm yet just synthesizes the old way.

    Only the current voice is warmed. Game mode's acks come from Edge-TTS, which
    has no lock to contend for and no GPU to wait on, so synthesizing those live
    costs nothing worth caching for.
    """

    def _fill() -> None:
        for line in lines:
            # Keyed by what the speaker will actually look up: _speechify runs
            # over every chunk on its way to synthesis.
            key = (_speechify(line), _use_clone())
            if not key[0] or key in _cache:
                continue
            try:
                _cache[key] = synthesize(key[0])
            except Exception:
                pass  # not worth a retry; it'll synthesize live if it's needed

    threading.Thread(target=_fill, daemon=True, name="tts-prewarm").start()


def synthesize_stream(text: str) -> Iterator[tuple[np.ndarray, int]]:
    """(samples, rate) for `text`, in playable pieces as early as possible.

    The clone's streaming path emits the first ~0.4s of audio while the rest of
    the sentence is still generating; everything else (clone without streaming,
    Edge-TTS) yields the finished clip as a single piece.
    """
    clone = _use_clone()
    cached = _cache.get((text, clone))
    if cached is not None:
        yield cached
        return
    if clone and VOICE_CLONE_STREAM:
        stream = _stream_clone(text)
        try:
            first = next(stream)
        except StopIteration:
            return
        except Exception:
            stream.close()  # clone unavailable — Edge-TTS takes this sentence
        else:
            yield first
            try:
                yield from stream
            except Exception:
                pass  # already speaking; ending the sentence beats restarting it
            return
    yield synthesize(text)


class Speaker:
    """Plays synthesized speech; interruptible; reports amplitude while talking."""

    def __init__(
        self,
        on_level: Callable[[float], None] | None = None,
        on_state: Callable[[bool], None] | None = None,
        on_first_audio: Callable[[], None] | None = None,
    ):
        self.on_level = on_level or (lambda level: None)
        self.on_state = on_state or (lambda speaking: None)
        # Fires once per utterance, when the first block actually hits the
        # speakers — on_state(True) is several seconds earlier, at the point we
        # start *trying* to speak, so it can't be used to measure latency.
        self.on_first_audio = on_first_audio or (lambda: None)
        self._stop = threading.Event()
        self.is_speaking = False

    def speak(self, text: str) -> None:
        """Blocking: synthesize and play. Call stop() from another thread to cut off."""
        self.speak_stream([text])

    def speak_stream(self, pieces: Iterable[str]) -> None:
        """Blocking: speak text as it arrives. `pieces` may be a live stream
        (e.g. model output); it's cut into sentences and synthesized a couple
        of sentences ahead of playback, so the first words are heard after the
        first sentence is ready rather than after the whole reply."""
        self._stop.clear()
        ready: queue.Queue = queue.Queue(maxsize=_LOOKAHEAD)

        def _produce() -> None:
            try:
                for chunk in _sentence_stream(pieces):
                    chunk = _speechify(chunk)
                    if not chunk:
                        continue
                    if self._stop.is_set():
                        return
                    try:
                        for audio in synthesize_stream(chunk):
                            if self._stop.is_set():
                                # Abandoning the generator closes the socket,
                                # which tells the server to stop generating for
                                # a reply nobody is listening to any more.
                                return
                            while not self._stop.is_set():
                                try:
                                    ready.put(audio, timeout=0.2)
                                    break
                                except queue.Full:
                                    pass
                    except Exception:
                        continue  # both engines failed on this sentence; skip it
            finally:
                while not self._stop.is_set():
                    try:
                        ready.put(None, timeout=0.2)  # end-of-speech sentinel
                        break
                    except queue.Full:
                        pass

        producer = threading.Thread(target=_produce, daemon=True, name="tts-synth")
        self.is_speaking = True
        self.on_state(True)
        producer.start()
        stream = None
        spoke = False
        try:
            while not self._stop.is_set():
                try:
                    item = ready.get(timeout=0.2)
                except queue.Empty:
                    continue  # synth still working on the next sentence
                if item is None:
                    break
                samples, rate = item
                if stream is None or stream.samplerate != rate:
                    if stream is not None:
                        stream.close()
                    stream = sd.OutputStream(
                        samplerate=rate, channels=1, dtype="float32", blocksize=_BLOCK
                    )
                    stream.start()
                if not spoke:
                    spoke = True
                    self.on_first_audio()
                self._play_into(stream, samples)
        finally:
            if stream is not None:
                stream.close()
            self.is_speaking = False
            self.on_level(0.0)
            self.on_state(False)

    def play_file(self, path: str) -> None:
        """Blocking: play an audio file (boot clip, sound effects) through the
        same interruptible pipeline as speech."""
        samples, rate = _decode_audio(path)
        self._stop.clear()
        self.is_speaking = True
        self.on_state(True)
        try:
            with sd.OutputStream(
                samplerate=rate, channels=1, dtype="float32", blocksize=_BLOCK
            ) as stream:
                self._play_into(stream, samples)
        finally:
            self.is_speaking = False
            self.on_level(0.0)
            self.on_state(False)

    def _play_into(self, stream, samples: np.ndarray) -> None:
        for start in range(0, len(samples), _BLOCK):
            if self._stop.is_set():
                break
            block = samples[start : start + _BLOCK]
            # RMS of this block -> UI spike intensity (0..~1)
            self.on_level(float(np.sqrt(np.mean(block**2))) * 4)
            stream.write(block.reshape(-1, 1))

    def stop(self) -> None:
        self._stop.set()
