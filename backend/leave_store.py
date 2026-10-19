"""Employee leave / holiday windows + overtime-to-comp-off conversion
(CLAUDE.md gotcha #119, extended by the leave management system,
2026-09-30 — see docs/superpowers/specs/2026-09-30-leave-management-design.md).

A row in `employee_leave` is an inclusive [start_date, end_date] window
with a status: 'pending' -> awaiting HR (emp009) approval, 'approved' ->
counts toward balance usage and exempts the person from the standup lock /
login nudges, 'rejected' / 'cancelled' -> inert.

Only 'approved' rows ever exempt anything or count toward balance. This is
the one deliberate behavior change from the original gotcha #119 feature:
a WhatsApp-set leave used to apply instantly, and now creates a pending
request instead, closing the self-approval loophole a real balance system
would otherwise have.

This module only imports `db` and `utils`, so it is safe to import from
routes, the scheduler, and the agent alike.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date as _date, timedelta
from uuid import uuid4

from db import get_connection
from utils import today_ist

logger = logging.getLogger(__name__)

ANNUAL_BASE_DAYS = 15
OT_BASELINE_HOURS = 9.0   # overtime starts after this; kept in sync with routes/attendance.py::_FULL_DAY_HOURS
OT_CONVERSION_HOURS = 24.0
VALID_LEAVE_TYPES = {"full", "half"}
_LEAVE_DEDUCTION = {"full": 1.0, "half": 0.5}

# Who can decide a leave request: HR plus the two founders (Vidit, Kshitij).
# Anyone can never decide their OWN request, so when HR applies for leave the
# founders decide it. The first decision wins; a later one is told who decided.
HR_ID = "emp009"
APPROVER_IDS = ("emp009", "emp001", "emp004")
# Can SEE pending requests and the office calendar, never decide or edit (Abhinav).
VIEWER_IDS = ("emp003",)
MAX_SPAN_DAYS = 60            # one request can cover at most this many calendar days
MAX_PAST_DAYS = 60            # backdating limit
MAX_AHEAD_DAYS = 366


def approvers_for(applicant_id: str) -> list:
    """The people who may decide `applicant_id`'s request (never themselves)."""
    return [a for a in APPROVER_IDS if a != applicant_id]


def is_approver_for(decider_id: str, applicant_id: str) -> bool:
    return decider_id in approvers_for(applicant_id)


def _holiday_dates(start_date: str, end_date: str) -> set:
    """Public-holiday dates (HR's office calendar, kind 'holiday') inside the
    window. Best effort: an unreadable calendar just means none."""
    out: set = set()
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT start_date, end_date FROM office_calendar WHERE kind='holiday' "
            "AND NOT (end_date < ? OR start_date > ?)", (start_date, end_date)).fetchall()
        conn.close()
        for s, e in rows:
            d, last = _date.fromisoformat(s[:10]), _date.fromisoformat(e[:10])
            while d <= last:
                out.add(d.isoformat())
                d += timedelta(days=1)
    except Exception:
        pass
    return out


def working_days(start_date: str, end_date: str, leave_type: str = "full",
                 holidays: set | None = None) -> float:
    """Days a window actually costs: weekdays only, public holidays free."""
    try:
        sd, ed = _date.fromisoformat(start_date[:10]), _date.fromisoformat(end_date[:10])
    except Exception:
        return 0.0
    if ed < sd:
        sd, ed = ed, sd
    if holidays is None:
        holidays = _holiday_dates(sd.isoformat(), ed.isoformat())
    per_day = _LEAVE_DEDUCTION.get(leave_type, 1.0)
    total, d = 0.0, sd
    while d <= ed:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            total += per_day
        d += timedelta(days=1)
    return total


def _validate_window(start_date: str, end_date: str, leave_type: str) -> float:
    """Reject nonsense dates before anything is stored. Returns the working
    days the window costs."""
    try:
        sd, ed = _date.fromisoformat(start_date[:10]), _date.fromisoformat(end_date[:10])
    except Exception:
        raise ValueError("those dates aren't valid")
    today = _date.fromisoformat(today_ist())
    if (ed - sd).days + 1 > MAX_SPAN_DAYS:
        raise ValueError(f"one request can cover at most {MAX_SPAN_DAYS} days, split it up")
    if sd < today - timedelta(days=MAX_PAST_DAYS):
        raise ValueError(f"that is more than {MAX_PAST_DAYS} days in the past")
    if ed > today + timedelta(days=MAX_AHEAD_DAYS):
        raise ValueError("that is more than a year ahead")
    days = working_days(start_date, end_date, leave_type)
    if days <= 0:
        raise ValueError("those dates are only weekend or public holiday, no leave is needed")
    return days


def _shortfall(user_id: str, days: float) -> float:
    """How many of the requested days go beyond the balance still free
    (remaining minus what other pending requests already claim). >0 means the
    approver is being asked to grant unpaid days."""
    try:
        free = get_balance(user_id)["remaining"]
        conn = get_connection()
        rows = conn.execute("SELECT start_date, end_date, leave_type FROM employee_leave "
                            "WHERE user_id=? AND status='pending'", (user_id,)).fetchall()
        conn.close()
        free -= sum(working_days(r[0], r[1], r[2]) for r in rows)
        return round(max(0.0, days - free), 2)
    except Exception:
        return 0.0

_DDL = """CREATE TABLE IF NOT EXISTS employee_leave (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    created_by  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    status      TEXT DEFAULT 'approved',
    leave_type  TEXT DEFAULT 'full',
    approved_by TEXT DEFAULT NULL,
    approved_at TEXT DEFAULT NULL
)"""

_COMP_OFF_LEDGER_DDL = """CREATE TABLE IF NOT EXISTS comp_off_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    days            REAL NOT NULL,
    source_ot_hours REAL NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
)"""

