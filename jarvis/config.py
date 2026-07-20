"""Central configuration for J.A.R.V.I.S."""

# "claude-code" = free, routes through your Claude Code subscription (no API key)
# "api"         = direct Anthropic API (needs ANTHROPIC_API_KEY, pay-per-use)
BACKEND = "claude-code"

MODEL = "claude-haiku-4-5"  # used by the "api" backend only

# Brain model for the "claude-code" backend. haiku starts speaking ~2s sooner
# and was the obvious pick for game mode until it was measured: haiku ignores
# the two-sentence limit in GAME_PROMPT and talks for 40s straight through the
# 25s window it was supposed to help with. sonnet obeys it — one sentence, no
# preamble — so it reaches the *answer* sooner in practice despite the slower
# start. Switch GAME_BRAIN_MODEL to "haiku" only if you want the extra margin
# and can live with the rambling.
BRAIN_MODEL = "sonnet"
GAME_BRAIN_MODEL = "sonnet"

# Extended thinking. Off, and this is the single biggest speed win there is:
# a traced "what's on my screen" turn spent 11.9 of its 17.5 seconds thinking.
# The Claude Code CLI inherits ~/.claude/settings.json, so the effortLevel you
# set for coding was silently applying to every spoken reply too. A voice
# assistant answering out loud in one or two sentences has nothing to reason
# about at that depth. Turn it on if you want deeper answers and can wait.
BRAIN_THINKING = False
MAX_TOKENS = 64000  # streaming, so a generous ceiling is safe
USER_NAME = "sir"  # how Jarvis addresses you; change to your name if preferred

# --- Voice ------------------------------------------------------------------
# Fallback voice only — the primary voice is the local clone below. Edge-TTS's
# "Multilingual" voices are a newer generation and sound far more human, but
# none of them are British. (Preview any voice with:
#   edge-tts --voice en-GB-RyanNeural --text "hello sir" --write-media t.mp3)
#   "en-US-AndrewMultilingualNeural"  most human, warm/confident (default)
#   "en-US-BrianMultilingualNeural"   most human, casual
#   "en-GB-RyanNeural"                British — the classic Jarvis accent
#   "en-GB-ThomasNeural"              British, a touch lighter
VOICE = "en-GB-ThomasNeural"
VOICE_RATE = "+4%"  # older-gen British voices sit best slightly brisk and low;
VOICE_PITCH = "-2Hz"  # for the multilingual voices use "+3%" / "+0Hz"

# --- Voice clone (the movie voice) -------------------------------------------
# When on, replies are synthesized by the local XTTS-v2 server
# (voiceclone_server.py in .venv-tts) cloning voice_samples/jarvis.mp3.
# Anything goes wrong — server not up yet, model still loading, GPU busy —
# and the Edge-TTS voice above takes over seamlessly for that reply.
VOICE_CLONE = True
VOICE_CLONE_URL = "http://127.0.0.1:8766"
VOICE_CLONE_PYTHON = ".venv-tts/Scripts/python.exe"  # relative to project root
VOICE_CLONE_TIMEOUT = 60.0  # max seconds to wait for one reply's audio
VOICE_CLONE_STREAM = True  # play each sentence as XTTS generates it (~0.4s to
#                            first word) instead of waiting for the whole clip

# Game mode speaks in the Edge-TTS voice above instead of the clone.
#
# The clone is a GPU model and a game owns the GPU. Measured on an RTX 3050 with
# GTA V Enhanced resident: the card was at 7088 of 8192 MB, XTTS's ~2 GB had
# nowhere to go, and the NVIDIA driver silently spilled the weights to system
# RAM over PCIe rather than failing — so synthesis ran at 5.2x SLOWER than real
# time (4.0s of speech took 21.0s to make). Since the player starts each ~0.4s
# chunk the moment it arrives, that lands as a word or two, then two seconds of
# silence, for the length of the reply.
#
# There is no VRAM strategy that wins this: "resident" is what causes the spill,
# and "on_demand" hops the PCIe bus for every sentence (measured at 10-50s per
# reply). The fix is to not ask a full GPU for inference at all. Edge-TTS is a
# cloud call that costs the card nothing, so replies stay snappy exactly when a
# mission timer is running. Set False to keep the movie voice while gaming and
# accept the stutter.
GAME_VOICE_EDGE = True

# How the clone treats the GPU while game mode is on:
#   "resident"  — model stays on the card (~2 GB VRAM held). Fast: a sentence
#                 starts speaking in well under a second.
#   "on_demand" — model parks in system RAM and only borrows the GPU to speak,
#                 releasing the VRAM after CLONE_IDLE_UNLOAD seconds of quiet.
# on_demand was previously the worst setting there was, because the model hopped
# the bus for EVERY sentence. With GAME_VOICE_EDGE on, the clone never speaks
# while gaming, so there is nothing left to hop for — the model just parks in
# system RAM and hands its ~2 GB straight back to the game, which is exactly
# what a card sitting at 7 of 8 GB needs. It hops back once, when gaming ends.
# Only meaningful if GAME_VOICE_EDGE is False.
CLONE_GAME_VRAM = "on_demand"
CLONE_IDLE_UNLOAD = 90.0  # on_demand only: seconds of quiet before VRAM is freed

STARTUP_CLIP = "voice_samples/jarvis-boot.wav"  # played at boot instead of the
#                       spoken wake line; set to "" to get the spoken line back

