"""Ambient info for the UI: local weather, latest Gmail messages, system stats.

Everything here is free: weather comes from Open-Meteo (no key) with the
location guessed from the public IP (ip-api.com, no key); email uses Gmail
IMAP with an app password. Every source degrades gracefully — unconfigured
or offline just means that panel stays empty.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import json
import os
import threading
import time
from email.header import decode_header
from pathlib import Path
from typing import Callable

import psutil
import requests

TEMP_UNIT = "fahrenheit"  # or "celsius"
WEATHER_REFRESH_S = 600
EMAIL_REFRESH_S = 180
SYSTEM_REFRESH_S = 3
EMAIL_COUNT = 5

_SECRETS_PATH = Path(__file__).resolve().parents[1] / "secrets.json"

# WMO weather code -> (label, kind, day glyph, night glyph)
_WMO = {
    0: ("Clear", "clear", "☀", "☾"),
    1: ("Mostly clear", "clear", "☀", "☾"),
    2: ("Partly cloudy", "cloudy", "⛅", "☁"),
    3: ("Overcast", "cloudy", "☁", "☁"),
    45: ("Fog", "fog", "🌫", "🌫"),
    48: ("Freezing fog", "fog", "🌫", "🌫"),
    51: ("Light drizzle", "rain", "🌦", "🌧"),
    53: ("Drizzle", "rain", "🌦", "🌧"),
    55: ("Heavy drizzle", "rain", "🌧", "🌧"),
    56: ("Freezing drizzle", "rain", "🌧", "🌧"),
    57: ("Freezing drizzle", "rain", "🌧", "🌧"),
    61: ("Light rain", "rain", "🌧", "🌧"),
    63: ("Rain", "rain", "🌧", "🌧"),
    65: ("Heavy rain", "rain", "🌧", "🌧"),
    66: ("Freezing rain", "rain", "🌧", "🌧"),
    67: ("Freezing rain", "rain", "🌧", "🌧"),
    71: ("Light snow", "snow", "🌨", "🌨"),
    73: ("Snow", "snow", "🌨", "🌨"),
    75: ("Heavy snow", "snow", "🌨", "🌨"),
    77: ("Snow grains", "snow", "🌨", "🌨"),
    80: ("Rain showers", "rain", "🌦", "🌧"),
    81: ("Rain showers", "rain", "🌧", "🌧"),
    82: ("Violent showers", "rain", "🌧", "🌧"),
    85: ("Snow showers", "snow", "🌨", "🌨"),
    86: ("Snow showers", "snow", "🌨", "🌨"),
    95: ("Thunderstorm", "storm", "⛈", "⛈"),
    96: ("Thunderstorm + hail", "storm", "⛈", "⛈"),
    99: ("Thunderstorm + hail", "storm", "⛈", "⛈"),
}


# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------


def _fetch_location() -> tuple[float, float, str] | None:
    try:
        r = requests.get(
            "http://ip-api.com/json/?fields=status,lat,lon,city", timeout=6
        )
        d = r.json()
        if d.get("status") == "success":
            return float(d["lat"]), float(d["lon"]), d.get("city", "")
    except Exception:
        pass
    return None


def _fetch_weather(lat: float, lon: float, city: str) -> dict | None:
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,weather_code,is_day",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "temperature_unit": TEMP_UNIT,
                "timezone": "auto",
                "forecast_days": 1,
            },
            timeout=8,
        )
        d = r.json()
        cur, daily = d["current"], d["daily"]
        code = int(cur["weather_code"])
        label, kind, day_glyph, night_glyph = _WMO.get(
            code, ("Unsettled", "cloudy", "☁", "☁")
        )
        is_day = bool(cur.get("is_day", 1))
        return {
            "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]),
            "condition": label,
            "kind": kind,
            "glyph": day_glyph if is_day else night_glyph,
            "hi": round(daily["temperature_2m_max"][0]),
            "lo": round(daily["temperature_2m_min"][0]),
            "precip_prob": int(daily["precipitation_probability_max"][0] or 0),
            "city": city,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Gmail (IMAP, app password)
# ---------------------------------------------------------------------------


def _gmail_creds() -> tuple[str, str] | None:
    user = os.environ.get("JARVIS_GMAIL_USER")
    pw = os.environ.get("JARVIS_GMAIL_APP_PASSWORD")
    if user and pw:
        return user, pw
    try:
        d = json.loads(_SECRETS_PATH.read_text(encoding="utf-8"))
        if d.get("gmail_user") and d.get("gmail_app_password"):
            return d["gmail_user"], d["gmail_app_password"]
    except Exception:
        pass
    return None


def _decode_hdr(value: str | None) -> str:
    if not value:
        return ""
    out = []
    for text, enc in decode_header(value):
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _relative_time(msg_date: str | None) -> str:
    try:
        dt = email.utils.parsedate_to_datetime(msg_date)
        delta = time.time() - dt.timestamp()
        if delta < 90:
            return "now"
        if delta < 3600:
            return f"{int(delta // 60)}m"
        if delta < 86400:
            return f"{int(delta // 3600)}h"
        if delta < 6 * 86400:
            return dt.astimezone().strftime("%a")
        return dt.astimezone().strftime("%b %d")
    except Exception:
        return ""


def fetch_latest_emails(count: int = EMAIL_COUNT) -> dict:
    """Returns {"status": "ok"|"unconfigured"|"error", "items": [...], "detail": str}."""
    creds = _gmail_creds()
    if creds is None:
        return {"status": "unconfigured", "items": []}
    try:
        with imaplib.IMAP4_SSL("imap.gmail.com") as conn:
            conn.login(*creds)
            conn.select("INBOX", readonly=True)
            _typ, data = conn.search(None, "ALL")
            ids = data[0].split()[-count:]
            items = []
            for uid in reversed(ids):
                _typ, msg_data = conn.fetch(
                    uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
                )
                msg = email.message_from_bytes(msg_data[0][1])
                name, addr = email.utils.parseaddr(_decode_hdr(msg.get("From")))
                items.append(
                    {
                        "from": _decode_hdr(name) or addr.split("@")[0],
                        "subject": _decode_hdr(msg.get("Subject")) or "(no subject)",
                        "when": _relative_time(msg.get("Date")),
                    }
                )
        return {"status": "ok", "items": items}
    except Exception as e:
        return {"status": "error", "items": [], "detail": str(e)[:80]}


# ---------------------------------------------------------------------------
# Hub: background refresh + push to UI
# ---------------------------------------------------------------------------


class InfoHub:
    """Refreshes weather/email/system stats in the background and pushes each
    update through the given callbacks. `weather` stays readable from other
    threads so personality lines can react to conditions."""

    def __init__(
        self,
        on_weather: Callable[[dict | None], None] | None = None,
        on_emails: Callable[[dict], None] | None = None,
        on_system: Callable[[dict], None] | None = None,
    ):
        self.weather: dict | None = None
        self.on_weather = on_weather or (lambda w: None)
        self.on_emails = on_emails or (lambda e: None)
        self.on_system = on_system or (lambda s: None)

    def start(self) -> None:
        for fn in (self._weather_loop, self._email_loop, self._system_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()

    def _weather_loop(self) -> None:
        location = None
        while True:
            location = location or _fetch_location()
            if location:
                w = _fetch_weather(*location)
                if w:  # keep the last good reading through transient failures
                    self.weather = w
                    self.on_weather(w)
            time.sleep(WEATHER_REFRESH_S if self.weather else 60)

    def _email_loop(self) -> None:
        while True:
            self.on_emails(fetch_latest_emails())
            time.sleep(EMAIL_REFRESH_S)

    def _system_loop(self) -> None:
        psutil.cpu_percent(interval=None)  # prime; first call always reads 0
        while True:
            time.sleep(SYSTEM_REFRESH_S)
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory().percent
            self.on_system({"cpu": round(cpu), "ram": round(mem)})