# comp_off_ledger.created_at is UTC (SQLite datetime('now')), matching how
# every other timestamp in this module and in db.py is stored. The leave
# YEAR, though, is an IST calendar-year question -- so the read side shifts
# the stored UTC value by +5:30 before bucketing it, rather than storing an
# IST timestamp and breaking consistency with every other created_at in the
# schema. COALESCE keeps a row with an unparseable timestamp comparing on
# its raw string instead of silently vanishing.
_COMP_OFF_IST_EXPR = "COALESCE(datetime(created_at, '+330 minutes'), created_at)"

_OVERTIME_LEDGER_DDL = """CREATE TABLE IF NOT EXISTS overtime_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    date         TEXT NOT NULL,
    worked_hours REAL NOT NULL,
    ot_hours     REAL NOT NULL,
    converted_at TEXT DEFAULT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, date)
)"""


def _ensure(conn) -> None:
    """Idempotent — db.init_db() also creates this, but a companion
    endpoint or the agent may touch the table before a redeploy has
    re-run init."""
    conn.execute(_DDL)


def fmt_hm(hours) -> str:
    """Decimal hours -> a plain "10h 31m" / "35m" / "0m" string. Every place a
    person reads a duration should use this (decimals like 10.52 confuse)."""
    try:
        mins = int(round(float(hours or 0) * 60))
    except (TypeError, ValueError):
        return "0m"
    h, m = divmod(max(mins, 0), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# Overtime worked on a weekend, work-from-home day or public holiday never counts toward comp-off
# leave (HR marks them in office_calendar, kind 'wfh'/'holiday'). Appended to every
# "unconverted overtime" query so it applies retroactively: the moment HR
# adds/changes a WFH day, that day's ledger hours stop counting for everyone,
# with no data deleted and nothing to re-run. Carry rows use a 'carry-<uuid>'
# date, which sorts after any digit date, so BETWEEN never matches them.
# Only still-unconverted rows are affected -- days already granted stay.
_NOT_WFH_SQL = (" AND NOT EXISTS (SELECT 1 FROM office_calendar oc WHERE oc.kind IN ('wfh','holiday') "
                "AND overtime_ledger.date BETWEEN oc.start_date AND oc.end_date)"
                " AND (overtime_ledger.date LIKE 'carry-%' "
                "OR strftime('%w', overtime_ledger.date) NOT IN ('0','6'))")


def _ensure_overtime_ledger(conn) -> None:
    """Same idempotent-defensive-create idiom as _ensure() above, so every
    public function in this module tolerates being called before
    db.init_db() has ever run — not just the ones that happened to touch
    employee_leave/comp_off_ledger already."""
    conn.execute(_OVERTIME_LEDGER_DDL)
    # _NOT_WFH_SQL reads office_calendar (HR's calendar) in every pending sum.
    conn.execute("""CREATE TABLE IF NOT EXISTS office_calendar (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, title TEXT DEFAULT '',
        start_date TEXT NOT NULL, end_date TEXT NOT NULL, created_by TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')))""")


def _ensure_comp_off_ledger(conn) -> None:
    """Deliberately called from WRITE paths only. get_balance() used to run
    this DDL inline on every single read — and it is read on every leave
    page load, every WhatsApp balance check, and once per employee inside
    the Excel export loop. db.init_db() creates the table, so the read path
    tolerates its absence with a try/except instead."""
    conn.execute(_COMP_OFF_LEDGER_DDL)


# ── low-level WhatsApp-facing API (existing, gotcha #119) ──────────────

def set_leave(user_id: str, start_date: str, end_date: str,
              reason: str = "", created_by: str = "",
              status: str = "pending", leave_type: str = "full") -> dict:
    """Record a leave request for one person, defaulting to 'pending' --
    the WhatsApp bot's set_leave tool calls this.

    Raises ValueError if the window overlaps a leave already APPROVED for
    this person, exactly like apply_leave() does: an approved day that
    gets requested and approved twice is deducted from the pool twice, so
    the conversational path must not be a way around that guard.

    An overlapping still-PENDING request is instead *superseded* — marked
    'cancelled' (never deleted, so the audit trail survives) and replaced
    by this one. Restating a request over chat ("actually make Thursday a
    half day") should update it, not error.

    `leave_type` ('full' or 'half') is normalized permissively — an
    invalid value silently falls back to 'full' rather than raising,
    unlike apply_leave()'s strict validation."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    if status not in ("pending", "approved"):
        status = "pending"
    if leave_type not in VALID_LEAVE_TYPES:
        leave_type = "full"
    days = _validate_window(start_date, end_date, leave_type)
    short = _shortfall(user_id, days)
    conn = get_connection()
    try:
        _ensure(conn)
        if _overlapping_status(conn, user_id, start_date, end_date,
                               statuses=("approved",)):
            raise ValueError("overlaps a leave window already approved for this person")
        with conn:
            conn.execute(
                "UPDATE employee_leave SET status='cancelled' WHERE user_id=? "
                "AND status='pending' AND NOT (end_date < ? OR start_date > ?)",
                (user_id, start_date, end_date),
            )
            cur = conn.execute(
                "INSERT INTO employee_leave "
                "(user_id, start_date, end_date, reason, created_by, status, leave_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, start_date, end_date, reason or "", created_by or "", status,
                 leave_type),
            )
        row = {"id": cur.lastrowid, "user_id": user_id,
               "start_date": start_date, "end_date": end_date,
               "reason": reason or "", "status": status, "leave_type": leave_type,
               "days": days, "shortfall": short}
    finally:
        conn.close()
    _email_hr(row)
    return row


def cancel_pending_for_user(user_id: str, on_date: str | None = None) -> int:
    """Cancel every still-PENDING request belonging to `user_id` (or only
    the one(s) covering `on_date`). Returns how many were cancelled.

    Same semantics as cancel_leave(), just addressed by user instead of by
    row id: a status transition to 'cancelled', own rows only, pending
    only. An APPROVED row is never touched — undoing HR's decision is an
    HR action, not something an employee can do by messaging "I'm back",
    and a hard delete would also silently refund balance with no trace of
    the leave that was actually granted."""
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            if on_date:
                cur = conn.execute(
                    "UPDATE employee_leave SET status='cancelled' WHERE user_id=? "
                    "AND status='pending' AND start_date <= ? AND end_date >= ?",
                    (user_id, on_date, on_date),
                )
            else:
                cur = conn.execute(
                    "UPDATE employee_leave SET status='cancelled' "
                    "WHERE user_id=? AND status='pending'",
                    (user_id,),
                )
        return cur.rowcount or 0
    finally:
        conn.close()


def clear_leave(user_id: str, on_date: str | None = None) -> int:
    """DEPRECATED — retained only as a safe alias so nothing can reach the
    old hard-DELETE behaviour. It used to `DELETE FROM employee_leave ...
    status IN ('pending','approved')`, which wiped HR-approved rows
    outright (losing approved_by/approved_at) and silently refunded the
    balance. Delegates to cancel_pending_for_user() instead."""
    return cancel_pending_for_user(user_id, on_date=on_date)


def is_on_leave(user_id: str, date_str: str) -> bool:
    return user_id in on_leave_ids(date_str)


def on_leave_ids(date_str: str) -> set[str]:
    """Every user_id whose APPROVED leave window covers `date_str`. A
    pending request deliberately does not count here."""
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM employee_leave "
            "WHERE status='approved' AND start_date <= ? AND end_date >= ?",
            (date_str, date_str),
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        logger.exception("leave_store.on_leave_ids failed")
        return set()
    finally:
        conn.close()


def active_leave(user_id: str, date_str: str) -> dict | None:
    conn = get_connection()
    try:
        _ensure(conn)
        r = conn.execute(
            "SELECT start_date, end_date, reason FROM employee_leave "
            "WHERE user_id=? AND status='approved' "
            "AND start_date <= ? AND end_date >= ? "
            "ORDER BY end_date DESC LIMIT 1",
            (user_id, date_str, date_str),
        ).fetchone()
        if not r:
            return None
        return {"start_date": r[0], "end_date": r[1], "reason": r[2] or ""}
    finally:
        conn.close()


# ── approval workflow ────────────────────────────────────────────────────

def _overlapping_status(conn, user_id: str, start_date: str, end_date: str,
                        statuses: tuple[str, ...] = ("pending", "approved"),
                        exclude_id: int | None = None) -> str | None:
    """The status of an existing live leave row for this person whose
    window overlaps [start_date, end_date], or None. Shared by every
    writer (apply_leave, set_leave, approve_leave) so the double-booking
    guard can't be present on one path and missing on another — it
    originally only existed on apply_leave(), and only for 'approved'.

    An overlapping 'approved' row is reported in preference to a merely
    'pending' one, since it's the stricter finding."""
    sql = ("SELECT status FROM employee_leave WHERE user_id=? "
           "AND NOT (end_date < ? OR start_date > ?) "
           f"AND status IN ({','.join('?' * len(statuses))})")
    params: list = [user_id, start_date, end_date, *statuses]
    if exclude_id is not None:
        sql += " AND id<>?"
        params.append(exclude_id)
    sql += " ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def apply_leave(user_id: str, start_date: str, end_date: str,
                 leave_type: str = "full", reason: str = "",
                 created_by: str = "", email_subject: str = "",
                 email_body: str = "") -> dict:
    """Create a pending leave request. Raises ValueError on an invalid
    leave_type or a date range that overlaps a leave already approved OR
    already pending for this same person (two pending requests for the
    same day could both be approved, double-deducting the pool)."""
    if leave_type not in VALID_LEAVE_TYPES:
        raise ValueError(f"leave_type must be one of {sorted(VALID_LEAVE_TYPES)}")
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    days = _validate_window(start_date, end_date, leave_type)
    short = _shortfall(user_id, days)
    conn = get_connection()
    try:
        _ensure(conn)
        clash = _overlapping_status(conn, user_id, start_date, end_date)
        if clash == "approved":
            raise ValueError("overlaps a leave window already approved for this person")
        if clash:
            raise ValueError("overlaps a leave request already pending for this person")
        with conn:
            cur = conn.execute(
                "INSERT INTO employee_leave "
                "(user_id, start_date, end_date, reason, created_by, status, leave_type) "
                "VALUES (?,?,?,?,?,'pending',?)",
                (user_id, start_date, end_date, reason or "", created_by or "", leave_type),
            )
        row = {"id": cur.lastrowid, "user_id": user_id, "start_date": start_date,
               "end_date": end_date, "leave_type": leave_type,
               "reason": reason or "", "status": "pending",
               "days": days, "shortfall": short}
    finally:
        conn.close()
    _email_hr(row, email_subject, email_body)
    return row


