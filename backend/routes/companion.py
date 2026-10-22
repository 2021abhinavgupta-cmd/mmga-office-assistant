"""
Companion Blueprint -- endpoints for the always-on laptop companion
(scripts/laptop_agent.py). All token-gated with the same ?token= /
Authorization: Bearer scheme as the storage-sync endpoints (STORAGE_SYNC_TOKEN
or FLASK_SECRET_KEY), so the laptop -- which already holds those tokens --
can call them without a browser session.

Routes:
  GET  /api/companion/digest?window=morning|evening   -- assembled daily brief
  GET  /api/companion/uploads-archive                  -- zip of logs/uploads/
  GET  /api/companion/sheets-health                    -- per-client sync health
  POST /api/companion/sheets-pull-all                  -- reconcile every linked sheet
  GET  /api/companion/tomorrow-live                     -- social posts due live tomorrow
  GET  /api/companion/content-calendar-recipients        -- who to DM it to (default Vidit)
  GET  /api/companion/not-logged-in                      -- who's missing check-in / standup (nudge target)
  GET  /api/companion/wa-call-outbox                     -- pending announcement calls to place
  POST /api/companion/wa-call-outbox/ack                 -- report calls placed/failed
  GET  /api/companion/followups                          -- dry run: what the initiative engine sees
  POST /api/companion/followups/run                      -- force a real follow-up sweep now
  GET  /api/companion/storage-report                      -- breakdown of what's using the Railway volume
  POST /api/companion/storage-cleanup?targets=hf_cache,vacuum -- free space (see docstring for order/safety)
  GET  /api/companion/db-table-sizes                      -- per-table breakdown of app.db itself
  GET  /api/companion/sheet-edit-log-stats                 -- why sheet_edit_log is big (top clients/tasks)
  GET  /api/companion/client-real-task-count?client_id=    -- read-only: real Notion task count vs. logged task_ids
  GET/POST /api/companion/group-allowlist                  -- view/edit the WhatsApp group allow-list
  GET  /api/companion/wa-context-keys                       -- recent chat context keys (no content), DM vs group
  GET  /api/companion/retention                            -- dry run: what the retention sweep would delete
  POST /api/companion/retention/run                        -- force a real retention sweep now
  POST /api/companion/backfill-overtime                     -- one-time catch-up of pre-2026-09-30 overtime_ledger rows
  POST /api/companion/freeze-ot-backlog                      -- one-time: revert backfill-granted comp-off, freeze the backlog
  GET  /api/companion/ot-ledger-summary                      -- read-only: current overtime_ledger/comp_off_ledger contents

Note: the follow-up sweep itself runs on the Railway scheduler
(task_scheduler._run_followup_sweep), NOT from the laptop -- delivery goes
through wa_outbox either way, so it needs nothing companion-side. The two
routes above exist to inspect and hand-trigger it.
"""
from __future__ import annotations

import hmac
import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from db import get_connection
import utils
import shutil

logger = logging.getLogger(__name__)
companion_bp = Blueprint("companion", __name__)

_UPLOADS = Path(__file__).parent.parent.parent / "logs" / "uploads"

# task statuses that count as "no longer needs attention"
_CLOSED = {"done", "approved", "posted", "final", "complete", "completed",
           "closed", "published", "live"}
# Additionally excluded from the OVERDUE nag specifically (not from
# DUE TODAY/TOMORROW, which are plain deadline-awareness, not a neglect
# signal) -- someone actively working a task, or a task sitting with
# someone else awaiting their review, isn't "overdue and forgotten" the
# way a stale not_started task is. Root-caused live 2026-09-04: 590 of
# 650 "overdue" tasks in this workspace were in_progress -- counting them
# made the nag number meaningless (excluding just in_progress drops it to
# 69, which is the real actionable figure).
_OVERDUE_EXEMPT = {"in_progress", "need_for_approval", "pending_review", "submitted"}


def _auth_ok() -> bool:
    expected = os.getenv("STORAGE_SYNC_TOKEN") or os.getenv("FLASK_SECRET_KEY") or ""
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.args.get("token", "")
    return bool(token) and hmac.compare_digest(token, expected)


def _emp_names() -> dict:
    try:
        return {e.get("id"): e.get("name", e.get("id"))
                for e in utils._load_employees().get("employees", [])}
    except Exception:
        return {}


# ── digest ─────────────────────────────────────────────────────────────────

def _iso(d: str) -> bool:
    return bool(d) and len(str(d)) >= 10 and str(d)[4] == "-" and str(d)[7] == "-"


