"""Local voice-clone TTS server for J.A.R.V.I.S.

Speaks in the voice of the reference clip (voice_samples/jarvis.mp3) using
XTTS-v2 on the GPU. Runs under the dedicated .venv-tts environment — the
cloning stack needs its own Python and a few GB of dependencies, so it stays
out of the main app entirely. The main app's Speaker POSTs text here and
falls back to Edge-TTS whenever this server isn't up.

The HTTP socket is only bound AFTER the model is loaded and warmed, so the
main app can treat "connection refused" as "clone voice not available yet"
and fall back instantly with no special cases.

Start manually for testing:
    .venv-tts\\Scripts\\python.exe voiceclone_server.py

voice_main starts (and reuses) it automatically when VOICE_CLONE is on; it
keeps running across Jarvis restarts so the model only loads once.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("COQUI_TOS_AGREED", "1")  # XTTS license: non-commercial use


def _ensure_ffmpeg_on_path() -> None:
    """torchcodec (torch's audio IO) needs FFmpeg's shared DLLs. The winget
    'Gyan.FFmpeg.Shared' install puts them under WinGet\\Packages, which isn't
    always on the PATH this process inherits — find and prepend them."""
    import glob

    for pattern in (
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg.Shared*\*\bin",
        r"%PROGRAMFILES%\ffmpeg*\bin",
    ):
        for hit in glob.glob(os.path.expandvars(pattern)):
            if glob.glob(os.path.join(hit, "avcodec-*.dll")):
                os.environ["PATH"] = hit + os.pathsep + os.environ["PATH"]
                return


_ensure_ffmpeg_on_path()

ROOT = Path(__file__).resolve().parent
REFERENCE = ROOT / "voice_samples" / "jarvis.mp3"
HOST, PORT = "127.0.0.1", 8766
SAMPLE_RATE = 24000  # XTTS output rate
CHUNK_CHARS = 240  # XTTS quality degrades past ~250 chars per generation

MODEL = None
GPT_LATENT = None
SPEAKER_EMB = None
LOCK = None  # one generation at a time; the GPU is shared with the game
GPU = False
# "resident": model lives on the GPU, fastest (~2 GB VRAM held at all times).
# "on_demand": model parks in system RAM and hops onto the GPU for a synth and
# stays there until IDLE_UNLOAD seconds of quiet — for gaming, where the VRAM
# belongs to the game. Jarvis switches this automatically with game mode.
#
# The hop is NOT free: moving ~2 GB across PCIe while a game owns the card cost
# 10-50s per reply in practice (measured: 6 chars -> 10.7s, 9 chars -> 54.8s).
# So on_demand never unloads *between the sentences of one reply* — it unloads
# only after the conversation goes quiet.
MODE = "resident"
IDLE_UNLOAD = 90.0  # seconds of quiet before on_demand hands the VRAM back
STREAM_CHUNK = 20  # GPT tokens per streamed audio chunk; lower = faster first
#                    audio, more per-chunk overhead. 20 ~= 0.4s to first sound.

_on_gpu = False  # where the weights actually are right now
_last_use = 0.0


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_model():
    global MODEL, GPT_LATENT, SPEAKER_EMB, LOCK, GPU, _on_gpu
    import threading

    import torch
    from TTS.api import TTS

    GPU = torch.cuda.is_available()
    device = "cuda" if GPU else "cpu"
    _log(f"loading XTTS-v2 on {device} (first run downloads ~1.8 GB)...")
    t0 = time.monotonic()
    api = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)
    MODEL = api.synthesizer.tts_model
    _on_gpu = GPU
    _log(f"model loaded in {time.monotonic() - t0:.0f}s; computing voice latents "
         f"from {REFERENCE.name}...")
    GPT_LATENT, SPEAKER_EMB = MODEL.get_conditioning_latents(
        audio_path=[str(REFERENCE)]
    )
    LOCK = threading.Lock()
    # Warm the kernels so the first real request isn't the slow one. Warming the
    # streaming path too: it compiles/caches separately from inference().
    _synthesize("Systems online.")
    for _ in _synthesize_stream("Systems online."):
        pass
    threading.Thread(target=_idle_watch, daemon=True, name="vram-idle").start()
    _log("ready")


def _ensure_on_gpu() -> None:
    """Bring the weights to the GPU if they're parked. Caller holds LOCK."""
    global _on_gpu
    if not GPU or _on_gpu:
        return
    t0 = time.monotonic()
    MODEL.to("cuda")
    _on_gpu = True
    _log(f"model -> gpu in {time.monotonic() - t0:.1f}s")


def _idle_watch() -> None:
    """Hand VRAM back once the user stops talking to Jarvis — never mid-reply."""
    global _on_gpu
    import torch

    while True:
        time.sleep(5.0)
        with LOCK:
            if (
                MODE == "on_demand"
                and _on_gpu
                and GPU
                and time.monotonic() - _last_use > IDLE_UNLOAD
            ):
                MODEL.to("cpu")
                torch.cuda.empty_cache()
                _on_gpu = False
                _log(f"idle {IDLE_UNLOAD:.0f}s -> vram released")


def _chunks(text: str) -> list[str]:
    """Sentence-ish chunks under the XTTS quality limit."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    cur = ""
    for s in sentences:
        while len(s) > CHUNK_CHARS:  # pathological run-on: hard split on a comma/space
            cut = s.rfind(",", 0, CHUNK_CHARS) + 1 or s.rfind(" ", 0, CHUNK_CHARS)
            cut = cut if cut > 0 else CHUNK_CHARS
            out.append((cur + " " + s[:cut]).strip())
            cur, s = "", s[cut:].strip()
        if len(cur) + len(s) + 1 > CHUNK_CHARS:
            out.append(cur.strip())
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return [c for c in out if c]


def _set_mode(mode: str) -> None:
    """Switch VRAM strategy. Caller holds LOCK."""
    global MODE, _on_gpu
    import torch

    MODE = mode
    if not GPU:
        return
    if mode == "on_demand":
        MODEL.to("cpu")
        torch.cuda.empty_cache()  # hand the VRAM back to the game right now
        _on_gpu = False
    else:
        MODEL.to("cuda")
        _on_gpu = True


def _pcm16(samples) -> bytes:
    """float32 mono -> little-endian PCM16 bytes."""
    import numpy as np

    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _gap(seconds: float = 0.12) -> bytes:
    import numpy as np

    return _pcm16(np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32))


def _synthesize_stream(text: str):
    """Yield PCM16 bytes in the cloned voice AS XTTS generates them.

    This is what gates time-to-first-word: the caller can start playing after
    the first chunk (~0.4s) instead of waiting out the whole sentence. Caller
    holds LOCK.
    """
    global _last_use
    import numpy as np
    import torch

    _ensure_on_gpu()
    device = "cuda" if GPU else "cpu"
    gpt = GPT_LATENT.to(device)
    emb = SPEAKER_EMB.to(device)
    try:
        with torch.inference_mode():
            for chunk in _chunks(text):
                for wav in MODEL.inference_stream(
                    chunk, "en", gpt, emb,
                    temperature=0.65,  # steadier delivery suits the persona
                    repetition_penalty=5.0,
                    stream_chunk_size=STREAM_CHUNK,
                    enable_text_splitting=False,  # _chunks already did it
                ):
                    yield _pcm16(
                        wav.detach().cpu().numpy().astype(np.float32).reshape(-1)
                    )
                yield _gap()
    finally:
        _last_use = time.monotonic()


def _synthesize(text: str) -> bytes:
    """Text -> mono 16-bit WAV bytes at 24 kHz, in the cloned voice."""
    global _last_use
    import numpy as np
    import torch

    _ensure_on_gpu()
    device = "cuda" if GPU else "cpu"
    gpt = GPT_LATENT.to(device)
    emb = SPEAKER_EMB.to(device)
    pieces = []
    try:
        with torch.inference_mode():
            for chunk in _chunks(text):
                out = MODEL.inference(
                    chunk, "en", gpt, emb,
                    temperature=0.65,  # steadier delivery suits the persona
                    repetition_penalty=5.0,
                )
                pieces.append(np.asarray(out["wav"], dtype=np.float32))
                pieces.append(np.zeros(int(SAMPLE_RATE * 0.12), dtype=np.float32))
    finally:
        _last_use = time.monotonic()
    samples = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(_pcm16(samples))
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet; we do our own logging
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/vram":
            try:
                length = int(self.headers.get("Content-Length", 0))
                mode = json.loads(self.rfile.read(length)).get("mode", "")
                if mode not in ("resident", "on_demand"):
                    raise ValueError(f"unknown mode '{mode}'")
                with LOCK:
                    _set_mode(mode)
                _log(f"vram mode -> {mode}")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode())
            return
        if self.path != "/tts":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            text = body.get("text", "").strip()
            if not text:
                raise ValueError("empty text")
            if body.get("stream"):
                self._stream_tts(text)
                return
            t0 = time.monotonic()
            with LOCK:
                wav = _synthesize(text)
            _log(f"synth {len(text)} chars in {time.monotonic() - t0:.1f}s")
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
        except Exception as e:
            _log(f"request failed: {e}")
            try:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
            except Exception:
                pass

    def _stream_tts(self, text: str) -> None:
        """Raw PCM16 out as it's generated, connection closed to mark the end.

        The client hanging up (user barged in over the reply) surfaces here as
        a broken pipe — that's the signal to stop generating and give the GPU
        straight back, so it's handled as a normal outcome, not an error.
        """
        t0 = time.monotonic()
        first = None
        sent = 0
        try:
            with LOCK:
                self.send_response(200)
                self.send_header("Content-Type", "audio/L16")
                self.send_header("X-Sample-Rate", str(SAMPLE_RATE))
                self.end_headers()
                for pcm in _synthesize_stream(text):
                    self.wfile.write(pcm)
                    self.wfile.flush()
                    sent += len(pcm)
                    if first is None:
                        first = time.monotonic() - t0
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            _log(f"stream {len(text)} chars: client hung up at {sent // 48000:.0f}s audio")
            return
        except Exception as e:
            _log(f"stream failed: {e}")
            return
        _log(
            f"stream {len(text)} chars: first audio {first or 0:.1f}s, "
            f"{sent / 48000:.1f}s audio in {time.monotonic() - t0:.1f}s"
        )


def main() -> None:
    if not REFERENCE.exists():
        _log(f"reference clip missing: {REFERENCE}")
        sys.exit(1)
    _load_model()
    # Bind only now — "port closed" therefore always means "not ready".
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