# ── one-click decision links (emailed / WhatsApped to each approver) ─────

def _secret() -> bytes:
    return (os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY") or "dev-secret").encode()


def action_token(leave_id: int, approver_id: str, action: str) -> str:
    """HMAC for one (request, approver, action). The link carries who is
    deciding, so no login is needed and nobody can act as someone else."""
    import hashlib
    import hmac
    msg = f"{int(leave_id)}:{approver_id}:{action}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()[:32]


def verify_action_token(leave_id: int, approver_id: str, action: str, token: str) -> bool:
    import hmac
    return bool(token) and hmac.compare_digest(action_token(leave_id, approver_id, action), str(token))


def action_url(leave_id: int, approver_id: str, action: str) -> str:
    base = (os.getenv("PUBLIC_BASE_URL") or "https://lumina.mmga.agency").rstrip("/")
    return (f"{base}/api/leave/act?id={int(leave_id)}&by={approver_id}&a={action}"
            f"&t={action_token(leave_id, approver_id, action)}")


def get_request(leave_id: int) -> dict | None:
    conn = get_connection()
    try:
        _ensure(conn)
        r = conn.execute(
            "SELECT id, user_id, start_date, end_date, leave_type, reason, status, approved_by, "
            "approved_at FROM employee_leave WHERE id=?", (leave_id,)).fetchone()
        if not r:
            return None
        return {"id": r[0], "user_id": r[1], "start_date": r[2], "end_date": r[3],
                "leave_type": r[4], "reason": r[5], "status": r[6],
                "decided_by": r[7], "decided_at": r[8]}
    finally:
        conn.close()


def _email_hr(row: dict, subject: str = "", message: str = "") -> None:
    """Tell every approver (HR + founders, never the applicant) about a new
    pending request, by email and WhatsApp, each with their own one-click
    approve / reject links. Fire-and-forget, never raises."""
    if row.get("status") != "pending":
        return
    try:
        import mailer
        mailer.notify_leave_request(row, subject, message)
    except Exception:
        logger.exception("leave_store: approver email failed")
    _whatsapp_hr(row, subject, message)


def _whatsapp_hr(row: dict, subject: str = "", message: str = "") -> None:
    """WhatsApp DM to each approver for a new pending request, with approve /
    reject links. Best-effort, never raises. Delivered by the laptop's outbox
    poller."""
    try:
        import mailer
        import wa_outbox
        from utils import _load_employees
        emps = {e.get("id"): e for e in _load_employees().get("employees", [])}
        who = mailer.name_for(row.get("user_id", ""))
        sd, ed = row.get("start_date", ""), row.get("end_date", "")
        when = sd if sd == ed else f"{sd} to {ed}"
        kind = "half day" if row.get("leave_type") == "half" else "full day"
        reason = (row.get("reason") or "").strip()
        for aid in approvers_for(row.get("user_id", "")):
            e = emps.get(aid) or {}
            if str(e.get("status", "active")).lower() in {"inactive", "disabled", "left", "removed", "archived", "former"}:
                continue
            jid = wa_outbox.wa_jid(e.get("whatsapp", ""))
            if not jid:
                continue
            text = f"*Leave request* from {who}\n{kind.capitalize()}: {when}"
            if row.get("days"):
                text += f" ({row['days']:g} working day{'s' if row['days'] != 1 else ''})"
            if reason:
                text += f"\nReason: {reason}"
            if row.get("shortfall"):
                text += f"\nNote: {row['shortfall']:g} of these days are beyond their leave balance (unpaid)."
            # Same signed one-click links as the email. A GET only opens a confirm
            # page (link previews/scanners can't decide anything), the button decides.
            if row.get("id"):
                text += (f"\n\nApprove: {action_url(row['id'], aid, 'approve')}"
                         f"\nReject: {action_url(row['id'], aid, 'reject')}"
                         f"\n\nOr reply here, e.g. \"approve {who.split()[0]}'s leave\".")
            else:
                text += f"\n\nReply here, e.g. \"approve {who.split()[0]}'s leave\"."
            wa_outbox.enqueue(jid, text)
    except Exception:
        logger.exception("leave_store: approver WhatsApp failed")


def _row_for_notice(conn, leave_id: int) -> dict | None:
    r = conn.execute(
        "SELECT user_id, start_date, end_date, leave_type FROM employee_leave WHERE id=?",
        (leave_id,)).fetchone()
    return ({"user_id": r[0], "start_date": r[1], "end_date": r[2], "leave_type": r[3]}
            if r else None)


def _notify_applicant(row: dict, decision: str, decided_by: str, reason: str = "") -> None:
    """Tell the person their leave request was approved/rejected -- email to
    their @mmga.agency address and a WhatsApp DM. Best-effort, never raises,
    so a notification problem can't undo HR's decision."""
    try:
        import mailer
        mailer.notify_leave_decision(row, decision, decided_by, reason)
    except Exception:
        logger.exception("leave_store: decision email failed")
    try:
        import mailer
        import wa_outbox
        from utils import _load_employees
        wa = next((e.get("whatsapp", "") for e in _load_employees().get("employees", [])
                   if e.get("id") == row.get("user_id")), "")
        jid = wa_outbox.wa_jid(wa)
        if jid:
            sd, ed = row["start_date"], row["end_date"]
            when = sd if sd == ed else f"{sd} to {ed}"
            by = mailer.name_for(decided_by)
            verb = "approved" if decision == "approved" else "rejected"
            text = f"Your leave request for {when} was *{verb}* by {by}."
            if decision == "rejected" and (reason or "").strip():
                text += f"\nReason: {reason.strip()}"
            wa_outbox.enqueue(jid, text)
    except Exception:
        logger.exception("leave_store: decision WhatsApp failed")


def approve_leave(leave_id: int, approved_by: str) -> bool:
    """Approve a pending request. Re-checks for a live overlap at decision
    time, not just at request time — a row can go stale between the two
    (another request for the same days was approved in the meantime), and
    approving both would deduct the same days twice. Raises ValueError in
    that case so the caller can say why, rather than returning the same
    False that means "no such pending request"."""
    conn = get_connection()
    try:
        _ensure(conn)
        row = conn.execute(
            "SELECT user_id, start_date, end_date FROM employee_leave "
            "WHERE id=? AND status='pending'", (leave_id,),
        ).fetchone()
        if row and not is_approver_for(approved_by, row[0]):
            raise PermissionError("you can't decide this request"
                                  + (" (it's your own)" if approved_by == row[0] else ""))
        if row and _overlapping_status(conn, row[0], row[1], row[2],
                                       statuses=("approved",), exclude_id=leave_id):
            raise ValueError("overlaps a leave window already approved for this person")
        with conn:
            cur = conn.execute(
                "UPDATE employee_leave SET status='approved', approved_by=?, "
                "approved_at=datetime('now') WHERE id=? AND status='pending'",
                (approved_by, leave_id),
            )
        done = cur.rowcount > 0
        decided = _row_for_notice(conn, leave_id) if done else None
    finally:
        conn.close()
    if decided:
        _notify_applicant(decided, "approved", approved_by)
        _notify_other_approvers(decided, leave_id, "approved", approved_by)
    return done


def reject_leave(leave_id: int, approved_by: str, reason: str = "") -> bool:
    conn = get_connection()
    try:
        _ensure(conn)
        row = conn.execute("SELECT user_id FROM employee_leave WHERE id=? AND status='pending'",
                           (leave_id,)).fetchone()
        if row and not is_approver_for(approved_by, row[0]):
            raise PermissionError("you can't decide this request"
                                  + (" (it's your own)" if approved_by == row[0] else ""))
        with conn:
            cur = conn.execute(
                "UPDATE employee_leave SET status='rejected', approved_by=?, "
                "approved_at=datetime('now') WHERE id=? AND status='pending'",
                (approved_by, leave_id),
            )
        done = cur.rowcount > 0
        decided = _row_for_notice(conn, leave_id) if done else None
    finally:
        conn.close()
    if decided:
        _notify_applicant(decided, "rejected", approved_by, reason)
        _notify_other_approvers(decided, leave_id, "rejected", approved_by)
    return done


def _notify_other_approvers(row: dict, leave_id: int, decision: str, decided_by: str,
                            what: str = "") -> None:
    """The approvers who did NOT decide are told who did, so two people never
    both think it is still theirs to handle. Best-effort, never raises."""
    try:
        import mailer
        import wa_outbox
        from utils import _load_employees
        emps = {e.get("id"): e for e in _load_employees().get("employees", [])}
        who = mailer.name_for(row.get("user_id", ""))
        by = mailer.name_for(decided_by)
        sd, ed = row.get("start_date", ""), row.get("end_date", "")
        when = sd if sd == ed else f"{sd} to {ed}"
        verb = what or decision
        text = f"{who}'s leave for {when} was {verb} by {by}."
        for aid in approvers_for(row.get("user_id", "")):
            if aid == decided_by:
                continue
            e = emps.get(aid) or {}
            jid = wa_outbox.wa_jid(e.get("whatsapp", ""))
            if jid:
                wa_outbox.enqueue(jid, text)
    except Exception:
        logger.exception("leave_store: other-approver notice failed")


def cancel_approved(leave_id: int, by_user: str) -> tuple:
    """Cancel an already-APPROVED leave. The owner may do it only while it has
    not started (the balance is refunded); an approver may do it until it is
    over. The row is kept as 'cancelled' (never deleted) so the trail stays.
    Returns (ok, reason-if-not)."""
    conn = get_connection()
    try:
        _ensure(conn)
        r = conn.execute("SELECT user_id, start_date, end_date, leave_type FROM employee_leave "
                         "WHERE id=? AND status='approved'", (leave_id,)).fetchone()
        if not r:
            return False, "no approved leave with that id"
        owner, sd, ed, lt = r
        today = today_ist()
        if by_user == owner:
            if sd <= today:
                return False, "that leave has already started, ask an approver to cancel it"
        elif not is_approver_for(by_user, owner):
            return False, "only the person or an approver can cancel this"
        elif ed < today:
            return False, "that leave is already over"
        with conn:
            conn.execute("UPDATE employee_leave SET status='cancelled' WHERE id=? AND status='approved'",
                         (leave_id,))
        row = {"user_id": owner, "start_date": sd, "end_date": ed, "leave_type": lt}
    finally:
        conn.close()
    try:
        _notify_other_approvers(row, leave_id, "cancelled", by_user, what="cancelled")
        if by_user != owner:
            import mailer
            import wa_outbox
            from utils import _load_employees
            wa = next((e.get("whatsapp", "") for e in _load_employees().get("employees", [])
                       if e.get("id") == owner), "")
            jid = wa_outbox.wa_jid(wa)
            if jid:
                when = sd if sd == ed else f"{sd} to {ed}"
                wa_outbox.enqueue(jid, f"Your approved leave for {when} was cancelled by {mailer.name_for(by_user)}.")
    except Exception:
        logger.exception("leave_store: cancel notice failed")
    return True, ""


def cancel_leave(leave_id: int, user_id: str) -> bool:
    """An employee cancelling their OWN still-pending request. Cannot
    cancel something already approved/rejected/someone else's row."""
    conn = get_connection()
    try:
        _ensure(conn)
        with conn:
            cur = conn.execute(
                "UPDATE employee_leave SET status='cancelled' "
                "WHERE id=? AND user_id=? AND status='pending'",
                (leave_id, user_id),
            )
        return cur.rowcount > 0
    finally:
        conn.close()


def list_pending(viewer_id: str | None = None) -> list[dict]:
    """Pending requests. With `viewer_id`, only those that person may decide
    (never their own)."""
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT id, user_id, start_date, end_date, leave_type, reason, created_by, created_at "
            "FROM employee_leave WHERE status='pending' ORDER BY created_at ASC"
        ).fetchall()
        return [
            {"id": r[0], "user_id": r[1], "start_date": r[2], "end_date": r[3],
             "leave_type": r[4], "reason": r[5], "created_by": r[6], "created_at": r[7]}
            for r in rows
            if viewer_id is None or is_approver_for(viewer_id, r[1])
        ]
    finally:
        conn.close()