def _build_digest(window: str) -> dict:
    today = utils.today_ist()
    _dt = datetime.strptime(today, "%Y-%m-%d")
    tomorrow = (_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    # only surface overdue items that are still plausibly actionable -- a task
    # 6 months past due with status "Not Started" is dead weight in a daily brief
    overdue_floor = (_dt - timedelta(days=30)).strftime("%Y-%m-%d")
    names = _emp_names()
    data: dict = {"window": window, "date": today}
    lines: list[str] = []

    # ── tasks (Notion is the source of truth when configured) ──
    overdue, due_today, due_tomorrow = [], [], []
    overdue_total = 0
    overdue_by_person: dict = {}   # name -> count, for a scannable tally
                                    # instead of a raw N-item list dump
    try:
        import notion_store
        if notion_store.is_configured():
            for t in notion_store.list_tasks():
                st = str(t.get("status") or "").strip().lower().replace(" ", "_")
                if st in _CLOSED:
                    continue
                due = str(t.get("due_date") or "")[:10]
                if not _iso(due):
                    continue
                label = t.get("title") or "(untitled)"
                who = t.get("assigned_to") or ""
                row = f"{label}" + (f" -> {who}" if who else "") + f" (due {due})"
                if due < today:
                    # in_progress/awaiting-review aren't "neglected" the way
                    # not_started/blank is -- see _OVERDUE_EXEMPT above.
                    # Still surfaced under DUE TODAY/TOMORROW if relevant,
                    # just not counted as an overdue nag.
                    if st in _OVERDUE_EXEMPT:
                        continue
                    overdue_total += 1
                    overdue_by_person[who or "Unassigned"] = overdue_by_person.get(who or "Unassigned", 0) + 1
                    if due >= overdue_floor:
                        overdue.append(row)
                elif due == today:
                    due_today.append(row)
                elif due == tomorrow:
                    due_tomorrow.append(row)
    except Exception:
        logger.debug("digest: task pull failed", exc_info=True)

    data["overdue"] = overdue
    data["overdue_total"] = overdue_total
    data["overdue_by_person"] = overdue_by_person
    data["due_today"] = due_today
    data["due_tomorrow"] = due_tomorrow

    # ── standups today ──
    submitted, done_ct, pending_ct = [], 0, 0
    try:
        conn = get_connection()
        rows = conn.execute("SELECT user_id FROM standups WHERE date=?", (today,)).fetchall()
        submitted = sorted({names.get(r[0], r[0]) for r in rows})
        for status, ct in conn.execute(
            "SELECT status, COUNT(*) FROM standup_tasks WHERE date=? GROUP BY status", (today,)
        ).fetchall():
            if str(status).lower() in ("done", "completed"):
                done_ct += ct
            else:
                pending_ct += ct
        conn.close()
    except Exception:
        logger.debug("digest: standup pull failed", exc_info=True)
    data["standups_submitted"] = submitted
    data["standup_done"] = done_ct
    data["standup_pending"] = pending_ct

    # ── sheet-sync health ──
    sync_bad = []
    try:
        conn = get_connection()
        for cid, cname, ok, lpull in conn.execute(
            "SELECT client_id, client_name, last_push_ok, last_pull_at FROM google_sheet_links"
        ).fetchall():
            if ok == 0:
                sync_bad.append(f"{cname or cid}: last push FAILED")
            elif lpull:
                try:
                    age = datetime.strptime(str(lpull)[:10], "%Y-%m-%d")
                    if (datetime.now() - age).days >= 3:
                        sync_bad.append(f"{cname or cid}: no pull in {(datetime.now()-age).days}d")
                except Exception:
                    pass
        conn.close()
    except Exception:
        logger.debug("digest: sheet health failed", exc_info=True)
    data["sheet_sync_issues"] = sync_bad

    # ── budget ──
    try:
        from budget_tracker import get_usage_summary
        s = get_usage_summary()
        data["budget_percent"] = s.get("percent_used", 0)
        data["budget_spent"] = s.get("total_spent_ever", s.get("monthly_spend"))
    except Exception:
        data["budget_percent"] = None

    # ── render plain text -- WhatsApp markdown (*bold* headers, • bullets,
    # blank-line section breaks) so this reads cleanly on a phone screen ──
    head = "*Good morning*" if window == "morning" else "*End of day*"
    lines.append(f"{head} — {today}")
    lines.append("")
    if overdue:
        older = overdue_total - len(overdue)
        lines.append(
            f"*Overdue: {overdue_total}*"
            + (f" ({len(overdue)} from the last 30 days, {older} older)" if older > 0 else "")
        )
        # a per-person tally is far more scannable on a phone than a raw
        # list of hundreds of task titles -- this is what was actually
        # unreadable before, not the counts themselves
        tally = sorted(overdue_by_person.items(), key=lambda kv: -kv[1])[:10]
        lines += [f"  • {name}: {ct}" for name, ct in tally]
        if len(overdue_by_person) > 10:
            lines.append(f"  ...and {len(overdue_by_person) - 10} more people")
        lines.append("")
    if window == "morning":
        lines.append(f"*Due today* ({len(due_today)})")
        lines += [f"  • {r}" for r in due_today[:20]] or ["  (nothing)"]
        lines.append("")
        lines.append(f"*Due tomorrow* ({len(due_tomorrow)})")
        lines += [f"  • {r}" for r in due_tomorrow[:15]] or ["  (nothing)"]
        lines.append("")
    else:
        lines.append(f"*Was due today* ({len(due_today)})")
        lines += [f"  • {r}" for r in due_today[:20]] or ["  (nothing)"]
        lines.append("")
    if submitted:
        lines.append(f"Standups in: {', '.join(submitted)}")
    else:
        lines.append("Standups in: none yet")
    lines.append(f"Standup tasks: *{done_ct} done* / {pending_ct} pending")
    if sync_bad:
        lines.append("")
        lines.append("*Sheet sync issues*")
        lines += [f"  • {r}" for r in sync_bad]
    if data.get("budget_percent") is not None:
        lines.append("")
        lines.append(f"API budget used: *{data['budget_percent']}%*")

    data["text"] = "\n".join(lines)
    return data


# ── attendance roll-call ───────────────────────────────────────────────────

_INACTIVE = {"inactive", "disabled", "left", "removed", "archived", "former"}

# a few lines so the noon roll-call isn't word-for-word identical every day
_ROLLCALL_LINES = [
    "*Noon roll-call.* Still missing in action: *{names}*. The login button doesn't bite. ⏰",
    "It's 12 o'clock and *{names}* still haven't graced the attendance sheet with their presence. 👀",
    "Half the day's gone and *{names}* are yet to clock in. Bold strategy.",
    "*Roll-call:* *{names}* officially 'not logged in yet'. We'll wait. ⏰",
    "12 o'clock headcount — *{names}* unaccounted for. Send a search party?",
    "*{names}*: the check-in button misses you. It's a two-second job, promise. 🙂",
]
_ROLLCALL_ALL_IN = "*Noon roll-call:* everyone's actually logged in. Someone mark the calendar. ✅"
_ROLLCALL_WA_HINT = (
    "Can't get to Lumina right now? Reply \"lumina in\" here in the group "
    "(or just DM me \"in\") and I'll check you in from WhatsApp."
)


@companion_bp.route("/api/companion/checkout-missing", methods=["GET"])
def companion_checkout_missing():
    """Who checked IN today but has NOT checked out -- powers the 11 PM
    personal "please check out" DM. Only people with a check-in today are
    ever listed (someone who never checked in is not nagged to check out).
    Excluded: inactive staff, anyone without a WhatsApp number, and the ids in
    app_settings key `checkout_nudge_exclude_ids` (extra ids; emp001 and\n    emp004 = Vidit and Kshitij are ALWAYS excluded -- explicitly never pinged)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    today = utils.today_ist()
    exclude = {"emp001", "emp004"}
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='checkout_nudge_exclude_ids'").fetchone()
        conn.close()
        if row and row[0] is not None:
            # Vidit and Kshitij stay excluded no matter what; the setting only ADDS people.
            exclude |= {x.strip() for x in str(row[0]).replace(",", " ").split() if x.strip()}
    except Exception:
        pass

    try:
        conn = get_connection()
        open_ids = {r[0] for r in conn.execute(
            "SELECT user_id FROM daily_attendance WHERE date=? "
            "AND checkin_time IS NOT NULL AND checkin_time<>'' "
            "AND (checkout_time IS NULL OR checkout_time='')", (today,)).fetchall()}
        conn.close()
    except Exception:
        logger.exception("checkout-missing: query failed")
        return jsonify({"error": "query failed"}), 500

    people = []
    try:
        for e in utils._load_employees().get("employees", []):
            eid = e.get("id", "")
            if eid not in open_ids or eid in exclude:
                continue
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            wa = re.sub(r"\D", "", e.get("whatsapp", "") or "")
            if not wa:
                continue
            people.append({"id": eid, "name": e.get("name") or eid, "whatsapp": wa})
    except Exception:
        logger.exception("checkout-missing: roster load failed")
        return jsonify({"error": "roster load failed"}), 500
    return jsonify({"date": today, "missing_checkout": people, "excluded_ids": sorted(exclude)})


@companion_bp.route("/api/companion/attendance-missing", methods=["GET"])
def companion_attendance_missing():
    """Who on the active roster has NOT checked in today. Powers the laptop
    companion's noon roll-call message to the team WhatsApp group."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    today = utils.today_ist()
    try:
        weekend = datetime.strptime(today, "%Y-%m-%d").weekday() >= 5  # 5=Sat 6=Sun
    except Exception:
        weekend = False

    roster = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            roster.append((e.get("id", ""), e.get("name") or e.get("id", "")))
    except Exception:
        logger.exception("attendance-missing: roster load failed")
        return jsonify({"error": "roster load failed"}), 500

    checked_in = set()
    try:
        conn = get_connection()
        for row in conn.execute(
            "SELECT user_id FROM daily_attendance "
            "WHERE date=? AND checkin_time IS NOT NULL AND checkin_time<>''",
            (today,),
        ).fetchall():
            checked_in.add(row[0])
        conn.close()
    except Exception:
        logger.exception("attendance-missing: query failed")
        return jsonify({"error": "query failed"}), 500

    present = [n for (i, n) in roster if i in checked_in]
    missing = [n for (i, n) in roster if i not in checked_in]

    # id + whatsapp for the ones missing, for the 10:30 personal nag
    wa_by_id = {}
    try:
        for e in utils._load_employees().get("employees", []):
            wa_by_id[e.get("id", "")] = re.sub(r"\D", "", e.get("whatsapp", "") or "")
    except Exception:
        pass
    missing_detail = [
        {"id": i, "name": n, "whatsapp": wa_by_id.get(i, "")}
        for (i, n) in roster if i not in checked_in
    ]

    if weekend:
        text = ""
    elif not missing:
        text = _ROLLCALL_ALL_IN
    else:
        names = ", ".join(missing)
        # rotate the wording by date so it's not a copy-paste every day
        line = _ROLLCALL_LINES[sum(ord(c) for c in today) % len(_ROLLCALL_LINES)]
        text = line.format(names=names) + "\n\n" + _ROLLCALL_WA_HINT

    return jsonify({
        "date": today,
        "weekend": weekend,
        "present": present,
        "missing": missing,
        "missing_detail": missing_detail,
        "roster_count": len(roster),
        "text": text,
    })


@companion_bp.route("/api/companion/office-day", methods=["GET"])
def companion_office_day():
    """What HR's office calendar says for one day (?date=YYYY-MM-DD, default today IST):
    {wfh, office, holiday, titles}. Used so the class-meeting link is only posted on WFH days."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        from datetime import datetime
        from utils import IST
        day = (request.args.get("date") or datetime.now(IST).strftime("%Y-%m-%d"))[:10]
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT kind, title FROM office_calendar WHERE ? BETWEEN start_date AND end_date",
                (day,)).fetchall()
        finally:
            conn.close()
        kinds = [r[0] for r in rows]
        return jsonify({"date": day, "wfh": "wfh" in kinds, "office": "office" in kinds,
                        "holiday": "holiday" in kinds, "titles": [r[1] for r in rows]})
    except Exception:
        logger.exception("companion office-day failed")
        return jsonify({"error": "failed"}), 500


@companion_bp.route("/api/companion/alert-recipients", methods=["GET"])
def companion_alert_recipients():
    """Employee ids in app_settings 'alert_recipient_ids' (default emp003),
    resolved to name + whatsapp -- who the laptop should DM for ops alerts
    (budget crossed 80%, sheet sync broke, ...)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    ids = ["emp003"]
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='alert_recipient_ids'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            ids = [x.strip() for x in str(row[0]).replace(",", " ").split() if x.strip()]
    except Exception:
        pass
    by_id = {}
    try:
        for e in utils._load_employees().get("employees", []):
            by_id[e.get("id", "")] = e
    except Exception:
        pass
    out = []
    for i in ids:
        e = by_id.get(i)
        if e and re.sub(r"\D", "", e.get("whatsapp", "") or ""):
            out.append({"id": i, "name": e.get("name") or i,
                        "whatsapp": re.sub(r"\D", "", e.get("whatsapp", ""))})
    return jsonify({"recipients": out})


# ── standup nudge ──────────────────────────────────────────────────────────

@companion_bp.route("/api/companion/standup-missing", methods=["GET"])
def companion_standup_missing():
    """Active employees (with a WhatsApp number) who have NOT added a fresh
    task to today's standup — i.e. they have no `standup_tasks` row for today
    that isn't just a carry-over. Powers the 11:30 personal nudge."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    today = utils.today_ist()
    try:
        weekend = datetime.strptime(today, "%Y-%m-%d").weekday() >= 5
    except Exception:
        weekend = False

    emps = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            wa = re.sub(r"\D", "", e.get("whatsapp", "") or "")
            if not wa:
                continue
            emps.append({"id": e.get("id", ""),
                         "name": e.get("name") or e.get("id", ""),
                         "whatsapp": wa})
    except Exception:
        logger.exception("standup-missing: roster load failed")
        return jsonify({"error": "roster load failed"}), 500

    have = set()
    try:
        conn = get_connection()
        # A fresh (non-carried) row counts as "added a task today". So does a
        # carried-over row (main task persisting day to day, e.g. Noorish's
        # setup) that's been PATCHed today -- status/blocker/subtasks all
        # bump updated_at (update_my_task, ops.py) -- since ticking subtasks
        # under a standing task IS today's work, even with no new row.
        for row in conn.execute(
            "SELECT DISTINCT user_id FROM standup_tasks "
            "WHERE date=? AND ("
            "  (carried_from IS NULL OR carried_from='')"
            "  OR updated_at IS NOT NULL"
            ")",
            (today,),
        ).fetchall():
            have.add(row[0])
        conn.close()
    except Exception:
        logger.exception("standup-missing: query failed")
        return jsonify({"error": "query failed"}), 500

    missing = [e for e in emps if e["id"] not in have]
    return jsonify({
        "date": today,
        "weekend": weekend,
        "checked": len(emps),
        "missing": missing,
    })


@companion_bp.route("/api/companion/not-logged-in", methods=["GET"])
def companion_not_logged_in():
    """Active employees (with a WhatsApp number) who, on a weekday and while
    NOT on recorded leave, have not fully "logged in" for today -- meaning
    they are missing their attendance check-in, or their standup has no
    task, or both. Powers the recurring login nudge: the every-5-min
    personal DM (job_login_nudge) and the mid-morning + follow-up group
    ping (job_login_group_ping). CLAUDE.md gotcha #119."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    today = utils.today_ist()
    try:
        weekend = datetime.strptime(today, "%Y-%m-%d").weekday() >= 5
    except Exception:
        weekend = False
    if weekend:
        return jsonify({"date": today, "weekend": True, "missing": []})

    try:
        import leave_store
        # a pending request also stops the nudges (approval can be slow)
        on_leave = leave_store.on_leave_or_pending_ids(today)
    except Exception:
        logger.exception("not-logged-in: leave lookup failed")
        on_leave = set()

    emps = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            if e.get("id", "") in on_leave:
                continue
            wa = re.sub(r"\D", "", e.get("whatsapp", "") or "")
            if not wa:
                continue
            emps.append({"id": e.get("id", ""),
                         "name": e.get("name") or e.get("id", ""),
                         "whatsapp": wa})
    except Exception:
        logger.exception("not-logged-in: roster load failed")
        return jsonify({"error": "roster load failed"}), 500

    checked_in, did_standup = set(), set()
    try:
        conn = get_connection()
        for row in conn.execute(
            "SELECT user_id FROM daily_attendance "
            "WHERE date=? AND checkin_time IS NOT NULL AND checkin_time<>''",
            (today,),
        ).fetchall():
            checked_in.add(row[0])
        # same "did something on today's standup" rule as standup-missing:
        # a fresh row, or a carried row PATCHed today (updated_at set)
        for row in conn.execute(
            "SELECT DISTINCT user_id FROM standup_tasks WHERE date=? AND ("
            "  (carried_from IS NULL OR carried_from='') OR updated_at IS NOT NULL)",
            (today,),
        ).fetchall():
            did_standup.add(row[0])
        conn.close()
    except Exception:
        logger.exception("not-logged-in: query failed")
        return jsonify({"error": "query failed"}), 500

    missing = []
    for e in emps:
        nc = e["id"] not in checked_in
        ns = e["id"] not in did_standup
        if nc or ns:
            missing.append({**e, "needs_checkin": nc, "needs_standup": ns})

    return jsonify({
        "date": today,
        "weekend": False,
        "checked": len(emps),
        "on_leave": sorted(on_leave),
        "missing": missing,
    })


@companion_bp.route("/api/companion/whatsapp-outbox", methods=["GET"])
def companion_whatsapp_outbox():
    """Pending proactive WhatsApp messages for the laptop companion to deliver
    via the local Baileys bridge. Two kinds of rows:
      - immediate (send_after IS NULL): expired if older than 24h since
        creation -- a stale 'remind X' nudge delivered a day late is worse
        than not at all.
      - scheduled (send_after set, IST "YYYY-MM-DD HH:MM[:SS]" -- see
        schedule_group_message in whatsapp_agent.py): not returned at all
        until that time has passed, and expired only if more than 24h
        *past its own scheduled time* -- comparing against created_at here
        would wrongly expire anything scheduled more than a day out before
        it ever got a chance to send."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    utc_cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    now_ist_str = f"{utils.today_ist()} {utils.now_ist()}"
    ist_cutoff = (datetime.now(utils.IST) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "UPDATE whatsapp_outbox SET status='expired' "
                "WHERE status='pending' AND send_after IS NULL AND image_token IS NULL AND created_at < ?",
                (utc_cutoff,),
            )
            # Picture messages (weekly digest) stay useful for a few days.
            conn.execute(
                "UPDATE whatsapp_outbox SET status='expired' "
                "WHERE status='pending' AND image_token IS NOT NULL AND created_at < ?",
                ((datetime.utcnow() - timedelta(hours=72)).isoformat(),),
            )
            conn.execute(
                "UPDATE whatsapp_outbox SET status='expired' "
                "WHERE status='pending' AND send_after IS NOT NULL AND send_after < ?",
                (ist_cutoff,),
            )
        rows = conn.execute(
            "SELECT id, to_number, body, image_token FROM whatsapp_outbox "
            "WHERE status='pending' AND (send_after IS NULL OR send_after <= ?) "
            # Picture rows wait until the laptop says it can send pictures
            # (?images=1); an older laptop would send the caption without it.
            + ("" if request.args.get("images") == "1" else "AND image_token IS NULL ")
            + "ORDER BY id LIMIT 50",
            (now_ist_str,),
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("companion whatsapp-outbox failed")
        return jsonify({"error": "failed"}), 500
    return jsonify({"messages": [
        {"id": r[0], "to": r[1], "text": r[2],
         **({"image_url": f"/api/public/cal-image/{r[3]}.png"} if r[3] else {})}
        for r in rows]})


_OUTBOX_MAX_ATTEMPTS = 3


@companion_bp.route("/api/companion/whatsapp-outbox/ack", methods=["POST"])
def companion_whatsapp_outbox_ack():
    """Laptop reports which queued messages it delivered (or failed to).
    `sent` may be bare ids or {id, wa_id} objects (the WhatsApp message id
    from the bridge, for a delivery receipt). A failed row stays 'pending'
    for retry until it has missed _OUTBOX_MAX_ATTEMPTS times, then 'failed'."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    sent, failed = [], []
    for item in (body.get("sent") or []):
        if isinstance(item, dict) and str(item.get("id", "")).isdigit():
            sent.append((int(item["id"]), item.get("wa_id") or None))
        elif str(item).isdigit():
            sent.append((int(item), None))
    for i in (body.get("failed") or []):
        if str(i).isdigit():
            failed.append(int(i))
    now = datetime.utcnow().isoformat()
    gave_up = 0
    try:
        conn = get_connection()
        with conn:
            for i, wa_id in sent:
                conn.execute(
                    "UPDATE whatsapp_outbox SET status='sent', sent_at=?, wa_message_id=? WHERE id=?",
                    (now, wa_id, i),
                )
            for i in failed:
                row = conn.execute(
                    "SELECT COALESCE(attempts,0) FROM whatsapp_outbox WHERE id=?", (i,)
                ).fetchone()
                att = (row[0] if row else 0) + 1
                if att >= _OUTBOX_MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE whatsapp_outbox SET status='failed', attempts=?, sent_at=? WHERE id=?",
                        (att, now, i),
                    )
                    gave_up += 1
                else:
                    conn.execute(
                        "UPDATE whatsapp_outbox SET attempts=? WHERE id=?", (att, i)
                    )
        conn.close()
    except Exception:
        logger.exception("companion whatsapp-outbox ack failed")
        return jsonify({"error": "failed"}), 500
    return jsonify({"ok": True, "sent": len(sent), "retrying": len(failed) - gave_up,
                    "failed": gave_up})


