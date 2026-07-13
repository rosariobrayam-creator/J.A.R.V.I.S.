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
from mcp.server.fastmcp import FastMCP

# Keep the REPL clean — the embedded HTTP server is an implementation detail.
logging.getLogger("httpx").setLevel(logging.WARNING)

from .tools import TOOL_FUNCTIONS, TOOL_SCHEMAS

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}/mcp"

server = FastMCP("jarvis", host=HOST, port=PORT, log_level="WARNING")

for _schema in TOOL_SCHEMAS:
    server.tool(name=_schema["name"], description=_schema["description"])(
        TOOL_FUNCTIONS[_schema["name"]]
    )


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
