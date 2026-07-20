"""Tool definitions and implementations for J.A.R.V.I.S. Phase 1.

Each tool is a plain function plus a JSON-schema definition. The brain looks
tools up in TOOL_FUNCTIONS by name and sends TOOL_SCHEMAS to the API.
"""

from __future__ import annotations

import ctypes
import datetime
import difflib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import psutil

from . import focuswatch, game, hearing, replay
from .mail import check_email, open_email, read_email, search_email
from .spotify import play_music

# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


def get_current_time() -> str:
    now = datetime.datetime.now()
    return now.strftime("%A, %B %d %Y, %I:%M %p")


def get_system_stats() -> str:
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")
    batt = psutil.sensors_battery()
    parts = [
        f"CPU: {cpu:.0f}%",
        f"RAM: {mem.percent:.0f}% used ({mem.used / 1e9:.1f} of {mem.total / 1e9:.1f} GB)",
        f"Disk C: {disk.percent:.0f}% used ({disk.free / 1e9:.0f} GB free)",
    ]
    if batt is not None:
        plugged = "plugged in" if batt.power_plugged else "on battery"
        parts.append(f"Battery: {batt.percent:.0f}% ({plugged})")
    return " | ".join(parts)


# Windows virtual-key codes for media keys
_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_VK_MEDIA_NEXT = 0xB0
_VK_MEDIA_PREV = 0xB1
_VK_MEDIA_STOP = 0xB2
_VK_MEDIA_PLAY_PAUSE = 0xB3
_KEYEVENTF_KEYUP = 0x0002


def _press_key(vk: int, times: int = 1) -> None:
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def set_volume(action: str, level: int | None = None) -> str:
    """Volume control via media keys. Each up/down press moves volume by 2%."""
    if action == "mute":
        _press_key(_VK_VOLUME_MUTE)
        return "Toggled mute."
    if action == "up":
        _press_key(_VK_VOLUME_UP, 5)  # +10%
        return "Volume raised by about 10%."
    if action == "down":
        _press_key(_VK_VOLUME_DOWN, 5)  # -10%
        return "Volume lowered by about 10%."
    if action == "set":
        if level is None:
            return "Error: 'set' requires a level between 0 and 100."
        level = max(0, min(100, level))
        _press_key(_VK_VOLUME_DOWN, 50)  # drive to 0
        _press_key(_VK_VOLUME_UP, round(level / 2))
        return f"Volume set to approximately {level}%."
    return f"Error: unknown action '{action}'."


def media_control(action: str) -> str:
    """Media transport via media keys — drives whatever player has the session."""
    keys = {
        "play": _VK_MEDIA_PLAY_PAUSE,
        "pause": _VK_MEDIA_PLAY_PAUSE,
        "next": _VK_MEDIA_NEXT,
        "previous": _VK_MEDIA_PREV,
        "stop": _VK_MEDIA_STOP,
    }
    vk = keys.get(action)
    if vk is None:
        return f"Error: unknown action '{action}'."
    _press_key(vk)
    return {
        "play": "Toggled playback.",
        "pause": "Toggled playback.",
        "next": "Skipped to the next track.",
        "previous": "Went back a track.",
        "stop": "Stopped playback.",
    }[action]


def open_website(url: str) -> str:
    url = url.strip()
    if not url:
        return "Error: empty URL."
    if "://" not in url:
        url = "https://" + url
    if not url.startswith(("http://", "https://")):
        return f"Error: refusing to open non-web URL '{url}'."
    webbrowser.open(url)
    return f"Opened {url} in the browser."


def search_web(query: str, site: str = "google") -> str:
    """Open browser search results — Google or YouTube."""
    q = urllib.parse.quote_plus(query.strip())
    if site == "youtube":
        webbrowser.open(f"https://www.youtube.com/results?search_query={q}")
        return f"Opened YouTube results for '{query}'."
    webbrowser.open(f"https://www.google.com/search?q={q}")
    return f"Opened Google results for '{query}'."