@companion_bp.route("/api/companion/wa-call-outbox", methods=["GET"])
def companion_wa_call_outbox():
    """Pending one-way WhatsApp announcement calls (wa_outbox.enqueue_call)
    for the laptop companion to place via the local bridge's /call
    endpoint (whatsapp-bridge/voice.js -- Piper TTS + baileys-caller). Same
    24h-stale-expiry shape as /whatsapp-outbox above; no send_after here,
    every call is "place it now"."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    utc_cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "UPDATE wa_call_outbox SET status='expired' "
                "WHERE status='pending' AND created_at < ?",
                (utc_cutoff,),
            )
        rows = conn.execute(
            "SELECT id, to_number, message FROM wa_call_outbox "
            "WHERE status='pending' ORDER BY id LIMIT 20",
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("companion wa-call-outbox failed")
        return jsonify({"error": "failed"}), 500
    return jsonify({"calls": [{"id": r[0], "to": r[1], "message": r[2]} for r in rows]})


_CALL_OUTBOX_MAX_ATTEMPTS = 2   # a failed *call* (not message) is expensive to retry blindly


@companion_bp.route("/api/companion/wa-call-outbox/ack", methods=["POST"])
def companion_wa_call_outbox_ack():
    """Laptop reports which queued calls it placed (or failed to). Same
    retry-then-give-up shape as /whatsapp-outbox/ack, lower attempt cap --
    see _CALL_OUTBOX_MAX_ATTEMPTS."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    sent = [int(i) for i in (body.get("sent") or []) if str(i).isdigit()]
    failed = [int(i) for i in (body.get("failed") or []) if str(i).isdigit()]
    now = datetime.utcnow().isoformat()
    gave_up = 0
    try:
        conn = get_connection()
        with conn:
            for i in sent:
                conn.execute(
                    "UPDATE wa_call_outbox SET status='sent', sent_at=? WHERE id=?",
                    (now, i),
                )
            for i in failed:
                row = conn.execute(
                    "SELECT COALESCE(attempts,0) FROM wa_call_outbox WHERE id=?", (i,)
                ).fetchone()
                att = (row[0] if row else 0) + 1
                if att >= _CALL_OUTBOX_MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE wa_call_outbox SET status='failed', attempts=?, sent_at=? WHERE id=?",
                        (att, now, i),
                    )
                    gave_up += 1
                else:
                    conn.execute(
                        "UPDATE wa_call_outbox SET attempts=? WHERE id=?", (att, i)
                    )
        conn.close()
    except Exception:
        logger.exception("companion wa-call-outbox ack failed")
        return jsonify({"error": "failed"}), 500
    return jsonify({"ok": True, "sent": len(sent), "retrying": len(failed) - gave_up,
                    "failed": gave_up})


@companion_bp.route("/api/companion/wa-audit", methods=["GET"])
def companion_wa_audit():
    """Recent writes the WhatsApp agent made on someone's behalf."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except Exception:
        limit = 50
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT created_at, sender_name, scope, action, detail FROM wa_action_log "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("companion wa-audit failed")
        return jsonify({"error": "failed"}), 500
    return jsonify({"actions": [
        {"at": r[0], "by": r[1], "scope": r[2], "action": r[3], "detail": r[4]}
        for r in rows
    ]})


# need_for_approval = a social-media task ticked off in standup (work finished, awaiting sign-off)
_DONE_WORDS = {"done", "completed", "complete", "need_for_approval"}


def _eod_rows(today: str) -> dict:
    """{user_id: {"pending":[titles], "done_titles":[titles], "done":n, "total":n}}
    from today's standup_tasks, ignoring deleted/delegated rows."""
    out: dict = {}
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT user_id, title, status FROM standup_tasks "
            "WHERE date=? AND status NOT IN ('deleted','delegated') ORDER BY id",
            (today,),
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("eod-summary: standup query failed")
        return out
    for uid, title, status in rows:
        d = out.setdefault(uid, {"pending": [], "done_titles": [], "done": 0, "total": 0})
        d["total"] += 1
        if str(status or "").strip().lower() in _DONE_WORDS:
            d["done"] += 1
            d["done_titles"].append(title or "(untitled)")
        else:
            d["pending"].append(title or "(untitled)")
    return out


# sarcasm for a big day — >5 done. rotated by name+date so it's not identical.
_RAISE_LINES = [
    "  *{n}* tasks done in one day — get this one a raise.",
    "  *{n}* done today. An early mark tomorrow seems fair.",
    "  *{n}* tasks cleared in a day — someone approve a holiday.",
    "  *{n}* done. At this rate they're owed a raise AND an early leave.",
    "  *{n}* in one day. Give them the afternoon off, they've earned it.",
]


