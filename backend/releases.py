"""What's-new announcements: one entry in config/releases.json becomes (1) a short
WhatsApp to every active employee and (2) a popup in Lumina the next time each
person opens it.

How it works
  * config/releases.json is a list of {id, date, title, bullets[], notify?}. Adding an
    entry and pushing is the whole job (scripts/add_release.py does the edit).
  * sync() runs on Railway shortly after every deploy and every few minutes after
    that. An entry whose id is not yet in release_log is recorded FIRST (that is the
    claim, so two overlapping runs can't both send) and then announced once.
  * The WhatsApp goes out through wa_outbox (the laptop delivers it). Outside
    09:00-21:00 IST it is held until 09:30 so nobody is pinged in the night.
  * The popup: GET /api/whatsnew returns releases this person has not dismissed
    (published in the last 30 days, newest first, at most 3); POST /api/whatsnew/seen
    records the dismissal. frontend/whatsnew.js shows it, loaded by auth.js.

"notify": false in an entry means popup only, no WhatsApp.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_INACTIVE = {"inactive", "disabled", "left", "removed", "archived", "former"}
FILE = os.path.join(os.path.dirname(__file__), "..", "config", "releases.json")
SHOW_DAYS = 30        # an unread release stops popping up after this long
MAX_POPUP = 3         # most releases shown at once
MAX_BULLETS_WA = 5
QUIET_START, QUIET_END = 9, 21   # IST hours when a WhatsApp may go out right away


def _ensure(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS release_log (
        slug         TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        body         TEXT NOT NULL,
        published_at TEXT NOT NULL,
        notified     INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS release_seen (
        user_id TEXT NOT NULL,
        slug    TEXT NOT NULL,
        seen_at TEXT NOT NULL,
        PRIMARY KEY (user_id, slug)
    )""")


def _now_ist() -> datetime:
    return datetime.now(_IST)


def load_entries() -> list:
    """The valid entries of config/releases.json (bad ones are skipped, never fatal)."""
    try:
        with open(FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        logger.exception("releases: could not read releases.json")
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        try:
            slug = str(e.get("id") or "").strip()
            title = str(e.get("title") or "").strip()
            bullets = [str(b).strip() for b in (e.get("bullets") or []) if str(b).strip()]
            if not re.fullmatch(r"[A-Za-z0-9._-]{3,80}", slug) or not title or not bullets:
                logger.warning("releases: skipping invalid entry %r", slug)
                continue
            out.append({"id": slug, "title": title[:120], "bullets": [b[:300] for b in bullets[:12]],
                        "date": str(e.get("date") or "")[:10], "notify": e.get("notify", True) is not False,
                        "how": str(e.get("how") or "").strip()[:300]})
        except Exception:
            continue
    return out


def _whatsapp_text(entry: dict) -> str:
    def cut(b: str, n: int = 170) -> str:
        return b if len(b) <= n else b[:n].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    lines = [f"- {cut(b)}" for b in entry["bullets"][:MAX_BULLETS_WA]]
    how = f"\n\n*How to use it:* {cut(entry['how'], 260)}" if entry.get("how") else ""
    return ("*What's new in Lumina*\n\n"
            f"*{entry['title']}*\n" + "\n".join(lines) + how +
            "\n\nOpen Lumina and you'll see the details.")


def _send_after() -> str | None:
    """None = send on the next poll; else IST 'YYYY-MM-DD HH:MM' of the next 09:30."""
    now = _now_ist()
    if QUIET_START <= now.hour < QUIET_END:
        return None
    nxt = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now >= nxt:
        nxt += timedelta(days=1)
    return nxt.strftime("%Y-%m-%d %H:%M")


def recipients() -> list:
    """Active employees with a WhatsApp number: [{id, name, whatsapp}]."""
    out = []
    try:
        import utils
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            if re.sub(r"\D", "", e.get("whatsapp", "") or ""):
                out.append({"id": e.get("id", ""), "name": e.get("name", ""), "whatsapp": e.get("whatsapp", "")})
    except Exception:
        logger.exception("releases: could not load employees")
    return out


def announce(entry: dict, only_ids: list | None = None) -> int:
    """Queue the WhatsApp for one entry. only_ids limits it (used by the test route)."""
    import wa_outbox
    text = _whatsapp_text(entry)
    when = _send_after()
    n = 0
    for r in recipients():
        if only_ids is not None and r["id"] not in only_ids:
            continue
        jid = wa_outbox.wa_jid(r["whatsapp"])
        if jid and wa_outbox.enqueue(jid, text, send_after=when):
            n += 1
    return n


def sync() -> dict:
    """Record and announce every entry in releases.json that has not gone out yet."""
    result = {"new": [], "notified": 0}
    entries = load_entries()
    if not entries:
        return result
    conn = get_connection()
    try:
        _ensure(conn)
        for e in entries:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO release_log (slug, title, body, published_at, notified) "
                    "VALUES (?,?,?,?,0)",
                    (e["id"], e["title"], json.dumps({"bullets": e["bullets"], "date": e["date"], "how": e["how"]}),
                     datetime.now(timezone.utc).isoformat()))
            if not cur.rowcount:
                continue                       # already published (or another run just claimed it)
            result["new"].append(e["id"])
            if not e["notify"]:
                continue
            try:
                sent = announce(e)
                result["notified"] += sent
                with conn:
                    conn.execute("UPDATE release_log SET notified=? WHERE slug=?", (sent, e["id"]))
            except Exception:
                logger.exception("releases: announcing %s failed", e["id"])
    finally:
        conn.close()
    if result["new"]:
        logger.info("releases: published %s, %s WhatsApp(s) queued", result["new"], result["notified"])
    return result


