"""
Attendance & Employee Blueprint
Routes: /api/attendance/*, /api/employees/*
"""
import csv
import json
import logging
import re
from datetime import datetime, time as dt_time
from io import StringIO

from flask import Blueprint, Response, jsonify, request

from utils import (IST, _is_admin, _load_employees, _save_employees,
                   now_ist, today_ist)

logger = logging.getLogger(__name__)
attendance_bp = Blueprint("attendance", __name__)


def _verified_admin() -> bool:
    """Real session check for the two routes below (security warning #6's
    caveat): _is_admin(user_id) is bool(user_id) everywhere else in this app
    by design (gotcha #60, every logged-in employee is admin) -- but reading
    that user_id from an unauthenticated query string here meant ANY
    non-empty string passed, not just a real logged-in employee. This ties
    it to an actual valid session_token cookie instead."""
    from routes.auth import _verify_session
    token = request.cookies.get("session_token", "")
    user_id = _verify_session(token)
    return bool(user_id) and _is_admin(user_id)

# Work-day window for the daily_attendance summary (checkin_time/checkout_time
# -- what the Live Attendance dashboard cards and "Total" hours are computed
# from). A login/logout outside this window (e.g. checking Lumina at
# midnight) still happened and is still logged verbatim in the raw
# `attendance` audit table, but must not be allowed to stretch the day's
# recorded work window earlier/later than the business day actually runs.
WORK_START = dt_time(9, 0, 0)
WORK_END = dt_time(22, 30, 0)


def _clamp_work_time(hhmmss: str) -> str:
    try:
        h, m, s = (int(p) for p in hhmmss.split(":"))
        actual = dt_time(h, m, s)
    except Exception:
        return hhmmss
    if actual < WORK_START:
        return WORK_START.strftime("%H:%M:%S")
    if actual > WORK_END:
        return WORK_END.strftime("%H:%M:%S")
    return hhmmss


# ── DB helpers ────────────────────────────────────────────────────────────────

def _attendance_conn():
    from db import get_connection
    return get_connection()


def _attendance_payload():
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body
    raw = request.get_data(cache=False, as_text=True) or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _attendance_checkin(user_id: str, lat=None, lng=None):
    """First IST login of day wins. checkin_time is clamped to the work-day
    window (see WORK_START/WORK_END) so a login before 9am doesn't record the
    day as having started at 2am.

    lat/lng are the browser's best-effort navigator.geolocation reading at
    the moment of check-in (HR-visibility request, 2026-09-28) -- optional,
    only written the same first-checkin-of-the-day the time itself is, never
    overwritten by a later call the way checkout is. None whenever the
    employee's browser doesn't support/denies geolocation, or the call came
    from somewhere with no browser at all (e.g. the WhatsApp bot's
    set_attendance tool) -- never blocks the check-in itself either way.

    Uses DO UPDATE ... WHERE checkin_time IS NULL rather than DO NOTHING:
    _attendance_ping() (the presence heartbeat) can create today's row first
    with checkin_time still NULL (it only ever writes last_seen_at). A plain
    DO NOTHING would then permanently skip setting checkin_time once that
    placeholder row exists, even on a genuine login -- this still preserves
    "first real checkin wins" (a NULL only gets filled once), it just also
    handles the row already existing from a ping with nothing in it yet."""
    d = today_ist()
    t = _clamp_work_time(now_ist())
    ts = datetime.now(IST).isoformat(timespec="seconds")
    conn = _attendance_conn()
    with conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO daily_attendance (user_id, date, checkin_time, checkin_lat, checkin_lng)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET
                   checkin_time = excluded.checkin_time,
                   checkin_lat = excluded.checkin_lat,
                   checkin_lng = excluded.checkin_lng
               WHERE daily_attendance.checkin_time IS NULL""",
            (user_id, d, t, lat, lng),
        )
        if cur.rowcount > 0:
            conn.execute(
                "INSERT INTO attendance (user_id, action, timestamp) VALUES (?, 'in', ?)",
                (user_id, ts),
            )
        else:
            # The day already has a check-in (the portal auto-checks anyone with a
            # live session in on their first page load, with no location). The time
            # stays as the first of the day, but a location supplied now fills in the
            # blank instead of being thrown away.
            if lat is not None and lng is not None:
                cur.execute(
                    """UPDATE daily_attendance SET checkin_lat = ?, checkin_lng = ?
                       WHERE user_id = ? AND date = ?
                         AND checkin_time IS NOT NULL
                         AND (checkin_lat IS NULL OR checkin_lng IS NULL)""",
                    (lat, lng, user_id, d),
                )
            # An explicit check-in after a Check Out / Logout the same day means
            # "I'm back" (a PIN login after a lunch-break logout, "in" on WhatsApp).
            # /api/auth/verify no longer reopens manual checkouts on a page refresh,
            # so this is where they resume.
            reopened = cur.execute(
                """UPDATE daily_attendance
                   SET checkout_time = NULL, checkout_lat = NULL, checkout_lng = NULL,
                       checkout_auto = 0
                   WHERE user_id = ? AND date = ? AND checkout_time IS NOT NULL""",
                (user_id, d),
            ).rowcount
            if reopened > 0:
                conn.execute(
                    "INSERT INTO attendance (user_id, action, timestamp) VALUES (?, 'in', ?)",
                    (user_id, ts),
                )
        cur.execute(
            "SELECT checkin_time FROM daily_attendance WHERE user_id=? AND date=?",
            (user_id, d),
        )
        row = cur.fetchone()
        stored_checkin = row[0] if row else t
    conn.close()
    return d, stored_checkin


def _attendance_ping(user_id: str) -> str:
    """Lightweight presence heartbeat -- called every 60s while a Lumina page
    is open (see auth.js). Only writes last_seen_at, nothing else. Real
    checkout is inferred later by sweep_stale_checkouts() (task_scheduler.py)
    from staleness of this timestamp, NOT from any unload/pagehide event --
    that approach was tried and reverted (see CLAUDE.md gotcha #70/#71: a
    pagehide-triggered checkout write fires on every internal page
    navigation, not just a real tab close, and the resulting write volume
    was implicated in a production DB hang)."""
    d = today_ist()
    t = now_ist()
    conn = _attendance_conn()
    with conn:
        conn.execute(
            """INSERT INTO daily_attendance (user_id, date, last_seen_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
            (user_id, d, t),
        )
    conn.close()
    return t