@companion_bp.route("/api/companion/eod-summary", methods=["GET"])
def companion_eod_summary():
    """End-of-day standup rollup for the laptop companion:
      group_text  -- 5pm: short per-person done-count line for the team group
      per_person  -- 7:30pm: each person's still-open tasks for their own DM
      leads_text / leads -- 7:30pm: cross-team 'not done' list for the leads
    """
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    today = utils.today_ist()
    try:
        weekend = datetime.strptime(today, "%Y-%m-%d").weekday() >= 5
    except Exception:
        weekend = False

    roster = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            roster.append({
                "id": e.get("id", ""),
                "name": e.get("name") or e.get("id", ""),
                "whatsapp": re.sub(r"\D", "", e.get("whatsapp", "") or ""),
            })
    except Exception:
        logger.exception("eod-summary: roster load failed")
        return jsonify({"error": "roster load failed"}), 500

    stats = _eod_rows(today)

    lead_ids = ["emp001", "emp004"]   # Vidit, Kshitij — overridable
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='eod_lead_ids'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            lead_ids = [x.strip() for x in str(row[0]).replace(",", " ").split() if x.strip()]
    except Exception:
        pass

    per_person, with_tasks, no_standup = [], [], []
    done_total = task_total = 0
    for r in roster:
        s = stats.get(r["id"])
        if not s:
            no_standup.append(r["name"])
            continue
        done_total += s["done"]
        task_total += s["total"]
        with_tasks.append((r, s))
        per_person.append({
            "id": r["id"], "name": r["name"], "whatsapp": r["whatsapp"],
            "pending": s["pending"], "done_titles": s["done_titles"],
            "done": s["done"], "total": s["total"],
        })

    if with_tasks:
        tick, cross = "✅", "❌"
        gl = [f"*Tasks done today: {done_total}/{task_total}* across the team."]
        for r, s in with_tasks:
            gl.append("")
            gl.append(f"*{r['name']}* ({s['done']}/{s['total']})")
            for t in s["done_titles"][:15]:
                gl.append(f"{tick} {t} - done")
            if len(s["done_titles"]) > 15:
                gl.append(f"+{len(s['done_titles']) - 15} more done")
            for t in s["pending"][:15]:
                gl.append(f"{cross} {t} - not done")
            if len(s["pending"]) > 15:
                gl.append(f"+{len(s['pending']) - 15} more not done")
        if no_standup:
            gl.append("")
            gl.append("No standup yet: " + ", ".join(no_standup))
        group_text = "\n".join(gl)
    else:
        group_text = ""

    # the leads' 7:30 DM uses the same layout as the 5 PM group post (header, then each
    # person with ✅ done / ❌ not done lines), plus a raise-or-holiday jab for a 5+ day.
    if with_tasks:
        tick, cross = "✅", "❌"
        ll = [f"*EOD check: {done_total}/{task_total} tasks done today* across the team."]
        for r, s in with_tasks:
            ll.append("")
            ll.append(f"*{r['name']}* ({s['done']}/{s['total']})")
            for t in s["done_titles"][:15]:
                ll.append(f"{tick} {t} - done")
            if len(s["done_titles"]) > 15:
                ll.append(f"+{len(s['done_titles']) - 15} more done")
            for t in s["pending"][:15]:
                ll.append(f"{cross} {t} - not done")
            if len(s["pending"]) > 15:
                ll.append(f"+{len(s['pending']) - 15} more not done")
            if s["done"] > 5:
                ll.append(_RAISE_LINES[(sum(ord(ch) for ch in r["name"] + today)) % len(_RAISE_LINES)]
                          .format(n=s["done"]).strip())
        if no_standup:
            ll.append("")
            ll.append("No standup yet: " + ", ".join(no_standup))
        leads_text = "\n".join(ll)
    else:
        leads_text = "EOD check: no standup tasks logged today."

    by_id = {r["id"]: r for r in roster}
    leads = [{"name": by_id[i]["name"], "whatsapp": by_id[i]["whatsapp"]}
             for i in lead_ids if i in by_id and by_id[i]["whatsapp"]]

    return jsonify({
        "date": today, "weekend": weekend,
        "group_text": group_text,
        "leads_text": leads_text,
        "leads": leads,
        "per_person": per_person,
    })


@companion_bp.route("/api/companion/weekly-summary", methods=["GET"])
def companion_weekly_summary():
    """Per-person completed-vs-total standup tasks for the current week
    (Monday -> today). Powers the Friday 6pm group wrap-up."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    today = utils.today_ist()
    try:
        d0 = datetime.strptime(today, "%Y-%m-%d")
    except Exception:
        return jsonify({"error": "bad date"}), 500
    monday = (d0 - timedelta(days=d0.weekday())).strftime("%Y-%m-%d")

    # A still-pending task gets a brand-new standup_tasks row every day it
    # carries over (same title, same carried_from = the ORIGIN day it first
    # went unfinished -- gotcha #7/#8) -- a plain COUNT(*) across a multi-day
    # range double(triple, ...)-counts that one task once per day it was
    # outstanding, inflating the denominator (and, for a task never
    # finished, contributing 0 done but N total, dragging the ratio down
    # for no real reason). Count distinct task LINEAGES for the total
    # instead, same idea as get_velocity_summary()'s dedup (gotcha #77) --
    # but that one's key is title+carried_from ALONE, which only works
    # because it deliberately excludes origin rows (carried_from IS NOT
    # NULL, "has this been carried at least once"). Here every row counts,
    # including a lineage's very first day, whose OWN row always has
    # carried_from=NULL (it hasn't been carried FROM anywhere yet) -- so the
    # dedup key has to fall back to that row's own `date` for the origin
    # day, or day 1 and days 2+ of the same lineage split into two "tasks".
    # `done` doesn't need any of this -- a task is only ever marked done on
    # the one row/day that happened, carry-over never re-inserts a done row.
    agg: dict = {}
    try:
        conn = get_connection()
        for uid, ct in conn.execute(
            "SELECT user_id, COUNT(DISTINCT title || '|' || COALESCE(carried_from, date)) "
            "FROM standup_tasks WHERE date >= ? AND date <= ? "
            "AND status NOT IN ('deleted','delegated') GROUP BY user_id",
            (monday, today)
        ).fetchall():
            agg.setdefault(uid, [0, 0])[1] = ct
        for uid, ct in conn.execute(
            "SELECT user_id, COUNT(*) FROM standup_tasks "
            "WHERE date >= ? AND date <= ? AND status IN ('done','completed','complete') "
            "GROUP BY user_id", (monday, today)
        ).fetchall():
            agg.setdefault(uid, [0, 0])[0] = ct
        conn.close()
    except Exception:
        logger.exception("weekly-summary: query failed")
        return jsonify({"error": "query failed"}), 500

    active = {}
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in _INACTIVE:
                continue
            active[e.get("id", "")] = e.get("name") or e.get("id", "")
    except Exception:
        pass

    people = sorted(
        ((active[u], a[0], a[1]) for u, a in agg.items() if u in active),
        key=lambda x: (-x[1], x[0]),
    )
    td = sum(p[1] for p in people)
    tt = sum(p[2] for p in people)
    if people:
        lines = [f"*Week wrap* ({monday} to {today}): *{td}/{tt}* tasks done."]
        lines += [f"  • {n}: {d}/{t}" for n, d, t in people]
        text = "\n".join(lines)
    else:
        text = ""
    return jsonify({"date": today, "monday": monday, "weekday": d0.weekday(),
                    "done": td, "total": tt, "text": text})


@companion_bp.route("/api/companion/digest", methods=["GET"])
def companion_digest():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    window = request.args.get("window", "morning").lower()
    if window not in ("morning", "evening"):
        window = "morning"
    try:
        return jsonify(_build_digest(window))
    except Exception:
        logger.exception("companion digest failed")
        return jsonify({"error": "digest failed"}), 500


# ── storage diagnostics (Railway volume full, 2026-09-28) ──────────────────

def _du(path: Path) -> tuple:
    """(total_bytes, file_count) for a file or a whole directory tree. Never
    raises -- a permission error or a symlink loop just gets skipped.

    Skips symlinks rather than following them (`is_symlink()` checked before
    `is_file()`, which itself follows a link by default). huggingface_hub's
    cache layout stores the real weights once under blobs/<hash> and adds a
    symlink to it under snapshots/<rev>/<filename> -- walking both without
    this guard double-counts every symlinked model file, which is exactly
    what happened the first time this route reported hf_cache's size (it
    read ~2x the real on-disk bytes, confirmed against the real
    shutil.disk_usage() delta from actually deleting it).
    """
    if not path.exists():
        return 0, 0
    if path.is_symlink():
        return 0, 0
    if path.is_file():
        try:
            return path.stat().st_size, 1
        except Exception:
            return 0, 0
    total = 0
    count = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_symlink():
                    continue
                if p.is_file():
                    total += p.stat().st_size
                    count += 1
            except Exception:
                continue
    except Exception:
        pass
    return total, count


@companion_bp.route("/api/companion/llm-status", methods=["GET"])
def companion_llm_status():
    """Which model the WhatsApp agent is using right now (Qwen via OpenRouter
    or Claude). ?test=1 makes one tiny live call to OpenRouter -- including a
    tool call, since the bot depends on tool calling -- and reports the result
    or the exact error. Switch back to Claude any time by setting
    WHATSAPP_LLM=claude (env) or the app_settings key whatsapp_llm."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import llm_openrouter as lo
    out = {
        "openrouter_key_set": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        "setting": lo._setting() or "(default)",
        "using": "qwen" if lo.enabled() else "claude",
        "qwen_model": lo.model_name(),
        "qwen_chain": lo.chain(),
        "prompt_caching": lo.supports_cache_control(),
    }
    if request.args.get("test") in ("1", "true"):
        if not out["openrouter_key_set"]:
            out["test"] = "OPENROUTER_API_KEY is not set on this server"
        else:
            try:
                tool = [{"name": "get_time", "description": "Returns the current time.",
                         "input_schema": {"type": "object", "properties": {}}}]
                r = lo.create(system="You are a test. Use the tool when asked.",
                              tools=tool,
                              messages=[{"role": "user", "content": "What time is it? Use the tool."}],
                              max_tokens=100, model=(request.args.get("model") or None))
                out["test"] = "ok"
                out["tool_call_made"] = any(b.type == "tool_use" for b in r.content)
                out["tokens"] = {"in": r.usage.input_tokens, "out": r.usage.output_tokens}
            except Exception as e:
                out["test"] = "failed"
                out["error"] = str(e)[:400]
    return jsonify(out)


