# J.A.R.V.I.S.

A personal AI assistant inspired by Iron Man's J.A.R.V.I.S. — built on the Claude API.

## Roadmap

1. **Text brain** (current) — Claude with tool calling, chat REPL
2. Voice loop — wake word ("Jarvis") + Whisper STT + TTS, with mute hotkey
3. Email — Gmail summaries and deep dives
4. Smart home — Home Assistant (Tuya air purifier, GU10 smart bulbs, LED strips)
5. PC control expansion, TV control
6. Proactive briefings, persistent memory, personality tuning

## Setup

```powershell
pip install -r requirements.txt
```

## Backends

Set `BACKEND` in `jarvis/config.py`:

- `"claude-code"` (default) — **free**: routes through your Claude Code subscription via
  headless `claude -p`. Requires the Claude Code CLI on PATH. Tools are exposed to it
  through a local MCP server (`mcp-config.json`, port 8765).
- `"api"` — direct Anthropic API with streaming. Requires `ANTHROPIC_API_KEY` (pay-per-use).

## Run

**Voice mode (the real thing):**

```powershell
python -m jarvis.voice_main
```

Say **"Hey Jarvis"**, wait for the chime after you finish talking, and he answers
out loud. The window shows a neural net that spikes while he speaks, a live mic
meter, and a mute button (spacebar also toggles mute — it's a hard gate: muted
frames are discarded before any processing).

**Interrupting:** Jarvis keeps listening for the wake word the whole time he's
working. Say "Hey Jarvis" **while he's thinking** and the pending answer is
abandoned — whatever you add ("actually I meant tomorrow") gets folded into
your original question and answered as one. Say it **while he's talking** and
he shuts up, chimes, and takes your new command.

**Transcript:** press **H** (or the LOG button) to open a scrollable panel with
the conversation history. Everything is also saved to `history.jsonl` at the
project root, so the log survives restarts and includes text-mode chats too.
Entries older than `HISTORY_RETENTION_DAYS` (config, default 30) are pruned at
startup.

**Restarting:** the RESTART button (click twice — first click arms it) relaunches
Jarvis in a fresh process. Saying "Jarvis, restart yourself" does the same via
the `restart_jarvis` tool — he signs off and reboots a few seconds later.
Conversation memory resets; the transcript file survives.

**Mic reliability:** the standby stream is recycled after every unmute, on any
device error, and every 5 minutes — Windows audio streams can wedge silently
(device switches, driver naps) and would otherwise leave Jarvis deaf. Mic
errors retry instead of killing the pipeline.

Say **"stand down"** (or "goodnight Jarvis", "Jarvis, power down") and he signs
off with a farewell and closes. Wake-up and farewell lines are varied — picked
from pools keyed to the time of day, the weekday, and the live weather.

The HUD also shows:

- **Clock + date** (top right), with **local weather** beneath it — current
  conditions, temp, high/low, rain chance. Location is guessed from your IP
  (ip-api.com) and the forecast comes from Open-Meteo. Both free, no keys.
  Units: `TEMP_UNIT` in `jarvis/info.py`.
- **Inbox** (bottom left) — your 5 latest Gmail messages, refreshed every
  3 minutes. Needs a one-time setup, see below.
- **CPU / RAM** (bottom right).

### Linking Gmail (optional, free)

1. Turn on 2-Step Verification for your Google account if it isn't already.
2. Go to <https://myaccount.google.com/apppasswords> and create an app password
   (name it "jarvis").
3. Put the credentials in `secrets.json` at the project root:

```json
{
  "gmail_user": "you@gmail.com",
  "gmail_app_password": "abcd efgh ijkl mnop"
}
```

(Or set `JARVIS_GMAIL_USER` / `JARVIS_GMAIL_APP_PASSWORD` env vars instead.)
Until then the panel just shows "GMAIL NOT LINKED".

Once linked, Jarvis can also **work with your mail by voice**: "any new emails?",
"summarize the one from Amazon", "search my email for the dentist appointment",
"what does that invoice say?" — he lists, searches (full Gmail search syntax),
and reads message bodies. Everything is fetched read-only; nothing gets marked
as read.