def _attendance_checkout(user_id: str, lat=None, lng=None):
    """Always updates checkout_time to latest IST logout (UPSERT).
    checkout_time is clamped to the work-day window (see WORK_START/WORK_END)
    so a logout after 10:30pm doesn't stretch the recorded work day later.

    lat/lng: same best-effort browser geolocation as _attendance_checkin()
    (HR-visibility request, 2026-09-28) -- overwritten on every checkout call
    the same way checkout_time itself always is (last logout wins), None if
    unavailable/denied/no browser, never blocks the checkout."""
    d = today_ist()
    t = _clamp_work_time(now_ist())
    ts = datetime.now(IST).isoformat(timespec="seconds")
    conn = _attendance_conn()
    with conn:
        cur = conn.cursor()
        cur.execute(
            # checkout_time always advances to this call's time (existing
            # "latest logout wins" behavior). checkout_lat/lng use COALESCE
            # the other direction on purpose: a checkout call placed with no
            # location (e.g. the WhatsApp bot's set_attendance tool, which
            # has no browser) must not blank out a location a PRIOR checkout
            # call this same day already captured.
            """INSERT INTO daily_attendance (user_id, date, checkout_time, checkout_lat, checkout_lng, checkout_auto)
               VALUES (?, ?, ?, ?, ?, 0)
               ON CONFLICT(user_id, date) DO UPDATE SET
                   checkout_time = excluded.checkout_time,
                   checkout_auto = 0,
                   checkout_lat = COALESCE(excluded.checkout_lat, daily_attendance.checkout_lat),
                   checkout_lng = COALESCE(excluded.checkout_lng, daily_attendance.checkout_lng)""",
            (user_id, d, t, lat, lng),
        )
        conn.execute(
            "INSERT INTO attendance (user_id, action, timestamp) VALUES (?, 'out', ?)",
            (user_id, ts),
        )
        cur.execute(
            "SELECT checkout_time FROM daily_attendance WHERE user_id=? AND date=?",
            (user_id, d),
        )
        row = cur.fetchone()
        stored_checkout = row[0] if row else t
    conn.close()
    return d, stored_checkout


def _attendance_undo_checkout(user_id: str):
    """Clears today's checkout_time (+ its lat/lng) back to NULL -- recovery
    for an accidental Check Out click (2026-09-30). Without this, a mistaken
    checkout is only self-correcting if the person clicks Check Out again
    later for real ("last wins") -- if they don't, the wrong early time sits
    there permanently, since sweep_stale_checkouts() (task_scheduler.py) only
    ever fires when checkout_time IS NULL and so never revisits one that's
    already set. Self-service, same as checkin/checkout: no-op (rowcount 0)
    if there's nothing to undo today."""
    d = today_ist()
    conn = _attendance_conn()
    with conn:
        cur = conn.execute(
            """UPDATE daily_attendance
               SET checkout_time = NULL, checkout_lat = NULL, checkout_lng = NULL,
                   checkout_auto = 0
               WHERE user_id = ? AND date = ? AND checkout_time IS NOT NULL""",
            (user_id, d),
        )
        undone = cur.rowcount > 0
    conn.close()
    return d, undone


def _attendance_set_location(user_id: str, kind: str, lat: float, lng: float):
    """Attach (or replace) the location on an EXISTING check-in or check-out
    without touching any time. For the person who checked in with location
    blocked/unavailable and wants to add it afterwards -- the dashboard's
    "Add check-in location" / "Add check-out location" buttons. Only updates
    when that side of today's row actually exists, so it can't invent a
    check-in or check-out. Returns (date, updated)."""
    if kind == "checkin":
        sql = ("UPDATE daily_attendance SET checkin_lat = ?, checkin_lng = ? "
               "WHERE user_id = ? AND date = ? AND checkin_time IS NOT NULL")
    elif kind == "checkout":
        sql = ("UPDATE daily_attendance SET checkout_lat = ?, checkout_lng = ? "
               "WHERE user_id = ? AND date = ? AND checkout_time IS NOT NULL")
    else:
        raise ValueError("kind must be 'checkin' or 'checkout'")
    d = today_ist()
    conn = _attendance_conn()
    with conn:
        updated = conn.execute(sql, (lat, lng, user_id, d)).rowcount > 0
    conn.close()
    return d, updated


# ── Attendance routes ─────────────────────────────────────────────────────────

def _parse_latlng(body):
    """Best-effort float parse of optional lat/lng fields off a request body
    -- (None, None) for anything missing/malformed, never raises. Also
    range-checked (real latitudes/longitudes only) so a garbled client value
    can't land in the DB as a number that only looks plausible."""
    try:
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng
    except (TypeError, ValueError):
        pass
    return None, None


@attendance_bp.route("/api/attendance/checkin", methods=["POST"])
def attendance_checkin():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    lat, lng = _parse_latlng(body)
    date_ist, checkin_time = _attendance_checkin(user_id, lat, lng)
    return jsonify({
        "success": True,
        "user_id": user_id,
        "date": date_ist,
        "checkin_time": checkin_time,
        "timezone": "IST",
    })


@attendance_bp.route("/api/attendance/checkout", methods=["POST"])
def attendance_checkout():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    lat, lng = _parse_latlng(body)
    date_ist, checkout_time = _attendance_checkout(user_id, lat, lng)
    return jsonify({
        "success": True,
        "user_id": user_id,
        "date": date_ist,
        "checkout_time": checkout_time,
        "timezone": "IST",
    })


