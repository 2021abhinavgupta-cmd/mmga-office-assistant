"""Short links for long URLs in WhatsApp replies: https://<host>/l/<code> redirects to the real link.
The code is derived from the URL, so the same URL always gets the same short link."""
from __future__ import annotations

import base64
import hashlib
import os

from db import get_connection

MIN_LEN = 40   # shorter URLs are left alone


def _base() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or "https://lumina.mmga.agency").rstrip("/")


def _ensure(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS short_links (code TEXT PRIMARY KEY, url TEXT NOT NULL, created_at TEXT)")


def shorten(url: str) -> str:
    """Short link for url, or url itself if it is already short / anything fails."""
    if not url or len(url) <= MIN_LEN or not url.lower().startswith(("http://", "https://")):
        return url
    try:
        code = base64.urlsafe_b64encode(hashlib.sha256(url.encode("utf-8")).digest())[:7].decode()
        conn = get_connection()
        try:
            _ensure(conn)
            conn.execute("INSERT OR IGNORE INTO short_links (code, url, created_at) VALUES (?,?,datetime('now'))",
                         (code, url))
            conn.commit()
            row = conn.execute("SELECT url FROM short_links WHERE code=?", (code,)).fetchone()
        finally:
            conn.close()
        if row and row[0] == url:
            return f"{_base()}/l/{code}"
    except Exception:
        pass
    return url


def resolve(code: str) -> str | None:
    try:
        conn = get_connection()
        try:
            _ensure(conn)
            row = conn.execute("SELECT url FROM short_links WHERE code=?", (code,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None