### Linking Spotify (optional, free)

Jarvis can already *open* Spotify without any setup, and "play something on
Spotify" will open the search page for you to click. For true voice-controlled
playback ("Jarvis, play Back in Black"):

1. Go to <https://developer.spotify.com/dashboard> (free account, no Premium
   needed) and create an app — any name, redirect URI can be
   `http://localhost/`.
2. Copy the **Client ID** and **Client Secret** into `secrets.json`:

```json
{
  "spotify_client_id": "...",
  "spotify_client_secret": "..."
}
```

(Or set `JARVIS_SPOTIFY_CLIENT_ID` / `JARVIS_SPOTIFY_CLIENT_SECRET` env vars.)
Jarvis then searches Spotify's catalog, opens the desktop app on the match,
and taps the media play key — playback happens in your normal Spotify app, so
a free Spotify account works fine.

**Text mode:**

```powershell
python -m jarvis.main
```

Commands: type anything; `reset` clears conversation memory; `exit` quits.

## Voice stack (all free, all local except the brain)

| Piece | Tech |
|---|---|
| Wake word | openWakeWord `hey_jarvis` (onnx, local) |
| Speech-to-text | faster-whisper `base.en` (local, CPU) |
| Voice | Edge-TTS (free MS neural voices) |
| UI | pywebview + canvas (neural net, mic meter, mute) |

The fallback voice is set in `jarvis/config.py` (`VOICE`, plus rate/pitch) —
any Edge-TTS voice name works (`edge-tts --list-voices` shows them all).
`voice_samples/` holds the files that matter: `jarvis.mp3` (the clone's
reference — **required**, the server rebuilds the voice from it at startup),
`jarvis-boot.wav` (boot sound), and `clone-sample.wav` (a demo of the cloned
voice).

### The movie voice (local clone, free)

Jarvis's primary voice is a local **XTTS-v2 voice clone** of the film
J.A.R.V.I.S., using `voice_samples/jarvis.mp3` (~25s of clean reference audio)
as the timbre. It runs entirely on your GPU — no API, no cost.

- **Setup** (one-time): the clone stack needs its own Python (3.13) because
  the main app runs 3.14:

  ```powershell
  py -V:3.13 -m venv .venv-tts
  .venv-tts\Scripts\python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
  .venv-tts\Scripts\python -m pip install coqui-tts "transformers<5"
  ```

  (`transformers` must stay below 5 — coqui-tts imports symbols that
  transformers 5.x removed.)

  First launch downloads the XTTS-v2 weights (~1.8 GB) automatically.
- **How it runs**: `voice_main` auto-starts `voiceclone_server.py` inside
  `.venv-tts` (port 8766). The server binds its port only once the model is
  loaded, and it survives Jarvis restarts so the model loads once. Log:
  `clone_server.log`.
- **Fallback**: until the server is ready — and any time a synth fails — the
  Edge-TTS voice (`VOICE` in config) speaks instead. Jarvis never goes silent
  waiting on the clone stack. Expect the first minute or two after boot to be
  the fallback voice while XTTS warms up.
- **Startup sound**: boot plays the trimmed movie line
  (`voice_samples/jarvis-boot.wav` — *"...importing all preferences from home
  interface. Systems are now fully operational."*) instead of a spoken wake
  line. `STARTUP_CLIP` in config; set it to `""` for the spoken line.
- **Gaming note**: idle, XTTS holds ~2 GB of VRAM for instant replies — but
  **game mode automatically frees it**: the model parks in system RAM and
  borrows the GPU only for the ~3 seconds it's actually speaking (measured:
  ~1.95 GB freed, replies go from 2.7s to 3.5s). GTA gets the whole card. If
  you ever want the clone fully off, set `VOICE_CLONE = False` in config.