@companion_bp.route("/api/companion/rhythm/run", methods=["POST"])
def companion_rhythm_run():
    """Test send of one rhythm message: ?kind=planning|reminder|approvals&to=emp003,..
    (`to` is required so it can never go to the whole company by accident)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import ops_rhythm
    ids = [x.strip() for x in (request.args.get("to") or "").split(",") if x.strip()]
    kind = request.args.get("kind", "")
    if kind not in ops_rhythm._WINDOWS or not ids:
        return jsonify({"error": "pass ?kind=planning|reminder|approvals&to=emp003"}), 400
    return jsonify(ops_rhythm.run_tick(force_kind=kind, only_ids=ids))


@companion_bp.route("/api/companion/ask-test", methods=["GET"])
def companion_ask_test():
    """Run one question through the real WhatsApp agent as an employee (default
    emp003) and return the reply plus which model answered. Nothing is sent to
    WhatsApp and the person's saved chat context is restored afterwards.
    ?q=...&user_id=emp003"""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import time
    import whatsapp_agent as wa
    uid = request.args.get("user_id", "emp003")
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "pass ?q="}), 400
    number = ""
    try:
        number = next((e.get("whatsapp", "") for e in utils._load_employees().get("employees", [])
                       if e.get("id") == uid), "")
    except Exception:
        pass
    if not number:
        return jsonify({"error": "that employee has no WhatsApp number"}), 400
    saved = None
    conn = get_connection()
    try:
        saved = conn.execute("SELECT messages, updated_at FROM whatsapp_agent_context WHERE sender=?",
                             (number,)).fetchone()
    finally:
        conn.close()
    seen = {}
    orig = wa._llm_create
    def spy(model, system, tools, messages, max_tokens, usage, smart=False, **kw):
        seen["smart"], seen["max_tokens"] = smart, max_tokens
        try:
            return orig(model, system, tools, messages, max_tokens, usage, smart=smart, **kw)
        finally:
            seen["tier"], seen["model"] = model.get("tier"), model.get("name")   # what really answered
    wa._llm_create = spy
    t0 = time.time()
    try:
        reply = wa.handle_message(number, q)
    finally:
        wa._llm_create = orig
        conn = get_connection()
        try:
            with conn:
                if saved:
                    conn.execute("UPDATE whatsapp_agent_context SET messages=?, updated_at=? WHERE sender=?",
                                 (saved[0], saved[1], number))
                else:
                    conn.execute("DELETE FROM whatsapp_agent_context WHERE sender=?", (number,))
        finally:
            conn.close()
    if isinstance(reply, dict):
        reply = reply.get("reply", "")
    out = {"reply": reply, "seconds": round(time.time() - t0, 1), **seen}
    if reply and reply.startswith("Sorry, I hit an error") and wa.LAST_ERROR.get("trace"):
        out["error_trace"] = wa.LAST_ERROR["trace"]
    return jsonify(out)


@companion_bp.route("/api/companion/attendance-fix", methods=["POST"])
def companion_attendance_fix():
    """HR correction of one day's checkout time (token-gated).
    ?user_id=emp003&date=2026-10-16&checkout=19:33   (checkout = HH:MM, IST)
    Only touches an existing row that already has a check-in, and never moves
    checkout earlier than check-in. Returns the before/after times."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import re as _re
    uid = (request.args.get("user_id") or "").strip()
    day = (request.args.get("date") or "").strip()
    co = (request.args.get("checkout") or "").strip()
    ci_new = (request.args.get("checkin") or "").strip()      # optional: also correct the check-in
    hm = r"([01]\d|2[0-3]):[0-5]\d"
    if (not uid or not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not _re.fullmatch(hm, co)
            or (ci_new and not _re.fullmatch(hm, ci_new))):
        return jsonify({"error": "pass user_id, date=YYYY-MM-DD, checkout=HH:MM (optional checkin=HH:MM)"}), 400
    conn = get_connection()
    try:
        row = conn.execute("SELECT checkin_time, checkout_time FROM daily_attendance WHERE user_id=? AND date=?",
                           (uid, day)).fetchone()
        if not row or not row[0]:
            return jsonify({"error": "no check-in recorded for that person and date"}), 404
        ci_final = (ci_new + ":00") if ci_new else str(row[0])
        if co + ":00" <= ci_final:
            return jsonify({"error": "checkout must be after check-in", "checkin": ci_final}), 400
        with conn:
            conn.execute("UPDATE daily_attendance SET checkin_time=?, checkout_time=? WHERE user_id=? AND date=?",
                         (ci_final, co + ":00", uid, day))
    finally:
        conn.close()
    logger.warning("attendance-fix: %s %s in %s -> %s, out %s -> %s:00", uid, day, row[0], ci_final, row[1], co)
    return jsonify({"success": True, "user_id": uid, "date": day, "checkin_before": row[0],
                    "checkin_after": ci_final, "checkout_before": row[1], "checkout_after": co + ":00"})


@companion_bp.route("/api/companion/group-summary-debug", methods=["GET"])
def companion_group_summary_debug():
    """Run the group summary once and show the model's raw points and which the
    checker accepted/rejected (token-gated). ?q=what was discussed in the group today"""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import group_digest
    import whatsapp_agent as wa
    q = (request.args.get("q") or "what was discussed in the group today").strip()
    group_digest.LAST.clear()
    out = group_digest.build(q, wa._group_allowlist(), llm=wa._digest_llm, llm_smart=wa._digest_llm_smart)
    return jsonify({"reply": out, "attempt": group_digest.LAST})


@companion_bp.route("/api/companion/morning-dash", methods=["GET"])
def companion_morning_dash():
    """Preview one person's dashboard (nothing sent): ?user_id=emp001"""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import morning_dash
    import week_digest
    uid = request.args.get("user_id", "emp003")
    emp = (week_digest._people(only_ids=[uid]) or [None])[0]
    if not emp:
        return jsonify({"error": "unknown or inactive employee"}), 404
    return jsonify({"text": morning_dash.build(emp)})


@companion_bp.route("/api/companion/morning-dash/run", methods=["POST"])
def companion_morning_dash_run():
    """Force-send dashboards to the given ids only: ?to=emp003,emp001"""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import morning_dash
    ids = [x.strip() for x in (request.args.get("to") or "").split(",") if x.strip()]
    return jsonify(morning_dash.run_daily(force=True, only_ids=ids))


@companion_bp.route("/api/companion/lid-alias", methods=["POST"])
def companion_lid_alias():
    """Tell the bot who an @mention number is: ?digits=71884968308912&name=Charulata
    (rewrites messages already stored). Token-gated."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import group_memory
    n = group_memory.set_alias(request.args.get("digits", ""), request.args.get("name", ""))
    return jsonify({"rewritten": n})


@companion_bp.route("/api/companion/mgmt-view", methods=["GET"])
def companion_mgmt_view():
    """Preview a management answer: ?view=at_risk|blocked|needs_vg|completed_week|
    overloaded|waiting_on_me|my_approvals&user_id=emp003 (user_id = whose point of view)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import mgmt_views
    uid = request.args.get("user_id", "emp003")
    name = ""
    try:
        name = next((e.get("name", "") for e in utils._load_employees().get("employees", [])
                     if e.get("id") == uid), "")
    except Exception:
        pass
    return jsonify({"text": mgmt_views.view(request.args.get("view", ""),
                                            {"kind": "employee", "id": uid, "name": name})})


@companion_bp.route("/api/companion/client-card", methods=["GET"])
def companion_client_card():
    """Preview the status card: ?client=Mellow"""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import client_card
    return jsonify({"card": client_card.card(request.args.get("client", ""))})


@companion_bp.route("/api/companion/week-digest", methods=["GET", "POST"])
def companion_week_digest():
    """GET: preview the coming week's text (sends nothing). POST ?to=emp003,emp001
    force-sends the Sunday digest to just those people (testing); without `to`
    it is refused so it can't be used to blast the whole company by accident."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import week_digest
    if request.method == "GET":
        mon = datetime.now(utils.IST).date()
        mon = mon + timedelta(days=7 - mon.weekday())
        return jsonify({"week": mon.isoformat(), "text": week_digest.week_text(mon)})
    to = (request.args.get("to") or "").strip()
    ids = [x.strip() for x in to.split(",") if x.strip() and x.strip() != "all"]
    if not ids and to != "all":
        return jsonify({"error": "pass ?to=emp003,... or ?to=all"}), 400
    return jsonify(week_digest.run_weekly(force=True, only_ids=ids or None,
                                          this_week=request.args.get("week") == "this"))


@companion_bp.route("/api/companion/email-status", methods=["GET"])
def companion_email_status():
    """Is outbound email (mailer.py, SMTP_* env vars) configured, and who
    gets leave-request mail. ?test=1 sends a real test message to HR
    synchronously and reports whether the SMTP server accepted it."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import mailer
    out = {
        "configured": mailer.is_configured(),
        "provider": mailer.provider(),
        "smtp_host": os.getenv("SMTP_HOST", ""),
        "smtp_port": os.getenv("SMTP_PORT", "587"),
        "sender": os.getenv("SMTP_USER", ""),
        "hr_email": mailer.email_for(mailer.HR_USER_ID),
    }
    if request.args.get("test") and out["configured"]:
        out["test_sent"] = mailer.send(
            out["hr_email"], "Lumina email test",
            "This is a test from Lumina. Leave requests will arrive at this address.",
            wait=True)
        if not out["test_sent"]:
            out["error"] = mailer.last_error
    if request.args.get("diag"):
        # Which outbound connections can this host actually make? Separates
        # "wrong password" from "the platform blocks SMTP" (Railway blocks
        # outbound SMTP on some plans) and from IPv6-only routing.
        import socket
        diag = {}
        host = out["smtp_host"] or "smtp.gmail.com"
        for fam, label in ((socket.AF_INET, "ipv4"), (socket.AF_INET6, "ipv6")):
            try:
                addrs = socket.getaddrinfo(host, None, fam)
                diag[f"{label}_resolves"] = bool(addrs)
            except Exception as e:
                diag[f"{label}_resolves"] = f"no ({e})"
                continue
            for port in (587, 465, 2525, 443):
                try:
                    s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(6)
                    s.connect((addrs[0][4][0], port) if fam == socket.AF_INET
                              else (addrs[0][4][0], port, 0, 0))
                    s.close(); diag[f"{label}:{port}"] = "open"
                except Exception as e:
                    diag[f"{label}:{port}"] = f"{type(e).__name__}: {e}"[:80]
        out["diag"] = diag
    return jsonify(out)