def on_leave_or_pending_ids(date_str: str) -> set[str]:
    """Approved leave OR a still-pending request covering the day. Used ONLY to
    stop the login nudges from pestering someone who has already asked for the
    day off while approval is slow; it never exempts the standup lock or
    counts toward anything."""
    ids = set(on_leave_ids(date_str))
    conn = get_connection()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM employee_leave WHERE status='pending' "
            "AND start_date <= ? AND end_date >= ?", (date_str, date_str)).fetchall()
        ids |= {r[0] for r in rows}
    except Exception:
        logger.exception("leave_store.on_leave_or_pending_ids failed")
    finally:
        conn.close()
    return ids


# ── balance (computed live, never stored) ───────────────────────────────

def _deduction_in_year(start_date: str, end_date: str, leave_type: str,
                       year: int, holidays: set | None = None) -> float:
    """How many days a single [start_date, end_date] leave window consumes
    from `year`'s pool.

    A row is a WINDOW, not a day — the balance used to charge one
    deduction per ROW, so an approved 5-day leave cost 1.0 day instead of
    5.0. Sat/Sun inside the window aren't leave (same weekend convention
    as calendar_days()), and a window straddling Dec 31 splits its
    deduction across both years rather than landing entirely in the start
    date's year."""
    try:
        sd = _date.fromisoformat(str(start_date)[:10])
        ed = _date.fromisoformat(str(end_date)[:10])
    except Exception:
        logger.warning("leave_store: unparseable leave window %r..%r",
                       start_date, end_date)
        return 0.0
    if ed < sd:
        sd, ed = ed, sd
    # Clamping to the requested year also bounds the loop below to at most
    # 366 iterations, however long the stored window happens to be.
    sd = max(sd, _date(year, 1, 1))
    ed = min(ed, _date(year, 12, 31))
    per_day = _LEAVE_DEDUCTION.get(leave_type, 1.0)
    total = 0.0
    d = sd
    holidays = holidays or set()
    while d <= ed:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            total += per_day
        d += timedelta(days=1)
    return total