- **Global mute hotkey**: press **Pause** to toggle mute from anywhere — game
  focused, window minimized, doesn't matter (`MUTE_HOTKEY` in config; any
  `keyboard`-library key name or combo works, e.g. `"f9"`, `"ctrl+m"`. A
  laptop Fn key usually can't — it's handled in keyboard firmware and never
  reaches Windows; the config comment shows how to test yours).
- The reference clip is a movie sound bite cloned for **personal use on this
  PC only** — don't redistribute generated audio of it.

## Current tools

| Tool | What it does |
|---|---|
| `get_current_time` | Date and time |
| `get_system_stats` | CPU / RAM / disk / battery |
| `set_volume` | Volume up/down/mute/set (media keys) |
| `media_control` | Play/pause, next, previous, stop (media keys) |
| `play_music` | Play a song / album / artist / playlist on Spotify |
| `search_web` | Open Google or YouTube search results in the browser |
| `open_website` | Open any URL in the browser |
| `check_email` | List latest Gmail messages (with unread flags) |
| `search_email` | Search Gmail (full Gmail search syntax) |
| `read_email` | Read one email's full contents |
| `open_email` | Open a specific email in Gmail in the browser |
| `launch_app` | Open apps by name (chrome, spotify, vscode, ...) |
| `set_timer` | Countdown timer with beep |
| `restart_jarvis` | Jarvis relaunches himself in a fresh process |
| `lock_pc` | Lock the workstation |
| `power_control` | Sleep / shutdown / restart (asks for confirmation) |
| `game_mode` | Gaming performance mode: Jarvis drops to background CPU priority and throttles UI updates |
| `get_active_window` | What app/game is in the foreground right now |
| `capture_screen` | Screenshot the screen so Jarvis can *see* it and answer "what am I looking at?" |
| `review_recent_screen` | Replay Jarvis's short-term visual memory — the last ~3 minutes of screen, as timestamped frames |
| `watch_screen` | Proactive watch: Jarvis speaks up on his own when the scene changes (cards dealt, result screens) |
| `save_game_notes` | Persistent per-game notes: grind routines, goals, unlocks |
| `read_game_notes` | Read back a game's saved notes/routines |

### Gaming copilot

Say "Jarvis, game mode" (or just tell him you're hopping on GTA) and he gets out
of the game's way: below-normal CPU priority, minimal UI animation, and Whisper
capped at `WHISPER_THREADS` cores (config). While you play he can answer
questions about the game, check this week's bonuses on the web, look at your
screen when you ask what you're seeing, and build a daily grind routine from
what you tell him — saved per game in `game_data/` so it survives restarts.

**Rolling replay (short-term visual memory):** while game mode is on, the
screen is recorded into a RAM ring buffer — one frame every ~1.2s, the last
3 minutes kept (~10 MB, negligible CPU; tune in `jarvis/config.py` under
*Screen replay & watch*). Ask "Jarvis, what just happened?" or "what did the
dealer have?" and he reviews the recent frames like he watched it live.
Nothing is written to disk; the buffer clears when replay stops.

**Watch mode (live call-outs):** say "Jarvis, watch the table and call my
blackjack plays." He then monitors the replay for the scene changing and
settling — cards being dealt — and automatically looks and speaks the play
("Fourteen against a dealer six — stand, sir") without being asked. Call-outs
run through a fast one-shot Haiku turn (`WATCH_MODEL` in config; still your
Claude Code subscription, no API key) and take roughly 10–20 seconds from deal
to speech — fine for casino blackjack's pace, not for split-second prompts.
Built for mostly-static scenes (card tables, menus); it would trigger
constantly during driving. Watch mode ends with game mode or on "stop
watching".

Screen capture note: games in *exclusive fullscreen* can screenshot as a black
frame — use borderless windowed (the default in most modern titles).

He also waits for `PAUSE_SECONDS` (default 3s) of silence before deciding
you're done talking, so mid-sentence pauses don't cut you off.

On the `claude-code` backend Jarvis can also **search the web himself**
(Claude Code's built-in WebSearch/WebFetch) to answer questions with live
information — distinct from `search_web`, which opens the browser for you.
