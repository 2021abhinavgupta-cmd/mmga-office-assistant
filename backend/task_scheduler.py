"""
Task Delay Scheduler — runs daily to check overdue tasks and escalate risk levels.
Integrated into Flask via APScheduler on app startup.
"""
import logging
from datetime import datetime, date, timedelta
from db import get_connection

logger = logging.getLogger(__name__)

import json
from pathlib import Path
import notion_store

# Risk escalation thresholds (days overdue)
RISK_THRESHOLDS = {
    "day1": 1,   # Friendly reminder to assignee
    "day2": 2,   # Reminder + notify founder
    "day3": 3,   # Mark AT_RISK, alert founder
    "day5": 5,   # Mark CRITICAL
}

def _load_emp_names() -> dict:
    try:
        emp_path = Path(__file__).parent.parent / "config" / "employees.json"
        with open(emp_path) as f:
            return {e["id"]: e["name"] for e in json.load(f).get("employees", [])}
    except Exception:
        return {}

def _today_str():
    return date.today().isoformat()

def _days_overdue(due_date_str: str) -> int:
    try:
        due = date.fromisoformat(due_date_str)
        delta = date.today() - due
        return max(0, delta.days)
    except Exception:
        return 0

def check_overdue_tasks():
    """
    Called daily. Scans all non-approved tasks with a due_date.
    Updates risk level in task_risk table and logs alerts.
    """
    logger.info(" Running daily overdue task check...")
    today = _today_str()
    conn = get_connection()
    cur = conn.cursor()

    if notion_store.is_configured():
        logger.info("Using Notion mode for overdue task check.")
        all_tasks = notion_store.list_tasks()
        tasks = []
        for t in all_tasks:
            if t.get("due_date") and t.get("status") not in ("approved", "not_started"):
                assignee = t.get("assigned_to", "")
                tasks.append((t["notion_id"], t["title"], assignee, t["due_date"], t["status"], t.get("client_name", "Unknown")))
    else:
        cur.execute("""
            SELECT t.id, t.title, t.assigned_to, t.due_date, t.status, c.name as client_name
            FROM tasks t
            JOIN clients c ON t.client_id = c.id
            WHERE t.due_date IS NOT NULL
              AND t.due_date != ''
              AND t.status NOT IN ('approved', 'not_started')
        """)
        tasks = cur.fetchall()

    alerts_fired = []
    emp_names = _load_emp_names()

    for (tid, title, assignee, due_date, status, client_name) in tasks:
        days = _days_overdue(due_date)
        if days == 0:
            continue

        emp_name = emp_names.get(assignee, assignee)

        # Fetch current risk row
        cur.execute("SELECT risk_level, alerted_day1, alerted_day2, alerted_day3, alerted_day5, last_checked FROM task_risk WHERE task_id=?", (tid,))
        row = cur.fetchone()
        risk_level    = row[0] if row else "normal"
        alerted_day1  = row[1] if row else 0
        alerted_day2  = row[2] if row else 0
        alerted_day3  = row[3] if row else 0
        alerted_day5  = row[4] if row else 0
        last_checked  = row[5] if row else None

        # Resilience: skip if already fully processed today (#2)
        if last_checked == today:
            continue


        new_risk = risk_level
        new_alerts = {}

        if days >= RISK_THRESHOLDS["day5"] and not alerted_day5:
            new_risk = "critical"
            new_alerts["alerted_day5"] = 1
            alerts_fired.append({
                "level": "CRITICAL",
                "task": title,
                "client": client_name,
                "assignee": emp_name,
                "days": days,
                "message": f" CRITICAL: {emp_name}'s task '{title}' for {client_name} is {days} days overdue."
            })

        elif days >= RISK_THRESHOLDS["day3"] and not alerted_day3:
            new_risk = "at_risk"
            new_alerts["alerted_day3"] = 1
            alerts_fired.append({
                "level": "AT_RISK",
                "task": title,
                "client": client_name,
                "assignee": emp_name,
                "days": days,
                "message": f" AT RISK: {emp_name}'s task '{title}' for {client_name} is {days} days overdue. Founder notified."
            })

        elif days >= RISK_THRESHOLDS["day2"] and not alerted_day2:
            new_alerts["alerted_day2"] = 1
            alerts_fired.append({
                "level": "WARNING",
                "task": title,
                "client": client_name,
                "assignee": emp_name,
                "days": days,
                "message": f"⚠ {emp_name}'s task '{title}' is {days} days overdue. Founder CC'd."
            })

        elif days >= RISK_THRESHOLDS["day1"] and not alerted_day1:
            new_alerts["alerted_day1"] = 1
            alerts_fired.append({
                "level": "REMINDER",
                "task": title,
                "client": client_name,
                "assignee": emp_name,
                "days": days,
                "message": f" Reminder: {emp_name} has '{title}' due {days} day(s) ago."
            })

        if new_alerts:
            # Build UPSERT — stamp last_checked=today to prevent re-firing after restart (#2)
            fields = ", ".join(new_alerts.keys())
            placeholders = ", ".join(["?"] * len(new_alerts))
            updates = ", ".join(f"{k}=?" for k in new_alerts.keys())
            with conn:
                conn.execute(f"""
                    INSERT INTO task_risk (task_id, risk_level, updated_at, last_checked, {fields})
                    VALUES (?, ?, ?, ?, {placeholders})
                    ON CONFLICT(task_id) DO UPDATE SET
                        risk_level=excluded.risk_level,
                        updated_at=excluded.updated_at,
                        last_checked=excluded.last_checked,
                        {updates}
                """, (tid, new_risk, today, today, *new_alerts.values(), *new_alerts.values()))


    conn.close()

    if alerts_fired:
        for a in alerts_fired:
            logger.warning(f"[TASK ALERT][{a['level']}] {a['message']}")
        _send_alert_to_dashboard(alerts_fired)
        _whatsapp_escalation(alerts_fired)
    else:
        logger.info(" No new overdue alerts today.")

    _check_budget_alert()
    return alerts_fired