# Friendly names -> commands. os.startfile handles anything on PATH or a full path;
# these cover common apps whose executable name differs from what people say.
_APP_ALIASES = {
    "chrome": "chrome",
    "browser": "chrome",
    "edge": "msedge",
    "firefox": "firefox",
    "notepad": "notepad",
    "calculator": "calc",
    "explorer": "explorer",
    "files": "explorer",
    "spotify": "spotify",
    "discord": "discord",
    "steam": "steam",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "vstudio": "visual studio code",
    "code": "visual studio code",
    "terminal": "wt",
    "task manager": "taskmgr",
    "settings": "ms-settings:",
}


_app_index: list[tuple[str, str]] | None = None  # (display name, AppID)


def _load_app_index(refresh: bool = False) -> list[tuple[str, str]]:
    """All launchable apps registered with Windows: Win32, Store, game shortcuts."""
    global _app_index
    if _app_index is None or refresh:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-StartApps | ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=40,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            data = [data]
        _app_index = [
            (a["Name"], str(a["AppID"])) for a in data if a.get("AppID") and a.get("Name")
        ]
    return _app_index


def _launch_appid(app_id: str) -> None:
    # shell:AppsFolder launches by AppUserModelID — works for Win32 and Store apps
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])


def launch_app(app_name: str) -> str:
    name = app_name.strip().lower()
    target = _APP_ALIASES.get(name, name)
    try:
        # 1) PATH executables and URI schemes (calc, ms-settings:, ...)
        resolved = shutil.which(target)
        if resolved:
            os.startfile(resolved)
            return f"Launched {app_name}."
        if target.endswith(":"):
            os.startfile(target)
            return f"Opened {app_name}."

        # 2) Installed-app registry (Start Menu + Store + game launchers)
        entries = [(n.lower(), n, aid) for n, aid in _load_app_index()]
        exact = [e for e in entries if e[0] == target]
        if exact:
            _launch_appid(exact[0][2])
            return f"Launched {exact[0][1]}."

        matches = [e for e in entries if e[0].startswith(target)] or [
            e for e in entries if target in e[0]
        ]
        if len(matches) == 1:
            _launch_appid(matches[0][2])
            return f"Launched {matches[0][1]}."
        if len(matches) > 1:
            names = ", ".join(sorted(e[1] for e in matches)[:8])
            return (
                f"Multiple installed apps match '{app_name}': {names}. "
                "Call launch_app again with the exact name."
            )

        # 3) Nothing matched — offer close spellings (typos, misheard speech)
        close = difflib.get_close_matches(
            target, [e[0] for e in entries], n=3, cutoff=0.6
        )
        if close:
            pretty = ", ".join(
                next(e[1] for e in entries if e[0] == c) for c in close
            )
            return (
                f"No app named '{app_name}'. Closest installed matches: {pretty}. "
                "Call launch_app again with the exact name if one of these fits."
            )
        return f"Error: no installed app matching '{app_name}' was found."
    except Exception as e:
        return f"Error: could not launch '{app_name}' ({e})."


_active_timers: list[threading.Timer] = []


def set_timer(seconds: int, label: str = "Timer") -> str:
    def _fire() -> None:
        try:
            import winsound

            for _ in range(3):
                winsound.Beep(880, 300)
        except Exception:
            pass
        if sys.stdout is not None:  # windowless (pythonw) has no console
            print(f"\n\a[J.A.R.V.I.S.] ⏰ {label} — time's up!\n> ", end="", flush=True)

    t = threading.Timer(seconds, _fire)
    t.daemon = True
    t.start()
    _active_timers.append(t)
    mins, secs = divmod(seconds, 60)
    human = f"{mins} minute{'s' if mins != 1 else ''}" if mins else f"{secs} seconds"
    if mins and secs:
        human += f" {secs} seconds"
    return f"Timer '{label}' set for {human}."