def get_balance(user_id: str, year: int | None = None) -> dict:
    if year is None:
        year = _date.fromisoformat(today_ist()).year
    y_start, y_end = f"{year}-01-01", f"{year}-12-31"
    conn = get_connection()
    try:
        _ensure(conn)
        # Every approved window OVERLAPPING the year, not just those
        # starting in it -- a Dec 29 -> Jan 3 window owes days to both.
        rows = conn.execute(
            "SELECT start_date, end_date, leave_type FROM employee_leave "
            "WHERE user_id=? AND status='approved' "
            "AND NOT (end_date < ? OR start_date > ?)",
            (user_id, y_start, y_end),
        ).fetchall()
        hol = _holiday_dates(y_start, y_end)   # public holidays cost no leave
        used = sum(_deduction_in_year(r[0], r[1], r[2], year, hol) for r in rows)

        try:
            comp_rows = conn.execute(
                "SELECT days FROM comp_off_ledger WHERE user_id=? "
                f"AND {_COMP_OFF_IST_EXPR} >= ? AND {_COMP_OFF_IST_EXPR} < ?",
                (user_id, f"{year}-01-01 00:00:00", f"{year + 1}-01-01 00:00:00"),
            ).fetchall()
            comp_earned = sum(r[0] or 0.0 for r in comp_rows)
        except sqlite3.OperationalError:
            # Table not created yet (db.init_db() makes it; this is a pure
            # read path and deliberately no longer runs DDL of its own).
            comp_earned = 0.0

        try:
            ot_row = conn.execute(
                "SELECT COALESCE(SUM(ot_hours), 0) FROM overtime_ledger "
                "WHERE user_id=? AND converted_at IS NULL" + _NOT_WFH_SQL, (user_id,),
            ).fetchone()
            ot_pending_hours = round(ot_row[0] or 0.0, 2)
        except sqlite3.OperationalError:
            ot_pending_hours = 0.0
    finally:
        conn.close()
    remaining = ANNUAL_BASE_DAYS + comp_earned - used
    return {"base": ANNUAL_BASE_DAYS, "comp_earned": round(comp_earned, 2),
            "used": round(used, 2), "remaining": round(remaining, 2), "year": year,
            "ot_pending_hours": ot_pending_hours,
            "ot_conversion_hours": OT_CONVERSION_HOURS}