def _whatsapp_escalation(alerts: list) -> None:
    """DM the assignee about each fired overdue alert (and the alert
    recipients for day2+). Best-effort -- never breaks the daily check."""
    try:
        import wa_outbox
    except Exception:
        return
    for a in alerts:
        try:
            who = a.get("assignee", "")
            title = a.get("task", "a task")
            client = a.get("client", "")
            days = a.get("days", 0)
            line = (f"Heads up: *{title}*"
                    + (f" for {client}" if client and client != "Unknown" else "")
                    + f" is *{days} day(s)* overdue. Can you close it out or move the date?")
            jid = wa_outbox.jid_for_name(who)
            if jid:
                wa_outbox.enqueue(jid, line)
            if a.get("level") in ("WARNING", "AT_RISK", "CRITICAL"):
                wa_outbox.notify_alert(
                    f"*[{a.get('level')}]* {who}'s *{title}*"
                    + (f" ({client})" if client and client != "Unknown" else "")
                    + f" is {days} day(s) overdue."
                )
        except Exception:
            logger.debug("task_scheduler: whatsapp escalation row failed", exc_info=True)


def _check_budget_alert() -> None:
    """Once per calendar month, DM the alert recipients if API spend has
    crossed 80% of the monthly budget."""
    try:
        import wa_outbox
        from budget_tracker import get_usage_summary, get_current_month_key
        s = get_usage_summary()
        pct = s.get("percent_used", 0) or 0
        if pct < 80:
            return
        month = get_current_month_key()
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='budget_alert_month'"
        ).fetchone()
        if row and row[0] == month:
            conn.close()
            return
        with conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES ('budget_alert_month', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (month,),
            )
        conn.close()
        wa_outbox.notify_alert(
            f"*API budget alert:* {pct}% of this month's ${s.get('budget_limit', '?')} "
            f"limit used (${s.get('monthly_spend', '?')} spent)."
        )
    except Exception:
        logger.debug("task_scheduler: budget alert check failed", exc_info=True)

