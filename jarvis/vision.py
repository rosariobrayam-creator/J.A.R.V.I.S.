"""Screen vision: grab the display so the brain can see what the user sees.

mss does the capture (fast, no window focus games), Pillow shrinks it to a
model-friendly JPEG. Exposed to headless Claude Code as the capture_screen
MCP tool, which returns the image itself — vision-capable models read it
directly from the tool result.

Note: games in *exclusive* fullscreen can capture as a black frame; borderless
windowed mode (the default in most modern games, including GTA V) works fine.
"""

from __future__ import annotations

import io

from mss import mss
from PIL import Image

from . import game
from .config import GAME_VISION_WIDTH, VISION_WIDTH

JPEG_QUALITY = 72


def grab_screen_jpeg(monitor: int = 1) -> bytes:
    """Screenshot one monitor (1 = primary) as resized JPEG bytes.

    Frames are smaller while gaming: fewer image tokens is ~1s less prefill on
    every question about the screen, and a mission timer is worth more than
    detail the model wasn't going to use.
    """
    max_width = GAME_VISION_WIDTH if game.is_game_mode() else VISION_WIDTH
    with mss() as sct:
        if not 1 <= monitor < len(sct.monitors):
            monitor = 1
        shot = sct.grab(sct.monitors[monitor])
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if img.width > max_width:
        img = img.resize(
            (max_width, round(img.height * max_width / img.width)),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()