@companion_bp.route("/api/companion/storage-report", methods=["GET"])
def companion_storage_report():
    """What's actually eating the Railway volume -- read-only, no side
    effects. Built specifically to answer that question without needing
    Railway CLI/shell access, since this session has neither.

    Breaks down: the live sqlite file + its WAL/SHM (WAL mode never gets
    cleaned up by itself if nothing ever checkpoints -- see db.py), the
    RotatingFileHandler's app.log (capped ~15MB, unlikely to be the culprit
    but reported for completeness), logs/uploads/ (client dependency files
    and voice notes -- gotcha #32 -- which have NO retention/cleanup policy
    anywhere in this codebase and are the leading suspect for slow organic
    growth), and the semantic-KB model cache dir if it exists.
    """
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        db_dir = Path(__file__).parent.parent.parent / "logs"
        db_path_env = os.getenv("DB_PATH", "")
        db_path = Path(db_path_env) if db_path_env else (db_dir / "app.db")

        def entry(label, p):
            size, count = _du(p)
            return {"path": str(p), "bytes": size, "mb": round(size / 1_000_000, 2),
                    "files": count}

        items = [
            entry("app.db", db_path),
            entry("app.db-wal", Path(str(db_path) + "-wal")),
            entry("app.db-shm", Path(str(db_path) + "-shm")),
            entry("app.log (rotated)", db_dir / "app.log"),
            entry("uploads/", db_dir / "uploads"),
            entry("hf_cache/ (semantic KB model)", db_dir / "hf_cache"),
        ]
        # Anything else sitting directly in logs/ that isn't one of the above
        # -- catches a stray file/folder nobody's accounted for.
        known = {"app.db", "app.db-wal", "app.db-shm", "app.log", "uploads", "hf_cache"}
        other = []
        try:
            for p in db_dir.iterdir():
                if p.name not in known:
                    other.append(entry(p.name, p))
        except Exception:
            pass

        disk_total, disk_used, disk_free = None, None, None
        try:
            disk_total, disk_used, disk_free = shutil.disk_usage(str(db_dir))
        except Exception:
            pass

        items_sorted = sorted(items + other, key=lambda x: -x["bytes"])
        return jsonify({
            "logs_dir": str(db_dir),
            "breakdown": items_sorted,
            "total_accounted_mb": round(sum(i["bytes"] for i in items_sorted) / 1_000_000, 2),
            "disk": None if disk_total is None else {
                "total_mb": round(disk_total / 1_000_000, 2),
                "used_mb": round(disk_used / 1_000_000, 2),
                "free_mb": round(disk_free / 1_000_000, 2),
                "used_pct": round(100 * disk_used / disk_total, 1) if disk_total else None,
            },
        })
    except Exception:
        logger.exception("companion storage-report failed")
        return jsonify({"error": "storage report failed"}), 500


@companion_bp.route("/api/companion/storage-cleanup", methods=["POST"])
def companion_storage_cleanup():
    """Free space on a nearly-full volume. `targets` (comma-separated query
    param, default 'hf_cache') selects what to run:

      hf_cache -- deletes semantic_kb.py's downloaded embedding-model cache.
                  Zero risk: semantic_kb.py is explicitly designed to
                  re-download the ~30MB model lazily the next time semantic
                  KB search runs (see CLAUDE.md gotcha #98/#112) -- nothing
                  else reads this directory. Do this FIRST when the disk is
                  nearly full.
      vacuum   -- runs SQLite VACUUM on the live app.db to reclaim space
                  left behind by deleted rows (SQLite never shrinks a file
                  on DELETE by itself). Safe even to attempt on a full disk:
                  VACUUM builds a fresh copy in a temp file first and only
                  swaps it in atomically at the end, so running out of space
                  mid-VACUUM just fails cleanly -- the live database is
                  never touched until the rebuild has fully succeeded. Only
                  actually try this once 'hf_cache' (or something else) has
                  freed real headroom -- attempting it while free space is
                  ~0 will just error out with "database or disk is full".
      wal_checkpoint -- PRAGMA wal_checkpoint(TRUNCATE): writes every
                  pending frame in app.db-wal back into app.db and truncates
                  the WAL file to 0 bytes. Unlike VACUUM this needs no extra
                  free space (it moves already-allocated content, it doesn't
                  build a new copy) -- use this FIRST when a large write
                  (e.g. a big DELETE) failed partway and left a bloated
                  app.db-wal holding all the free space hostage (CLAUDE.md
                  gotcha, 2026-09-28 round 2: a 207K-row DELETE grew the WAL
                  to 61MB before failing, and the WAL doesn't auto-shrink
                  back down just because the transaction that grew it
                  rolled back).

    Pass targets=hf_cache,vacuum to do both in one call, in that order.
    """
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    raw = (request.args.get("targets") or request.form.get("targets")
           or "hf_cache")
    targets = {t.strip() for t in raw.split(",") if t.strip()}
    db_dir = Path(__file__).parent.parent.parent / "logs"
    result = {}

    if "hf_cache" in targets:
        cache_dir = db_dir / "hf_cache"
        before_bytes, before_n = _du(cache_dir)
        try:
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            result["hf_cache"] = {
                "freed_mb": round(before_bytes / 1_000_000, 2),
                "files_removed": before_n,
            }
        except Exception as e:
            result["hf_cache"] = {"error": str(e)}

    if "vacuum" in targets:
        db_path_env = os.getenv("DB_PATH", "")
        db_path = Path(db_path_env) if db_path_env else (db_dir / "app.db")
        try:
            before = db_path.stat().st_size if db_path.exists() else 0
            conn = get_connection()
            conn.execute("VACUUM")
            conn.close()
            after = db_path.stat().st_size if db_path.exists() else 0
            result["vacuum"] = {
                "before_mb": round(before / 1_000_000, 2),
                "after_mb": round(after / 1_000_000, 2),
                "freed_mb": round((before - after) / 1_000_000, 2),
            }
        except Exception as e:
            result["vacuum"] = {"error": str(e)}

    if "wal_checkpoint" in targets:
        db_path_env = os.getenv("DB_PATH", "")
        db_path = Path(db_path_env) if db_path_env else (db_dir / "app.db")
        wal_path = db_path.parent / (db_path.name + "-wal")
        try:
            before_wal = wal_path.stat().st_size if wal_path.exists() else 0
            conn = get_connection()
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.close()
            after_wal = wal_path.stat().st_size if wal_path.exists() else 0
            result["wal_checkpoint"] = {
                "before_wal_mb": round(before_wal / 1_000_000, 2),
                "after_wal_mb": round(after_wal / 1_000_000, 2),
                # PRAGMA wal_checkpoint returns (busy, log_frames, checkpointed_frames)
                "busy": bool(row[0]) if row else None,
                "log_frames": row[1] if row else None,
                "checkpointed_frames": row[2] if row else None,
            }
        except Exception as e:
            result["wal_checkpoint"] = {"error": str(e)}

    try:
        disk_total, disk_used, disk_free = shutil.disk_usage(str(db_dir))
        result["disk_after"] = {
            "total_mb": round(disk_total / 1_000_000, 2),
            "used_mb": round(disk_used / 1_000_000, 2),
            "free_mb": round(disk_free / 1_000_000, 2),
            "used_pct": round(100 * disk_used / disk_total, 1) if disk_total else None,
        }
    except Exception:
        pass
    return jsonify(result)


@companion_bp.route("/api/companion/db-table-sizes", methods=["GET"])
def companion_db_table_sizes():
    """Per-table breakdown of app.db itself -- storage-report only shows
    app.db as one 350MB blob; this answers 'which TABLE is actually big'.
    Uses SQLite's built-in dbstat virtual table (real per-table byte usage,
    not just row counts) when available, falling back to row counts alone
    if dbstat isn't compiled into this Python's sqlite3 build."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")
        tables = [r[0] for r in cur.fetchall()]

        sizes = {}
        dbstat_ok = True
        try:
            cur.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")
            sizes = {r[0]: r[1] for r in cur.fetchall()}
        except Exception:
            dbstat_ok = False

        rows = []
        for t in tables:
            try:
                count = cur.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]
            except Exception:
                count = None
            b = sizes.get(t)
            rows.append({
                "table": t, "rows": count,
                "bytes": b, "mb": round(b / 1_000_000, 2) if b else None,
            })
        conn.close()
        rows.sort(key=lambda r: (r["bytes"] or 0, r["rows"] or 0), reverse=True)
        return jsonify({"dbstat_available": dbstat_ok, "tables": rows})
    except Exception:
        logger.exception("companion db-table-sizes failed")
        return jsonify({"error": "table size report failed"}), 500


@companion_bp.route("/api/companion/sheet-edit-log-stats", methods=["GET"])
def companion_sheet_edit_log_stats():
    """Diagnostic for a bloated sheet_edit_log (CLAUDE.md gotcha, 2026-09-28
    round 2): db-table-sizes only says the table is big, this says WHY --
    which client/task is generating the most versions, and the date range,
    so a prune policy can be chosen with real numbers instead of a guess."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        conn = get_connection()
        cur = conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM sheet_edit_log").fetchone()[0]
        distinct_tasks = cur.execute("SELECT COUNT(DISTINCT task_id) FROM sheet_edit_log").fetchone()[0]
        date_range = cur.execute("SELECT MIN(edited_at), MAX(edited_at) FROM sheet_edit_log").fetchone()
        cur.execute("""SELECT client_id, COUNT(*) AS c FROM sheet_edit_log
                       GROUP BY client_id ORDER BY c DESC LIMIT 15""")
        by_client = [{"client_id": r[0], "rows": r[1]} for r in cur.fetchall()]
        cur.execute("""SELECT task_id, client_id, COUNT(*) AS c,
                              MIN(edited_at) AS first_edit, MAX(edited_at) AS last_edit
                       FROM sheet_edit_log GROUP BY task_id ORDER BY c DESC LIMIT 15""")
        by_task = [{"task_id": r[0], "client_id": r[1], "rows": r[2],
                    "first_edit": r[3], "last_edit": r[4]} for r in cur.fetchall()]
        conn.close()
        return jsonify({
            "total_rows": total,
            "distinct_tasks": distinct_tasks,
            "avg_rows_per_task": round(total / distinct_tasks, 1) if distinct_tasks else None,
            "date_range": {"earliest": date_range[0], "latest": date_range[1]},
            "top_clients_by_row_count": by_client,
            "top_tasks_by_row_count": by_task,
        })
    except Exception:
        logger.exception("companion sheet-edit-log-stats failed")
        return jsonify({"error": "sheet-edit-log stats failed"}), 500