# ── calendar (per-day dot status for the UI) ────────────────────────────

_FULL_DAY_HOURS = 5.0   # 2026-10-19 (owner): 5h+ worked = a full day on the calendar and in the
#                         Excel (routes/attendance.py::_FULL_DAY_MIN_HOURS). Overtime is separate:
#                         it still starts only after OT_BASELINE_HOURS (9h).
_HALF_DAY_MIN_HOURS = 6.0   # unused leftover


def _hours_worked(checkin, checkout) -> float | None:
    if not checkin or not checkout:
        return None
    try:
        h1, m1, s1 = (int(p) for p in checkin.split(":"))
        h2, m2, s2 = (int(p) for p in checkout.split(":"))
        hrs = ((h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)) / 3600.0
    except Exception:
        return None
    return round(hrs, 2) if hrs >= 0 else None


def calendar_days(user_id: str, year: int, month: int) -> dict:
    """{"YYYY-MM-DD": {"status": ..., "hours": float|None}} for every day
    in the given month. status is one of: 'weekend', 'leave_approved',
    'leave_pending', 'full', 'half', 'none'. Leave days also carry
    'leave_type' ('full'/'half'). A day covered by a REJECTED request (and
    no live pending/approved one) keeps its normal status but gains
    'rejected': True so the person can see it was turned down."""
    import calendar as _cal
    conn = get_connection()
    try:
        _ensure(conn)
        att_rows = conn.execute(
            "SELECT date, checkin_time, checkout_time FROM daily_attendance "
            "WHERE user_id=? AND date >= ? AND date < ?",
            (user_id, f"{year:04d}-{month:02d}-01",
             f"{year:04d}-{month + 1:02d}-01" if month < 12 else f"{year + 1:04d}-01-01"),
        ).fetchall()
        att_by_date = {r[0]: (r[1], r[2]) for r in att_rows}

        leave_rows = conn.execute(
            "SELECT id, start_date, end_date, status, leave_type FROM employee_leave "
            "WHERE user_id=? AND status IN ('approved','pending','rejected') "
            "AND start_date <= ? AND end_date >= ?",
            (user_id, f"{year:04d}-{month:02d}-{_cal.monthrange(year, month)[1]:02d}",
             f"{year:04d}-{month:02d}-01"),
        ).fetchall()
    finally:
        conn.close()

    days_in_month = _cal.monthrange(year, month)[1]
    out = {}
    for d in range(1, days_in_month + 1):
        dstr = f"{year:04d}-{month:02d}-{d:02d}"
        weekday = _date(year, month, d).weekday()
        if weekday >= 5:
            out[dstr] = {"status": "weekend", "hours": None}
            continue

        leave_status = None
        leave_id = None
        leave_type = "full"
        rejected = False
        for lid, sd, ed, st, lt in leave_rows:
            if not (sd <= dstr <= ed):
                continue
            if st == "rejected":
                rejected = True
                continue
            leave_status = "leave_approved" if st == "approved" else "leave_pending"
            leave_id = lid
            leave_type = lt or "full"
            if st == "approved":
                break  # approved wins over a coincidentally-also-pending row
        if leave_status:
            out[dstr] = {"status": leave_status, "hours": None, "leave_id": leave_id,
                         "leave_type": leave_type}
            continue

        cin, cout = att_by_date.get(dstr, (None, None))
        hrs = _hours_worked(cin, cout)
        if hrs is None:
            out[dstr] = {"status": "none", "hours": None}
        elif hrs >= _FULL_DAY_HOURS:   # 5h = full day, same rule as the Excel export
            out[dstr] = {"status": "full", "hours": hrs}
        else:
            out[dstr] = {"status": "half", "hours": hrs}
        if rejected:
            out[dstr]["rejected"] = True
    return out