def _send_alert_to_dashboard(alerts):
    """Store alerts in DB so founder dashboard can display them."""
    conn = get_connection()
    with conn:
        for alert in alerts:
            conn.execute(
                "INSERT INTO usage_logs (data) VALUES (?)",
                (json.dumps({"type": "task_alert", **alert}),)
            )
    conn.close()


def get_task_risk_levels() -> dict:
    """Return dict of task_id → risk_level for dashboard display."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT task_id, risk_level FROM task_risk")
    result = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()
    return result


def get_all_alerts(days_back: int = 7) -> list:
    """
    Return recent alert data for the founder dashboard.
    Pulls all tasks with risk_level != 'normal' and their current state.
    """
    conn = get_connection()
    cur = conn.cursor()
    if notion_store.is_configured():
        rows = []
        cur.execute("SELECT task_id, risk_level, updated_at FROM task_risk WHERE risk_level IN ('at_risk', 'critical')")
        risk_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        if not risk_map:
            conn.close()
            return []
            
        all_tasks = notion_store.list_tasks()
        for t in all_tasks:
            if t["notion_id"] in risk_map and t.get("status") != "approved":
                r_lvl, u_at = risk_map[t["notion_id"]]
                rows.append((t["notion_id"], r_lvl, u_at, t["title"], t.get("assigned_to", ""), t.get("due_date", ""), t.get("status", ""), t.get("client_name", "")))
        
        # Sort critical first
        rows.sort(key=lambda x: 0 if x[1] == 'critical' else 1)
    else:
        cur.execute("""
            SELECT tr.task_id, tr.risk_level, tr.updated_at,
                   t.title, t.assigned_to, t.due_date, t.status,
                   c.name as client_name
            FROM task_risk tr
            JOIN tasks t ON tr.task_id = t.id
            JOIN clients c ON t.client_id = c.id
            WHERE tr.risk_level IN ('at_risk', 'critical')
              AND t.status NOT IN ('approved')
            ORDER BY CASE tr.risk_level WHEN 'critical' THEN 0 WHEN 'at_risk' THEN 1 ELSE 2 END
        """)
        rows = cur.fetchall()
    
    conn.close()
    
    emp_names = _load_emp_names()
    
    return [
        {
            "task_id":     r[0],
            "risk_level":  r[1],
            "updated_at":  r[2],
            "title":       r[3],
            "assigned_to": r[4],
            "assignee_name": emp_names.get(r[4], r[4]),
            "due_date":    r[5],
            "status":      r[6],
            "client_name": r[7],
        }
        for r in rows
    ]


def _run_attendance_sweep():
    """Wraps routes.attendance.sweep_stale_checkouts() for the interval job
    below -- imported lazily so this module has no hard import-time
    dependency on the attendance blueprint."""
    try:
        from routes.attendance import sweep_stale_checkouts
        swept = sweep_stale_checkouts()
        if swept:
            logger.info(f"Attendance sweep: auto-checked-out {swept} stale session(s).")
    except Exception as e:
        logger.warning(f"Attendance sweep failed (non-fatal): {e}")


def _run_event_reminders():
    try:
        import office_events
        n = office_events.reminder_sweep()
        if n:
            logger.info(f"office_events: reminded {n} event(s)")
    except Exception:
        logger.exception("office event reminder sweep failed")


def _run_group_intel():
    """Read new team-group messages and track the promises in them
    (group_intel.py). One small model call per batch; nothing when quiet."""
    try:
        import group_intel
        result = group_intel.extract_pending()
        if result:
            logger.info("group_intel: %s", result)
    except Exception:
        logger.exception("group_intel pass failed")


def _run_week_digest():
    """Sunday-night office schedule (week_digest.py). Runs every 10 min;
    run_weekly() itself only acts Sunday 21:00-23:00 IST, once per week."""
    try:
        import week_digest
        week_digest.flush_changes()  # held calendar-change alerts whose 60 min are up
        result = week_digest.run_weekly()
        if result.get("sent"):
            logger.info("week_digest: %s", result)
    except Exception:
        logger.exception("week digest failed")


def _run_rhythm():
    """MMGA OS rhythm messages (ops_rhythm.py): Mon 10:00 planning, Mon 15:00
    reminder, weekday 17:30 approvals. Every 5 min; each kind has its own IST
    window and fires at most once a day."""
    try:
        import ops_rhythm
        for r in ops_rhythm.run_tick():
            logger.info("ops_rhythm: %s", r)
    except Exception:
        logger.exception("ops rhythm failed")


def _run_pa_brief():
    """Role-based morning dashboard for every employee (morning_dash.py; replaced the
    boss-only pa_brief send). Runs every 10 min; run_daily() keeps it to one send per
    person per weekday, 09:00-11:00 IST."""
    try:
        import morning_dash
        result = morning_dash.run_daily()
        if result.get("sent"):
            logger.info("pa_brief: %s", result)
    except Exception:
        logger.exception("pa_brief failed")


def _run_nightly_group_summary():
    """22:30 IST every day: the day's group summary to Vidit and Kshitij (group_digest.run_nightly)."""
    try:
        import group_digest
        result = group_digest.run_nightly()
        if result.get("sent"):
            logger.info("nightly group summary: %s", result)
    except Exception:
        logger.exception("nightly group summary failed")