@companion_bp.route("/api/companion/client-real-task-count", methods=["GET"])
def companion_client_real_task_count():
    """Read-only: how many REAL tasks a client currently has in Notion right
    now, compared against how many distinct task_ids sheet_edit_log has
    logged for that same client -- answers "did a sync malfunction actually
    leave garbage tasks behind in Notion, or was it all created-and-deleted
    churn that's already gone" without guessing or touching anything.
    CLAUDE.md gotcha, 2026-09-28 round 2 (Omotec sync burst investigation).
    ?client_id= is the client's Notion page id (client_notion_id)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    client_id = (request.args.get("client_id") or "").strip()
    if not client_id:
        return jsonify({"error": "client_id required"}), 400
    try:
        import notion_store
        if not notion_store.is_configured():
            return jsonify({"error": "Notion not configured"}), 400
        real_tasks = notion_store.list_tasks(client_notion_id=client_id)
        conn = get_connection()
        logged_task_ids = conn.execute(
            "SELECT COUNT(DISTINCT task_id) FROM sheet_edit_log WHERE client_id=?", (client_id,)
        ).fetchone()[0]
        conn.close()
        return jsonify({
            "client_id": client_id,
            "real_tasks_in_notion_now": len(real_tasks),
            "distinct_task_ids_ever_logged": logged_task_ids,
            "sample_titles": [t.get("title") for t in real_tasks[:10]],
        })
    except Exception:
        logger.exception("companion client-real-task-count failed")
        return jsonify({"error": "check failed"}), 500


@companion_bp.route("/api/companion/group-allowlist", methods=["GET", "POST"])
def companion_group_allowlist():
    """View/edit the WhatsApp group allow-list via the companion token
    (STORAGE_SYNC_TOKEN/FLASK_SECRET_KEY) -- the same setting
    /api/whatsapp/groups manages, but that route is gated by the SEPARATE
    WHATSAPP_BRIDGE_TOKEN (app.py::_bridge_auth_ok, checked first if set),
    which isn't always on hand. Same underlying storage
    (app_settings.whatsapp_group_allowlist, a JSON array, merged at
    read-time with any WHATSAPP_GROUP_ALLOWLIST env var by
    whatsapp_agent._group_allowlist()) so a change here takes effect
    immediately, no redeploy.

      GET                    -> {stored, env, effective}
      POST {"add": "<id>"}   -> add a group (numeric id or full ...@g.us)
      POST {"remove": "<id>"} -> remove one

    CLAUDE.md gotcha, 2026-09-29: used to diagnose "the bot replies to DMs
    but not in the group" -- DM working ruled out a dead bridge, pointing
    straight at this allow-list.
    """
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    def _digits(s):
        return re.sub(r"\D", "", str(s or ""))

    def _load():
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key='whatsapp_group_allowlist'"
            ).fetchone()
            conn.close()
            if row and row[0]:
                return [str(x) for x in json.loads(row[0])]
        except Exception:
            logger.exception("companion group-allowlist load failed")
        return []

    env_ids = sorted({_digits(t) for t in re.split(r"[,\s]+", os.getenv("WHATSAPP_GROUP_ALLOWLIST", "")) if _digits(t)})
    stored = _load()

    if request.method == "GET":
        return jsonify({
            "stored": stored,
            "env": env_ids,
            "effective": sorted(set(stored) | set(env_ids)),
        })

    body = request.get_json(silent=True) or {}
    add_id = _digits(body.get("add", ""))
    remove_id = _digits(body.get("remove", ""))
    if not add_id and not remove_id:
        return jsonify({"error": "pass 'add' or 'remove' with a group id"}), 400
    ids = set(stored)
    if add_id:
        ids.add(add_id)
    if remove_id:
        ids.discard(remove_id)
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('whatsapp_group_allowlist', ?)",
                (json.dumps(sorted(ids)),),
            )
        conn.close()
    except Exception:
        logger.exception("companion group-allowlist save failed")
        return jsonify({"error": "save failed"}), 500
    return jsonify({"stored": sorted(ids), "env": env_ids, "effective": sorted(ids | set(env_ids))})


@companion_bp.route("/api/companion/wa-context-keys", methods=["GET"])
def companion_wa_context_keys():
    """Read-only, keys + timestamps only (never message content): recent
    whatsapp_agent_context rows. The sender key is 'digits' for a DM or
    'digits|group_digits' for a group thread (see whatsapp_agent.py) -- so
    this answers "has a group conversation ever actually been built" without
    exposing any chat content. Diagnostic for the 2026-09-29 "bot replies to
    DMs but not in the group" investigation."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT sender, updated_at FROM whatsapp_agent_context ORDER BY updated_at DESC LIMIT 30"
        ).fetchall()
        conn.close()
        return jsonify({
            "rows": [{"sender": r[0], "updated_at": r[1], "is_group": "|" in r[0]} for r in rows],
        })
    except Exception:
        logger.exception("companion wa-context-keys failed")
        return jsonify({"error": "check failed"}), 500


# ── proactive follow-ups (backend/followups.py) ────────────────────────────

@companion_bp.route("/api/companion/followups", methods=["GET"])
def companion_followups():
    """What the initiative engine currently sees, WITHOUT sending anything.

    Sends nothing and records nothing -- this is the inspection window into a
    feature that otherwise only shows itself by messaging people. The sweep
    itself runs on the Railway scheduler (task_scheduler._run_followup_sweep),
    not from the laptop, since delivery goes through wa_outbox either way.
    """
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import followups
        return jsonify(followups.sweep(dry_run=True))
    except Exception:
        logger.exception("companion followups (dry run) failed")
        return jsonify({"error": "followups failed"}), 500


@companion_bp.route("/api/companion/followups/run", methods=["POST"])
def companion_followups_run():
    """Force a real sweep now. Still obeys every rail inside sweep() --
    working-hours window, leave, per-item cooldown, per-person daily cap --
    so calling this repeatedly can't be used to spam anyone."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import followups
        return jsonify(followups.sweep())
    except Exception:
        logger.exception("companion followups run failed")
        return jsonify({"error": "followups failed"}), 500


# ── group chat intelligence (backend/group_intel.py) ────────────────────────

@companion_bp.route("/api/companion/group-commitments", methods=["GET"])
def companion_group_commitments():
    """Promises tracked from the team group chat (?status=open|done|dropped|all,
    ?person=). The inspection window for group_intel."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import group_intel
        rows = group_intel.list_commitments(request.args.get("person", ""),
                                            request.args.get("status", "open"), 100)
        return jsonify({"count": len(rows), "commitments": rows,
                        "status": group_intel.stats()})
    except Exception:
        logger.exception("companion group-commitments failed")
        return jsonify({"error": "failed"}), 500


@companion_bp.route("/api/companion/group-commitments/reject", methods=["POST"])
def companion_group_commitment_reject():
    """Close a tracked item that was never a real commitment (?id=24&note=...).
    Also teaches the extractor (same as the boss replying 'that wasn't a task')."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import group_intel
        cid = int(request.args.get("id", "0"))
        ok = group_intel.reject_commitment(cid, request.args.get("note", "") or "not a promise")
        return jsonify({"closed": bool(ok)})
    except Exception:
        logger.exception("companion group-commitment reject failed")
        return jsonify({"error": "failed"}), 500


@companion_bp.route("/api/companion/group-intel/run", methods=["POST"])
def companion_group_intel_run():
    """Run one extraction pass now (normally every 8 min on the scheduler)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import group_intel
        return jsonify(group_intel.extract_pending())
    except Exception:
        logger.exception("companion group-intel run failed")
        return jsonify({"error": "failed"}), 500


@companion_bp.route("/api/companion/group-intel/simulate", methods=["POST"])
def companion_group_intel_simulate():
    """Dry run: body {"messages":[{"name","text"}]}. Runs the REAL model over the
    sample messages and reports what the group pipeline would do (tasks, DMs,
    facts). Writes nothing and messages nobody."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import group_intel
        return jsonify(group_intel.simulate((request.get_json(silent=True) or {}).get("messages") or []))
    except Exception:
        logger.exception("companion group-intel simulate failed")
        return jsonify({"error": "failed"}), 500


@companion_bp.route("/api/companion/nightly-group-summary", methods=["POST"])
def companion_nightly_group_summary():
    """Send the daily group summary now to the named people only (?to=emp003,emp001) --
    for testing. The real send is the 22:30 IST scheduler job (group_digest.run_nightly)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    to = [x for x in re.split(r"[\s,]+", request.args.get("to", "")) if x]
    if not to:
        return jsonify({"error": "to=<employee ids> is required"}), 400
    try:
        import group_digest
        return jsonify(group_digest.run_nightly(force_ids=to))
    except Exception:
        logger.exception("companion nightly group summary failed")
        return jsonify({"error": "failed"}), 500


@companion_bp.route("/api/companion/pa-brief", methods=["GET"])
def companion_pa_brief():
    """Preview the boss's morning brief (?user_id=emp003) without sending it."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import pa_brief
        import utils
        uid = request.args.get("user_id", "emp003")
        emp = next((e for e in utils._load_employees().get("employees", []) if e.get("id") == uid), {"id": uid, "name": uid})
        parts = pa_brief.gather()
        return jsonify({"facts": pa_brief.facts_text(parts),
                        "brief": pa_brief.build_brief(emp) if request.args.get("write") == "1" else None})
    except Exception:
        logger.exception("companion pa-brief failed")
        return jsonify({"error": "failed"}), 500


@companion_bp.route("/api/companion/calendar-status", methods=["GET"])
def companion_calendar_status():
    """Is Google Calendar connected? ?test=1 fetches today's events."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import gcal
        import google_sheets_store as gs
        out = {"configured": gcal.configured(), "calendar_ids": gcal.calendar_ids(),
               "share_calendars_with": gs.service_account_email()}
        if request.args.get("test") == "1":
            out["today"] = gcal.format_events(gcal.upcoming(days=1))
        return jsonify(out)
    except Exception:
        logger.exception("companion calendar-status failed")
        return jsonify({"error": "failed"}), 500


# ── data retention (backend/data_retention.py) ─────────────────────────────

@companion_bp.route("/api/companion/retention", methods=["GET"])
def companion_retention_dry_run():
    """What the retention sweep would delete right now, without deleting
    anything. Same inspection-before-acting pattern as
    GET /api/companion/followups."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import data_retention
        return jsonify(data_retention.run_retention_sweep(dry_run=True))
    except Exception:
        logger.exception("companion retention (dry run) failed")
        return jsonify({"error": "retention sweep failed"}), 500


@companion_bp.route("/api/companion/retention/run", methods=["POST"])
def companion_retention_run():
    """Force a real retention sweep now, instead of waiting for the daily
    03:30 IST scheduled job (task_scheduler.py)."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import data_retention
        return jsonify(data_retention.run_retention_sweep())
    except Exception:
        logger.exception("companion retention run failed")
        return jsonify({"error": "retention sweep failed"}), 500


