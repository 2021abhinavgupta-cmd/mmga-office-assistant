"""Daily mood check-in: after logging in each person picks happy / mid / sad once a day.
Identity is the session cookie. The Live Attendance cards show the mood next to the name."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from db import get_connection
from utils import today_ist, now_ist
from routes.leave import _require_session

mood_bp = Blueprint("mood", __name__)

MOODS = ("awesome", "happy", "mid", "sad", "stressed")


def ensure_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_mood (
               user_id TEXT NOT NULL,
               date TEXT NOT NULL,
               mood TEXT NOT NULL,
               created_at TEXT,
               PRIMARY KEY (user_id, date))"""
    )
    try:
        conn.execute("ALTER TABLE daily_mood ADD COLUMN reason TEXT DEFAULT ''")
    except Exception:
        pass   # column already there


def moods_for(date_str: str) -> dict:
    conn = get_connection()
    try:
        ensure_table(conn)
        rows = conn.execute("SELECT user_id, mood FROM daily_mood WHERE date=?", (date_str,)).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def mood_reasons_for(date_str: str) -> dict:
    """user_id -> the optional note they typed with today's mood (only non-empty ones)."""
    conn = get_connection()
    try:
        ensure_table(conn)
        rows = conn.execute(
            "SELECT user_id, reason FROM daily_mood WHERE date=? AND COALESCE(reason,'')<>''", (date_str,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


@mood_bp.route("/api/mood/today", methods=["GET"])
def mood_today():
    uid, err = _require_session()
    if err:
        return err
    today = today_ist()
    conn = get_connection()
    try:
        ensure_table(conn)
        m = conn.execute("SELECT mood FROM daily_mood WHERE user_id=? AND date=?", (uid, today)).fetchone()
        a = conn.execute(
            "SELECT checkin_time FROM daily_attendance WHERE user_id=? AND date=?", (uid, today)
        ).fetchone()
    finally:
        conn.close()
    checked_in = bool(a and a[0])
    return jsonify({"mood": m[0] if m else None, "checked_in": checked_in,
                    "needs_mood": checked_in and not m})


@mood_bp.route("/api/mood", methods=["POST"])
def mood_set():
    uid, err = _require_session()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    mood = str(body.get("mood", "")).strip().lower()
    if mood not in MOODS:
        return jsonify({"success": False, "error": "mood must be one of: " + ", ".join(MOODS)}), 400
    # optional note: why they feel this way (single line, capped)
    reason = " ".join(str(body.get("reason") or "").split())[:300]
    conn = get_connection()
    prev = None
    prev_reason = ""
    try:
        ensure_table(conn)
        row = conn.execute("SELECT mood, COALESCE(reason,'') FROM daily_mood WHERE user_id=? AND date=?",
                           (uid, today_ist())).fetchone()
        prev = row[0] if row else None
        prev_reason = row[1] if row else ""
        conn.execute(
            """INSERT INTO daily_mood (user_id, date, mood, created_at, reason) VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, date) DO UPDATE SET mood=excluded.mood, reason=excluded.reason""",
            (uid, today_ist(), mood, str(now_ist()), reason),
        )
        conn.commit()
    finally:
        conn.close()
    if prev != mood or reason != prev_reason:
        _tell_hr(uid, mood, prev, reason)
    return jsonify({"success": True, "mood": mood, "reason": reason})


HR_USER_ID = "emp009"   # Noorish
_EMOJI = {"awesome": "🤩", "happy": "😊", "mid": "😐", "sad": "😞", "stressed": "😫"}


def _tell_hr(uid: str, mood: str, prev: str | None, reason: str = "") -> None:
    """WhatsApp HR when someone fills or changes today's mood. Never raises."""
    try:
        if uid == HR_USER_ID:
            return
        import wa_outbox
        emps = {e.get("id"): e for e in wa_outbox._employees()}
        hr = emps.get(HR_USER_ID) or {}
        jid = wa_outbox.wa_jid(hr.get("whatsapp", ""))
        if not jid:
            return
        name = (emps.get(uid) or {}).get("name") or uid
        if prev:
            text = f"{name} changed their mood today: {_EMOJI.get(prev, '')} {prev} -> {_EMOJI.get(mood, '')} {mood}"
        else:
            text = f"{name} is feeling {_EMOJI.get(mood, '')} {mood} today."
        if reason:
            text += f"\nReason: {reason}"
        wa_outbox.enqueue(jid, text)
    except Exception:
        pass