def _run_release_sync():
    """Announce new entries of config/releases.json (releases.py): a short WhatsApp to every
    active employee plus the what's-new popup. Idempotent; runs shortly after each deploy and
    every few minutes after that."""
    try:
        import releases
        res = releases.sync()
        if res.get("new"):
            logger.info("release sync: %s", res)
    except Exception:
        logger.exception("release sync failed")


def _run_asks_digest():
    """17:00-18:30 IST weekday digest to whoever directed work in the group (group_intel)."""
    try:
        import group_intel
        res = group_intel.run_asks_digest()
        if res.get("sent"):
            logger.info("asks digest: %s", res)
    except Exception:
        logger.exception("asks digest failed")


def _run_followup_sweep():
    """Wraps followups.sweep() for the interval job below.

    Deliberately an INTERVAL job rather than a cron one: followups.sweep()
    checks the IST weekday + working-hours window itself, so it doesn't
    matter what timezone this container's clock is in (the 08:00 cron above
    is whatever the host says 08:00 is). It also means the bot notices a
    loose end within the hour instead of at two fixed moments, which is the
    point of the feature.
    """
    try:
        import followups
        result = followups.sweep()
        if result.get("sent"):
            logger.info("Follow-up sweep: messaged %s person(s).", result["sent"])
    except Exception as e:
        logger.warning(f"Follow-up sweep failed (non-fatal): {e}")


def _run_data_retention():
    """Wraps data_retention.run_retention_sweep() for the daily job below --
    the durable fix for unbounded log-table growth (2026-09-28 disk-full
    incident). Deliberately a plain cron, not tied to disk pressure: it's
    cheap, safe to run on a schedule regardless of current usage, and
    catching growth early beats waiting until the volume is nearly full
    again."""
    try:
        import data_retention
        result = data_retention.run_retention_sweep()
        deleted = sum(r.get("deleted", 0) for r in result["results"])
        if deleted:
            logger.info("Data retention sweep: deleted %s old row(s) across %s table(s).",
                       deleted, len(result["results"]))
    except Exception as e:
        logger.warning(f"Data retention sweep failed (non-fatal): {e}")