@companion_bp.route("/api/companion/ot-ledger-summary", methods=["GET"])
def companion_ot_ledger_summary():
    """Read-only: exactly what's in overtime_ledger/comp_off_ledger right
    now, so a correction to either can be targeted precisely instead of
    guessed at — same "inspect before you touch balance data" discipline
    as every other destructive-adjacent companion route in this file."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        from utils import today_ist
        conn = get_connection()
        comp_rows = conn.execute(
            "SELECT id, user_id, days, source_ot_hours, created_at "
            "FROM comp_off_ledger ORDER BY created_at DESC"
        ).fetchall()
        unconverted = conn.execute(
            "SELECT user_id, date, worked_hours, ot_hours, created_at "
            "FROM overtime_ledger WHERE converted_at IS NULL ORDER BY date ASC"
        ).fetchall()
        converted_count = conn.execute(
            "SELECT COUNT(*) FROM overtime_ledger WHERE converted_at IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        return jsonify({
            "today": today_ist(),
            "comp_off_ledger": [
                {"id": r[0], "user_id": r[1], "days": r[2], "source_ot_hours": r[3],
                 "created_at": r[4]} for r in comp_rows
            ],
            "unconverted_overtime_rows": [
                {"user_id": r[0], "date": r[1], "worked_hours": r[2], "ot_hours": r[3],
                 "created_at": r[4]} for r in unconverted
            ],
            "converted_overtime_row_count": converted_count,
        })
    except Exception:
        logger.exception("companion ot-ledger-summary failed")
        return jsonify({"error": "lookup failed"}), 500


@companion_bp.route("/api/companion/backfill-overtime", methods=["POST"])
def companion_backfill_overtime():
    """One-time catch-up: compute overtime_ledger rows for every day of
    real attendance history that predates the nightly conversion sweep
    (leave management system shipped 2026-09-30) — see
    leave_store.backfill_overtime_ledger()'s docstring. Idempotent, safe
    to call more than once."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import leave_store
        return jsonify(leave_store.backfill_overtime_ledger())
    except Exception:
        logger.exception("companion backfill-overtime failed")
        return jsonify({"error": "backfill failed"}), 500


@companion_bp.route("/api/companion/freeze-ot-backlog", methods=["POST"])
def companion_freeze_ot_backlog():
    """One-time correction for the backfill above: reverts any comp-off
    days it granted from pre-existing attendance history and freezes that
    whole backlog out of future conversion, so the 24h carry-forward
    cycle starts fresh from today — see
    leave_store.freeze_backlog_and_revert_grants()'s docstring."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import leave_store
        return jsonify(leave_store.freeze_backlog_and_revert_grants())
    except Exception:
        logger.exception("companion freeze-ot-backlog failed")
        return jsonify({"error": "freeze failed"}), 500


# ── uploads archive ────────────────────────────────────────────────────────

@companion_bp.route("/api/companion/uploads-archive", methods=["GET"])
def companion_uploads_archive():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not _UPLOADS.exists():
        return jsonify({"error": "no uploads dir", "files": 0}), 404
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in _UPLOADS.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(_UPLOADS).as_posix())
                n += 1
    if n == 0:
        return jsonify({"error": "uploads dir empty", "files": 0}), 404
    buf.seek(0)
    stamp = utils.today_ist()
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"uploads-{stamp}.zip")


# ── sheets health / bulk pull ──────────────────────────────────────────────

@companion_bp.route("/api/companion/sheets-health", methods=["GET"])
def companion_sheets_health():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    out = []
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT client_id, client_name, last_push_at, last_push_ok, "
            "last_pull_at, last_pull_summary FROM google_sheet_links"
        ).fetchall()
        conn.close()
        for cid, cname, lpush, ok, lpull, summ in rows:
            stale = False
            if lpull:
                try:
                    d = datetime.strptime(str(lpull)[:10], "%Y-%m-%d")
                    stale = (datetime.now() - d).days >= 3
                except Exception:
                    pass
            out.append({
                "client_id": cid, "client_name": cname,
                "last_push_at": lpush, "last_push_ok": (None if ok is None else bool(ok)),
                "last_pull_at": lpull, "last_pull_summary": summ,
                "push_failing": ok == 0, "pull_stale": stale,
            })
    except Exception:
        logger.exception("companion sheets-health failed")
        return jsonify({"error": "failed"}), 500
    issues = [r for r in out if r["push_failing"] or r["pull_stale"]]
    return jsonify({"links": out, "count": len(out), "issues": len(issues)})


@companion_bp.route("/api/companion/sheets-pull-all", methods=["POST"])
def companion_sheets_pull_all():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    from routes.sheets_sync import pull_one_link
    results = []
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT client_id, spreadsheet_id, link_token, is_notion, client_name, "
            "linked_by, multi_tab FROM google_sheet_links"
        ).fetchall()
        conn.close()
    except Exception:
        logger.exception("companion pull-all: link list failed")
        return jsonify({"error": "failed"}), 500

    for r in rows:
        link = {"client_id": r[0], "spreadsheet_id": r[1], "is_notion": bool(r[3]),
                "client_name": r[4], "linked_by": r[5], "multi_tab": bool(r[6])}
        try:
            summary = pull_one_link(link, r[2])
            results.append({"client": r[4] or r[0], "ok": True, **summary})
        except Exception as e:
            logger.warning("companion pull-all: %s failed: %s", r[4] or r[0], e)
            results.append({"client": r[4] or r[0], "ok": False, "error": str(e)[:200]})
    return jsonify({"pulled": len(results), "results": results})


@companion_bp.route("/api/companion/content-calendar-recipients", methods=["GET"])
def companion_content_calendar_recipients():
    """Employee ids in app_settings 'content_calendar_recipient_ids' (default
    emp001 Vidit + emp007 Charulata), resolved to name + whatsapp. Who gets DM'd the
    tomorrow-live reminder."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    ids = ["emp001", "emp007"]   # Vidit + Charulata (default; a stored setting replaces it)
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='content_calendar_recipient_ids'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            ids = [x.strip() for x in str(row[0]).replace(",", " ").split() if x.strip()]
    except Exception:
        pass
    by_id = {}
    try:
        for e in utils._load_employees().get("employees", []):
            by_id[e.get("id", "")] = e
    except Exception:
        pass
    out = []
    for i in ids:
        e = by_id.get(i)
        if e and re.sub(r"\D", "", e.get("whatsapp", "") or ""):
            out.append({"id": i, "name": e.get("name") or i,
                        "whatsapp": re.sub(r"\D", "", e.get("whatsapp", ""))})
    return jsonify({"recipients": out})


def _social_task(t: dict) -> bool:
    """Same social-media detection used everywhere else (gotcha #87 fifth
    audit round -- unanchored, matches a bracket type anywhere in the title,
    not just at position 0)."""
    ty = (t.get("type") or t.get("service") or "").lower()
    return ("social" in ty) or bool(re.search(
        r"\[(story|reel|static|carousel|post|video)\]",
        t.get("title") or "", re.I))


@companion_bp.route("/api/companion/tomorrow-live", methods=["GET"])
def companion_tomorrow_live():
    """Every social-media task (Sheets row) due to go live tomorrow, pulled
    straight from Notion -- powers the evening reminder to Vidit so nothing
    scheduled for the next day gets missed. Flags anything not yet approved
    or missing a caption so it reads as an actionable heads-up, not just a
    list."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    tomorrow = (datetime.strptime(utils.today_ist(), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    tasks = []
    try:
        import notion_store
        if notion_store.is_configured():
            tasks = notion_store.list_tasks()
    except Exception:
        logger.exception("companion tomorrow-live: notion read failed")

    rows = [t for t in tasks
            if str(t.get("due_date") or "")[:10] == tomorrow
            and _social_task(t)
            and (t.get("status") or "").strip().lower() not in ("cancelled", "canceled")]
    rows.sort(key=lambda t: (t.get("client_name") or "", t.get("title") or ""))

    items = []
    for t in rows:
        status = (t.get("status") or "").strip().lower()
        ready = status in ("approved", "final", "scheduled") and bool((t.get("caption") or "").strip())
        items.append({
            "client": t.get("client_name") or "?",
            "type": t.get("type") or "post",
            "title": t.get("title") or "",
            "assigned_to": t.get("assigned_to") or "unassigned",
            "status": status or "planned",
            "has_caption": bool((t.get("caption") or "").strip()),
            "ready": ready,
        })

    if not items:
        text = f"Nothing scheduled to go live tomorrow ({tomorrow})."
    else:
        not_ready = [i for i in items if not i["ready"]]
        lines = [f"*Going live tomorrow* ({tomorrow}) — {len(items)} post(s):", ""]
        for i in items:
            lines.append(f"• *{i['client']}* — {i['type']}: \"{i['title']}\"")
            detail = f"  Assigned: {i['assigned_to']} · Status: {i['status']}"
            if not i["has_caption"]:
                detail += " · no caption"
            if not i["ready"]:
                detail += " — *NOT READY*"
            lines.append(detail)
        if not_ready:
            lines.append("")
            lines.append(f"*{len(not_ready)} of {len(items)}* not yet approved/captioned.")
        text = "\n".join(lines)

    return jsonify({"date": tomorrow, "count": len(items), "items": items, "text": text})

@companion_bp.route("/api/companion/alexa-status", methods=["GET"])
def companion_alexa_status():
    """Alexa skill health: config, last request/error, who is linked."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import alexa_link
    import alexa_verify
    from routes import alexa as ax
    names = {}
    try:
        names = {e.get("id"): e.get("name") for e in utils._load_employees().get("employees", [])}
    except Exception:
        pass
    links = alexa_link.links()
    for l in links:
        l["name"] = names.get(l["employee_id"], l["employee_id"])
    return jsonify({"skill_id": ax._skill_id(), "invocation": ax.INVOCATION,
                    "signature_check": alexa_verify.enabled(),
                    "crypto_available": alexa_verify.available(),
                    "last": ax.LAST, "links": links})


@companion_bp.route("/api/companion/releases", methods=["GET"])
def companion_releases():
    """What's-new announcements: the file's entries, what has gone out, who has seen it."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import releases
    return jsonify({"file": [{"id": e["id"], "title": e["title"], "notify": e["notify"]}
                             for e in releases.load_entries()],
                    "published": releases.status(),
                    "recipients": [r["name"] for r in releases.recipients()]})


@companion_bp.route("/api/companion/releases/sync", methods=["POST"])
def companion_releases_sync():
    """Run the announcer now instead of waiting for the next tick."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import releases
    return jsonify(releases.sync())


@companion_bp.route("/api/companion/releases/test", methods=["POST"])
def companion_releases_test():
    """Send ONE entry's WhatsApp to specific people only (?slug=<id>&to=emp003,emp001), nothing
    is recorded as published. Needs `to`, so it can never reach everyone by accident."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    import releases
    ids = [x for x in re.split(r"[,\s]+", request.args.get("to", "")) if x]
    if not ids:
        return jsonify({"error": "to= is required (employee ids)"}), 400
    entry = next((e for e in releases.load_entries() if e["id"] == request.args.get("slug", "")), None)
    if not entry:
        return jsonify({"error": "unknown slug"}), 404
    return jsonify({"queued": releases.announce(entry, only_ids=ids), "text": releases._whatsapp_text(entry)})
