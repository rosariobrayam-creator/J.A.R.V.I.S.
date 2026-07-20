"""Local MCP server exposing J.A.R.V.I.S. tools over HTTP.

Runs inside the main REPL process (background thread) so stateful things like
timers survive across turns. Headless Claude Code connects to it via the
mcp-config.json at the project root.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx
from mcp.server.fastmcp import FastMCP, Image

# Keep the REPL clean — the embedded HTTP server is an implementation detail.
logging.getLogger("httpx").setLevel(logging.WARNING)

from . import replay
from .config import GLANCE_FRAMES, GLANCE_SECONDS, REPLAY_SECONDS
from .tools import TOOL_FUNCTIONS, TOOL_SCHEMAS
from .vision import grab_screen_jpeg

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/mcp"

server = FastMCP("jarvis", host=HOST, port=PORT, log_level="WARNING")

for _schema in TOOL_SCHEMAS:
    server.tool(name=_schema["name"], description=_schema["description"])(
        TOOL_FUNCTIONS[_schema["name"]]
    )


# Vision returns an image, not text, so it registers here rather than in the
# TOOL_FUNCTIONS table (which the direct-API backend expects to return str).
@server.tool(
    name="capture_screen",
    description=(
        "See the user's screen. Use this whenever the user asks about what's "
        "on their screen or what they're looking at — a game scene, a menu, an "
        "error, a page. Returns the current frame of the primary monitor; "
        "while the screen recording is running (game mode / watch mode) it "
        "also includes a short filmstrip of the last few seconds, oldest "
        "first, so you can see motion — cards being dealt, a kill, a scene "
        "change. The final frame labeled 'live' is the present moment. "
        "Optionally pass seconds (how far back the filmstrip reaches, max 15) "
        "and frames (how many snapshots of it, max 12) — a dense strip like "
        "seconds=5, frames=10 is best for fast action."
    ),
)
def capture_screen(seconds: float = GLANCE_SECONDS, frames: int = GLANCE_FRAMES):
    seconds = max(1.0, min(float(seconds), 15.0))
    frames = max(1, min(int(frames), 12))  # 12 frames ≈ 15k image tokens, cap
    live = Image(data=grab_screen_jpeg(), format="jpeg")
    if not replay.replay_running():
        return live
    strip = replay.recent_frames(seconds, frames)
    if not strip:
        return live
    out: list = [
        f"The last {round(seconds)} seconds of the screen, oldest "
        "first, then the live frame:"
    ]
    for age, jpeg in strip:
        out.append(f"[{age:.1f} seconds ago]")
        out.append(Image(data=jpeg, format="jpeg"))
    out.append("[live, right now]")
    out.append(live)
    return out


@server.tool(
    name="review_recent_screen",
    description=(
        "Scrub back through Jarvis's screen recording: 720p frames captured "
        "several times a second while game mode or watch mode is on, each "
        "labeled with how many seconds ago it was taken, oldest first. Use "
        "this when the user asks what JUST happened on screen — 'what did "
        "the dealer have', 'did you see that', 'what killed me' — anything "
        "about a moment that's already gone. For the current screen use "
        "capture_screen instead (it already includes the last few seconds). "
        f"seconds = how far back to look (max {REPLAY_SECONDS}), frames = how "
        "many snapshots of that window to return (max 12) — pick a narrow "
        "window with more frames to watch one moment closely."
    ),
)
def review_recent_screen(seconds: int = 60, frames: int = 6):
    seconds = max(5, min(int(seconds), REPLAY_SECONDS))
    frames = max(2, min(int(frames), 12))
    if not replay.replay_running():
        replay.start_replay()
        return [
            "The screen replay wasn't recording (it runs during game mode or "
            "watch mode) — so there's no footage of what already happened. "
            "I've started recording now; from here on the last "
            f"{REPLAY_SECONDS // 60} minutes will be available. "
            "Here is the current screen:",
            Image(data=grab_screen_jpeg(), format="jpeg"),
        ]
    sampled = replay.recent_frames(seconds, frames)
    if not sampled:
        return [
            "The replay just started recording and has no frames yet. "
            "Here is the current screen:",
            Image(data=grab_screen_jpeg(), format="jpeg"),
        ]
    out: list = [
        f"{len(sampled)} frames from the last {seconds} seconds, oldest first:"
    ]
    for age, jpeg in sampled:
        out.append(f"[{age:.1f} seconds ago]")
        out.append(Image(data=jpeg, format="jpeg"))
    return out


def start_in_background(timeout: float = 10.0) -> None:
    """Start the MCP server in a daemon thread and wait until it accepts connections."""
    thread = threading.Thread(
        target=lambda: server.run(transport="streamable-http"),
        daemon=True,
        name="jarvis-mcp-server",
    )
    thread.start()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # Any HTTP response means the socket is up; MCP handshake comes later.
            httpx.get(URL, timeout=1.0)
            return
        except httpx.TransportError:
            time.sleep(0.2)
    raise RuntimeError(f"MCP server failed to start on {URL} within {timeout}s")