@attendance_bp.route("/api/attendance/location", methods=["POST"])
def attendance_set_location():
    """Add a location to today's existing check-in or check-out. Unlike
    checkin/checkout this requires the caller's real session cookie to match
    user_id -- it edits a record HR relies on after the fact, with no time
    change to anchor it."""
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    kind = str(body.get("kind", "")).strip().lower()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    from routes.auth import _verify_session
    if _verify_session(request.cookies.get("session_token", "")) != user_id:
        return jsonify({"error": "login required"}), 403
    if kind not in ("checkin", "checkout"):
        return jsonify({"error": "kind must be 'checkin' or 'checkout'"}), 400
    lat, lng = _parse_latlng(body)
    if lat is None:
        return jsonify({"error": "a valid lat and lng are required"}), 400
    date_ist, updated = _attendance_set_location(user_id, kind, lat, lng)
    if not updated:
        return jsonify({"error": f"no {kind} recorded today to attach a location to"}), 404
    return jsonify({"success": True, "user_id": user_id, "date": date_ist, "kind": kind,
                    "lat": lat, "lng": lng})


@attendance_bp.route("/api/attendance/undo-checkout", methods=["POST"])
def attendance_undo_checkout():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    date_ist, undone = _attendance_undo_checkout(user_id)
    return jsonify({
        "success": True,
        "user_id": user_id,
        "date": date_ist,
        "undone": undone,
        "timezone": "IST",
    })


@attendance_bp.route("/api/attendance/ping", methods=["POST"])
def attendance_ping():
    body = _attendance_payload()
    user_id = str(body.get("user_id", "")).strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    last_seen = _attendance_ping(user_id)
    return jsonify({"success": True, "last_seen_at": last_seen})


def sweep_stale_checkouts(stale_after_seconds: int = 240):
    """Called by task_scheduler.py every ~3min. For today's rows with a
    checkin but no checkout, whose last_seen_at is older than
    stale_after_seconds, mark checkout_time = last_seen_at -- i.e. the
    employee's tab went away (closed / crashed / lost network) and their
    heartbeat simply stopped. Explicit Logout (attendance_checkout above)
    already handles the instant case; this is the fallback for everyone
    who just closes the tab."""
    d = today_ist()
    now_dt = datetime.strptime(now_ist(), "%H:%M:%S")
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT user_id, last_seen_at FROM daily_attendance
           WHERE date=? AND checkin_time IS NOT NULL
             AND checkout_time IS NULL AND last_seen_at IS NOT NULL""",
        (d,),
    )
    rows = cur.fetchall()
    swept = 0
    with conn:
        for user_id, last_seen_at in rows:
            try:
                last_seen_dt = datetime.strptime(last_seen_at, "%H:%M:%S")
            except ValueError:
                continue
            if (now_dt - last_seen_dt).total_seconds() >= stale_after_seconds:
                conn.execute(
                    "UPDATE daily_attendance SET checkout_time=?, checkout_auto=1 "
                    "WHERE user_id=? AND date=?",
                    (_clamp_work_time(last_seen_at), user_id, d),
                )
                swept += 1
    conn.close()
    return swept


@attendance_bp.route("/api/attendance/summary", methods=["GET"])
def attendance_summary():
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT date, checkin_time, checkout_time,
                  checkin_lat, checkin_lng, checkout_lat, checkout_lng
           FROM daily_attendance WHERE user_id=? ORDER BY date DESC""",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return jsonify({
        "user_id": user_id,
        "timezone": "IST",
        "records": [
            {"date": r[0], "checkin_time": r[1], "checkout_time": r[2],
             "checkin_lat": r[3], "checkin_lng": r[4],
             "checkout_lat": r[5], "checkout_lng": r[6]}
            for r in rows
        ],
    })


@attendance_bp.route("/api/attendance/today", methods=["GET"])
def attendance_today():
    # Optional ?date=YYYY-MM-DD to view a past day's attendance (Live
    # Attendance dashboard date picker); defaults to today when omitted,
    # same as before this param existed.
    date_ist = request.args.get("date", "").strip() or today_ist()
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT user_id, checkin_time, checkout_time,
                  checkin_lat, checkin_lng, checkout_lat, checkout_lng
           FROM daily_attendance WHERE date=?""",
        (date_ist,),
    )
    rows = cur.fetchall()
    conn.close()
    records = [
        {"user_id": r[0], "date": date_ist, "checkin_time": r[1], "checkout_time": r[2],
         "checkin_lat": r[3], "checkin_lng": r[4],
         "checkout_lat": r[5], "checkout_lng": r[6]}
        for r in rows
    ]
    return jsonify({"date": date_ist, "timezone": "IST", "records": records})


@attendance_bp.route("/api/attendance/logs", methods=["GET"])
def attendance_logs():
    if not _verified_admin():
        return jsonify({"error": "Unauthorized"}), 403
    from db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, action, timestamp FROM attendance ORDER BY timestamp DESC LIMIT 500")
    logs = [{"id": r[0], "user_id": r[1], "action": r[2], "timestamp": r[3]} for r in cursor.fetchall()]
    conn.close()
    return jsonify({"logs": logs})


@attendance_bp.route("/api/attendance/export", methods=["GET"])
def attendance_export():
    if not _verified_admin():
        return "Unauthorized", 403

    # daily_attendance (not the raw attendance event log) is already one row
    # per user per day with checkin_time/checkout_time -- both stored as
    # plain HH:MM:SS IST (see now_ist()/_clamp_work_time() above), so no
    # timezone conversion is needed here.
    from db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT date, user_id, checkin_time, checkout_time,
                  checkin_lat, checkin_lng, checkout_lat, checkout_lng
           FROM daily_attendance ORDER BY date DESC, user_id"""
    )
    rows = cursor.fetchall()

    # Standup task counts per (user_id, date), same definitions the Velocity
    # chart uses (routes/ops.py::get_velocity) so the two stay consistent:
    # completed = marked done that day; carried = still-pending tasks that
    # were carried in from an earlier day (not a new task started that day).
    cursor.execute(
        """SELECT user_id, date,
                  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
           FROM standup_tasks
           WHERE status != 'deleted'
           GROUP BY user_id, date"""
    )
    task_counts = {(r[0], r[1]): (r[2] or 0, r[3] or 0) for r in cursor.fetchall()}
    conn.close()

    emp_map = {}
    try:
        emps = _load_employees().get("employees", [])
        for e in emps:
            emp_map[e["id"]] = e["name"]
            if e.get("whatsapp"):
                emp_map[e["whatsapp"]] = e["name"]
                emp_map[e["whatsapp"].replace('+', '')] = e["name"]
    except Exception:
        pass

    def format_user(uid):
        if uid in emp_map:
            return emp_map[uid]
        if re.match(r"^\+?\d{10,15}$", uid):
            return f"WhatsApp ({uid[-4:]})"
        return uid

    def _maps_link(lat, lng):
        return f"https://www.google.com/maps?q={lat},{lng}" if lat is not None and lng is not None else ""

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Date", "In", "Out", "Employee", "Tasks Completed", "Tasks Carried Forward",
                 "Location In", "Location Out"])
    for date, user_id, checkin_time, checkout_time, cin_lat, cin_lng, cout_lat, cout_lng in rows:
        completed, carried = task_counts.get((user_id, date), (0, 0))
        cw.writerow([date, checkin_time or "", checkout_time or "", format_user(user_id), completed, carried,
                     _maps_link(cin_lat, cin_lng), _maps_link(cout_lat, cout_lng)])

    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=claude_attendance_logs.csv"}
    )