def _run_overtime_conversion():
    """Wraps leave_store.run_overtime_conversion_sweep() for the nightly
    job below -- computes yesterday's per-employee overtime and converts
    any 24hr-crossing into a +1 leave day. Imported lazily so this module
    has no hard import-time dependency on leave_store."""
    try:
        import leave_store
        result = leave_store.run_overtime_conversion_sweep()
        if result.get("converted_events"):
            logger.info(
                "Overtime conversion: %s comp-off day(s) accrued across %s employee(s) processed.",
                result["converted_events"], result["processed"],
            )
    except Exception as e:
        logger.warning(f"Overtime conversion sweep failed (non-fatal): {e}")


def init_scheduler(app):
    """Call this once from app.py to register the background job."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        # Run daily at 8:00 AM
        scheduler.add_job(check_overdue_tasks, "cron", hour=8, minute=0,
                          id="daily_overdue_check", replace_existing=True)
        # Attendance presence sweep -- infers checkout from a stale heartbeat
        # (see routes/attendance.py::sweep_stale_checkouts) instead of any
        # pagehide/unload event. See CLAUDE.md gotcha #70/#71 for why the
        # unload-event approach was tried once and reverted.
        scheduler.add_job(_run_attendance_sweep, "interval", minutes=3,
                          id="attendance_presence_sweep", replace_existing=True)
        # Proactive follow-up sweep -- the bot reaching out first about things
        # that have quietly stalled (backend/followups.py). Its own
        # working-hours/leave/cooldown/per-person-cap rails keep it quiet;
        # this interval only decides how soon it can notice.
        scheduler.add_job(_run_followup_sweep, "interval", minutes=90,
                          id="followup_sweep", replace_existing=True)
        # Group chat -> tracked promises (group_intel.py). Only calls the model
        # when there are new messages in an allow-listed group.
        scheduler.add_job(_run_group_intel, "interval", minutes=8,
                          id="group_intel", replace_existing=True)
        scheduler.add_job(_run_asks_digest, "interval", minutes=10,
                          id="asks_digest", replace_existing=True)
        # What's-new announcements. First run ~1 min after boot (i.e. right after a deploy).
        scheduler.add_job(_run_release_sync, "interval", minutes=3,
                          id="release_sync", replace_existing=True,
                          next_run_time=datetime.now() + timedelta(seconds=60))
        scheduler.add_job(_run_pa_brief, "interval", minutes=10,
                          id="pa_brief", replace_existing=True)
        scheduler.add_job(_run_nightly_group_summary, "interval", minutes=10,
                          id="nightly_group_summary", replace_existing=True)
        # Data retention -- prunes old rows from tables that have no other
        # cleanup (usage_logs, wa_action_log, wa_call_outbox). Daily is
        # plenty; this isn't reacting to anything time-sensitive.
        scheduler.add_job(_run_data_retention, "cron", hour=3, minute=30,
                          id="data_retention_sweep", replace_existing=True)
        # Overtime -> comp-off conversion -- computes yesterday's overtime
        # for every active employee and converts any 24hr crossing into a
        # +1 leave day (leave_store.py). Off-peak overnight job (02:00 IST).
        scheduler.add_job(_run_overtime_conversion, "cron", hour=2, minute=0,
                          id="overtime_conversion_sweep", replace_existing=True)
        # Office-calendar event reminders -- emails + WhatsApps everyone ~30
        # min before an HR-created timed event (office_events.py). Interval
        # job; the sweep reads the IST clock itself, so container TZ is moot.
        scheduler.add_job(_run_event_reminders, "interval", minutes=5,
                          id="office_event_reminders", replace_existing=True)
        scheduler.add_job(_run_rhythm, "interval", minutes=5,
                          id="ops_rhythm", replace_existing=True)
        scheduler.add_job(_run_week_digest, "interval", minutes=10,
                          id="week_digest", replace_existing=True)
        scheduler.start()
        logger.info(" Task delay scheduler started (runs daily at 08:00).")

        # Run immediately on startup to catch anything already overdue
        import threading
        threading.Thread(target=check_overdue_tasks, daemon=True).start()

        return scheduler
    except Exception as e:
        logger.warning(f"Scheduler failed to start (non-fatal): {e}")
        return None
