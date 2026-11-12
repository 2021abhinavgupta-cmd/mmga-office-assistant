"""Daily mood check-in: after logging in each person picks happy / mid / sad once a day.
Identity is the session cookie. The Live Attendance cards show the mood next to the name."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from db import get_connection
from utils import today_ist, now_ist
from routes.leave import _require_session

mood_bp = Blueprint("mood", __name__)

MOODS = ("happy", "mid", "sad")


def ensure_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_mood (
               user_id TEXT NOT NULL,
               date TEXT NOT NULL,
               mood TEXT NOT NULL,
               created_at TEXT,
               PRIMARY KEY (user_id, date))"""
    )


def moods_for(date_str: str) -> dict:
    conn = get_connection()
    try:
        ensure_table(conn)
        rows = conn.execute("SELECT user_id, mood FROM daily_mood WHERE date=?", (date_str,)).fetchall()
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
    mood = str((request.get_json(silent=True) or {}).get("mood", "")).strip().lower()
    if mood not in MOODS:
        return jsonify({"success": False, "error": "mood must be happy, mid or sad"}), 400
    conn = get_connection()
    prev = None
    try:
        ensure_table(conn)
        row = conn.execute("SELECT mood FROM daily_mood WHERE user_id=? AND date=?", (uid, today_ist())).fetchone()
        prev = row[0] if row else None
        conn.execute(
            """INSERT INTO daily_mood (user_id, date, mood, created_at) VALUES (?,?,?,?)
               ON CONFLICT(user_id, date) DO UPDATE SET mood=excluded.mood""",
            (uid, today_ist(), mood, str(now_ist())),
        )
        conn.commit()
    finally:
        conn.close()
    if prev != mood:
        _tell_hr(uid, mood, prev)
    return jsonify({"success": True, "mood": mood})


HR_USER_ID = "emp009"   # Noorish
_EMOJI = {"happy": "😊", "mid": "😐", "sad": "😞"}


def _tell_hr(uid: str, mood: str, prev: str | None) -> None:
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
        wa_outbox.enqueue(jid, text)
    except Exception:
        pass