# ── overtime -> comp-off conversion (nightly job, task_scheduler.py) ────

def _overtime_hours_for(hrs: float | None) -> float | None:
    if hrs is None:
        return None
    return round(max(0.0, hrs - OT_BASELINE_HOURS), 2)


def _active_employee_ids() -> list[str]:
    from utils import _load_employees
    inactive = {"inactive", "disabled", "left", "removed", "archived", "former"}
    out = []
    try:
        for e in _load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() not in inactive:
                out.append(e.get("id", ""))
    except Exception:
        logger.exception("leave_store: roster load failed")
    return [e for e in out if e]


def _convert_overtime_for_user(conn, user_id: str) -> float:
    """Convert this user's unconverted overtime into whole comp-off days,
    right now, carrying any leftover hours forward instead of discarding
    them. 26 unconverted hours converts to +1 day and leaves exactly 2
    hours still pending for next time — not 0.

    Every currently-unconverted ledger row is marked converted (its hours
    are fully accounted for in the total), one comp_off_ledger row records
    the whole days earned, and — if there's a remainder — a single new
    synthetic ledger row carries it forward as still-unconverted overtime.
    That row's `date` is a non-date sentinel (`carry-<uuid>`), never a real
    calendar date, purely so it keeps UNIQUE(user_id, date) happy and reads
    back indistinguishably from any other unconverted row next time this
    runs. Returns how many whole days were converted (0 if under 24h)."""
    rows = conn.execute(
        "SELECT id, ot_hours FROM overtime_ledger WHERE user_id=? "
        "AND converted_at IS NULL" + _NOT_WFH_SQL, (user_id,),
    ).fetchall()
    total = sum((ot or 0.0) for _rid, ot in rows)
    if total < OT_CONVERSION_HOURS:
        return 0.0
    whole_days = int(total // OT_CONVERSION_HOURS)
    consumed = whole_days * OT_CONVERSION_HOURS
    remainder = round(total - consumed, 4)
    ids = [rid for rid, _ot in rows]
    conn.execute(
        f"UPDATE overtime_ledger SET converted_at=datetime('now') "
        f"WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    )
    conn.execute(
        "INSERT INTO comp_off_ledger (user_id, days, source_ot_hours) "
        "VALUES (?, ?, ?)", (user_id, whole_days, consumed),
    )
    if remainder > 1e-6:
        conn.execute(
            "INSERT INTO overtime_ledger (user_id, date, worked_hours, ot_hours) "
            "VALUES (?, ?, ?, ?)",
            (user_id, f"carry-{uuid4()}", remainder, remainder),
        )
    return float(whole_days)


def get_unconverted_ot_hours(user_id: str) -> float:
    """Total still-pending (not yet converted to comp-off) overtime hours
    for this person, right now — what the Leave page shows next to the
    Convert action. Read-only; tolerates the table not existing yet
    (db.init_db() creates it) rather than running DDL on every read, same
    convention as get_balance()'s comp_off_ledger read."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(ot_hours), 0) FROM overtime_ledger "
            "WHERE user_id=? AND converted_at IS NULL" + _NOT_WFH_SQL, (user_id,),
        ).fetchone()
        return round(row[0] or 0.0, 2)
    except sqlite3.OperationalError:
        return 0.0
    except Exception:
        logger.exception("leave_store.get_unconverted_ot_hours failed")
        return 0.0
    finally:
        conn.close()


def convert_overtime_now(user_id: str) -> dict:
    """Manually trigger the overtime -> comp-off conversion for one person
    immediately, instead of waiting for the nightly sweep (which runs the
    exact same logic, just automatically, once a day for every employee).
    Safe to call anytime, including with under 24h pending (a no-op)."""
    conn = get_connection()
    try:
        _ensure_overtime_ledger(conn)
        _ensure_comp_off_ledger(conn)
        with conn:
            converted_days = _convert_overtime_for_user(conn, user_id)
        row = conn.execute(
            "SELECT COALESCE(SUM(ot_hours), 0) FROM overtime_ledger "
            "WHERE user_id=? AND converted_at IS NULL" + _NOT_WFH_SQL, (user_id,),
        ).fetchone()
        pending_hours = round(row[0] or 0.0, 2)
        return {"converted_days": converted_days, "pending_hours": pending_hours}
    finally:
        conn.close()


def run_overtime_conversion_sweep(for_date: str | None = None) -> dict:
    """Compute overtime_ledger rows for `for_date` (default: yesterday IST)
    across every active employee, then run the comp-off conversion check.
    Safe to call more than once for the same date — the UNIQUE(user_id,
    date) constraint on overtime_ledger makes the per-day insert a no-op
    on a re-run (caught via sqlite3.IntegrityError), and conversion only
    ever consumes rows that are still unconverted.

    `converted_events` in the returned dict is actually a count of whole
    comp-off DAYS converted across everyone this run (_convert_overtime_
    for_user can return >1 in one call if someone's pending total already
    spans multiple 24h blocks) — kept as the same dict key for anything
    already reading this return value."""
    if for_date is None:
        for_date = (_date.fromisoformat(today_ist()) - timedelta(days=1)).isoformat()

    conn = get_connection()
    processed = 0
    converted_events = 0
    try:
        _ensure_overtime_ledger(conn)
        _ensure_comp_off_ledger(conn)
        _ensure(conn)
        att_rows = conn.execute(
            "SELECT user_id, checkin_time, checkout_time FROM daily_attendance WHERE date=?",
            (for_date,),
        ).fetchall()
        att_by_user = {r[0]: (r[1], r[2]) for r in att_rows}

        for user_id in _active_employee_ids():
            cin, cout = att_by_user.get(user_id, (None, None))
            hrs = _hours_worked(cin, cout)
            if hrs is None:
                continue
            ot = _overtime_hours_for(hrs)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO overtime_ledger (user_id, date, worked_hours, ot_hours) "
                        "VALUES (?,?,?,?)",
                        (user_id, for_date, hrs, ot),
                    )
                processed += 1
            except sqlite3.IntegrityError:
                pass  # already computed for this user+date, re-run is a no-op here

            # Each user's conversion gets its OWN transaction. Previously
            # these writes sat in the connection's implicit transaction
            # with no `with conn:` of their own, so the NEXT user's
            # IntegrityError-rollback could discard a prior user's
            # already-committed-looking conversion in the same sweep.
            try:
                with conn:
                    converted_events += _convert_overtime_for_user(conn, user_id)
            except Exception:
                logger.exception("leave_store: comp-off conversion failed for %s",
                                 user_id)
    finally:
        conn.close()
    return {"processed": processed, "converted_events": converted_events}


def freeze_backlog_and_revert_grants() -> dict:
    """One-time correction for the 2026-10-01 OT backfill, per an explicit
    user decision made right after seeing its effect: the 24h->1-day
    carry-forward cycle should start fresh from today, not retroactively
    grant leave from whatever attendance history existed before this
    feature was ever built.

    backfill_overtime_ledger()'s trailing conversion step treated that
    entire historical backlog as live pending overtime and immediately
    granted comp-off days from it — this undoes exactly that:
      - Every comp_off_ledger row is deleted (this session's own
        ot-ledger-summary diagnostic confirmed, before running this, that
        every existing row carries that exact backfill run's timestamp —
        not any separate legitimate grant — so there's nothing else to
        preserve).
      - Every still-unconverted overtime_ledger row dated before today,
        PLUS any leftover carry-<uuid> remainder row from that run's own
        conversion (a synthetic sentinel date that doesn't compare as
        "before today" via plain string ordering, so it needs its own
        clause), is marked with a converted_at sentinel ('frozen-backlog'
        — never a real timestamp) so it's permanently excluded from
        every future pending-hours sum and conversion check. The row
        itself is kept, not deleted — it stays as a historical record,
        it just stops counting toward anything from here on.

    A genuinely new row dated today or later is never touched by this —
    that's exactly the "starts fresh from today" behavior this
    implements. Safe to call more than once; a second call simply finds
    nothing left to revert or freeze."""
    conn = get_connection()
    try:
        _ensure_overtime_ledger(conn)
        _ensure_comp_off_ledger(conn)
        today = today_ist()
        with conn:
            agg = conn.execute(
                "SELECT COALESCE(SUM(days), 0), COUNT(*) FROM comp_off_ledger"
            ).fetchone()
            days_reverted, rows_reverted = agg[0] or 0.0, agg[1] or 0
            conn.execute("DELETE FROM comp_off_ledger")
            frozen = conn.execute(
                "UPDATE overtime_ledger SET converted_at='frozen-backlog' "
                "WHERE converted_at IS NULL AND (date < ? OR date LIKE 'carry-%')",
                (today,),
            ).rowcount
        return {"comp_off_days_reverted": days_reverted,
                "comp_off_rows_reverted": rows_reverted,
                "overtime_rows_frozen": frozen}
    finally:
        conn.close()


def backfill_overtime_ledger() -> dict:
    """One-time (but idempotent — safe to re-run) catch-up for real
    history that predates this feature.

    run_overtime_conversion_sweep() only started running nightly once the
    leave management system shipped (2026-09-30), so it only ever
    computed overtime_ledger rows for "yesterday" from that point on.
    Someone who worked a lot of overtime across September never got any
    of it into the ledger — even though the Attendance Excel export has
    always shown that same overtime, computed straight from
    daily_attendance with no dependency on this table at all. This walks
    every day of real attendance history (excluding today, which is still
    in progress and will be picked up by tomorrow's regular sweep like
    always) for every active employee, inserting whatever rows are
    missing using the exact same hours/overtime math as the nightly job,
    then runs the normal conversion check once so any comp-off days
    already earned fire immediately instead of waiting for tomorrow.

    Already-present dates are skipped up front (not relied on via
    IntegrityError) since a full-history backfill can touch thousands of
    rows."""
    conn = get_connection()
    processed = 0
    try:
        _ensure_overtime_ledger(conn)
        _ensure_comp_off_ledger(conn)
        today = today_ist()
        existing = {(r[0], r[1]) for r in conn.execute(
            "SELECT user_id, date FROM overtime_ledger").fetchall()}
        active = set(_active_employee_ids())
        att_rows = conn.execute(
            "SELECT user_id, date, checkin_time, checkout_time FROM daily_attendance "
            "WHERE date < ?", (today,),
        ).fetchall()
        for user_id, date, cin, cout in att_rows:
            if user_id not in active or (user_id, date) in existing:
                continue
            hrs = _hours_worked(cin, cout)
            if hrs is None:
                continue
            ot = _overtime_hours_for(hrs)
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO overtime_ledger (user_id, date, worked_hours, ot_hours) "
                        "VALUES (?,?,?,?)", (user_id, date, hrs, ot),
                    )
                processed += 1
            except sqlite3.IntegrityError:
                pass  # race with a concurrent sweep/backfill — harmless, already there

        converted_events = 0
        for user_id in active:
            try:
                with conn:
                    converted_events += _convert_overtime_for_user(conn, user_id)
            except Exception:
                logger.exception("leave_store: backfill conversion failed for %s", user_id)
    finally:
        conn.close()
    return {"processed": processed, "converted_events": converted_events}