def restart_jarvis(delay_seconds: int = 8) -> str:
    """Relaunch the running Jarvis entry point in a new process and exit this
    one. The delay gives the model time to reply and speak before the lights
    go out; the UI restart button passes a much shorter delay."""
    module = "jarvis.voice_main" if "jarvis.voice_main" in sys.modules else "jarvis.main"
    root = Path(__file__).resolve().parents[1]

    def _go() -> None:
        subprocess.Popen(
            [sys.executable, "-m", module],
            cwd=str(root),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        os._exit(0)  # hard exit frees the mic and MCP port for the new instance

    threading.Timer(max(0.2, delay_seconds), _go).start()
    return (
        f"Restarting in about {int(delay_seconds)} seconds — say goodbye briefly; "
        "conversation memory resets with the process."
    )


def game_mode(action: str, game_title: str = "") -> str:
    """Toggle gaming performance mode (lower Jarvis priority, lighter UI)."""
    if action == "on":
        return game.set_game_mode(True, game_title)
    if action == "off":
        return game.set_game_mode(False)
    if action == "status":
        if game.is_game_mode():
            title = game.current_game()
            return f"Game mode is on{f' for {title}' if title else ''}."
        return "Game mode is off."
    return f"Error: unknown action '{action}'."


def watch_screen(action: str, focus: str = "") -> str:
    """Proactive screen watch: Jarvis speaks up on his own — gaming-buddy
    commentary by default, or steered by an optional focus."""
    if action == "on":
        return replay.set_watch(True, focus)
    if action == "off":
        return replay.set_watch(False)
    if action == "status":
        return replay.watch_status()
    return f"Error: unknown action '{action}'."


def get_active_window() -> str:
    """Title + process of the window in the foreground — usually the game."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc = ""
        try:
            proc = psutil.Process(pid.value).name()
        except Exception:
            pass
        title = buf.value or "(untitled)"
        return f"Active window: '{title}'" + (f" — process {proc}" if proc else "")
    except Exception as e:
        return f"Error: could not read the active window ({e})."


def lock_pc() -> str:
    ctypes.windll.user32.LockWorkStation()
    return "Workstation locked."


def power_control(action: str) -> str:
    """Sleep, shutdown, or restart. The model is instructed to confirm first."""
    if action == "sleep":
        subprocess.Popen(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], shell=False
        )
        return "Putting the PC to sleep."
    if action == "shutdown":
        subprocess.Popen(
            ["shutdown", "/s", "/t", "10"], shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "Shutting down in 10 seconds. Run 'shutdown /a' to abort."
    if action == "restart":
        subprocess.Popen(
            ["shutdown", "/r", "/t", "10"], shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "Restarting in 10 seconds. Run 'shutdown /a' to abort."
    return f"Error: unknown action '{action}'."


# ---------------------------------------------------------------------------
# Schemas + dispatch table
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "get_current_time",
        "description": "Get the current local date and time. Call this whenever the user asks about the time, date, or day of the week.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_system_stats",
        "description": "Get current PC stats: CPU load, RAM usage, disk space, and battery. Call this when the user asks how the PC is doing or about resource usage.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "set_volume",
        "description": "Control the PC's master volume. Call this when the user asks to change, mute, or set the volume.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["up", "down", "mute", "set"],
                    "description": "up/down adjust by ~10%, mute toggles, set moves to an absolute level",
                },
                "level": {
                    "type": "integer",
                    "description": "Target volume 0-100, only used with action 'set'",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "launch_app",
        "description": "Launch any installed application or game by name — searches the full Windows app registry (Start Menu, Store apps, Steam/Epic game shortcuts). E.g. spotify, steam, discord, chrome, visual studio code, or a game title. If the result lists multiple or close matches instead of launching, call again with the exact name from the list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "Name of the app to launch"}
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "set_timer",
        "description": "Set a countdown timer that beeps and prints a message when it fires. Call this when the user asks for a timer or a short reminder.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "Duration in seconds"},
                "label": {"type": "string", "description": "Short label for what the timer is for"},
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "play_music",
        "description": "Play a song, album, artist, or playlist on Spotify by name. Searches Spotify, opens the desktop app on the best match, and starts playback. Use this whenever the user asks to play music or a specific song/artist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to play, e.g. 'Back in Black AC/DC' — include the artist when known for better matches",
                },
                "kind": {
                    "type": "string",
                    "enum": ["track", "album", "playlist", "artist"],
                    "description": "What the query names; defaults to track",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "media_control",
        "description": "Control media playback with the system media keys: play/pause toggle, next or previous track, stop. Works with Spotify, YouTube, or whatever is playing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "pause", "next", "previous", "stop"],
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "open_website",
        "description": "Open a specific website URL in the default browser, e.g. youtube.com or github.com. For searching, prefer search_web.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL or domain to open"}
            },
            "required": ["url"],
        },
    },
    {
        "name": "search_web",
        "description": "Open the browser showing search results on Google or YouTube. Use when the user wants to SEE results or watch a video (e.g. 'search youtube for lofi beats', 'look up X on google'). If the user just wants an answer spoken aloud, answer directly or use your own web search instead of this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search terms"},
                "site": {
                    "type": "string",
                    "enum": ["google", "youtube"],
                    "description": "Where to search; defaults to google",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_email",
        "description": "List the user's latest Gmail messages (sender, subject, age, unread flag, and an id). Call this when the user asks about their inbox, new mail, or wants an email summary. Follow up with read_email for full contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "How many to list, 1-20 (default 5)"},
                "unread_only": {"type": "boolean", "description": "Only unread messages"},
            },
            "required": [],
        },
    },
    {
        "name": "read_email",
        "description": "Read one email's full headers and body text, given the bracketed id from check_email or search_email. Use this to summarize or answer questions about a specific message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The id shown in brackets in email listings"}
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "open_email",
        "description": "Open a specific email in the Gmail web interface in the browser, given the bracketed id from check_email or search_email. Use this when the user wants to SEE or open an email (e.g. to click a link in it) rather than have it read aloud.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The id shown in brackets in email listings"}
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "search_email",
        "description": "Search the user's Gmail with Gmail search syntax (from:amazon, subject:invoice, newer_than:7d, has:attachment, or plain words). Returns a listing with ids for read_email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail-style search query"},
                "count": {"type": "integer", "description": "Max results, 1-20 (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "restart_jarvis",
        "description": "Restart the Jarvis application itself (fresh process, fresh microphone, fresh everything — conversation memory resets). Call this when the user asks you to restart/reboot yourself, or when your own systems are clearly misbehaving. The restart happens a few seconds after this returns, so give a brief sign-off in your reply.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "game_mode",
        "description": "Toggle gaming performance mode. When on, Jarvis drops to background CPU priority and reduces UI updates so games run smoothly; voice still works normally. Turn it on when the user says they're starting a gaming session (or asks for game mode), off when they're done playing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["on", "off", "status"]},
                "game_title": {
                    "type": "string",
                    "description": "Which game the user is playing, if known — e.g. 'GTA 5 Online'",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "watch_screen",
        "description": (
            "Proactive live screen watch. When on, the screen is recorded into a "
            "rolling replay and Jarvis automatically looks and speaks short "
            "call-outs without being asked — reacting to big on-screen moments "
            "(a crash, a kill, a result screen), to scenes that change and "
            "settle (a blackjack hand being dealt), and checking in during "
            "sustained action like driving or firefights. Turn it on when the "
            "user asks you to watch their gameplay, be their gaming buddy, "
            "watch the table or dealer, or call their plays live; turn it off "
            "when they're done. Each call-out needs a few seconds of analysis "
            "before it's spoken."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["on", "off", "status"]},
                "focus": {
                    "type": "string",
                    "description": (
                        "Optional for 'on': leave empty for general gaming-buddy "
                        "commentary, or steer the watcher with what to look for "
                        "and what to say, e.g. 'GTA Online casino blackjack: "
                        "read my hand and the dealer upcard, call the "
                        "basic-strategy play in one sentence'"
                    ),
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "hear_game_audio",
        "description": (
            "Hear the game. Transcribes the last several seconds of the PC's "
            "audio output (WASAPI loopback) with local speech recognition — "
            "game dialogue, mission briefings, in-game phone calls, squad "
            "callouts. Use it when the user asks what a character just said, "
            "what the mission wants, or anything they didn't catch by ear, or "
            "when sound would answer better than the screen. Capture runs "
            "while game mode is on. The transcript contains everything the "
            "speakers played, so Jarvis's own recent replies and song lyrics "
            "may appear — ignore those."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "How far back to transcribe, 5-90 (default 20)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_focus_changes",
        "description": (
            "Diagnostic: list every window that took keyboard focus while "
            "game mode was on, with timestamps, oldest first. A game only "
            "receives inputs while its window holds focus, so when the user "
            "says their game inputs stopped registering or their character "
            "stopped responding, call this and report what grabbed focus at "
            "that moment (a launched app, the browser, Spotify, an overlay, "
            "a Windows popup)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_active_window",
        "description": "Get the title and process name of the window currently in the foreground. Use this to find out what game or app the user is in right now, e.g. before answering game questions or enabling game mode.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "save_game_notes",
        "description": "Save persistent notes about a specific game — grind routines, the user's goals, what they own, unlocks, strategies that worked. One notes file per game, kept across restarts. Append by default; use replace to rewrite the file (e.g. when updating a routine).",
        "input_schema": {
            "type": "object",
            "properties": {
                "game": {"type": "string", "description": "Game name, e.g. 'GTA 5 Online'"},
                "content": {
                    "type": "string",
                    "description": "Markdown notes to store — routines, goals, facts the user told you",
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "replace"],
                    "description": "append adds to existing notes (default); replace rewrites them",
                },
            },
            "required": ["game", "content"],
        },
    },
    {
        "name": "read_game_notes",
        "description": "Read the saved notes for a game (routines, goals, past info the user shared). Call with no game name to list which games have notes. Always check this before building or updating a routine for a game.",
        "input_schema": {
            "type": "object",
            "properties": {
                "game": {"type": "string", "description": "Game name; omit to list all games with notes"}
            },
            "required": [],
        },
    },
    {
        "name": "lock_pc",
        "description": "Lock the Windows workstation immediately. Call this when the user asks to lock the PC or screen.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "power_control",
        "description": "Sleep, shut down, or restart the PC. Destructive — always confirm with the user before calling this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["sleep", "shutdown", "restart"]}
            },
            "required": ["action"],
        },
    },
]

TOOL_FUNCTIONS = {
    "get_current_time": get_current_time,
    "get_system_stats": get_system_stats,
    "set_volume": set_volume,
    "media_control": media_control,
    "play_music": play_music,
    "open_website": open_website,
    "search_web": search_web,
    "check_email": check_email,
    "read_email": read_email,
    "open_email": open_email,
    "search_email": search_email,
    "launch_app": launch_app,
    "set_timer": set_timer,
    "restart_jarvis": restart_jarvis,
    "lock_pc": lock_pc,
    "power_control": power_control,
    "game_mode": game_mode,
    "watch_screen": watch_screen,
    "get_active_window": get_active_window,
    "hear_game_audio": hearing.hear_recent,
    "get_focus_changes": focuswatch.get_focus_changes,
    "save_game_notes": game.save_game_notes,
    "read_game_notes": game.read_game_notes,
}


def execute_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """Run a tool by name. Returns (result_text, is_error)."""
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return f"Unknown tool: {name}", True
    try:
        result = fn(**tool_input)
        return result, result.startswith("Error:")
    except Exception as e:  # tool failures go back to the model, not the user
        return f"Error executing {name}: {e}", True