# Weekday-only Full Day / Half Day / Leave threshold. Anything shorter but
# with a real checkin is a Half Day, a weekday with no checkin at all is a
# Leave. Raised 8 -> 9 hours per explicit user request, 2026-09-28 (same
# change also defines "overtime" below: hours worked beyond this).
_FULL_DAY_HOURS = 9.0


_HM_FMT = '[h]"h" mm"m"'


def _hm(hours):
    """Decimal hours -> an Excel time value (days) rounded to the minute, so a
    cell shows "10h 31m" via _HM_FMT yet stays a real number (sums, filters)."""
    if hours is None:
        return None
    return round(float(hours) * 60) / 60 / 24


def _overtime_hours(hrs):
    """Hours worked beyond _FULL_DAY_HOURS on one day, or None if hrs is
    None (no checkout yet / no checkin at all -- nothing to compute
    overtime from). 0.0 for a normal day, never negative."""
    if hrs is None:
        return None
    return round(max(0.0, hrs - _FULL_DAY_HOURS), 2)
_INACTIVE_STATUSES = {"inactive", "disabled", "left", "removed", "archived", "former"}


def _hours_and_day_type(checkin, checkout, is_today, is_approved_leave=False, is_holiday=False):
    """(hours:float|None, label:str) for one (checkin_time, checkout_time)
    pair, both plain 'HH:MM:SS' IST or falsy. No midnight-rollover handling
    needed -- both are already clamped inside the same work-day window by
    _clamp_work_time() before they're ever stored. is_approved_leave
    distinguishes a day covered by an HR-approved leave request (leave
    management system, 2026-09-30) from a plain unexplained absence --
    both still mean "no checkin", but only the approved kind is excluded
    from the attendance-% denominator (see `considered` below, unchanged
    on purpose: it only ever sums the plain "Leave" bucket)."""
    if not checkin:
        # A public holiday on the office calendar is a day off for everyone:
        # not a Leave, not an absence, and it stays out of Attendance %.
        if is_holiday:
            return None, "Holiday"
        return None, ("Leave (Approved)" if is_approved_leave else "Leave")
    if not checkout:
        return None, ("In Progress" if is_today else "Incomplete")
    try:
        h1, m1, s1 = (int(p) for p in checkin.split(":"))
        h2, m2, s2 = (int(p) for p in checkout.split(":"))
        hrs = ((h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)) / 3600.0
    except Exception:
        return None, "Incomplete"
    if hrs < 0:
        return None, "Incomplete"
    return round(hrs, 2), ("Full Day" if hrs >= _FULL_DAY_HOURS else "Half Day")


def _parse_time_obj(hhmmss):
    """'HH:MM:SS' -> a real datetime.time object (so Excel treats it as an
    actual time value, sortable/filterable, not a left-aligned text
    string) -- None if missing or unparseable."""
    if not hhmmss:
        return None
    try:
        h, m, s = (int(p) for p in hhmmss.split(":"))
        return dt_time(h, m, s)
    except Exception:
        return None


def _attendance_credit(day_type):
    """Half Day leave best practice (ExcelDemy/Indzara/Clockify-style HR
    templates researched for this feature): a Half Day contributes 0.5 to
    an attendance-percentage numerator, a Full Day contributes 1.0, and
    Leave/Incomplete contribute 0. "In Progress" (today, still ongoing) is
    excluded from both the numerator and the denominator entirely --
    there's no final answer for it yet."""
    if day_type == "Full Day":
        return 1.0
    if day_type == "Half Day":
        return 0.5
    return 0.0