def unseen_for(user_id: str) -> list:
    """Releases this person has not dismissed yet: [{id, title, date, bullets}]."""
    if not user_id:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SHOW_DAYS)).isoformat()
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT slug, title, body, published_at FROM release_log "
            "WHERE published_at>=? AND slug NOT IN (SELECT slug FROM release_seen WHERE user_id=?) "
            "ORDER BY published_at DESC, slug DESC LIMIT ?",
            (cutoff, user_id, MAX_POPUP)).fetchall()
    finally:
        conn.close()
    hows = {e["id"]: e["how"] for e in load_entries()}
    out = []
    for slug, title, body, pub in rows:
        try:
            b = json.loads(body)
        except Exception:
            b = {}
        out.append({"id": slug, "title": title, "date": b.get("date") or pub[:10],
                    "bullets": b.get("bullets") or [],
                    "how": hows.get(slug) or b.get("how") or ""})
    return out


def all_for(user_id: str) -> list:
    """Every published release, newest first, for the dashboard's history list; `new` = not dismissed yet."""
    if not user_id:
        return []
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute("SELECT slug, title, body, published_at FROM release_log "
                            "ORDER BY published_at DESC, slug DESC LIMIT 100").fetchall()
        seen = {r[0] for r in conn.execute("SELECT slug FROM release_seen WHERE user_id=?", (user_id,)).fetchall()}
    finally:
        conn.close()
    hows = {e["id"]: e["how"] for e in load_entries()}
    out = []
    for slug, title, body, pub in rows:
        try:
            b = json.loads(body)
        except Exception:
            b = {}
        out.append({"id": slug, "title": title, "date": b.get("date") or pub[:10],
                    "bullets": b.get("bullets") or [], "how": hows.get(slug) or b.get("how") or "",
                    "new": slug not in seen})
    return out


def mark_seen(user_id: str, slugs: list) -> int:
    slugs = [str(s) for s in (slugs or []) if re.fullmatch(r"[A-Za-z0-9._-]{3,80}", str(s))][:20]
    if not user_id or not slugs:
        return 0
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            for s in slugs:
                conn.execute("INSERT OR IGNORE INTO release_seen (user_id, slug, seen_at) VALUES (?,?,?)",
                             (user_id, s, datetime.now(timezone.utc).isoformat()))
        return len(slugs)
    finally:
        conn.close()


def status() -> list:
    """For the companion inspection route: every release with how many people have seen it."""
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute("SELECT slug, title, published_at, notified FROM release_log "
                            "ORDER BY published_at DESC LIMIT 30").fetchall()
        seen = dict(conn.execute("SELECT slug, COUNT(*) FROM release_seen GROUP BY slug").fetchall())
    finally:
        conn.close()
    return [{"id": s, "title": t, "published_at": p, "whatsapp_queued": n, "seen_by": seen.get(s, 0)}
            for s, t, p, n in rows]