FOLLOWUP_SECONDS = 6.0  # how long Jarvis keeps listening after he replies
HISTORY_RETENTION_DAYS = 30  # transcript entries older than this are pruned at startup

# --- Screen replay & watch ---------------------------------------------------
# Rolling visual memory: while game mode (or watch mode) is on, the screen is
# recorded into a RAM ring buffer — Jarvis's screenshare. Every in-game look
# sends a short filmstrip from this buffer (see GLANCE_* below), so he sees
# motion, not one still. 720p at 5 fps: measured 36.9 ms per frame end-to-end
# (grab+resize+encode, with GTA running), so 5 fps costs ~18% of one core and
# true 30 fps would eat a whole core — video framerates buy nothing anyway,
# since the model reads discrete frames.
REPLAY_SECONDS = 180  # how far back the replay remembers. At 5 fps / 720p this
#                       is ~900 frames ≈ 65 MB of RAM, held only while recording
REPLAY_INTERVAL = 0.2  # seconds between frames (5 fps)
REPLAY_WIDTH = 1280  # 720p — sharp enough to read cards and HUD text
REPLAY_JPEG_QUALITY = 70  # ~72 KB/frame measured at 720p

# The filmstrip: while the replay is recording, every capture_screen call
# returns the live frame PLUS this many recent frames spanning this window, so
# "what's the play?" sees the cards being dealt, not just the aftermath.
# Each 720p frame costs ~1.2k tokens ≈ +0.5s of prefill; 6 frames ≈ +2-3s per
# look. Drop GLANCE_FRAMES to 4 if in-game replies start missing the 15s target.
GLANCE_SECONDS = 5.0
GLANCE_FRAMES = 6

# Live screenshot width. 1568 is the most the API will use. Game mode used to
# send 1024 to save ~1s of prefill, but that's where card misreads came from:
# a rank glyph is ~18px tall at native 1080p and ~10px after the downscale,
# and a fast wrong answer at the blackjack table is worth less than nothing.
VISION_WIDTH = 1568
GAME_VISION_WIDTH = 1568

# Watch mode: proactive call-outs. Three trigger paths: change-then-settle
# (cards dealt at the casino table), sudden spikes (a crash, a kill banner),
# and a heartbeat during sustained action that never settles (driving,
# firefights). Detection = fraction of thumbnail pixels changed.
WATCH_MODEL = "sonnet"  # haiku misread cards at the table (July 2026); sonnet
#                         costs a couple seconds more per call-out and reads them
WATCH_TRIGGER = 0.030  # scene counts as "changing" above this fraction
WATCH_SETTLE = 0.012  # ...and "settled" again below this one
WATCH_SPIKE = 0.25  # a sudden change this big fires immediately, no settle wait
WATCH_COOLDOWN = 6.0  # min seconds between call-outs
WATCH_HEARTBEAT = 20.0  # action ongoing this long with no look -> look anyway
# Each look lands in a resumed watcher session so Jarvis remembers what he
# already said (no repeats, callbacks, cached prefill). Every look adds
# ~10-15k image tokens of context, so the session resets after this many.
WATCH_SESSION_MAX_LOOKS = 8
# The filmstrip the watcher requests per look: denser than the default glance
# so fast action reads as motion (~2 fps effective across the window).
WATCH_GLANCE_SECONDS = 5.0
WATCH_GLANCE_FRAMES = 10

# --- Game audio hearing -------------------------------------------------------
# The ears' twin to the screen replay: while game mode is on, everything the PC
# plays through the speakers (game dialogue, mission briefings, radio callouts)
# is captured via WASAPI loopback into a RAM ring buffer, and hear_game_audio
# transcribes a slice on demand with a local Whisper. 16 kHz mono float32 is
# ~64 KB/s, so 90 s ≈ 5.5 MB of RAM, held only while capturing.
HEARING_SECONDS = 90  # how far back the game-audio memory reaches

# --- Global mute hotkey -------------------------------------------------------
# Toggles mute from ANYWHERE — game focused, Jarvis minimized, doesn't matter.
# Keys joined with "+" must all be held at once: "m+n", "ctrl+m", or a single
# key like "pause" or "f9" (see jarvis/hotkey.py for the recognized names).
# Detection polls key state directly, so it keeps working inside games where
# ordinary keyboard hooks go silent. Set "" to disable.
# NOTE: a laptop Fn key can't work here — it's handled inside the keyboard's
# own firmware and invisible to Windows.
MUTE_HOTKEY = "m+n"

# --- Listening --------------------------------------------------------------
PAUSE_SECONDS = 3.0  # silence before Jarvis decides you're done talking;
#                      higher = more patient with mid-sentence pauses
# This briefly sat at 2.0 to shave a second off game-mode turns, and it did
# exactly what the old warning here said it would: Jarvis started cutting the
# user off mid-sentence (reported July 18 2026). Patience beats speed here —
# don't lower this again without asking.
GAME_PAUSE_SECONDS = 3.0
MAX_COMMAND_SECONDS = 30.0  # hard cap on one utterance
WHISPER_THREADS = 2  # CPU threads for local transcription; low keeps games
#                      smooth and base.en int8 is still quick on two cores

# --- Latency ------------------------------------------------------------------
# Log a per-turn breakdown (heard -> transcribed -> first token -> first word)
# to the console. This is how you check the 15s target is actually being met;
# costs nothing, so it stays on.
TIMING_LOG = True