@attendance_bp.route("/api/attendance/export-sheets", methods=["GET"])
def attendance_export_sheets():
    """An .xlsx workbook (not a flat CSV, so it can hold multiple sheets):
    an "All" sheet (same combined view the plain CSV export has, plus Day
    Type), a "Summary" sheet with one row per employee (Full/Half/Leave
    counts, attendance %, total hours), and one additional sheet per
    active employee with every WEEKDAY from their own start date through
    today filled in as its own row -- a day with no checkin at all shows
    as Leave rather than just being absent from the list.

    Each employee's sheet starts from config/employees.json's optional
    "joined_date" (YYYY-MM-DD) if set; otherwise from THAT employee's own
    earliest daily_attendance record (not a company-wide date), so a
    recently-onboarded employee doesn't get backfilled with Leave for
    months before they ever used the app. Set "joined_date" by hand in
    employees.json for anyone whose real hire date predates their first
    login, to backfill Leave correctly from the actual join date instead.

    Dates/times are written as REAL Excel date/time values (DD/MM/YYYY,
    HH:MM display format -- matches the dd/mm/yyyy convention already
    used app-wide, see gotcha #84), not plain text, so they sort/filter
    correctly in Excel instead of alphabetically. Attendance % is a real
    percentage number too, not a formatted string. Every sheet also gets
    an auto-filter on its header row and thin borders on every data cell."""
    if not _verified_admin():
        return "Unauthorized", 403

    import io
    import re as _re
    from datetime import date as _date, timedelta as _timedelta
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    from db import get_connection
    import leave_store
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT date, user_id, checkin_time, checkout_time,
                  checkin_lat, checkin_lng, checkout_lat, checkout_lng
           FROM daily_attendance ORDER BY date DESC, user_id"""
    )
    rows = cursor.fetchall()
    cursor.execute(
        """SELECT user_id, date,
                  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
           FROM standup_tasks
           WHERE status != 'deleted'
           GROUP BY user_id, date"""
    )
    task_counts = {(r[0], r[1]): (r[2] or 0, r[3] or 0) for r in cursor.fetchall()}
    cursor.execute(
        "SELECT user_id, start_date, end_date FROM employee_leave WHERE status='approved'"
    )
    approved_leave_rows = cursor.fetchall()
    # HR's office calendar: public holidays + WFH / in-person office days.
    # {date: {"holiday": title|None, "modes": [labels]}} for weekday lookups.
    cal_by_day = {}
    cursor.execute("SELECT kind, title, start_date, end_date FROM office_calendar "
                   "WHERE kind IN ('holiday','wfh','office')")
    for k_, t_, sd_, ed_ in cursor.fetchall():
        try:
            d_ = _date.fromisoformat(sd_)
            e_ = _date.fromisoformat(ed_)
        except ValueError:
            continue
        while d_ <= e_:
            slot = cal_by_day.setdefault(d_.isoformat(), {"holiday": None, "modes": []})
            if k_ == "holiday":
                slot["holiday"] = (t_ or "").strip() or "Holiday"
            else:
                lab = "Work from home" if k_ == "wfh" else "In-person office"
                if lab not in slot["modes"]:
                    slot["modes"].append(lab)
            d_ += _timedelta(days=1)
    # Comp-off ("+1 leave for every 24h overtime", leave management system,
    # gotcha #126) earned per calendar month -- same IST-shifted bucketing
    # leave_store.get_balance() itself uses for this table (comp_off_ledger.
    # created_at is stored UTC), so a conversion near the UTC/IST midnight
    # boundary lands in the same month here as it does in the employee's own
    # balance. Summed by (user, year, month) up front rather than re-querying
    # per employee per month inside the loop below.
    cursor.execute(
        f"SELECT user_id, days, {leave_store._COMP_OFF_IST_EXPR} AS ist_ts FROM comp_off_ledger"
    )
    comp_off_by_user_month = {}
    for c_uid, c_days, c_ist_ts in cursor.fetchall():
        try:
            c_key = (c_uid, int(c_ist_ts[:4]), int(c_ist_ts[5:7]))
        except (TypeError, ValueError):
            continue
        comp_off_by_user_month[c_key] = comp_off_by_user_month.get(c_key, 0.0) + (c_days or 0.0)
    conn.close()

    def _is_holiday(dstr):
        return bool((cal_by_day.get(dstr) or {}).get("holiday"))

    def _work_mode(dstr):
        slot = cal_by_day.get(dstr) or {}
        bits = ([f"Holiday: {slot['holiday']}"] if slot.get("holiday") else []) + list(slot.get("modes") or [])
        return " / ".join(bits)

    def _is_approved_leave_day(uid, dstr):
        return any(u == uid and sd <= dstr <= ed for u, sd, ed in approved_leave_rows)

    emp_map = {}
    active_employees = []
    try:
        for e in _load_employees().get("employees", []):
            emp_map[e["id"]] = e["name"]
            if e.get("whatsapp"):
                emp_map[e["whatsapp"]] = e["name"]
                emp_map[e["whatsapp"].replace("+", "")] = e["name"]
            if str(e.get("status", "active")).strip().lower() not in _INACTIVE_STATUSES:
                active_employees.append(e)
    except Exception:
        logger.exception("attendance_export_sheets: employee load failed")

    def format_user(uid):
        if uid in emp_map:
            return emp_map[uid]
        if re.match(r"^\+?\d{10,15}$", uid):
            return f"WhatsApp ({uid[-4:]})"
        return uid

    by_user_date = {}
    earliest_by_user = {}
    for d, uid, cin, cout, *_loc in rows:
        by_user_date[(uid, d)] = (cin, cout)
        if uid not in earliest_by_user or d < earliest_by_user[uid]:
            earliest_by_user[uid] = d

    today = today_ist()
    today_d = _date.fromisoformat(today)

    wb = Workbook()
    HEADER_FILL = PatternFill("solid", fgColor="1F2937")
    HEADER_FONT = Font(color="FFFFFF", bold=True)
    TITLE_FONT = Font(bold=True, size=13)
    META_FONT = Font(italic=True, color="4B5563")
    SUMMARY_LABEL_FONT = Font(bold=True)
    THIN = Side(style="thin", color="D1D5DB")
    THIN_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    DATE_FMT = "DD/MM/YYYY"
    TIME_FMT = "HH:MM"
    DAY_TYPE_FILLS = {
        "Holiday": PatternFill("solid", fgColor="DBEAFE"),
        "Full Day": PatternFill("solid", fgColor="D1FAE5"),
        "Half Day": PatternFill("solid", fgColor="FEF3C7"),
        "Leave": PatternFill("solid", fgColor="FEE2E2"),
        "Leave (Approved)": PatternFill("solid", fgColor="FBCFE8"),
    }

    def style_header_cells(ws, headers, row):
        for col_idx, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=col_idx, value=h)
            c.font = HEADER_FONT
            c.fill = HEADER_FILL
            c.alignment = Alignment(horizontal="center")
            c.border = THIN_BORDER

    def style_header(ws, headers, row=1):
        style_header_cells(ws, headers, row)
        # A plain coordinate STRING, not ws.cell(...).coordinate -- calling
        # .cell() to merely read a coordinate still reserves that cell in
        # openpyxl's internal sheet dimensions, which silently bumps
        # max_row and shifts every later ws.append() down by one row.
        ws.freeze_panes = f"A{row + 1}"

    def border_row(ws, row, ncols):
        for c in range(1, ncols + 1):
            ws.cell(row=row, column=c).border = THIN_BORDER

    def set_widths(ws, widths):
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # ── Sheet 1: "All" -- same combined view as the plain CSV export,
    # plus a Day Type column. Unlike the per-employee sheets below, this
    # one is NOT filled in with missing-day rows -- it's one row per real
    # daily_attendance record, same as before.
    ws_all = wb.active
    ws_all.title = "All"
    ws_all.sheet_properties.tabColor = "374151"
    def _maps_link(lat, lng):
        return f"https://www.google.com/maps?q={lat},{lng}" if lat is not None and lng is not None else None

    style_header(ws_all, ["Date", "In", "Out", "Employee", "Tasks Completed",
                          "Tasks Carried Forward", "Day Type", "Location In", "Location Out", "Work Mode"])
    for d, uid, cin, cout, cin_lat, cin_lng, cout_lat, cout_lng in rows:
        completed, carried = task_counts.get((uid, d), (0, 0))
        _, day_type = _hours_and_day_type(cin, cout, d == today,
                                          is_approved_leave=_is_approved_leave_day(uid, d),
                                          is_holiday=_is_holiday(d))
        cin_obj, cout_obj = _parse_time_obj(cin), _parse_time_obj(cout)
        ws_all.append([_date.fromisoformat(d), cin_obj, cout_obj, format_user(uid),
                       completed, carried, day_type, "", "", _work_mode(d)])
        r = ws_all.max_row
        ws_all.cell(row=r, column=1).number_format = DATE_FMT
        if cin_obj is not None:
            ws_all.cell(row=r, column=2).number_format = TIME_FMT
        if cout_obj is not None:
            ws_all.cell(row=r, column=3).number_format = TIME_FMT
        fill = DAY_TYPE_FILLS.get(day_type)
        if fill:
            ws_all.cell(row=r, column=7).fill = fill
        in_link = _maps_link(cin_lat, cin_lng)
        if in_link:
            c = ws_all.cell(row=r, column=8, value="View")
            c.hyperlink = in_link
            c.font = Font(color="2563EB", underline="single")
        out_link = _maps_link(cout_lat, cout_lng)
        if out_link:
            c = ws_all.cell(row=r, column=9, value="View")
            c.hyperlink = out_link
            c.font = Font(color="2563EB", underline="single")
        border_row(ws_all, r, 10)
    if ws_all.max_row > 1:
        ws_all.auto_filter.ref = f"A1:J{ws_all.max_row}"
    set_widths(ws_all, [12, 10, 10, 18, 14, 18, 12, 12, 12, 26])

    # ── Per-employee sheets: each starts from its own real start date
    # (joined_date override, else that employee's own earliest
    # daily_attendance record, else today for a brand-new nobody's-logged-
    # in-yet employee) through today, every WEEKDAY filled in as a row.
    used_titles = set()
    emp_summaries = []  # collected for the "Summary" overview sheet below
    for emp in active_employees:
        uid = emp["id"]
        name = emp.get("name") or uid
        joined = str(emp.get("joined_date") or "").strip()
        start = None
        if joined:
            try:
                start = _date.fromisoformat(joined)
            except ValueError:
                logger.warning("attendance_export_sheets: bad joined_date %r for %s", joined, uid)
        if start is None:
            own_earliest = earliest_by_user.get(uid)
            start = _date.fromisoformat(own_earliest) if own_earliest else today_d
        if start > today_d:
            start = today_d
        end = today_d

        title = _re.sub(r'[\[\]:\*\?/\\]', "", name)[:31] or uid
        base_title, n = title, 2
        while title in used_titles:
            title = f"{base_title[:28]}~{n}"
            n += 1
        used_titles.add(title)

        ws = wb.create_sheet(title=title)
        ws.cell(row=1, column=1, value=f"Attendance -- {name}").font = TITLE_FONT
        meta_bits = [b for b in [emp.get("role"), emp.get("department")] if b]
        meta = " | ".join(meta_bits + [
            f"Period: {start.strftime('%d/%m/%Y')} to {end.strftime('%d/%m/%Y')}"
        ])
        ws.cell(row=2, column=1, value=meta).font = META_FONT
        style_header(ws, ["Date", "Day", "In", "Out", "Hours Worked", "Overtime Hours",
                          "Day Type", "Tasks Completed", "Tasks Carried Forward", "Work Mode"], row=4)

        counts = {"Full Day": 0, "Half Day": 0, "Leave": 0, "Incomplete": 0, "In Progress": 0,
                  "Holiday": 0, "WFH": 0}
        total_hours = 0.0
        total_overtime = 0.0
        total_ot_office = 0.0
        credit_total = 0.0
        # Per-calendar-month breakdown, in the order months are first seen
        # (chronological, since d walks forward) -- a plain dict already
        # preserves insertion order in this Python version.
        months = {}
        d = start
        while d <= end:
            if d.weekday() < 5:  # Mon-Fri only -- weekends aren't a "Leave"
                dstr = d.isoformat()
                cin, cout = by_user_date.get((uid, dstr), (None, None))
                hrs, day_type = _hours_and_day_type(
                    cin, cout, dstr == today,
                    is_approved_leave=_is_approved_leave_day(uid, dstr),
                    is_holiday=_is_holiday(dstr))
                overtime = _overtime_hours(hrs)
                completed, carried = task_counts.get((uid, dstr), (0, 0))
                cin_obj, cout_obj = _parse_time_obj(cin), _parse_time_obj(cout)
                ws.append([d, d.strftime("%A"), cin_obj, cout_obj,
                          _hm(hrs), _hm(overtime), day_type, completed, carried, _work_mode(dstr)])
                r = ws.max_row
                ws.cell(row=r, column=1).number_format = DATE_FMT
                if cin_obj is not None:
                    ws.cell(row=r, column=3).number_format = TIME_FMT
                if cout_obj is not None:
                    ws.cell(row=r, column=4).number_format = TIME_FMT
                if hrs is not None:
                    ws.cell(row=r, column=5).number_format = _HM_FMT
                if overtime is not None:
                    ws.cell(row=r, column=6).number_format = _HM_FMT
                fill = DAY_TYPE_FILLS.get(day_type)
                if fill:
                    ws.cell(row=r, column=7).fill = fill
                border_row(ws, r, 10)
                counts[day_type] = counts.get(day_type, 0) + 1
                is_wfh = "Work from home" in ((cal_by_day.get(dstr) or {}).get("modes") or [])
                if is_wfh:
                    counts["WFH"] += 1
                # overtime done in the office: weekdays not marked work-from-home or holiday
                office_ot = (overtime if (overtime is not None and not is_wfh
                                          and not (cal_by_day.get(dstr) or {}).get("holiday")) else 0.0)
                total_ot_office += office_ot
                credit_total += _attendance_credit(day_type)
                if hrs is not None:
                    total_hours += hrs
                if overtime is not None:
                    total_overtime += overtime
                mkey = (d.year, d.month)
                mrow = months.setdefault(mkey, {
                    "Full Day": 0, "Half Day": 0, "Leave": 0, "Incomplete": 0,
                    "In Progress": 0, "hours": 0.0, "overtime": 0.0, "credit": 0.0,
                    "Holiday": 0, "WFH": 0, "ot_office": 0.0,
                })
                mrow["ot_office"] += office_ot
                if is_wfh:
                    mrow["WFH"] += 1
                mrow[day_type] = mrow.get(day_type, 0) + 1
                mrow["credit"] += _attendance_credit(day_type)
                if hrs is not None:
                    mrow["hours"] += hrs
                if overtime is not None:
                    mrow["overtime"] += overtime
            d += _timedelta(days=1)
        data_end_row = ws.max_row
        if data_end_row >= 5:
            ws.auto_filter.ref = f"A4:J{data_end_row}"
        set_widths(ws, [12, 12, 10, 10, 14, 14, 12, 14, 18, 26])

        considered = counts["Full Day"] + counts["Half Day"] + counts["Leave"] + counts["Incomplete"]
        pct = (credit_total / considered) if considered else None

        # ── Monthly Summary table -- one row per calendar month in range,
        # same metrics as the old single flat Summary block, broken out by
        # month (payroll/HR review is almost always done month by month).
        summary_row = data_end_row + 2
        ws.cell(row=summary_row, column=1, value="Monthly Summary").font = TITLE_FONT
        month_header_row = summary_row + 1
        # "Approved Leaves" = weekdays that month covered by an HR-approved
        # leave request (leave management system, gotcha #126) -- distinct
        # from "Leaves" (col D), which is a plain no-checkin absence with no
        # approved request behind it. "Comp-Off Earned" = leave days this
        # employee's overtime converted into that month (every 24 cumulative
        # OT hours = +1, see leave_store.run_overtime_conversion_sweep()) --
        # the "worked extra +1" the export is meant to surface.
        style_header_cells(ws, ["Month", "Full Days", "Half Days", "Leaves", "Incomplete",
                                "Working Days", "Total Hours", "Overtime Hours", "Attendance %",
                                "Approved Leaves", "Comp-Off Earned", "Holidays", "WFH Days",
                                "OT (Work From Office)"], month_header_row)
        mrow_idx = month_header_row
        comp_off_total = 0.0
        for (yr, mo), m in months.items():
            m_considered = m["Full Day"] + m["Half Day"] + m["Leave"] + m["Incomplete"]
            m_pct = (m["credit"] / m_considered) if m_considered else None
            m_comp_off = comp_off_by_user_month.get((uid, yr, mo), 0.0)
            comp_off_total += m_comp_off
            mrow_idx += 1
            ws.cell(row=mrow_idx, column=1, value=_date(yr, mo, 1).strftime("%B %Y"))
            ws.cell(row=mrow_idx, column=2, value=m["Full Day"])
            ws.cell(row=mrow_idx, column=3, value=m["Half Day"])
            ws.cell(row=mrow_idx, column=4, value=m["Leave"])
            ws.cell(row=mrow_idx, column=5, value=m["Incomplete"])
            ws.cell(row=mrow_idx, column=6, value=m_considered)
            hrs_cell = ws.cell(row=mrow_idx, column=7, value=_hm(m["hours"]))
            hrs_cell.number_format = _HM_FMT
            ot_cell = ws.cell(row=mrow_idx, column=8, value=_hm(m["overtime"]))
            ot_cell.number_format = _HM_FMT
            pct_cell = ws.cell(row=mrow_idx, column=9, value=m_pct if m_pct is not None else "N/A")
            if m_pct is not None:
                pct_cell.number_format = "0.0%"
            ws.cell(row=mrow_idx, column=10, value=m.get("Leave (Approved)", 0))
            comp_off_cell = ws.cell(row=mrow_idx, column=11, value=round(m_comp_off, 1))
            comp_off_cell.number_format = "0.0"
            ws.cell(row=mrow_idx, column=12, value=m.get("Holiday", 0))
            ws.cell(row=mrow_idx, column=13, value=m.get("WFH", 0))
            ws.cell(row=mrow_idx, column=14, value=_hm(m.get("ot_office", 0.0))).number_format = _HM_FMT
            border_row(ws, mrow_idx, 14)
        # A bold "Total (All Time)" row underneath every month, using the
        # same aggregate counts the old single Summary block reported.
        total_row = mrow_idx + 1
        total_cells = [
            "Total (All Time)", counts["Full Day"], counts["Half Day"], counts["Leave"],
            counts["Incomplete"], considered, _hm(total_hours), _hm(total_overtime),
            pct if pct is not None else "N/A",
            counts.get("Leave (Approved)", 0), round(comp_off_total, 1),
            counts.get("Holiday", 0), counts.get("WFH", 0), _hm(total_ot_office),
        ]
        for col, val in enumerate(total_cells, start=1):
            c = ws.cell(row=total_row, column=col, value=val)
            c.font = SUMMARY_LABEL_FONT
        ws.cell(row=total_row, column=7).number_format = _HM_FMT
        ws.cell(row=total_row, column=8).number_format = _HM_FMT
        ws.cell(row=total_row, column=14).number_format = _HM_FMT
        if pct is not None:
            ws.cell(row=total_row, column=9).number_format = "0.0%"
        ws.cell(row=total_row, column=11).number_format = "0.0"
        border_row(ws, total_row, 14)
        ws.column_dimensions["N"].width = max(ws.column_dimensions["N"].width or 0, 22)
        ws.column_dimensions["J"].width = max(ws.column_dimensions["J"].width or 0, 16)
        ws.column_dimensions["K"].width = max(ws.column_dimensions["K"].width or 0, 16)

        bal = leave_store.get_balance(uid)
        emp_summaries.append({
            "name": name, "start": start, "full": counts["Full Day"],
            "half": counts["Half Day"], "leave": counts["Leave"],
            "incomplete": counts["Incomplete"], "hours": _hm(total_hours),
            "overtime": _hm(total_overtime), "pct": pct,
            "leave_used": bal["used"], "comp_earned": bal["comp_earned"],
            "leave_remaining": bal["remaining"],
        })

    # ── "Summary" overview sheet: one row per employee, placed right after
    # "All" so it's the second tab (before diving into individual sheets).
    ws_sum = wb.create_sheet(title="Summary", index=1)
    ws_sum.sheet_properties.tabColor = "D97706"
    ws_sum.cell(row=1, column=1, value="Attendance Summary").font = TITLE_FONT
    ws_sum.cell(row=2, column=1, value=f"As of {today_d.strftime('%d/%m/%Y')}").font = META_FONT
    ws_sum.cell(row=3, column=1, value="Legend:").font = META_FONT
    for j, (label, fill) in enumerate(DAY_TYPE_FILLS.items(), start=2):
        c = ws_sum.cell(row=3, column=j, value=label)
        c.fill = fill
        c.alignment = Alignment(horizontal="center")
    ws_sum.cell(
        row=4, column=1,
        value=(f"Full Day = worked {_FULL_DAY_HOURS:g}+ hours  |  Half Day = worked under "
               f"{_FULL_DAY_HOURS:g} hours (but checked in)  |  Leave = no check-in that weekday  |  "
               "Leave (Approved) = no check-in, covered by an HR-approved leave request "
               "(does not count against Attendance %)  |  "
               "Holiday = public holiday set on the office calendar (no check-in needed, not counted "
               "in working days or Attendance %)  |  "
               "Work Mode column = what HR set that day: work from home / in-person office / holiday  |  "
               "Incomplete = checked in but no checkout was logged  |  "
               "In Progress = still checked in today, not final yet  |  "
               f"Overtime = hours worked beyond {_FULL_DAY_HOURS:g} on a day"),
    ).font = META_FONT
    style_header(ws_sum, ["Employee", "Period Start", "Full Days", "Half Days",
                          "Leaves", "Incomplete", "Attendance %", "Total Hours Worked",
                          "Total Overtime Hours", "Leave Used (Formal)",
                          "Comp-Off Earned", "Leave Remaining"], row=6)
    for s in emp_summaries:
        ws_sum.append([s["name"], s["start"], s["full"], s["half"], s["leave"],
                      s["incomplete"], s["pct"] if s["pct"] is not None else "N/A",
                      s["hours"], s["overtime"], s["leave_used"], s["comp_earned"],
                      s["leave_remaining"]])
        r = ws_sum.max_row
        ws_sum.cell(row=r, column=2).number_format = DATE_FMT
        ws_sum.cell(row=r, column=8).number_format = _HM_FMT
        ws_sum.cell(row=r, column=9).number_format = _HM_FMT
        if s["pct"] is not None:
            ws_sum.cell(row=r, column=7).number_format = "0.0%"
        border_row(ws_sum, r, 12)
    if ws_sum.max_row >= 7:
        ws_sum.auto_filter.ref = f"A6:L{ws_sum.max_row}"
    set_widths(ws_sum, [18, 14, 10, 10, 10, 10, 14, 16, 16, 16, 14, 14])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment;filename=lumina_attendance.xlsx"},
    )


# ── Employee routes ───────────────────────────────────────────────────────────

@attendance_bp.route("/api/employees", methods=["GET"])
def get_employees():
    """Returns all employees with their current status."""
    data = _load_employees()
    return jsonify(data)


@attendance_bp.route("/api/employees/checkin", methods=["POST"])
def employee_checkin():
    """
    Records an employee check-in or check-out.
    Body: { emp_id OR whatsapp, action: 'in'|'out', notes? }
    """
    body     = request.get_json(silent=True) or {}
    emp_id   = body.get("emp_id", "").strip()
    whatsapp = body.get("whatsapp", "").strip()
    action   = body.get("action", "in").strip()
    notes    = body.get("notes", "")

    if not emp_id and not whatsapp:
        return jsonify({"error": "emp_id or whatsapp required"}), 400

    data = _load_employees()
    found = None
    for emp in data["employees"]:
        if emp_id and emp.get("id") == emp_id:
            found = emp
            break
        if whatsapp and emp.get("whatsapp") == whatsapp:
            found = emp
            break

    if not found:
        return jsonify({"error": "Employee not found"}), 404

    entry = {
        "timestamp": datetime.now(IST).isoformat(timespec="seconds"),
        "action":    action,
        "notes":     notes,
    }
    found.setdefault("checkins", []).append(entry)
    found["last_action"] = action
    found["last_seen"]   = entry["timestamp"]

    _save_employees(data)
    if action == "out":
        date_ist, time_ist = _attendance_checkout(found.get("id", ""))
    else:
        date_ist, time_ist = _attendance_checkin(found.get("id", ""))
    logger.info(f"Employee {found['name']} checked {action} at {entry['timestamp']}")

    return jsonify({
        "success":  True,
        "employee": found["name"],
        "action":   action,
        "time":     entry["timestamp"],
        "date":     date_ist,
        "time_ist": time_ist,
        "timezone": "IST",
    })


@attendance_bp.route("/api/employees/summary", methods=["GET"])
def employee_summary():
    """Returns today's attendance summary."""
    data  = _load_employees()
    today = today_ist()
    conn = _attendance_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, checkin_time, checkout_time FROM daily_attendance WHERE date=?",
        (today,),
    )
    attendance_map = {r[0]: {"checkin_time": r[1], "checkout_time": r[2]} for r in cur.fetchall()}
    conn.close()

    summary = []
    for emp in data["employees"]:
        daily = attendance_map.get(emp.get("id", ""), {})
        checkin_time = daily.get("checkin_time")
        checkout_time = daily.get("checkout_time")
        if checkin_time and not checkout_time:
            status = "in"
        elif checkin_time and checkout_time:
            status = "out"
        else:
            status = "not checked in"
        summary.append({
            "emp_id":        emp.get("id", ""),
            "name":          emp["name"],
            "role":          emp["role"],
            "department":    emp["department"],
            "status":        status,
            "checkin_time":  checkin_time,
            "checkout_time": checkout_time,
            "today_logs":    [],
        })

    return jsonify({"date": today, "employees": summary, "total": len(summary)})
