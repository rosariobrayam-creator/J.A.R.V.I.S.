"""Gmail tools for the brain: list, read, and search messages over IMAP.

Reuses the app-password credentials from info.py. Listings include the IMAP
UID in brackets so the model can follow up with read_email. All fetches use
BODY.PEEK so nothing gets marked as read.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
import urllib.parse
import webbrowser

from .info import _decode_hdr, _gmail_creds, _relative_time

MAX_BODY_CHARS = 4000
_NOT_CONFIGURED = (
    "Error: Gmail is not linked — add gmail_user and gmail_app_password to "
    "secrets.json (see README)."
)


def _connect() -> imaplib.IMAP4_SSL | None:
    creds = _gmail_creds()
    if creds is None:
        return None
    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    conn.login(*creds)
    conn.select("INBOX", readonly=True)
    return conn


def _body_text(msg: email.message.Message) -> str:
    """Best-effort plain text: prefer text/plain, fall back to de-tagged HTML."""

    def _dec(part: email.message.Message) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")

    plain, html = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if "attachment" in str(part.get("Content-Disposition") or ""):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not plain:
            plain = _dec(part)
        elif ctype == "text/html" and not html:
            html = _dec(part)
    text = plain
    if not text and html:
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + " […truncated]"
    return text or "(no readable body)"


def _summary_lines(conn: imaplib.IMAP4_SSL, uids: list[bytes]) -> list[str]:
    lines = []
    for uid in uids:
        _typ, md = conn.uid(
            "fetch", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
        )
        if not md or md[0] is None:
            continue
        meta, raw = md[0][0], md[0][1]
        msg = email.message_from_bytes(raw)
        name, addr = email.utils.parseaddr(_decode_hdr(msg.get("From")))
        unread = b"\\Seen" not in (meta or b"")
        lines.append(
            f"[{uid.decode()}]{' (unread)' if unread else ''} "
            f"{name or addr} — {_decode_hdr(msg.get('Subject')) or '(no subject)'} "
            f"({_relative_time(msg.get('Date')) or '?'})"
        )
    return lines


def check_email(count: int = 5, unread_only: bool = False) -> str:
    conn = _connect()
    if conn is None:
        return _NOT_CONFIGURED
    try:
        _typ, data = conn.uid("search", None, "UNSEEN" if unread_only else "ALL")
        uids = data[0].split()
        if not uids:
            return "No unread emails." if unread_only else "The inbox is empty."
        count = max(1, min(20, count))
        lines = _summary_lines(conn, list(reversed(uids[-count:])))
        return (
            f"{'Unread' if unread_only else 'Latest'} emails, newest first "
            "(bracketed id works with read_email):\n" + "\n".join(lines)
        )
    except Exception as e:
        return f"Error: could not check email ({e})."
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def search_email(query: str, count: int = 5) -> str:
    """Gmail-syntax search (X-GM-RAW): supports from:, subject:, newer_than:7d,
    has:attachment, or plain words — same as the Gmail search box."""
    conn = _connect()
    if conn is None:
        return _NOT_CONFIGURED
    try:
        quoted = '"' + query.replace('"', '\\"') + '"'
        _typ, data = conn.uid("search", None, "X-GM-RAW", quoted)
        uids = data[0].split()
        if not uids:
            return f"No emails match '{query}'."
        count = max(1, min(20, count))
        lines = _summary_lines(conn, list(reversed(uids[-count:])))
        return (
            f"Emails matching '{query}', newest first (bracketed id works with "
            "read_email):\n" + "\n".join(lines)
        )
    except Exception as e:
        return f"Error: email search failed ({e})."
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def open_email(email_id: str) -> str:
    """Open one message in the Gmail web UI via a rfc822msgid: search — the
    old hex-msgid permalinks 404 in current Gmail, this form is supported."""
    conn = _connect()
    if conn is None:
        return _NOT_CONFIGURED
    try:
        uid = str(email_id).strip().strip("[]")
        _typ, md = conn.uid(
            "fetch", uid.encode(), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
        )
        if not md or md[0] is None:
            return f"Error: no email with id {uid} — get a current id from check_email."
        msg = email.message_from_bytes(md[0][1])
        msgid = (msg.get("Message-ID") or "").strip().strip("<>")
        if not msgid:
            return "Error: that email has no Message-ID header to link to."
        user = (_gmail_creds() or ("",))[0]
        url = (
            f"https://mail.google.com/mail/?authuser={urllib.parse.quote(user)}"
            f"#search/rfc822msgid:{urllib.parse.quote(msgid, safe='')}"
        )
        webbrowser.open(url)
        return "Opened the email in the browser."
    except Exception as e:
        return f"Error: could not open email ({e})."
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def read_email(email_id: str) -> str:
    conn = _connect()
    if conn is None:
        return _NOT_CONFIGURED
    try:
        uid = str(email_id).strip().strip("[]")
        _typ, md = conn.uid("fetch", uid.encode(), "(BODY.PEEK[])")
        if not md or md[0] is None:
            return f"Error: no email with id {uid} — get a current id from check_email."
        msg = email.message_from_bytes(md[0][1])
        header = (
            f"From: {_decode_hdr(msg.get('From'))}\n"
            f"To: {_decode_hdr(msg.get('To'))}\n"
            f"Date: {_decode_hdr(msg.get('Date'))}\n"
            f"Subject: {_decode_hdr(msg.get('Subject')) or '(no subject)'}\n"
        )
        return header + "\n" + _body_text(msg)
    except Exception as e:
        return f"Error: could not read email ({e})."
    finally:
        try:
            conn.logout()
        except Exception:
            pass
