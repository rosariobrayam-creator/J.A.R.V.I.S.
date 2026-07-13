"""Spotify control that works on a free Spotify account.

Track lookup uses the Web API client-credentials flow (a free developer app —
no Premium required) when spotify_client_id / spotify_client_secret are in
secrets.json or env vars. Playback happens in the local desktop app: we open
the item's spotify: URI (os.startfile, per the ShellExecute quirk) and then
tap the media play key. Without API credentials we fall back to opening
Spotify's search page for the query.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
import urllib.parse

import psutil
import requests

from .info import _SECRETS_PATH

_VK_MEDIA_PLAY_PAUSE = 0xB3
_KEYEVENTF_KEYUP = 0x0002

_KINDS = ("track", "album", "playlist", "artist")


def _press_play() -> None:
    ctypes.windll.user32.keybd_event(_VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    ctypes.windll.user32.keybd_event(_VK_MEDIA_PLAY_PAUSE, 0, _KEYEVENTF_KEYUP, 0)


def _creds() -> tuple[str, str] | None:
    cid = os.environ.get("JARVIS_SPOTIFY_CLIENT_ID")
    sec = os.environ.get("JARVIS_SPOTIFY_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    try:
        d = json.loads(_SECRETS_PATH.read_text(encoding="utf-8"))
        if d.get("spotify_client_id") and d.get("spotify_client_secret"):
            return d["spotify_client_id"], d["spotify_client_secret"]
    except Exception:
        pass
    return None


_token: dict = {"value": None, "expires": 0.0}


def _get_token() -> str | None:
    if _token["value"] and time.time() < _token["expires"] - 30:
        return _token["value"]
    creds = _creds()
    if creds is None:
        return None
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=creds,
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    _token["value"] = d["access_token"]
    _token["expires"] = time.time() + d.get("expires_in", 3600)
    return _token["value"]


def _spotify_running() -> bool:
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == "spotify.exe":
            return True
    return False


def play_music(query: str, kind: str = "track") -> str:
    kind = kind.lower().strip()
    if kind not in _KINDS:
        kind = "track"

    try:
        token = _get_token()
    except Exception as e:
        return f"Error: Spotify authentication failed ({e})."

    if token is None:
        # No API credentials — best we can do is open the search page.
        os.startfile("spotify:search:" + urllib.parse.quote(query))
        return (
            f"Opened Spotify search for '{query}' — no Spotify API credentials are "
            "configured, so the user has to click a result themselves. Auto-play "
            "needs spotify_client_id/spotify_client_secret in secrets.json (free, "
            "see README)."
        )

    try:
        r = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": query, "type": kind, "limit": 1},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        items = [i for i in r.json().get(kind + "s", {}).get("items", []) if i]
    except Exception as e:
        return f"Error: Spotify search failed ({e})."
    if not items:
        return f"Error: no Spotify {kind} found for '{query}'."

    item = items[0]
    cold_start = not _spotify_running()
    os.startfile(item["uri"])
    time.sleep(6.0 if cold_start else 2.0)  # let the app open and land on the item
    _press_play()

    name = item.get("name", query)
    artists = ", ".join(a["name"] for a in item.get("artists", []) or [])
    what = f"{name} by {artists}" if artists and kind != "artist" else name
    return (
        f"Playing {what} on Spotify. (Playback is toggled with the media key — "
        "if something was already playing this may have paused instead; the "
        "media_control tool's 'play' fixes that.)"
    )
