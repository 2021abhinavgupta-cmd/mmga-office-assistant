"""
Standup, Alerts, Notion, Quick-Tasks, SQLite helpers, and Export Blueprint
Routes: /api/standup/*, /api/alerts/*, /api/notion/*, /api/quick-tasks, /api/sqlite/*, /api/export
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta

import notion_store
import task_scheduler
import wa_outbox
from flask import Blueprint, Response, jsonify, request, send_file
from utils import _load_employees, _save_employees, now_ist, today_ist, IST
from werkzeug.security import generate_password_hash
from routes.auth import _verify_session
from utils import _is_admin

logger = logging.getLogger(__name__)
ops_bp = Blueprint("ops", __name__)


def _client_activity_bg(client: dict, kind: str, detail: str = "") -> None:
    """Fire-and-forget: ping the assignees of this client's tasks (+ the alert
    recipients) that the client did something in the portal."""
    try:
        threading.Thread(
            target=wa_outbox.notify_client_activity,
            args=(client.get("client_name", ""),
                  client.get("client_notion_id", "") or "",
                  kind, detail),
            daemon=True,
        ).start()
    except Exception:
        logger.debug("ops: client-activity notify failed to start", exc_info=True)


def _claude_call(system: str, user: str, max_tokens: int = 1024) -> str:
    """Call System API using Haiku model for fast, cheap task assistant responses."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        model = os.getenv("HAIKU_MODEL", "claude-haiku-4-5")
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"System API call failed: {e}")
        raise


# ── Helpers ───────────────────────────────────────────────────────────────────

def _su_conn():
    from db import get_connection
    return get_connection()


_EMP_NAMES_FALLBACK = {"emp001":"Vidit","emp002":"Nupur","emp003":"Abhinav",
                       "emp004":"Kshitij","emp006":"Mohit",
                       "emp007":"Palak","emp008":"Happy"}

def _load_emp_names() -> dict:
    """id -> display name, read live from employees.json so new hires are picked
    up automatically. Falls back to a stale hardcoded list only if the file
    can't be read at all (this fallback is deliberately NOT kept in sync with
    employees.json — don't add new hires here, fix the read instead)."""
    try:
        data = _load_employees()
        names = {e["id"]: e["name"] for e in data.get("employees", [])}
        return names or _EMP_NAMES_FALLBACK
    except Exception:
        return _EMP_NAMES_FALLBACK


_EDIT_FIELD_LABELS = {
    "new_title": "Title/Type", "assigned_to": "Assigned To", "due_date": "Post Day",
    "status": "Status", "progress": "Progress", "submission_note": "Content/Notes",
    "creation_date": "Creation Date",
}


def _edit_summary_from_body(body: dict) -> str:
    """Human-readable list of which Sheets fields a PATCH touched, e.g.
    'Status, Assigned To' -- built straight from the request body's own keys
    so it always matches what was actually changed, no separate diff needed."""
    return ", ".join(label for key, label in _EDIT_FIELD_LABELS.items() if key in body)


def _task_creation_is_future(notion_id: str, today_str: str) -> bool:
    """True if the live Notion Creation Date for this task is after today.
    Mirrors the property-then-text-fallback logic used in auto_fill_standup()."""
    summary = notion_store.get_task_summary(notion_id)
    if not summary:
        return False
    cr_date_val = summary.get("creation_date")
    if cr_date_val:
        try:
            return cr_date_val.split("T")[0] > today_str
        except Exception:
            return False
    desc = summary.get("description", "") or ""
    m = re.search(r'creation date\s*:\s*([\d-]+)', desc, re.IGNORECASE)
    if not m:
        return False
    cr_date = m.group(1).strip()
    if re.match(r"^\d{2}-\d{2}-\d{4}$", cr_date):
        cr_date = f"{cr_date[6:10]}-{cr_date[3:5]}-{cr_date[0:2]}"
    return cr_date > today_str

def _pt_conn():
    from db import get_connection
    return get_connection()

def _task_row_to_dict(row) -> dict:
    return {
        "id": row[0], "client_id": row[1], "title": row[2],
        "description": row[3], "assigned_to": row[4], "status": row[5],
        "progress": row[6], "due_date": row[7], "submission_note": row[8],
        "submission_file": row[9], "rejection_note": row[10],
        "submission_count": row[11], "opened_at": row[12], "created_at": row[13],
    }


# ══════════════════════════════════════════════════════════════════════════════
# STANDUP
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/standup", methods=["POST"])
def submit_standup():
    """Submit daily standup. One per user per day."""
    body = request.get_json(silent=True) or {}
    user_id   = body.get("user_id", "").strip()
    yesterday = body.get("yesterday", "").strip()
    today_txt = body.get("today", "").strip()
    blockers  = body.get("blockers", "").strip()

    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    if not yesterday and not today_txt:
        return jsonify({"error": "Provide at least one update field"}), 400

    date_str = today_ist()
    conn = _su_conn()
    try:
        with conn:
            conn.execute("""
                INSERT INTO standups (user_id, date, yesterday, today, blockers, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET
                    yesterday=excluded.yesterday,
                    today=excluded.today,
                    blockers=excluded.blockers,
                    submitted_at=excluded.submitted_at
            """, (user_id, date_str, yesterday, today_txt, blockers, datetime.utcnow().isoformat()+"Z"))
    finally:
        conn.close()
    return jsonify({"success": True, "date": date_str})


@ops_bp.route("/api/standup/today", methods=["GET"])
def get_standups_today():
    """Get all standups for a specific date (defaults to today). Founder view."""
    date_str = request.args.get("date") or today_ist()
    user_id  = request.args.get("user_id", "")
    conn = _su_conn()
    cur = conn.cursor()
    
    # Auto-carry-over for all users if querying today
    if date_str == today_ist() and not user_id:
        cur.execute("SELECT DISTINCT user_id FROM standup_tasks WHERE status='pending' AND date < ? AND user_id NOT IN (SELECT user_id FROM standup_tasks WHERE date=?)", (date_str, date_str))
        missing_users = [r[0] for r in cur.fetchall()]
        for u in missing_users:
            cur.execute("SELECT MAX(date) FROM standup_tasks WHERE user_id=? AND date<?", (u, date_str))
            last_date_row = cur.fetchone()
            if last_date_row and last_date_row[0]:
                last_date = last_date_row[0]
                cur.execute("SELECT title, blocker, carried_from, notion_id, due_date, subtasks, delegated_to, delegated_from FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'", (u, last_date))
                pending = cur.fetchall()
                if pending:
                    with conn:
                        for title, blocker, carried_from, nid, dd, sub, d_to, d_from in pending:
                            orig_carry_from = carried_from if carried_from else last_date
                            conn.execute("INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker, notion_id, due_date, subtasks, delegated_to, delegated_from) VALUES (?,?,?,'pending',?,?,?,?,?,?,?)", (u, date_str, title, orig_carry_from, blocker, nid, dd, sub, d_to, d_from))

    # Fetch text standups
    if user_id:
        cur.execute("SELECT user_id,date,yesterday,today,blockers,submitted_at FROM standups WHERE date=? AND user_id=?", (date_str, user_id))
    else:
        cur.execute("SELECT user_id,date,yesterday,today,blockers,submitted_at FROM standups WHERE date=? ORDER BY submitted_at", (date_str,))
    rows = cur.fetchall()
    
    # Fetch task lists
    if user_id:
        cur.execute("SELECT user_id, title, status, blocker, carried_from, subtasks, notion_id, due_date FROM standup_tasks WHERE date=? AND user_id=? AND status NOT IN ('deleted', 'delegated') ORDER BY CASE WHEN due_date IS NULL OR due_date = '' THEN '9999-12-31' ELSE due_date END ASC, id ASC", (date_str, user_id))
    else:
        cur.execute("SELECT user_id, title, status, blocker, carried_from, subtasks, notion_id, due_date FROM standup_tasks WHERE date=? AND status NOT IN ('deleted', 'delegated') ORDER BY CASE WHEN due_date IS NULL OR due_date = '' THEN '9999-12-31' ELSE due_date END ASC, id ASC", (date_str,))
    task_rows = cur.fetchall()
    
    conn.close()
    
    tasks_by_user = {}
    for uid, title, status, blocker, carried_from, subtasks_json, notion_id, due_date in task_rows:
        if uid not in tasks_by_user:
            tasks_by_user[uid] = []
            
        st = []
        try:
            st = json.loads(subtasks_json) if subtasks_json else []
        except: pass
            
        tasks_by_user[uid].append({"id": len(tasks_by_user[uid]), "title": title, "status": status, "blocker": blocker, "carried_from": carried_from, "subtasks": st, "notion_id": notion_id, "due_date": due_date})

    # Sort each user's tasks chronologically
    import re
    def parse_date_for_sort(d):
        if not d:
            return "9999-12-31"
        if re.match(r"^\d{2}-\d{2}-\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        if re.match(r"^\d{2}/\d{2}/\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        return d.strip()

    for uid in tasks_by_user:
        tasks_by_user[uid].sort(key=lambda x: (parse_date_for_sort(x["due_date"]), x["id"]))

    EMP_NAMES = _load_emp_names()
    standups = [{"user_id":r[0],"name":EMP_NAMES.get(r[0],r[0]),"date":r[1],
                 "yesterday":r[2],"today":r[3],"blockers":r[4],"submitted_at":r[5]} for r in rows]
                 
    return jsonify({"standups": standups, "tasks_by_user": tasks_by_user, "date": date_str})


@ops_bp.route("/api/standup/ai-coach", methods=["POST"])
def ai_priority_advisor():
    """Provides AI priority advice based on the user's tasks."""
    body = request.get_json(silent=True) or {}
    question = body.get("question", "")
    assigned_name = body.get("assigned_name", "")
    tasks = body.get("tasks", [])
    
    if not tasks:
        return jsonify({"reply": "I don't see any active tasks for you right now. You're all caught up!"})
        
    system_prompt = f"You are an AI priority advisor for {assigned_name}. The user will provide a list of their tasks and ask a question. Provide a direct, actionable, and encouraging answer."
    user_prompt = "Here are my current tasks:\n" + "\n".join(tasks) + f"\n\nQuestion: {question}"

    try:
        reply = _claude_call(system_prompt, user_prompt)
    except Exception:
        return jsonify({"error": "Could not get priority advice right now. Please try again."}), 500
    return jsonify({"reply": reply})


@ops_bp.route("/api/standup/history", methods=["GET"])
def get_standup_history():
    """Get standup history for a user (last 7 days)."""
    user_id = request.args.get("user_id", "")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("""SELECT user_id,date,yesterday,today,blockers,submitted_at
                   FROM standups WHERE user_id=? ORDER BY date DESC LIMIT 7""", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"history": [{"date":r[1],"yesterday":r[2],"today":r[3],"blockers":r[4],"submitted_at":r[5]} for r in rows]})


@ops_bp.route("/api/standup/velocity", methods=["GET"])
def get_velocity():
    """
    GET /api/standup/velocity?user_id=&days=14
    Returns per-day counts of completed vs pending/carried tasks.
    If user_id is omitted, returns team-wide aggregates.
    """
    user_id = request.args.get("user_id", "").strip()
    range_param = request.args.get("range", "").strip()

    conn = _su_conn()
    cur  = conn.cursor()

    if range_param == "month":
        since = datetime.now(IST).replace(day=1).strftime("%Y-%m-%d")
    else:
        days = int(request.args.get("days", 14))
        since = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")

    # strftime('%w', date) is 0=Sunday..6=Saturday in SQLite -- excludes
    # weekends from the chart, since carry-over still runs on Sat/Sun
    # (gotcha #8) even though nobody's actually working those days, which
    # otherwise shows up as misleading "carried over" spikes.
    if user_id:
        cur.execute("""
            SELECT date,
                   SUM(CASE WHEN status='done' THEN 1 ELSE 0 END)     AS completed,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END)   AS pending,
                   SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
            FROM standup_tasks
            WHERE user_id=? AND date >= ? AND status != 'deleted' AND strftime('%w', date) NOT IN ('0','6')
            GROUP BY date ORDER BY date ASC
        """, (user_id, since))
    else:
        cur.execute("""
            SELECT date,
                   SUM(CASE WHEN status='done' THEN 1 ELSE 0 END)     AS completed,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END)   AS pending,
                   SUM(CASE WHEN carried_from IS NOT NULL AND status='pending' THEN 1 ELSE 0 END) AS carried
            FROM standup_tasks
            WHERE date >= ? AND status != 'deleted' AND strftime('%w', date) NOT IN ('0','6')
            GROUP BY date ORDER BY date ASC
        """, (since,))

    rows = cur.fetchall()
    conn.close()

    data = [{"date": r[0], "completed": r[1] or 0, "pending": r[2] or 0, "carried": r[3] or 0} for r in rows]
    return jsonify({"velocity": data})


@ops_bp.route("/api/standup/velocity-summary", methods=["GET"])
def get_velocity_summary():
    """
    GET /api/standup/velocity-summary?user_id=
    Month-to-date totals: distinct tasks completed this month, distinct tasks
    that went overdue this month. Always scoped to the current calendar
    month, independent of the /velocity chart's day-range selector.

    A task rolls forward as a NEW standup_tasks row each day it stays
    pending (see the carry-over INSERTs elsewhere in this file), all sharing
    the same `carried_from` (the date it first went unfinished) and `title`.
    Counting distinct (user_id, title, carried_from) avoids counting the same
    overdue task once per day it was carried.
    """
    user_id = request.args.get("user_id", "").strip()
    month_start = datetime.now(IST).replace(day=1).strftime("%Y-%m-%d")

    conn = _su_conn()
    cur = conn.cursor()

    if user_id:
        cur.execute(
            "SELECT COUNT(*) FROM standup_tasks WHERE user_id=? AND status='done' AND date >= ?",
            (user_id, month_start),
        )
        completed = cur.fetchone()[0] or 0
        cur.execute(
            """SELECT COUNT(DISTINCT user_id || '|' || title || '|' || carried_from)
               FROM standup_tasks WHERE user_id=? AND carried_from IS NOT NULL AND carried_from >= ?""",
            (user_id, month_start),
        )
        overdue = cur.fetchone()[0] or 0
    else:
        cur.execute(
            "SELECT COUNT(*) FROM standup_tasks WHERE status='done' AND date >= ?",
            (month_start,),
        )
        completed = cur.fetchone()[0] or 0
        cur.execute(
            """SELECT COUNT(DISTINCT user_id || '|' || title || '|' || carried_from)
               FROM standup_tasks WHERE carried_from IS NOT NULL AND carried_from >= ?""",
            (month_start,),
        )
        overdue = cur.fetchone()[0] or 0

    conn.close()
    return jsonify({"completed": completed, "overdue": overdue, "month_start": month_start})


# ══════════════════════════════════════════════════════════════════════════════
# PERSONAL DAILY TASK TRACKER (separate from project tasks)
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/standup/actions", methods=["POST"])
def execute_standup_actions():
    data = request.json or {}
    actions = data.get("actions", [])
    user_id = data.get("user_id", "")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    conn = _su_conn()
    cur = conn.cursor()
    results = []

    for action_item in actions:
        action = action_item.get("action")
        try:
            if action == "update_task":
                task_id = action_item.get("task_id")
                new_status = action_item.get("status")
                if task_id and new_status:
                    cur.execute("SELECT notion_id, title FROM standup_tasks WHERE id=?", (task_id,))
                    row = cur.fetchone()
                    if row:
                        cur.execute("UPDATE standup_tasks SET status=? WHERE id=?", (new_status, task_id))
                        notion_id = row[0]
                        if notion_id and notion_store.is_configured():
                            n_status = "Done" if new_status == "done" else "In progress" if new_status == "pending" else new_status
                            notion_store.update_task(notion_id, status=n_status)
                        results.append({"task_id": task_id, "status": new_status, "updated": True})
            elif action == "add_task":
                title = action_item.get("title")
                due_date = action_item.get("due_date") or today_ist()
                status = action_item.get("status", "pending")
                if title:
                    cur.execute("INSERT INTO standup_tasks (user_id, date, due_date, title, status) VALUES (?, ?, ?, ?, ?)",
                                (user_id, today_ist(), due_date, title, status))
                    new_id = cur.lastrowid
                    results.append({"task_id": new_id, "title": title, "added": True})
        except Exception as e:
            logger.error(f"Error executing standup action {action_item}: {e}")

    conn.commit()
    conn.close()
    return jsonify({"success": True, "results": results})



@ops_bp.route("/api/standup/lock-status", methods=["GET"])
def standup_lock_status():
    """Powers auth.js's daily-standup lock (CLAUDE.md gotchas #108/#119):
    every employee page except standup.html itself redirects there until,
    for today, the person has BOTH checked in (attendance PIN) AND put at
    least one task on their standup. Frontend-only nudge, not an API-level
    block -- every other endpoint keeps working regardless of this value.
    Skipped on weekends (matches the standup velocity chart, gotcha #77)
    and for anyone on recorded leave (leave_store). Deliberately does NOT
    trigger the auto-carry-over insert that get_my_tasks does -- that only
    needs to happen once someone is actually on the Standup screen; this
    route just reads what's there."""
    user_id = request.args.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    weekend = datetime.now(IST).weekday() >= 5  # 5=Sat, 6=Sun
    if weekend:
        return jsonify({"locked": False, "weekend": True})

    date_str = today_ist()

    try:
        import leave_store
        if leave_store.is_on_leave(user_id, date_str):
            return jsonify({"locked": False, "weekend": False, "on_leave": True})
    except Exception:
        logging.getLogger(__name__).exception("lock-status: leave check failed")

    conn = _su_conn()
    has_tasks = bool(conn.execute(
        "SELECT 1 FROM standup_tasks WHERE user_id=? AND date=? "
        "AND status NOT IN ('deleted','delegated') LIMIT 1",
        (user_id, date_str),
    ).fetchone())
    has_checkin = bool(conn.execute(
        "SELECT 1 FROM daily_attendance WHERE user_id=? AND date=? "
        "AND checkin_time IS NOT NULL AND checkin_time<>'' LIMIT 1",
        (user_id, date_str),
    ).fetchone())
    conn.close()

    return jsonify({
        "locked": not (has_tasks and has_checkin),
        "weekend": False,
        "on_leave": False,
        "needs_checkin": not has_checkin,
        "needs_standup": not has_tasks,
    })


@ops_bp.route("/api/standup/my-tasks", methods=["GET"])
def get_my_tasks():
    """
    Get an employee's task list for a given date.
    Carried-over tasks from yesterday are automatically seeded when first
    fetching today if they don't exist yet.
    Query: user_id, date (optional — defaults to today UTC)
    """
    user_id  = request.args.get("user_id", "").strip()
    date_str = request.args.get("date", "") or today_ist()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    conn = _su_conn()
    cur  = conn.cursor()

    # Auto-carry-over: if no tasks exist for today, copy pending ones from yesterday
    cur.execute("SELECT id FROM standup_tasks WHERE user_id=? AND date=?", (user_id, date_str))
    existing = cur.fetchall()
    if not existing and date_str == today_ist():
        cur.execute("SELECT MAX(date) FROM standup_tasks WHERE user_id=? AND date<?", (user_id, date_str))
        last_date_row = cur.fetchone()
        if last_date_row and last_date_row[0]:
            last_date = last_date_row[0]
            cur.execute(
                "SELECT title, blocker, carried_from, notion_id, due_date, subtasks, delegated_to, delegated_from FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'",
                (user_id, last_date),
            )
            pending = cur.fetchall()
            if pending:
                with conn:
                    for title, blocker, carried_from, nid, dd, sub, d_to, d_from in pending:
                        orig_carry_from = carried_from if carried_from else last_date
                        conn.execute(
                            "INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker, notion_id, due_date, subtasks, delegated_to, delegated_from) VALUES (?,?,?,'pending',?,?,?,?,?,?,?)",
                            (user_id, date_str, title, orig_carry_from, blocker, nid, dd, sub, d_to, d_from),
                        )

    import json
    import re

    def parse_date_for_sort(d):
        if not d:
            return "9999-12-31"
        # If DD-MM-YYYY, convert to YYYY-MM-DD
        if re.match(r"^\d{2}-\d{2}-\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        # If DD/MM/YYYY, convert to YYYY-MM-DD
        if re.match(r"^\d{2}/\d{2}/\d{4}$", d.strip()):
            return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"
        return d.strip()

    cur.execute(
        "SELECT id, title, status, carried_from, created_at, blocker, notion_id, subtasks, delegated_to, delegated_from, due_date FROM standup_tasks WHERE user_id=? AND date=?",
        (user_id, date_str),
    )
    rows = cur.fetchall()

    # Auto-clean: a task pulled into this snapshot earlier may have had its live
    # Notion Creation Date pushed to the future since then (e.g. postponed in
    # Sheets) — the local row doesn't self-heal on its own, so re-check and drop
    # it here rather than leaving it stuck until the next manual Auto-Fill/Sync.
    if date_str == today_ist() and notion_store.is_configured():
        future_ids = []
        for r in rows:
            nid, row_status = r[6], r[2]
            if nid and row_status not in ("done", "deleted", "delegated"):
                try:
                    if _task_creation_is_future(nid, date_str):
                        future_ids.append(r[0])
                except Exception:
                    pass
        if future_ids:
            with conn:
                conn.executemany("DELETE FROM standup_tasks WHERE id=?", [(i,) for i in future_ids])
            rows = [r for r in rows if r[0] not in future_ids]

    conn.close()

    tasks = []
    for r in rows:
        if r[2] in ("deleted", "delegated"): continue
        st = []
        try:
            st = json.loads(r[7]) if r[7] else []
        except: pass
        
        tasks.append({
            "id": r[0], "title": r[1], "status": r[2],
            "carried_from": r[3], "created_at": r[4], 
            "blocker": r[5], "notion_id": r[6], "subtasks": st,
            "delegated_to": r[8], "delegated_from": r[9], "due_date": r[10]
        })

    tasks.sort(key=lambda x: (parse_date_for_sort(x["due_date"]), x["id"]))

    return jsonify({"tasks": tasks, "date": date_str})


@ops_bp.route("/api/standup/tasks/<int:task_id>/delegate", methods=["POST"])
def delegate_task(task_id):
    body = request.get_json(silent=True) or {}
    target_user_id = body.get("target_user_id")
    target_user_name = body.get("target_user_name")
    
    if not target_user_id or not target_user_name:
        return jsonify({"error": "target_user_id and target_user_name required"}), 400
        
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, date, title, blocker FROM standup_tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "Task not found"}), 404
        
    orig_user_id, old_date_str, title, blocker = row

    orig_user_name = _load_emp_names().get(orig_user_id, orig_user_id)
    
    today_str = today_ist()
    
    with conn:
        # 1. Mark original task as delegated
        conn.execute("UPDATE standup_tasks SET status='delegated', delegated_to=? WHERE id=?", (target_user_name, task_id))
        
        # 2. Create new task for target user on TODAY's date (so it appears immediately)
        conn.execute(
            "INSERT INTO standup_tasks (user_id, date, title, status, blocker, delegated_from) VALUES (?, ?, ?, 'pending', ?, ?)",
            (target_user_id, today_str, title, blocker, orig_user_name)
        )
    conn.close()
    # Tell the person on WhatsApp (queued; the laptop delivers it). Best-effort: never fails the handover.
    try:
        if target_user_id != orig_user_id:
            emp = next((e for e in _load_employees().get("employees", [])
                        if e.get("id") == target_user_id), None)
            jid = wa_outbox.wa_jid((emp or {}).get("whatsapp", ""))
            if jid:
                first = (target_user_name or "").split(" ")[0] or "there"
                wa_outbox.enqueue(jid, f"Hi {first}, {orig_user_name} handed you a task on today's standup: "
                                       f"{title}" + (f"\nBlocker: {blocker}" if blocker else "")
                                       + "\nIt is on your list now.")
    except Exception:
        logger.exception("delegate_task: WhatsApp notify failed")
    return jsonify({"success": True})


def _normalize_title(t: str) -> str:
    if not t: return ""
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = re.sub(r'^\[.*?\]\s*', '', t)
    return t.lower().strip()


@ops_bp.route("/api/standup/my-tasks", methods=["POST"])
def add_my_task():
    """
    Add a task to today's personal task list.
    Body: { user_id, title }
    """
    body    = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    title   = body.get("title", "").strip()
    notion_id = body.get("notion_id", None)
    if not user_id or not title:
        return jsonify({"error": "user_id and title required"}), 400

    date_str = today_ist()
    conn = _su_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO standup_tasks (user_id, date, title, notion_id) VALUES (?, ?, ?, ?)",
            (user_id, date_str, title, notion_id),
        )
        task_id = cur.lastrowid
    conn.close()
    # Putting a real task on today's standup is at least as strong a signal
    # of "I'm here and working today" as clicking Check In -- so it also
    # satisfies the attendance side of the daily-standup lock (gotcha
    # #108/#119), without needing a separate Check In click. Idempotent
    # (_attendance_checkin only ever fills a NULL checkin_time), so this is
    # safe to call unconditionally and best-effort -- a failure here must
    # never block the task that was already saved above.
    try:
        from routes.attendance import _attendance_checkin
        _attendance_checkin(user_id)
    except Exception:
        logger.exception("add_my_task: auto-checkin failed")
    return jsonify({"success": True, "task_id": task_id, "date": date_str}), 201


_STALE_DUE_DAYS = 14


def _stale_due(due, today):
    """True when a Notion due date is more than 14 days before today."""
    try:
        dd = datetime.strptime(str(due).split("T")[0], "%Y-%m-%d").date()
        return (today.date() - dd).days > _STALE_DUE_DAYS
    except Exception:
        return False


@ops_bp.route("/api/standup/auto-fill", methods=["POST"])
def auto_fill_standup():
    """
    Fetch tasks from Notion that are active (in_progress) or due today, 
    and add them to today's standup list (avoiding duplicates).
    Also includes social media tasks whose Creation Date is today
    (i.e. work starts today — the assignee sees it for the first time).
    Body: { user_id, assigned_name }
    """
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    assigned_name = body.get("assigned_name", "").strip()
    sync_all = body.get("sync_all", False)
    
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    try:
        import notion_store
        if not notion_store.is_configured():
            return jsonify({"error": "Notion is not configured"}), 400
            
        if sync_all:
            all_tasks = notion_store.list_tasks()
        else:
            if not assigned_name:
                return jsonify({"error": "assigned_name required"}), 400
            all_tasks = notion_store.list_tasks(assigned_to=assigned_name)
    except Exception as e:
        logger.error(f"Failed to fetch Notion tasks for auto-fill: {e}")
        return jsonify({"error": str(e)}), 500

    # Load employee name-to-id mapping for sync_all
    emp_name_to_id = {}
    try:
        _emp_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "employees.json")
        with open(_emp_path, "r", encoding="utf-8") as _f:
            _emp_data = json.load(_f)
        emp_name_to_id = {e["name"]: e["id"] for e in _emp_data.get("employees", [])}
    except Exception:
        emp_name_to_id = {"Vidit":"emp001","Nupur":"emp002","Abhinav":"emp003",
                          "Kshitij":"emp004","Mohit":"emp006",
                          "Palak":"emp007","Happy":"emp008"}

    today = datetime.now(IST)
    today_str = today.strftime("%Y-%m-%d")
    
    # Pre-fetch existing notion_ids for today to ensure we update them even if they are future tasks
    existing_notion_ids = set()
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT notion_id FROM standup_tasks WHERE date=? OR status NOT IN ('Completed', 'Archived')", (today_str,))
    for r in cur.fetchall():
        if r[0]: existing_notion_ids.add(r[0])
    
    valid_tasks = []
    for t in all_tasks:
        s = t.get("status", "").lower().replace(" ", "_").replace("-", "_")
        d = t.get("due_date", "")
        raw_title = t.get("title", "").strip()
        nid = t.get("notion_id") or t.get("id")

        client = t.get("client_name", "").strip()
        content = t.get("content", "").strip() or t.get("description", "").strip() or t.get("brief", "").strip()
        
        search_title = raw_title
        if client and not search_title.startswith(client):
            search_title = f"{client} — {search_title}"
        if content:
            preview = content[:40] + "..." if len(content) > 40 else content
            search_title = f"{search_title} ({preview})"

        # AUTO-REPAIR: If a task already exists locally but lost its due_date/notion_id 
        # (e.g. from a past carry-over bug), restore it now regardless of status.
        if search_title:
            # First figure out target_user_id for this task
            target_uids = []
            assignees = t.get("assigned_to", "")
            if not sync_all and assigned_name:
                target_uids = [user_id]
                insert_allowed = True
            else:
                names = [n.strip() for n in assignees.split(",") if n.strip()]
                for n in names:
                    for ename, eid in emp_name_to_id.items():
                        if ename.lower() in n.lower():
                            if eid not in target_uids:
                                target_uids.append(eid)
                if target_uids:
                    insert_allowed = True
                else:
                    target_uids = list(emp_name_to_id.values())
                    insert_allowed = False


            # Robust cleanup: Ensure users who are no longer assigned do not keep this task
            if sync_all and nid:
                if insert_allowed and target_uids:
                    placeholders = ",".join("?" * len(target_uids))
                    query = f"DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived')) AND user_id NOT IN ({placeholders})"
                    params = [nid, today_str] + target_uids
                    cur.execute(query, params)
                else:
                    cur.execute("DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (nid, today_str))
            for target_user_id in target_uids:
                cur.execute("SELECT id, due_date, notion_id, title FROM standup_tasks WHERE user_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (target_user_id, today_str))
                local_tasks = cur.fetchall()
                
                matched_id = None
                norm_search = _normalize_title(search_title)
                norm_raw = _normalize_title(raw_title)
                
                for lid, ldue, lnid, ltitle in local_tasks:
                    if lnid and nid and lnid == nid:
                        matched_id = lid
                        break
                    norm_ltitle = _normalize_title(ltitle)
                    if norm_ltitle and (norm_ltitle == norm_search or norm_ltitle == norm_raw or 
                                        norm_search.endswith(norm_ltitle) or norm_ltitle.endswith(norm_raw)):
                        matched_id = lid
                        break
                
                if matched_id:
                    if s in ("approved", "done", "submitted", "in_review", "pending_review", "posted", "final"):
                        cur.execute("UPDATE standup_tasks SET status='done' WHERE id=? AND status='pending'", (matched_id,))
                    else:
                        # Update local task with accurate Notion data
                        cur.execute("UPDATE standup_tasks SET title=?, due_date=?, notion_id=? WHERE id=?", (search_title, d, nid, matched_id))

        # Don't pull finished tasks
        if s in ("approved", "done", "submitted", "in_review", "pending_review", "posted", "final"):
            continue
            
        # Add if status is active
        is_active = (s == "in_progress")
        
        # Add if due date is today EXACTLY. Overdue tasks should not be indiscriminately added 
        # unless they are carried over via the app's wrap-up feature.
        is_due = False
        if d:
            try:
                due_dt = d.split("T")[0]
                if due_dt == today_str:
                    is_due = True
            except: pass
            
        # Check if this is a task with a specific Creation Date
        is_creation_today = False
        has_creation_date = False
        is_future_creation = False
        cr_date_val = t.get("creation_date")
        
        if cr_date_val:
            # It's an actual Notion Date Property
            has_creation_date = True
            try:
                cr_date = cr_date_val.split("T")[0]
                if cr_date == today_str and s in ("not_started", "need_to_start"):
                    is_creation_today = True
                elif cr_date > today_str:
                    is_future_creation = True
            except: pass
        else:
            desc = t.get("description", "") or t.get("content", "") or t.get("brief", "") or ""
            if desc:
                import re as _re
                cr_match = _re.search(r'creation date\s*:\s*([\d-]+)', desc, _re.IGNORECASE)
                if cr_match:
                    has_creation_date = True
                    try:
                        cr_date = cr_match.group(1).strip()
                        if _re.match(r"^\d{2}-\d{2}-\d{4}$", cr_date):
                            cr_date = f"{cr_date[6:10]}-{cr_date[3:5]}-{cr_date[0:2]}"
                        if cr_date == today_str and s in ("not_started", "need_to_start"):
                            is_creation_today = True
                        elif cr_date > today_str:
                            is_future_creation = True
                    except: pass
        # If the user has "not started" tasks, pull them in if they are due within 7 days
        # BUT skip this for tasks that have a specific Creation Date (they should only appear on their exact Creation Date)
        is_upcoming = False
        is_not_started = s in ("not_started", "need_to_start", "")
        if d and is_not_started and not has_creation_date:
            try:
                due_dt = datetime.strptime(d.split("T")[0], "%Y-%m-%d")
                if (due_dt - today).days <= 7:
                    is_upcoming = True
            except: pass

        # Also, if body explicitly requested "upcoming", we can pull all not_started tasks
        pull_all_upcoming = body.get("pull_upcoming", False)
        
        if is_future_creation:
            # If it's already in the local DB mistakenly, delete it!
            if t.get("notion_id") in existing_notion_ids:
                try:
                    cur.execute("DELETE FROM standup_tasks WHERE notion_id=?", (t.get("notion_id"),))
                except: pass
            continue
        
        # Check for /daily and /alternate features
        combined_text = f"{raw_title} {desc}".lower()
        is_daily_trigger = ("/daily" in combined_text)
        is_alternate_trigger = ("/alternate" in combined_text and today.toordinal() % 2 == 0)

        if is_active or is_due or is_upcoming or is_creation_today or is_daily_trigger or is_alternate_trigger or (pull_all_upcoming and is_not_started) or (t.get("notion_id") in existing_notion_ids):
            valid_tasks.append(t)

            # Clean up any tasks that were in the local DB but no longer exist in fetched list
        fetched_notion_ids = set(t.get("notion_id") or t.get("id") for t in all_tasks if t.get("notion_id") or t.get("id"))
        
        if sync_all:
            for nid in existing_notion_ids:
                if nid not in fetched_notion_ids:
                    try:
                        cur.execute("DELETE FROM standup_tasks WHERE notion_id=?", (nid,))
                    except: pass
        else:
            cur.execute("SELECT notion_id FROM standup_tasks WHERE user_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (user_id, today_str))
            my_existing_nids = set(r[0] for r in cur.fetchall() if r[0])
            for nid in my_existing_nids:
                if nid not in fetched_notion_ids:
                    try:
                        cur.execute("DELETE FROM standup_tasks WHERE notion_id=? AND user_id=?", (nid, user_id))
                    except: pass

    conn.commit()
    conn.close()

    if not valid_tasks:
        return jsonify({"success": True, "added": 0})

    conn = _su_conn()
    added_count = 0
    with conn:
        cur = conn.cursor()
        closed_pairs = set()
        for _u, _n in cur.execute(
            "SELECT DISTINCT user_id, notion_id FROM standup_tasks "
            "WHERE notion_id IS NOT NULL AND notion_id != '' "
            "AND status IN ('done','deleted','delegated')"
        ).fetchall():
            closed_pairs.add((_u, _n))
        for vt in valid_tasks:
            nid = vt.get("notion_id")
            raw_title = vt.get("title", "Untitled").strip()
            d = vt.get("due_date", "")
            
            # Add context for tasks (especially from social sheet)
            client = vt.get("client_name", "").strip()
            content = vt.get("content", "").strip() or vt.get("description", "").strip() or vt.get("brief", "").strip()
            
            search_title = raw_title
            if client and not search_title.startswith(client):
                search_title = f"{client} — {search_title}"
            if content:
                # Append a short preview of the content
                preview = content[:40] + "..." if len(content) > 40 else content
                search_title = f"{search_title} ({preview})"
                
            # If sync_all is true, we update tasks for ALL assignees
            if sync_all:
                # Live roster, active employees only. This used to be a
                # hardcoded name->id dict that still mapped "Mohit"/"Palak"
                # onto emp006/emp007 after those ids were reassigned to
                # Lakshmi/Charulata, so every old Mohit/Palak task in Notion
                # was pushed into the new people's standups on Sync All.
                _inactive = {"inactive", "disabled", "left", "removed", "archived", "former"}
                emp_name_to_id = {
                    e["name"]: e["id"]
                    for e in _load_employees().get("employees", [])
                    if e.get("name") and str(e.get("status", "active")).lower() not in _inactive
                }
                assignees = vt.get("assigned_to", "")
                names = [n.strip() for n in assignees.split(",") if n.strip()]
                target_uids = []
                for n in names:
                    for ename, eid in emp_name_to_id.items():
                        if ename.lower() in n.lower():
                            if eid not in target_uids:
                                target_uids.append(eid)
                if target_uids:
                    insert_allowed = True
                else:
                    target_uids = list(emp_name_to_id.values())
                    insert_allowed = False
            else:
                target_uids = [user_id]
                insert_allowed = True
                

            # Robust cleanup: Ensure users who are no longer assigned do not keep this task
            if sync_all and nid:
                if insert_allowed and target_uids:
                    placeholders = ",".join("?" * len(target_uids))
                    query = f"DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived')) AND user_id NOT IN ({placeholders})"
                    params = [nid, today_str] + target_uids
                    cur.execute(query, params)
                else:
                    cur.execute("DELETE FROM standup_tasks WHERE notion_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (nid, today_str))
            for target_user_id in target_uids:
                cur.execute("SELECT id, notion_id, title FROM standup_tasks WHERE user_id=? AND (date=? OR status NOT IN ('Completed', 'Archived'))", (target_user_id, today_str))
                local_tasks = cur.fetchall()
                
                matched_id = None
                norm_search = _normalize_title(search_title)
                norm_raw = _normalize_title(raw_title)
                
                for lid, lnid, ltitle in local_tasks:
                    if lnid and nid and lnid == nid:
                        matched_id = lid
                        break
                    norm_ltitle = _normalize_title(ltitle)
                    if norm_ltitle and (norm_ltitle == norm_search or norm_ltitle == norm_raw or 
                                        norm_search.endswith(norm_ltitle) or norm_ltitle.endswith(norm_raw)):
                        matched_id = lid
                        break
                                
                if not matched_id:
                    # Never re-add what this person already finished, deleted or
                    # handed off, and (Sync All only) never resurrect a task whose
                    # due date is long past: Notion keeps hundreds of old tasks as
                    # "in progress" and Sync All used to dump them all back.
                    if nid and (target_user_id, nid) in closed_pairs:
                        continue
                    if sync_all and _stale_due(d, today):
                        continue
                    if insert_allowed:
                        cur.execute(
                            "INSERT INTO standup_tasks (user_id, date, title, notion_id, due_date) VALUES (?, ?, ?, ?, ?)",
                            (target_user_id, today_str, search_title, nid, d)
                        )
                        added_count += 1
                else:
                    if not insert_allowed:
                        cur.execute("DELETE FROM standup_tasks WHERE id=?", (matched_id,))
                    else:
                        cur.execute(
                            "UPDATE standup_tasks SET title=?, due_date=?, notion_id=? WHERE id=?",
                            (search_title, d, nid, matched_id)
                        )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "added": added_count})


@ops_bp.route("/api/debug/tasks", methods=["GET"])
def debug_tasks():
    user_id = request.args.get("user_id", "emp008")
    date_str = today_ist()
    
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, notion_id, date, status FROM standup_tasks WHERE user_id=? AND date=? ORDER BY id DESC", (user_id, date_str))
    db_tasks = [{"id": r[0], "title": r[1], "notion_id": r[2], "date": r[3], "status": r[4]} for r in cur.fetchall()]
    conn.close()
    
    import notion_store
    notion_tasks = notion_store.list_tasks(assigned_to="Happy")
    
    processed_notion = []
    for vt in notion_tasks:
        title = vt.get("title", "Untitled").strip()
        client = vt.get("client_name", "").strip()
        content = vt.get("content", "").strip() or vt.get("description", "").strip() or vt.get("brief", "").strip()
        
        if client and not title.startswith(client):
            title = f"{client} — {title}"
        if content:
            preview = content[:40] + "..." if len(content) > 40 else content
            title = f"{title} ({preview})"
            
        processed_notion.append({
            "notion_id": vt.get("notion_id"),
            "original_title": vt.get("title"),
            "computed_title": title,
            "client_name": client,
            "client_notion_id": vt.get("client_notion_id"),
            "content": content
        })
        
    return jsonify({
        "db_tasks": db_tasks,
        "notion_tasks": processed_notion
    })

@ops_bp.route("/api/debug/cleanup-today", methods=["GET"])
def cleanup_today():
    today_str = today_ist()
    conn = _su_conn()
    cur = conn.cursor()
    # Delete all auto-synced tasks for today (that were not carried over from yesterday)
    cur.execute("DELETE FROM standup_tasks WHERE date=? AND carried_from IS NULL AND notion_id IS NOT NULL", (today_str,))
    deleted_count = cur.rowcount
    conn.commit()
    conn.close()
    return f"Successfully cleaned up {deleted_count} mistakenly synced tasks for today! You can close this tab and click 'Sync All Tasks' again."


@ops_bp.route("/api/standup/smart-add", methods=["POST"])
def standup_smart_add():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    assigned_to = body.get("assigned_to", "")
    title = body.get("title", "").strip()
    due_date = body.get("due_date", "").strip()

    if not user_id or not title:
        return jsonify({"error": "user_id and title required"}), 400

    system_prompt = """You are an AI task router.
The user just typed a new task into their daily standup list.
Is this a "Project Task" that should be tracked in a main project board (e.g. creating a feature, designing a page, writing a proposal), or is it a "Quick Chore" (e.g. check email, call client, meeting, lunch)?
If it's a Project Task, guess the Client Name from the title if possible (otherwise use "Internal").
Respond ONLY in valid JSON format:
{
  "is_project_task": true/false,
  "client_name": "Name or Internal"
}"""
    
    try:
        resp = _claude_call(system_prompt, title, 200)
        import re
        match = re.search(r'\{.*\}', resp, re.DOTALL)
        resp_json = json.loads(match.group(0)) if match else json.loads(resp)
            
        is_project = resp_json.get("is_project_task", False)
        client = resp_json.get("client_name", "Internal")
    except Exception as e:
        logger.error(f"Auto-Router failed: {e}")
        is_project = False
        client = "Internal"

    notion_id = None
    
    if is_project and notion_store.is_configured():
        if not due_date:
            due_date = today_ist()
        created = notion_store.create_task(
            title=title,
            client_name=client,
            client_notion_id="",
            assigned_to=assigned_to,
            due_date=due_date,
            status="in_progress",
            creation_date=today_ist()
        )
        if created and "id" in created:
            notion_id = created["id"]
            
    conn = _su_conn()
    with conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO standup_tasks (user_id, title, status, date, notion_id, due_date) VALUES (?, ?, 'pending', date('now'), ?, ?)",
            (user_id, title, notion_id, due_date)
        )
        task_id = cur.lastrowid

    try:
        from routes.attendance import _attendance_checkin
        _attendance_checkin(user_id)
    except Exception:
        logger.exception("standup_smart_add: auto-checkin failed")

    return jsonify({
        "success": True,
        "task_id": task_id,
        "title": title,
        "notion_id": notion_id,
        "is_project": is_project
    })


@ops_bp.route("/api/standup/push-to-notion/<int:task_id>", methods=["POST"])
def push_to_notion(task_id):
    body = request.get_json(silent=True) or {}
    assigned_to = body.get("assigned_to", "")
    
    conn = _su_conn()
    with conn:
        cur = conn.cursor()
        cur.execute("SELECT title, notion_id FROM standup_tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        
    if not row:
        return jsonify({"error": "Task not found"}), 404
        
    title = row[0]
    if row[1]:
        return jsonify({"error": "Task already in Notion"}), 400
        
    system_prompt = """Extract client name from this task title. Return ONLY json: {"client_name": "Client Name or Internal"}"""
    try:
        resp = _claude_call(system_prompt, title, 100)
        import re
        match = re.search(r'\{.*\}', resp, re.DOTALL)
        client = json.loads(match.group(0)).get("client_name", "Internal") if match else "Internal"
    except:
        client = "Internal"
        
    due_date = today_ist()
    created = notion_store.create_task(
        title=title,
        client_name=client,
        client_notion_id="",
        assigned_to=assigned_to,
        due_date=due_date,
        status="in_progress",
        creation_date=due_date
    )
    if created and "id" in created:
        notion_id = created["id"]
        with conn:
            conn.execute("UPDATE standup_tasks SET notion_id=?, due_date=? WHERE id=?", (notion_id, due_date, task_id))
        return jsonify({"success": True, "notion_id": notion_id})
    return jsonify({"error": "Failed to push to Notion"}), 500


@ops_bp.route("/api/standup/my-tasks/<int:task_id>", methods=["PATCH"])
def update_my_task(task_id: int):
    """
    Update a task's status or blocker.
    Body: { status?: 'done' | 'pending', blocker?: str }
    """
    body   = request.get_json(silent=True) or {}
    status = body.get("status")
    blocker = body.get("blocker")
    title = body.get("title")
    progress = body.get("progress")  # optional progress override (int 0-100)
    subtasks = body.get("subtasks")  # list of dicts: [{"title": "x", "done": true}]
    
    updates = []
    params = []
    if status is not None:
        if status not in ("done", "pending"):
            return jsonify({"error": "status must be 'done' or 'pending'"}), 400
        updates.append("status=?")
        params.append(status)
    if blocker is not None:
        updates.append("blocker=?")
        params.append(blocker)
    if title is not None:
        updates.append("title=?")
        params.append(title.strip())
    if subtasks is not None:
        import json
        updates.append("subtasks=?")
        params.append(json.dumps(subtasks))

    if not updates:
        return jsonify({"error": "no updates provided"}), 400

    updates.append("updated_at=?")
    params.append(now_ist())
    params.append(task_id)

    conn = _su_conn()
    with conn:
        conn.execute(f"UPDATE standup_tasks SET {', '.join(updates)} WHERE id=?", params)

    # Sync to Notion if progress is provided explicitly, OR if subtasks are updated
    cur = conn.cursor()
    cur.execute("SELECT notion_id, subtasks, status FROM standup_tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    conn.close()

    notion_id = row[0] if row else None
    current_subtasks_json = row[1] if row else '[]'
    current_status = row[2] if row else 'pending'

    if notion_id:
        try:
            import notion_store
            notion_status = None
            notion_progress = None

            if status == "pending":
                # Un-tick: only the toggle sends `status`, so this always means
                # "undo the tick". Without it Notion kept saying Done / Need for
                # approval for a task the person had just reopened.
                notion_store.update_task(notion_id, status="in_progress")

            # Explicit progress override
            if progress is not None and current_status == "done":
                notion_progress = int(progress)
                if notion_progress == 100: notion_status = "Done"
                elif notion_progress > 0: notion_status = "In Progress"
            
            # Auto-calculate progress from subtasks
            elif subtasks is not None:
                import json
                st = json.loads(current_subtasks_json) if current_subtasks_json else []
                if st:
                    done_count = sum(1 for s in st if s.get("done"))
                    notion_progress = int((done_count / len(st)) * 100)
                    if notion_progress == 100: notion_status = "Done"
                    elif notion_progress > 0: notion_status = "In Progress"
            
            if notion_progress is not None:
                if notion_status == "Done":
                    task_type = notion_store.get_task_type(notion_id)
                    if task_type and task_type.lower() == "social media":
                        notion_status = "need_for_approval"
                        
                        # Also update local db to reflect this
                        conn = _su_conn()
                        conn.execute("UPDATE standup_tasks SET status='need_for_approval' WHERE id=?", (task_id,))
                        conn.commit()
                        conn.close()

                notion_store.update_task(notion_id, progress=notion_progress, status=notion_status)
        except Exception as e:
            logger.warning(f"Notion sync failed for task {notion_id}: {e}")

    return jsonify({"success": True})


@ops_bp.route("/api/standup/my-tasks/<int:task_id>", methods=["DELETE"])
def delete_my_task(task_id: int):
    """Delete a task from the personal list.

    Social-media (Lumina Sheets) tasks are cross-off-only: crossing one off
    your own daily standup must NOT touch the underlying task at all -- it
    stays exactly as-is in Notion / the Sheets tab / any linked Google
    Sheet for everyone else. Only the local standup_tasks row is removed.
    (User request, 2026-09-07: standup is a personal daily view, not a
    delete button for the shared Sheets board.)

    For a non-social Notion task (branding/website/etc, pushed to the board
    via standup smart-add or the '-> push to board' button), the old
    archive-on-delete behavior is kept: archive that Notion page too, and if
    its client has a linked Google Sheet, remove the row there as well --
    otherwise it's left dangling, and since Notion can't see archived pages
    at all (CLAUDE.md gotcha #93), the next reconcile pass wouldn't
    recognize that task_id as "current" anymore and would silently RECREATE
    it from the still-present stale Sheet row. Mirrors the fix already
    applied to the Sheets tab's own row-delete (routes/sheets_sync.py::
    delete_sheet_task, gotcha #87 2026-08-14)."""
    conn = _su_conn()
    row = conn.execute("SELECT notion_id FROM standup_tasks WHERE id=?", (task_id,)).fetchone()
    notion_id = row[0] if row else None
    with conn:
        conn.execute("UPDATE standup_tasks SET status='deleted' WHERE id=?", (task_id,))
    conn.close()
    if notion_id:
        try:
            task_type = notion_store.get_task_type(notion_id)
        except Exception:
            logger.exception(f"get_task_type failed for {notion_id} while deleting standup row {task_id}")
            task_type = ""
        if task_type and task_type.lower() == "social media":
            # Cross-off-only: leave the Notion page and any Sheet row alone.
            return jsonify({"success": True})

        # Client id has to be read BEFORE archiving -- an archived page is
        # invisible to the Notion API afterward, so this is a one-shot window.
        client_notion_id = ""
        try:
            client_notion_id = (notion_store.get_task_summary(notion_id) or {}).get("client_notion_id", "")
        except Exception:
            logger.exception(f"Failed to look up client for Notion task {notion_id} before deleting standup row {task_id}")
        try:
            notion_store.archive_notion_page(notion_id)
        except Exception:
            logger.exception(f"Failed to archive Notion task {notion_id} after deleting standup row {task_id}")
        if client_notion_id:
            try:
                from routes.sheets_sync import get_link_for_client
                import google_sheets_store as gs
                link = get_link_for_client(client_notion_id)
                if link:
                    delete_fn = gs.delete_task_from_sheet_multi_tab if link.get("multi_tab") else gs.delete_task_from_sheet
                    delete_fn(link, notion_id)
            except Exception:
                logger.exception(f"Failed to remove Sheet row for deleted standup task {notion_id}")
    return jsonify({"success": True})


@ops_bp.route("/api/standup/carry-over", methods=["POST"])
def carry_over_tasks():
    """
    Manually trigger carry-over of today's pending tasks to tomorrow.
    Called when the employee wraps up their day.
    Body: { user_id }
    Returns: count of tasks carried over.
    """
    body    = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    from datetime import timedelta
    today    = today_ist()
    tomorrow = (datetime.now(IST) + timedelta(days=1)).strftime("%Y-%m-%d")

    conn = _su_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT title, blocker, carried_from, notion_id, due_date, subtasks, delegated_to, delegated_from FROM standup_tasks WHERE user_id=? AND date=? AND status='pending'",
        (user_id, today),
    )
    pending = cur.fetchall()

    carried = 0
    if pending:
        with conn:
            for title, blocker, carried_from, nid, dd, sub, d_to, d_from in pending:
                orig_carry_from = carried_from if carried_from else today
                # Avoid duplicating if already carried (idempotent)
                cur2 = conn.cursor()
                cur2.execute(
                    "SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND title=? AND carried_from=?",
                    (user_id, tomorrow, title, orig_carry_from),
                )
                if not cur2.fetchone():
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, carried_from, blocker, notion_id, due_date, subtasks, delegated_to, delegated_from) VALUES (?,?,?,'pending',?,?,?,?,?,?,?)",
                        (user_id, tomorrow, title, orig_carry_from, blocker, nid, dd, sub, d_to, d_from),
                    )
                    carried += 1
    conn.close()
    logger.info(f"Carry-over: {user_id} → {carried} tasks moved to {tomorrow}")
    return jsonify({"success": True, "carried": carried, "date": tomorrow})




# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/alerts", methods=["GET"])
def get_alerts():
    """Return AT_RISK and CRITICAL tasks for founderDashboard."""
    user_id = request.args.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    alerts = task_scheduler.get_all_alerts()
    return jsonify({"alerts": alerts})


@ops_bp.route("/api/alerts/run-check", methods=["POST"])
def run_alert_check():
    """Manually trigger the overdue check. Admin only."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "")
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    fired = task_scheduler.check_overdue_tasks()
    return jsonify({"success": True, "alerts_fired": len(fired), "details": fired})


# ══════════════════════════════════════════════════════════════════════════════
# NOTION
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/notion/status", methods=["GET"])
def notion_status():
    configured = notion_store.is_configured()
    return jsonify({
        "configured": configured,
        "message": "Notion is connected" if configured else (
            "Set NOTION_TOKEN, NOTION_CLIENTS_DB_ID, NOTION_TASKS_DB_ID in config/.env"
        ),
    })


@ops_bp.route("/api/notion/schema-check", methods=["GET"])
def notion_schema_check():
    """
    Compares the live Notion Tasks/Clients DB schemas against what this
    codebase actually reads/writes. Hit this after editing Notion properties
    by hand, or whenever something that reads Notion data starts silently
    returning empty values — it's usually a renamed/missing property.
    """
    return jsonify(notion_store.get_schema_report())


@ops_bp.route("/api/notion/clients", methods=["GET"])
def notion_list_clients():
    status_filter = request.args.get("status", "")
    clients = notion_store.list_clients(status_filter=status_filter)
    return jsonify({"clients": clients, "count": len(clients)})


@ops_bp.route("/api/notion/clients", methods=["POST"])
def notion_create_client():
    from utils import _is_admin
    token = request.cookies.get("session_token", "")
    user_id = _verify_session(token)
    if not user_id or not _is_admin(user_id):
        return jsonify({"error": "Unauthorized: Admin access required"}), 403

    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not notion_store.is_configured():
        return jsonify({"error": "Notion is not configured. Add NOTION_TOKEN and DB IDs to config/.env"}), 503

    c_user = body.get("client_username", "").strip()
    c_pass = body.get("client_password", "").strip()
    if c_user:
        try:
            # Admin is forcing creation: wipe any stuck or old credential with this username
            # so the new client can claim it seamlessly.
            conn = _pt_conn()
            cur = conn.cursor()
            cur.execute("DELETE FROM client_users WHERE username=?", (c_user,))
            conn.commit()
            conn.close()
        except Exception as e:
            pass

    client = notion_store.create_client(
        name=name,
        contact=body.get("contact", ""),
        requirements=body.get("requirements", ""),
        deadline=body.get("deadline", ""),
        budget=body.get("budget", ""),
        notes=body.get("notes", ""),
        status="active",
    )
    if not client:
        return jsonify({"error": "Failed to create client in Notion"}), 500

    c_user = body.get("client_username", "").strip()
    c_pass = body.get("client_password", "").strip()
    if c_user and c_pass:
        try:
            hashed_pass = generate_password_hash(c_pass)
            conn = _pt_conn()
            with conn:
                conn.execute(
                    "INSERT INTO client_users (username, password, client_name, client_notion_id) VALUES (?,?,?,?)",
                    (c_user, hashed_pass, name, client["notion_id"])
                )
            conn.close()
        except Exception as e:
            logger.error(f"Failed to create client user: {e}")

    EMP_NAMES = _load_emp_names()
    SVC_TASKS = {
        "content":  [("Content Brief & Research", "emp006"), ("Write Copy / Content Draft", "emp006"), ("Content Review & Approval", "emp004")],
        # emp005 doesn't exist in employees.json (confirmed live) -- was silently
        # showing the raw id "emp005" instead of a name via EMP_NAMES.get(e, e)'s
        # fallback below. emp012 (Saurav Gupta, Video Editor) is the closest real match.
        "video":    [("Video Script Writing", "emp006"), ("Video Shoot / Production", "emp012"), ("Video Editing & Post", "emp012"), ("AI Video Enhancements", "emp008")],
        "design":   [("Design Brief & Moodboard", "emp002"), ("UI/UX Design — Wireframes", "emp002"), ("Final Design Handoff", "emp002")],
        "website":  [("Website Architecture Plan", "emp001"), ("Frontend Development", "emp003"), ("Backend / Integrations", "emp001"), ("QA & Launch", "emp003")],
        "accounts": [("Invoice & Payment Setup", "emp007"), ("Monthly Reporting", "emp007")],
        # "social" intentionally omitted — social posts are added via the Add Tasks calendar
    }
    deadline = body.get("deadline", "")
    tasks_created = 0
    custom_tasks = body.get("tasks", [])

    if custom_tasks:
        for t in custom_tasks:
            task_title = t.get("title", "").strip()
            if not task_title:
                continue
            emp_ids   = [e.strip() for e in t.get("who", "emp001").split(",")]
            emp_names = ", ".join(EMP_NAMES.get(e, e) for e in emp_ids)
            svc_map   = {"emp001":"website","emp002":"design","emp003":"website",
                         "emp004":"accounts","emp012":"video","emp006":"content",
                         "emp007":"accounts","emp008":"video"}
            first_emp = emp_ids[0] if emp_ids else "emp001"
            result = notion_store.create_task(
                title=task_title, client_name=name,
                client_notion_id=client["notion_id"], assigned_to=emp_names,
                due_date=t.get("due_date", "") or deadline,
                status="not_started", service=svc_map.get(first_emp, "general"),
            )
            if result:
                tasks_created += 1
    else:
        for svc in body.get("services", []):
            for (task_title, emp_id) in SVC_TASKS.get(svc, []):
                result = notion_store.create_task(
                    title=task_title, client_name=name,
                    client_notion_id=client["notion_id"],
                    assigned_to=EMP_NAMES.get(emp_id, emp_id),
                    due_date=deadline, status="not_started", service=svc,
                )
                if result:
                    tasks_created += 1

    logger.info(f"Notion: onboarded client '{name}' with {tasks_created} tasks")
    return jsonify({
        "success": True,
        "notion_id": client["notion_id"],
        "name": name,
        "tasks_created": tasks_created,
        "notion_url": f"https://notion.so/{client['notion_id'].replace('-', '')}",
    })


@ops_bp.route("/api/notion/tasks", methods=["GET", "POST"])
def notion_list_or_create_tasks():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        title = body.get("title", "").strip()
        assigned_to = body.get("assigned_to", "")
        due_date = body.get("due_date", "")
        client_name = body.get("client_name", "Internal")
        client_notion_id = body.get("client_id", "")
        
        if not title:
            return jsonify({"error": "Title is required"}), 400
            
        result = notion_store.create_task(
            title=title, 
            client_name=client_name,
            client_notion_id=client_notion_id, 
            assigned_to=assigned_to,
            due_date=due_date,
            status="not_started"
        )
        if result:
            return jsonify({"success": True, "task": result})
        return jsonify({"error": "Failed to create task in Notion"}), 500

    # GET request
    tasks = notion_store.list_tasks(
        assigned_to=request.args.get("assigned_to", ""),
        client_notion_id=request.args.get("client_id", ""),
        status_filter=request.args.get("status", ""),
    )
    return jsonify({"tasks": tasks, "count": len(tasks)})



@ops_bp.route("/api/notion/tasks/<string:notion_id>", methods=["PATCH"])
def notion_update_task(notion_id: str):
    body   = request.get_json(silent=True) or {}

    EMP_NAMES = _load_emp_names()

    raw_assigned = body.get("assigned_to")
    raw_assigned_ids = [a.strip() for a in raw_assigned.split(",") if a.strip()] if raw_assigned is not None else []
    mapped_assigned = ",".join(EMP_NAMES.get(a, a) for a in raw_assigned_ids) if raw_assigned is not None else None

    edit_summary = _edit_summary_from_body(body)
    last_edited = None
    if edit_summary:
        editor_name = EMP_NAMES.get(body.get("user_id", ""), body.get("user_id", "")) or "Someone"
        last_edited = f"{today_ist()} {now_ist()}|{editor_name}|{edit_summary}"

    # Marking a social-media row "Posted"/"Final" straight from the Lumina
    # Sheets tab used to skip approval entirely -- only the standup-tick
    # path (update_my_task) routed through "Need for approval" first. Same
    # gate here now: ticking a social task complete, from either place,
    # always lands in review first. (User request, 2026-09-07.)
    effective_status = body.get("status")
    if effective_status and str(effective_status).strip().lower() in ("posted", "final"):
        try:
            task_type = notion_store.get_task_type(notion_id)
        except Exception:
            logger.exception(f"get_task_type failed while gating Sheets status for {notion_id}")
            task_type = ""
        if task_type and task_type.lower() == "social media":
            effective_status = "need_for_approval"

    result = notion_store.update_task(
        notion_id=notion_id, status=effective_status,
        progress=body.get("progress"), submission_note=body.get("submission_note"),
        assigned_to=mapped_assigned, new_title=body.get("new_title"),
        due_date=body.get("due_date"), creation_date=body.get("creation_date"),
        task_title=body.get("task_title", ""),
        assignee=body.get("assignee", ""), client_name=body.get("client_name", ""),
        last_edited=last_edited,
    )
    
    # ── Auto-sync assignment to standup_tasks ─────────────────────────────────
    if result:
        task_title = body.get("new_title") or body.get("task_title") or ""
        if not task_title:
            summary = notion_store.get_task_summary(notion_id)
            parts = [p for p in (summary.get("client_name"), summary.get("title")) if p]
            task_title = " — ".join(parts) or "Untitled Task"
            if summary.get("content"):
                preview = summary["content"][:40] + "..." if len(summary["content"]) > 40 else summary["content"]
                task_title = f"{task_title} ({preview})"
        new_due = body.get("due_date")
        
        su_conn = _su_conn()
        
        # Sync title & due_date to any existing standup tasks tied to this notion_id
        with su_conn:
            su_conn.execute(
                "UPDATE standup_tasks SET title=?, due_date=COALESCE(?, due_date) WHERE notion_id=?",
                (task_title, new_due, notion_id)
            )
            
        today_str = today_ist()
        for uid in raw_assigned_ids:
            cur2 = su_conn.cursor()
            cur2.execute(
                "SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND notion_id=?",
                (uid, today_str, notion_id)
            )
            if not cur2.fetchone():
                with su_conn:
                    su_conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, notion_id, due_date) VALUES (?,?,?,'pending',?,?)",
                        (uid, today_str, task_title, notion_id, new_due)
                    )
        su_conn.close()
    # ─────────────────────────────────────────────────────────────────────────

    # The WhatsApp notification is handled internally by notion_store.update_task

    if result:
        return jsonify({"success": True})
    return jsonify({"error": "Failed to update task in Notion"}), 500


@ops_bp.route("/api/notion/tasks/<string:notion_id>", methods=["DELETE"])
def notion_delete_task(notion_id: str):
    if notion_store.archive_notion_page(notion_id):
        return jsonify({"success": True})
    return jsonify({"error": "Failed to delete task"}), 500


@ops_bp.route("/api/notion/tasks/<string:notion_id>/set-client-id", methods=["POST"])
def notion_set_task_client_id(notion_id: str):
    """Manual repair tool for a task whose Client ID rich_text property is
    blank/wrong -- makes it invisible to per-client views (Sheets tab,
    dashboard) even though it displays a correct client name everywhere
    else (that comes from a different property). See notion_store.
    set_client_id() and CLAUDE.md gotcha #79. Admin-only, not linked from
    any UI -- meant to be called directly when this specific symptom is
    diagnosed, not a general-purpose bulk tool."""
    body = request.get_json(silent=True) or {}
    user_id = str(body.get("user_id", "")).strip()
    if not _is_admin(user_id):
        return jsonify({"error": "Unauthorized"}), 403
    client_notion_id = str(body.get("client_notion_id", "")).strip()
    if not client_notion_id:
        return jsonify({"error": "client_notion_id required"}), 400
    if notion_store.set_client_id(notion_id, client_notion_id):
        return jsonify({"success": True})
    return jsonify({"error": "Failed to set Client ID"}), 500


def _unlink_google_sheet_for_client(client_id: str):
    """Best-effort cleanup, called whenever a client is deleted. Without
    this, a client's google_sheet_links row (and its still-valid,
    unguessable link_token) survives the client's own deletion -- the
    physical Google Sheet and its installed Apps Script trigger are
    completely outside this app's control, so nothing stops an employee
    from continuing to edit it after "deleting" the client in Lumina.
    The next edit fires the webhook, which reconciles against
    _current_tasks_by_id(client_id, ...) -- now empty, since every task
    was just archived/deleted -- so EVERY row in that now-orphaned Sheet
    looks unrecognized and gets recreated as a brand-new task under a
    client_id that no longer exists anywhere in Lumina's UI, forever, on
    every future edit to a sheet nobody remembers is still connected.
    Deleting the link row makes the webhook 404 (get_link_by_token finds
    nothing) instead of silently resurrecting a "deleted" client's entire
    task board as orphaned pages. Never allowed to raise -- a failure
    here must not block the client deletion that already succeeded."""
    try:
        conn = _su_conn()
        with conn:
            conn.execute("DELETE FROM google_sheet_links WHERE client_id=?", (str(client_id),))
        conn.close()
    except Exception:
        logger.exception(f"Failed to unlink Google Sheet for deleted client {client_id}")


@ops_bp.route("/api/notion/clients/<string:notion_id>", methods=["DELETE"])
def notion_delete_client(notion_id: str):
    """Idempotent: calling this on an already-deleted (archived) client is a
    safe no-op success, not a 500. Notion's API rejects re-archiving a page
    that's already archived, so without this check, a retried/duplicate
    delete call (e.g. a double-click, or manual recovery after a stale
    client_id caused a bad Google Sheet link -- see CLAUDE.md gotcha #94)
    used to fail outright, even though there was nothing left to actually
    delete. Still archives any straggler tasks and unlinks any Sheet found
    under this id either way, so re-running delete on an already-gone
    client remains useful cleanup, not just a no-op."""
    from utils import _is_admin
    token = request.cookies.get("session_token", "")
    user_id = _verify_session(token)
    if not user_id or not _is_admin(user_id):
        return jsonify({"error": "Unauthorized: Admin access required"}), 403

    if not notion_store.is_client_active(notion_id):
        all_client_tasks = notion_store.list_tasks(client_notion_id=notion_id)
        for t in all_client_tasks:
            tid = t.get("notion_id") or t.get("id")
            if tid:
                notion_store.archive_notion_page(tid)
        _unlink_google_sheet_for_client(notion_id)
        return jsonify({"success": True, "already_deleted": True})

    all_client_tasks = notion_store.list_tasks(client_notion_id=notion_id)
    for t in all_client_tasks:
        tid = t.get("notion_id") or t.get("id")
        if tid:
            notion_store.archive_notion_page(tid)
    if notion_store.archive_notion_page(notion_id):
        _unlink_google_sheet_for_client(notion_id)
        return jsonify({"success": True})
    return jsonify({"error": "Failed to delete client"}), 500


@ops_bp.route("/api/notion/dashboard", methods=["GET"])
def notion_dashboard():
    data = notion_store.get_dashboard_data()
    user_id = request.args.get("user_id", "")
    
    try:
        from utils import _is_admin
        if _is_admin(user_id):
            conn = _pt_conn()
            cur = conn.cursor()
            cur.execute("SELECT client_notion_id, username FROM client_users WHERE client_notion_id IS NOT NULL")
            users = {r[0]: {"username": r[1]} for r in cur.fetchall()}
            conn.close()
            
            if "clients" in data:
                for c in data["clients"]:
                    nid = c.get("notion_id") or c.get("id")
                    if nid in users:
                        c["client_username"] = users[nid]["username"]
        
        # Attach task feedback
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("SELECT task_id, status, comments, audio_url, updated_at FROM client_task_feedback")
        feedback_map = {r[0]: {"status": r[1], "comments": r[2], "audio_url": r[3], "updated_at": r[4]} for r in cur.fetchall()}
        conn.close()

        # Attach standup subtasks & filter out tasks done in standup
        su_conn = _su_conn()
        su_cur = su_conn.cursor()
        su_cur.execute("SELECT notion_id, status, subtasks FROM standup_tasks WHERE notion_id IS NOT NULL ORDER BY date ASC, id ASC")
        standup_map = {r[0]: {"status": r[1], "subtasks": r[2]} for r in su_cur.fetchall()}

        # "Daily Standup Tasks" board block = only tasks sitting in someone's
        # CURRENT standup. standup_map above keeps the last row ever seen per
        # task (any date, any status incl. deleted/delegated), so old rows
        # kept hundreds of finished/abandoned tasks on the board. Here, a
        # person's "current" standup is their most recent standup date (not
        # strictly today, so someone who hasn't opened the app yet still
        # counts). A task shows only while it is open there, and drops the
        # moment anyone has it ticked done.
        su_cur.execute(
            "SELECT s.notion_id, s.status FROM standup_tasks s "
            "JOIN (SELECT user_id, MAX(date) AS d FROM standup_tasks GROUP BY user_id) m "
            "ON m.user_id = s.user_id AND m.d = s.date WHERE s.notion_id IS NOT NULL")
        _gone = {"deleted", "delegated"}
        standup_open, standup_done = set(), set()
        for nid_, st_ in su_cur.fetchall():
            st_l = (st_ or "").strip().lower()
            if st_l == "done":
                standup_done.add(nid_)
            elif st_l not in _gone:
                standup_open.add(nid_)
        su_conn.close()

        if "clients" in data:
            for c in data["clients"]:
                filtered_tasks = []
                for t in c.get("tasks", []):
                    t_id = t.get("notion_id") or t.get("id")
                    if c.get("notion_id") == "unassigned":
                        if t_id not in standup_open or t_id in standup_done:
                            continue
                    # Filter out if marked done in standup, and filter out random unassigned tasks
                    if t_id and t_id in standup_map:
                        if standup_map[t_id]["status"] == "done":
                            continue  # Skip adding this task to the list
                            
                        # Attach subtasks
                        su_subtasks = standup_map[t_id]["subtasks"]
                        if su_subtasks and su_subtasks != '[]':
                            try:
                                import json
                                t["subtasks"] = json.loads(su_subtasks)
                            except:
                                pass
                    else:
                        # If this is the Daily Standup Tasks block ("unassigned"), and it's NOT in standup_map, drop it!
                        if c.get("notion_id") == "unassigned":
                            continue
                            
                    if t_id and t_id in feedback_map:
                        t["feedback"] = feedback_map[t_id]
                        
                    filtered_tasks.append(t)
                c["tasks"] = filtered_tasks
                        
    except Exception as e:
        logger.error(f"Error attaching dashboard metadata: {e}")
        
    return jsonify(data)


# ══════════════════════════════════════════════════════════════════════════════
# QUICK TASKS
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/quick-tasks", methods=["GET"])
def get_quick_tasks():
    if notion_store.is_configured():
        tasks = notion_store.list_tasks(client_notion_id="__quick__")
        return jsonify({"tasks": tasks})
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id,client_id,title,description,assigned_to,status,progress,
               due_date,submission_note,submission_file,rejection_note,
               submission_count,opened_at,created_at
        FROM tasks WHERE client_id IS NULL ORDER BY created_at DESC
    """)
    tasks = [_task_row_to_dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"tasks": tasks})


@ops_bp.route("/api/quick-tasks", methods=["POST"])
def save_quick_task():
    body = request.get_json(silent=True) or {}
    title       = body.get("title", "").strip()
    assigned_to = body.get("assigned_to", "")
    due_date    = body.get("due_date", "")
    notes       = body.get("notes", "")
    if not title:
        return jsonify({"error": "title required"}), 400

    if notion_store.is_configured():
        result = notion_store.create_task(
            title=title, client_name="Quick Task", client_notion_id="__quick__",
            assigned_to=assigned_to, due_date=due_date, status="not_started",
        )
        if result:
            return jsonify({"success": True, "notion_id": result["notion_id"]}), 201
        return jsonify({"error": "Failed to create quick task in Notion"}), 500

    conn = _pt_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks (client_id,title,description,assigned_to,due_date,status,progress) VALUES (?,?,?,?,?,'not_started',0)",
            (None, title, notes, assigned_to, due_date)
        )
        task_id = cur.lastrowid
    conn.close()
    return jsonify({"success": True, "task_id": task_id}), 201


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE FALLBACK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/sqlite/tasks/<int:task_id>", methods=["PATCH"])
def sqlite_patch_task(task_id: int):
    body = request.get_json(silent=True) or {}
    updates, vals = [], []
    for field, col in [("new_title","title"),("assigned_to","assigned_to"),
                       ("due_date","due_date"),("status","status"),("progress","progress"),
                       ("submission_note","description")]:
        if field in body:
            updates.append(f"{col}=?"); vals.append(body[field])
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    editor_summary = _edit_summary_from_body(body)
    if editor_summary:
        editor_id = body.get("user_id", "")
        editor_name = _load_emp_names().get(editor_id, editor_id) or "Someone"
        updates.append("last_edited_by=?"); vals.append(editor_name)
        updates.append("last_edited_at=?"); vals.append(f"{today_ist()} {now_ist()}")
        updates.append("last_edited_summary=?"); vals.append(editor_summary)

    vals.append(task_id)
    # ── Fetch old state for WhatsApp notifications ────────────────────────
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT title, assigned_to, status, client_id FROM tasks WHERE id=?", 
        (task_id,)
    )
    old_row = cur.fetchone()
    old_status = old_row[2] if old_row else None
    
    with conn:
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", vals)
        
    # ── Trigger WhatsApp Notification if status changed ─────────────────────
    new_status = body.get("status")
    if old_row and new_status and new_status != old_status:
        try:
            from notifications import notify_task_status_changed

            task_title = body.get("new_title", old_row[0])
            raw_assignee = body.get("assigned_to", old_row[1])
            assignee_name = _load_emp_names().get(raw_assignee, raw_assignee)
            client_id = old_row[3]
            client_name = "Internal"
            
            if client_id:
                cur.execute("SELECT name FROM clients WHERE id=?", (client_id,))
                c_row = cur.fetchone()
                if c_row:
                    client_name = c_row[0]
                    
            notify_task_status_changed(
                task_title=task_title, 
                assignee=assignee_name, 
                client_name=client_name, 
                old_status=old_status, 
                new_status=new_status
            )
        except Exception as e:
            logger.error(f"Failed to send WhatsApp notification: {e}")
    # ─────────────────────────────────────────────────────────────────────────
    # ── Auto-sync assignment to standup_tasks ─────────────────────────────────
    new_assignee = body.get("assigned_to", "").strip()
    task_title = body.get("new_title") or (old_row[0] if old_row else f"Task #{task_id}")
    new_due = body.get("due_date")
    
    su_conn = _su_conn()
    
    # Sync title and due_date for existing standup tasks matching the old title.
    # Scoped to the task's assignee too — title alone isn't unique across the
    # team (e.g. two clients both have a "Weekly Sync" task), and without this
    # scoping an edit here would silently overwrite every employee's standup
    # row sharing that title, not just the person this task actually belongs to.
    sync_assignee = new_assignee or (old_row[1] if old_row else None)
    if old_row and old_row[0] and sync_assignee:
        with su_conn:
            su_conn.execute(
                "UPDATE standup_tasks SET title=COALESCE(?, title), due_date=COALESCE(?, due_date) WHERE title=? AND user_id=?",
                (task_title if task_title != old_row[0] else None, new_due, old_row[0], sync_assignee)
            )
            
    if new_assignee:
        today_str = today_ist()
        cur2 = su_conn.cursor()
        # Only insert if not already in today's standup for this user
        cur2.execute(
            "SELECT id FROM standup_tasks WHERE user_id=? AND date=? AND title=?",
            (new_assignee, today_str, task_title)
        )
        if not cur2.fetchone():
            with su_conn:
                su_conn.execute(
                    "INSERT INTO standup_tasks (user_id, date, title, status, due_date) VALUES (?,?,?,'pending',?)",
                    (new_assignee, today_str, task_title, new_due)
                )
    su_conn.close()
    # ─────────────────────────────────────────────────────────────────────────

    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/sheets/tasks/<string:task_id>/log-version", methods=["POST"])
def log_sheet_version(task_id: str):
    """Appends one full-snapshot version to a Sheets row's history. Fire-and-
    forget from the frontend -- never blocks the actual row save."""
    body = request.get_json(silent=True) or {}
    if not _is_admin(body.get("user_id", "")):
        return jsonify({"error": "Unauthorized"}), 403
    client_id = str(body.get("client_id", "")).strip()
    editor_name = str(body.get("editor_name", "")).strip() or "Someone"
    snapshot = body.get("snapshot")
    changed_fields = body.get("changed_fields")
    if not client_id or not isinstance(snapshot, dict):
        return jsonify({"error": "client_id and snapshot required"}), 400

    conn = _su_conn()
    with conn:
        conn.execute(
            "INSERT INTO sheet_edit_log (task_id, client_id, editor_name, edited_at, snapshot, changed_fields) VALUES (?,?,?,?,?,?)",
            (task_id, client_id, editor_name, f"{today_ist()} {now_ist()}", json.dumps(snapshot),
             json.dumps(changed_fields) if isinstance(changed_fields, list) else None),
        )
    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/sheets/tasks/<string:task_id>/versions", methods=["GET"])
def list_sheet_versions(task_id: str):
    """Full version history for one Sheets row, newest first."""
    if not _is_admin(request.args.get("user_id", "")):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, editor_name, edited_at, snapshot, changed_fields FROM sheet_edit_log WHERE task_id=? ORDER BY edited_at DESC, id DESC",
        (task_id,),
    )
    rows = cur.fetchall()
    conn.close()
    versions = []
    for r in rows:
        try:
            snapshot = json.loads(r[3])
        except Exception:
            snapshot = {}
        try:
            changed_fields = json.loads(r[4]) if r[4] else None
        except Exception:
            changed_fields = None
        versions.append({"id": r[0], "editor_name": r[1], "edited_at": r[2], "snapshot": snapshot, "changed_fields": changed_fields})
    return jsonify({"versions": versions})


@ops_bp.route("/api/sheets/clients/<string:client_id>/versions", methods=["GET"])
def list_client_sheet_versions(client_id: str):
    """Every version across every row for one client's sheet, newest first --
    powers the global whole-sheet version browser (as opposed to
    list_sheet_versions above, which is scoped to a single row)."""
    if not _is_admin(request.args.get("user_id", "")):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, task_id, editor_name, edited_at, snapshot, changed_fields FROM sheet_edit_log WHERE client_id=? ORDER BY edited_at DESC, id DESC",
        (client_id,),
    )
    rows = cur.fetchall()
    conn.close()
    versions = []
    for r in rows:
        try:
            snapshot = json.loads(r[4])
        except Exception:
            snapshot = {}
        try:
            changed_fields = json.loads(r[5]) if r[5] else None
        except Exception:
            changed_fields = None
        versions.append({"id": r[0], "task_id": r[1], "editor_name": r[2], "edited_at": r[3], "snapshot": snapshot, "changed_fields": changed_fields})
    return jsonify({"versions": versions})


@ops_bp.route("/api/sheets/hidden-rows", methods=["GET"])
def list_all_hidden_rows():
    """Every hidden Sheets row across every client, in one shot -- powers
    the frontend's shared hidden-rows cache loaded once at page load, so
    the row-hide feature is visible to every employee, not just the one
    whose browser hid it."""
    if not _is_admin(request.args.get("user_id", "")):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT task_id, client_id, hidden_by, hidden_at FROM sheet_hidden_rows")
    rows = cur.fetchall()
    conn.close()
    hidden = {}
    for task_id, client_id, hidden_by, hidden_at in rows:
        hidden.setdefault(client_id, []).append(
            {"task_id": task_id, "hidden_by": hidden_by, "hidden_at": hidden_at}
        )
    return jsonify({"hidden": hidden})


@ops_bp.route("/api/sheets/tasks/<string:task_id>/hide", methods=["POST"])
def hide_sheet_row(task_id: str):
    body = request.get_json(silent=True) or {}
    if not _is_admin(body.get("user_id", "")):
        return jsonify({"error": "Unauthorized"}), 403
    client_id = str(body.get("client_id", "")).strip()
    hidden_by = str(body.get("hidden_by", "")).strip() or "Someone"
    if not client_id:
        return jsonify({"error": "client_id required"}), 400
    conn = _su_conn()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO sheet_hidden_rows (task_id, client_id, hidden_by, hidden_at) VALUES (?,?,?,?)",
            (task_id, client_id, hidden_by, f"{today_ist()} {now_ist()}"),
        )
    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/sheets/tasks/<string:task_id>/unhide", methods=["POST"])
def unhide_sheet_row(task_id: str):
    body = request.get_json(silent=True) or {}
    if not _is_admin(body.get("user_id", "")):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _su_conn()
    with conn:
        conn.execute("DELETE FROM sheet_hidden_rows WHERE task_id=?", (task_id,))
    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/sheets/clients/<string:client_id>/unhide-all", methods=["POST"])
def unhide_all_sheet_rows(client_id: str):
    body = request.get_json(silent=True) or {}
    if not _is_admin(body.get("user_id", "")):
        return jsonify({"error": "Unauthorized"}), 403
    conn = _su_conn()
    with conn:
        conn.execute("DELETE FROM sheet_hidden_rows WHERE client_id=?", (client_id,))
    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/sqlite/tasks/<int:task_id>", methods=["DELETE"])
def sqlite_delete_task(task_id: int):
    conn = _pt_conn()
    with conn:
        conn.execute("DELETE FROM dependencies WHERE task_id=? OR depends_on_task_id=?", (task_id, task_id))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.close()
    return jsonify({"success": True})


@ops_bp.route("/api/sqlite/clients/<int:client_id>", methods=["DELETE"])
def sqlite_delete_client(client_id: int):
    conn = _pt_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM tasks WHERE client_id=?", (client_id,))
    task_ids = [r[0] for r in cur.fetchall()]
    with conn:
        for tid in task_ids:
            conn.execute("DELETE FROM dependencies WHERE task_id=? OR depends_on_task_id=?", (tid, tid))
        conn.execute("DELETE FROM tasks WHERE client_id=?", (client_id,))
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
    conn.close()
    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENT EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/export", methods=["POST"])
def export_document():
    """Convert AI-generated markdown to a downloadableDOCX,PDF, orPPTX file."""
    from document_exporter import export_docx, export_pdf, export_pptx

    body    = request.get_json(silent=True) or {}
    content = body.get("content", "").strip()
    fmt     = body.get("format", "pdf").lower()
    title   = body.get("title", "System Export")[:120]

    if not content:
        return jsonify({"error": "No content provided"}), 400
    if fmt not in ("docx", "pdf", "pptx"):
        return jsonify({"error": "Unsupported format. Use docx, pdf, or pptx."}), 400

    try:
        if fmt == "docx":
            buf = export_docx(content, title=title)
            mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ext = "docx"
        elif fmt == "pdf":
            buf = export_pdf(content, title=title)
            mimetype = "application/pdf"
            ext = "pdf"
        else:
            buf = export_pptx(content, title=title)
            mimetype = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ext = "pptx"

        safe_name = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_") or "export"
        return send_file(buf, mimetype=mimetype, as_attachment=True,
                         download_name=f"{safe_name}.{ext}")
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception:
        logger.exception("Export failed")
        return jsonify({"error": "Export failed"}), 500


@ops_bp.route("/api/export/standup-tasks", methods=["GET"])
def export_standup_tasks():
    """Export all personal standup tasks to a CSV file."""
    import csv
    from io import StringIO
    from flask import Response
    
    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, date, title, status, blocker, carried_from, created_at FROM standup_tasks ORDER BY date DESC, user_id ASC")
    rows = cur.fetchall()
    conn.close()

    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["user_id", "date", "title", "status", "blocker", "carried_from", "created_at"])
    cw.writerows(rows)
    
    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=standup_tasks_export.csv"}
    )


@ops_bp.route("/api/export/standup-sheets", methods=["GET"])
def export_standup_sheets():
    """Excel workbook of the daily standup, laid out like the attendance
    export: All / Summary / one sheet per person (see standup_export.py).
    Optional ?from=YYYY-MM-DD&to=YYYY-MM-DD; default is everything. Real
    session required (same check as the attendance export)."""
    from routes.attendance import _verified_admin
    if not _verified_admin():
        return "Unauthorized", 403
    import standup_export
    from datetime import date as _date
    d_from = (request.args.get("from") or "").strip()
    d_to = (request.args.get("to") or "").strip()
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if (d_from and not pat.match(d_from)) or (d_to and not pat.match(d_to)):
        return jsonify({"error": "from/to must be YYYY-MM-DD"}), 400

    sql = ("SELECT user_id, date, title, status, blocker, carried_from, created_at, "
           "notion_id, subtasks, delegated_to, delegated_from, due_date FROM standup_tasks "
           "WHERE COALESCE(status,'') != 'deleted'")
    params = []
    if d_from:
        sql += " AND date >= ?"
        params.append(d_from)
    if d_to:
        sql += " AND date <= ?"
        params.append(d_to)
    conn = _su_conn()
    try:
        raw = conn.execute(sql + " ORDER BY date, user_id", params).fetchall()
    finally:
        conn.close()

    emp_map, active = {}, []
    inactive = {"inactive", "disabled", "left", "removed", "archived", "former"}
    try:
        for e in _load_employees().get("employees", []):
            emp_map[e["id"]] = e["name"]
            if str(e.get("status", "active")).strip().lower() not in inactive:
                active.append(e)
    except Exception:
        logger.exception("export_standup_sheets: employee load failed")

    # Client / task type / due date for standup rows linked to a board task.
    # One Notion listing for the whole export; any failure just leaves those
    # columns blank rather than failing the download.
    notion_info = {}
    wanted = {r[7] for r in raw if r[7]}
    if wanted:
        try:
            if notion_store.is_configured():
                for t in notion_store.list_tasks():
                    if t.get("notion_id") in wanted:
                        notion_info[t["notion_id"]] = {
                            "client": t.get("client_name") or "",
                            "type": t.get("service") or t.get("type") or "",
                            "due": t.get("due_date") or "",
                        }
        except Exception:
            logger.exception("export_standup_sheets: notion lookup failed")

    data = standup_export.build_workbook(raw, emp_map, active, _date.fromisoformat(today_ist()),
                                         notion_info)
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment;filename=lumina_standup.xlsx"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# CLAUDE AI ROUTES — Task Intelligence
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/ai/breakdown", methods=["POST"])
def ai_task_breakdown():
    """
    Feature 1: Ask System to auto-generate sub-tasks for a given task title.
    Body: { task_title, client_name (optional) }
    Returns: { subtasks: ["step 1", "step 2", ...] }
    """
    body = request.get_json(silent=True) or {}
    task_title = body.get("task_title", "").strip()
    client_name = body.get("client_name", "").strip()

    if not task_title:
        return jsonify({"error": "task_title is required"}), 400

    system = (
        "You are a senior project manager helping an agency team member break down tasks into actionable sub-steps. "
        "Return ONLY a JSON array of strings. Each string is one clear, concise sub-task (max 10 words). "
        "No explanations, no markdown, no numbering — just a raw JSON array like: "
        '[\"Design wireframe\", \"Write copy\", \"Code component\"]'
    )
    client_ctx = f" for client '{client_name}'" if client_name else ""
    user = f"Break down this task into 4-7 practical sub-tasks: '{task_title}'{client_ctx}"

    try:
        raw = _claude_call(system, user, max_tokens=512)
        import re
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            raw = match.group(0)
        # Parse raw JSON array
        subtasks = json.loads(raw)
        if not isinstance(subtasks, list):
            raise ValueError("Not a list")
        return jsonify({"subtasks": subtasks})
    except (json.JSONDecodeError, ValueError):
        # Fallback: extract lines from raw text
        lines = [l.strip().lstrip("•-123456789. \",") for l in raw.split("\n") if l.strip() and l.strip() not in ("```json", "```", "[", "]")]
        return jsonify({"subtasks": lines[:8]})
    except Exception as e:
        logger.exception("ai_task_breakdown failed")
        return jsonify({"error": str(e)}), 500


@ops_bp.route("/api/ai/proof-of-work", methods=["POST"])
def ai_proof_of_work():
    """
    Feature 2: Auto-draft a professional submission note.
    Body: { task_title, client_name, subtasks: [{ text, done }] }
    Returns: { draft: "..." }
    """
    body = request.get_json(silent=True) or {}
    task_title = body.get("task_title", "").strip()
    client_name = body.get("client_name", "").strip()
    subtasks = body.get("subtasks", [])

    if not task_title:
        return jsonify({"error": "task_title is required"}), 400

    done_list = [st.get("text", "") for st in subtasks if st.get("done")]
    pending_list = [st.get("text", "") for st in subtasks if not st.get("done")]

    completed_str = "\n".join(f"- {t}" for t in done_list) if done_list else "- All work completed"
    pending_str = "\n".join(f"- {t}" for t in pending_list) if pending_list else "None"

    system = (
        "You are a professional agency employee writing a polished, concise progress update for your manager. "
        "Write 2-4 sentences in first person. Be specific, confident and professional. "
        "Do NOT use placeholders or generic filler. Return only the note text, no preamble."
    )
    user = (
        f"Task: '{task_title}'" + (f" (Client: {client_name})" if client_name else "") + "\n"
        f"Completed steps:\n{completed_str}\n"
        f"Remaining:\n{pending_str}\n\n"
        "Write a professional proof-of-work submission note."
    )

    try:
        draft = _claude_call(system, user, max_tokens=300)
        return jsonify({"draft": draft})
    except Exception as e:
        logger.exception("ai_proof_of_work failed")
        return jsonify({"error": str(e)}), 500


@ops_bp.route("/api/ai/parse-task", methods=["POST"])
def ai_parse_task():
    """
    Feature 3: Natural language task parsing — extract title, client, due_date from free text.
    Body: { text, assigned_name }
    Returns: { title, client_name, due_date (YYYY-MM-DD or null) }
    """
    body = request.get_json(silent=True) or {}
    text = body.get("text", "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400

    today = today_ist()
    tomorrow = (datetime.now(IST) + timedelta(days=1)).strftime("%Y-%m-%d")
    next_week = (datetime.now(IST) + timedelta(days=7)).strftime("%Y-%m-%d")

    system = (
        "You are a task parser. Extract structured data from the user's natural language task description. "
        f"Today's date is {today}. Tomorrow is {tomorrow}. Next week is {next_week}. "
        "Return ONLY a JSON object with exactly these keys: title (string), client_name (string or null), due_date (YYYY-MM-DD string or null). "
        'Example: {"title": "Fix responsive header bug", "client_name": "MetaZune", "due_date": "2026-05-20"}'
    )
    user = f"Parse this task: '{text}'"

    try:
        raw = _claude_call(system, user, max_tokens=200)
        parsed = json.loads(raw)
        return jsonify({
            "title": parsed.get("title", text),
            "client_name": parsed.get("client_name") or "",
            "due_date": parsed.get("due_date") or today,
        })
    except Exception as e:
        logger.exception("ai_parse_task failed")
        return jsonify({"title": text, "client_name": "", "due_date": today})


@ops_bp.route("/api/ai/coach", methods=["POST"])
def ai_coach():
    """
    Feature 4: Daily AI Coach — answer questions about the user's Notion task list.
    Body: { question, assigned_name, user_id }
    Returns: { reply: "..." }
    """
    body = request.get_json(silent=True) or {}
    question = body.get("question", "").strip()
    assigned_name = body.get("assigned_name", "")

    if not question:
        return jsonify({"error": "question is required"}), 400

    # Fetch the user's tasks from Notion
    tasks = notion_store.list_tasks(assigned_to=assigned_name) if assigned_name else []

    today = today_ist()
    task_summary = ""
    if tasks:
        lines = []
        for t in tasks:
            status = t.get("status", "not_started")
            due = t.get("due_date", "no date")
            title = t.get("title", "Untitled")
            client = t.get("client_name", "")
            overdue = " [OVERDUE]" if due and due < today and status not in ("approved", "submitted") else ""
            lines.append(f"- {title} | Status: {status} | Due: {due}{overdue}" + (f" | Client: {client}" if client else ""))
        task_summary = "\n".join(lines)
    else:
        task_summary = "No tasks found."

    system = (
        f"You are a friendly, concise AI productivity coach for {assigned_name or 'the user'} at a creative agency. "
        f"Today is {today}. Here is their current Notion task list:\n\n{task_summary}\n\n"
        "Based on this, answer their question helpfully and specifically. "
        "Be direct and action-oriented. Max 3-4 sentences. "
        "If they ask what to do next, suggest the most urgent/important task. "
        "If they're overwhelmed, offer practical relief (defer, focus on one thing). "
        "Never invent tasks not in the list."
    )

    try:
        reply = _claude_call(system, question, max_tokens=400)
        return jsonify({"reply": reply})
    except Exception as e:
        logger.exception("ai_coach failed")
        return jsonify({"error": str(e)}), 500


# ── Meeting notes -> real tasks ───────────────────────────────────────────────
# Rebuild of the meeting-notes-to-tasks feature removed in gotcha #46 (the old
# version was underbuilt and got ripped out). This one takes whatever raw
# markdown/plain-text a local transcription tool (meeting-scribe, TalkTrack --
# see CLAUDE.md's gotcha for scripts/meeting_sync.py, which is what actually
# calls this) already produced, and extracts real action items via one Haiku
# call rather than guessing at that tool's exact JSON export schema -- a
# summary's prose is far more reliably parseable by an LLM than hand-matching
# field names from a third-party tool's README, and this codebase has been
# burned by exactly that kind of guess before (see the "guessed a schema,
# it was wrong" family of gotchas).
@ops_bp.route("/api/ai/meeting-to-tasks", methods=["POST"])
def meeting_to_tasks():
    """
    Body: { notes: str (required -- markdown/plain text meeting summary or
            transcript), meeting_title: str, client: str, user_id: str }
    Extracts action items via Haiku, resolves each owner/client against the
    live roster/Notion clients (never a hardcoded name map -- see gotcha
    #63), creates a real board task + a standup_tasks row per item. Reports
    per-item success/failure honestly (see gotcha #93 -- a bulk-create loop
    must never claim blanket success over a batch that partially failed).
    """
    if not _is_admin(request.args.get("user_id") or
                     (request.get_json(silent=True) or {}).get("user_id")):
        return jsonify({"error": "unauthorized"}), 403

    body = request.get_json(silent=True) or {}
    notes = (body.get("notes") or "").strip()
    meeting_title = (body.get("meeting_title") or "").strip()
    default_client = (body.get("client") or "").strip()
    uploader_uid = (body.get("user_id") or "").strip()
    if not notes:
        return jsonify({"error": "notes is required"}), 400

    # Idempotency: the same notes arriving twice (a retried request, the same
    # meeting exported under two filenames) must not create every task twice.
    # `force: true` overrides it for a deliberate re-run.
    import hashlib
    notes_key = "meeting_notes_" + hashlib.sha256(
        re.sub(r"\s+", " ", notes).lower().encode("utf-8")).hexdigest()[:24]
    if not body.get("force"):
        try:
            _c = _su_conn()
            _row = _c.execute("SELECT value FROM app_settings WHERE key=?",
                              (notes_key,)).fetchone()
            _c.close()
            if _row:
                return jsonify({"success": True, "tasks_created": 0, "tasks_failed": 0,
                                "items": [], "duplicate": True,
                                "message": "These notes were already processed "
                                           f"({_row[0]}); nothing was created again. "
                                           "Send force:true to run them again."})
        except Exception:
            logger.debug("meeting_to_tasks: dedupe lookup failed", exc_info=True)
    truncated = len(notes) > 12000

    today = today_ist()
    system = (
        "You extract action items from a meeting transcript or summary for "
        "a creative agency. Return ONLY a JSON array (no prose, no markdown "
        "fences), each item exactly: "
        '{"title": "short imperative task title", '
        '"owner": "a person\'s name mentioned as responsible, or null", '
        '"due_date": "YYYY-MM-DD if a real date/deadline was mentioned, else null"}. '
        f"Today is {today}. Only include real, concrete action items -- "
        "skip general discussion. If nothing actionable was said, return []. "
        "Max 15 items."
    )
    try:
        raw = _claude_call(system, notes[:12000], max_tokens=1200)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        try:
            items = json.loads(raw)
        except ValueError:
            # The model sometimes wraps the array in a sentence. Take the
            # outermost [...] instead of failing the whole request.
            lo, hi = raw.find("["), raw.rfind("]")
            if lo == -1 or hi <= lo:
                raise
            items = json.loads(raw[lo:hi + 1])
        if not isinstance(items, list):
            items = []
    except Exception:
        logger.exception("meeting_to_tasks: extraction failed")
        return jsonify({"error": "couldn't extract action items from those notes"}), 500

    if not items:
        return jsonify({"success": True, "tasks_created": 0, "tasks_failed": 0,
                        "items": [], "message": "No action items found in those notes."})

    from whatsapp_agent import _find_client, _resolve_employee

    default_c = _find_client(default_client) if default_client else None
    created, failed = [], []
    for it in items[:15]:
        if not isinstance(it, dict):      # e.g. the model returned ["do X", ...]
            continue
        title = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()
        if not title:
            continue
        if meeting_title:
            title = f"{title} ({meeting_title})"
        title = title[:500]
        owner_name = str(it.get("owner") or "").strip()
        due = str(it.get("due_date") or "").strip()
        if due:
            # A real calendar date, not just a date-shaped string: Notion
            # rejects the WHOLE create on "2026-13-45".
            try:
                datetime.strptime(due, "%Y-%m-%d")
            except ValueError:
                due = ""
        emp = _resolve_employee(owner_name) if owner_name else None
        cname = (default_c or {}).get("name", "") or default_client
        cnid = (default_c or {}).get("notion_id", "")
        try:
            nid = ""
            if notion_store.is_configured():
                res = notion_store.create_task(
                    title=title, client_name=cname, client_notion_id=cnid,
                    assigned_to=(emp["name"] if emp else ""),
                    due_date=due, creation_date=today,
                )
                nid = (res or {}).get("notion_id", "")
            su_uid = emp["id"] if emp else (uploader_uid or "")
            if su_uid:
                conn = _su_conn()
                with conn:
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, notion_id, due_date, delegated_from) "
                        "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                        (su_uid, today, title[:500], (nid or None), (due or None),
                         "Meeting notes" if not emp else None),
                    )
                conn.close()
            created.append({"title": title, "owner": (emp or {}).get("name") or owner_name or None,
                            "due_date": due or None, "notion_id": nid or None})
        except Exception:
            logger.exception("meeting_to_tasks: item creation failed (%r)", title)
            failed.append(title)

    if created:
        # Remember these notes only once something was actually created, so a
        # run where everything failed can simply be retried.
        try:
            _c = _su_conn()
            with _c:
                _c.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                           (notes_key, today))
            _c.close()
        except Exception:
            logger.debug("meeting_to_tasks: dedupe record failed", exc_info=True)

    return jsonify({
        "success": True,
        "tasks_created": len(created),
        "tasks_failed": len(failed),
        "items": created,
        "failed_items": failed,
        "truncated": truncated,
    })


# ── Feature 3: Manager's End-of-Day Summary ───────────────────────────────────
@ops_bp.route("/api/ai/daily-summary", methods=["POST"])
def daily_summary():
    """
    Read today's standup tasks for all employees and produce a manager summary.
    Body: { user_id: str }
    """
    body = request.get_json(silent=True) or {}
    today = today_ist()

    conn = _su_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, title, status, blocker
        FROM standup_tasks
        WHERE date = ?
        ORDER BY user_id
    """, (today,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return jsonify({"summary": "No standup data found for today. Ask your team to log their tasks!"}), 200

    # Group by user_id
    by_user = {}
    for user_id, title, status, blocker in rows:
        if user_id not in by_user:
            by_user[user_id] = []
        by_user[user_id].append({"title": title, "status": status, "blocker": blocker})

    # Load employee name map
    try:
        import json as _json
        emp_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'employees.json')
        with open(emp_path) as f:
            data = _json.load(f)
            employees_list = data.get("employees", [])
        emp_map = {e.get("id", ""): e.get("name", e.get("id", "")) for e in employees_list}
    except Exception as e:
        logger.error(f"Failed to load employee names for daily report: {e}")
        emp_map = {}

    standup_text = ""
    for uid, tasks in by_user.items():
        name = emp_map.get(uid, uid)
        standup_text += f"\n {name}:\n"
        for t in tasks:
            status_icon = "" if t["status"] == "done" else "⏳"
            standup_text += f"  {status_icon} {t['title']}"
            if t.get("blocker"):
                standup_text += f" [BLOCKER: {t['blocker']}]"
            standup_text += "\n"

    system = f"""You are a senior project manager reviewing the team's daily standup for {today}.
Here is a summary of what everyone did today:
{standup_text}

Write a concise end-of-day manager report with exactly 3 sections:
1.  **Wins Today** – What was accomplished (2-3 bullet points max)
2. ⚠ **Watch List** – Who is behind or has blockers (name specific people)
3.  **Tomorrow's Priority** – The single most critical thing the team must focus on

Be direct, specific, and use names. Keep total response under 200 words."""

    try:
        summary = _claude_call(system, "Generate the daily report.", max_tokens=400)
        return jsonify({"summary": summary})
    except Exception as e:
        logger.exception("daily_summary failed")
        return jsonify({"error": str(e)}), 500


# ── Feature 5: AI Tone-Matched Client Update Draft ───────────────────────────
@ops_bp.route("/api/ai/client-update", methods=["POST"])
def client_update_draft():
    """
    Generate a professional client update message after a task is submitted.
    Body: { task_title: str, client_name: str, submission_note: str, channel: 'email'|'whatsapp' }
    """
    body = request.get_json(silent=True) or {}
    task_title = body.get("task_title", "").strip()
    client_name = body.get("client_name", "Client").strip()
    submission_note = body.get("submission_note", "").strip()
    channel = body.get("channel", "email").lower()

    if not task_title:
        return jsonify({"error": "task_title is required"}), 400

    if channel == "whatsapp":
        style = "casual, friendly WhatsApp message (2-3 sentences, use one relevant emoji, no formal greeting)"
    else:
        style = "professional email (subject line + 3-4 sentence body, polite and concise)"

    system = f"""You are writing on behalf of a creative agency team member.
They just completed a task and want to send a {channel} update to their client.

Task completed: {task_title}
Client: {client_name}
Work summary: {submission_note or "Work has been completed as discussed."}

Write a {style}.
Do NOT mention internal tools, Notion, or anything technical.
Sound warm, professional, and confident. Make the client feel well taken care of."""

    try:
        draft = _claude_call(system, "Write the client update now.", max_tokens=300)
        return jsonify({"draft": draft})
    except Exception as e:
        logger.exception("client_update_draft failed")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT PORTAL — task feed (called from client-dashboard.html)
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/client-portal/tasks", methods=["GET"])
def client_portal_tasks():
    """
    Return tasks for the logged-in client. Reads identity from the
    verified client_session_token cookie (via auth.client_verify logic).
    Query params:
      client_name      — name of the client (set by the session verify)
      client_notion_id — optional Notion DB id to filter by
    """
    # Re-verify the client session here so this endpoint is self-contained
    from routes.auth import _verify_client_session
    token = (
        request.cookies.get("client_session_token", "")
        or request.headers.get("X-Client-Token", "")
        or request.args.get("token", "")
    )
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    client_name = client.get("client_name", "")
    client_notion_id = client.get("client_notion_id", "")

    tasks = []
    source = "sqlite"

    # ── Try Notion first ──────────────────────────────────────────────────────
    try:
        if notion_store.is_configured():
            notion_tasks = notion_store.list_tasks(
                client_notion_id=client_notion_id if client_notion_id else None,
            )
            # If no notion_id, filter by client name
            if not client_notion_id:
                notion_tasks = [
                    t for t in notion_tasks
                    if (t.get("client_name") or "").lower() == client_name.lower()
                ]
            tasks = [_shape_client_task(t, "notion") for t in notion_tasks]
            source = "notion"
    except Exception as e:
        logger.warning(f"Notion client portal fetch failed: {e}")

    # ── SQLite fallback ───────────────────────────────────────────────────────
    if source == "sqlite" or not tasks:
        conn = _su_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT t.id, t.title, t.status, t.progress, t.due_date,
                   t.assigned_to, t.description, t.created_at, c.name AS client_name
            FROM tasks t
            JOIN clients c ON c.id = t.client_id
            WHERE LOWER(c.name) = LOWER(?)
            ORDER BY t.due_date ASC, t.created_at DESC
        """, (client_name,))
        rows = cur.fetchall()
        conn.close()
        tasks = [_shape_client_task({
            "id": r[0], "title": r[1], "status": r[2], "progress": r[3] or 0,
            "due_date": r[4], "assigned_to": r[5], "description": r[6],
            "created_at": r[7], "client_name": r[8],
        }, "sqlite") for r in rows]
        source = "sqlite"

    # Fetch feedback for these tasks
    try:
        conn = _pt_conn()
        cur = conn.cursor()
        task_ids = [str(t.get("id")) for t in tasks if t.get("id")]
        if task_ids:
            placeholders = ",".join("?" * len(task_ids))
            cur.execute(f"SELECT task_id, status, comments, audio_url, updated_at FROM client_task_feedback WHERE task_id IN ({placeholders})", task_ids)
            feedback_map = {r[0]: {"status": r[1], "comments": r[2], "audio_url": r[3], "updated_at": r[4]} for r in cur.fetchall()}
            for t in tasks:
                t_id = str(t.get("id"))
                if t_id in feedback_map:
                    t["feedback"] = feedback_map[t_id]
        conn.close()
    except Exception as e:
        logger.error(f"Error fetching task feedback: {e}")

    return jsonify({
        "tasks": tasks,
        "client_name": client_name,
        "source": source,
        "count": len(tasks),
    })


def _shape_client_task(t: dict, source: str) -> dict:
    """Normalise a task dict for the clientDashboard frontend."""
    status = (t.get("status") or "not_started").lower().replace(" ", "_").replace("-", "_")
    progress = int(t.get("progress") or 0)

    # Compute progress from status when not explicitly set
    if progress == 0:
        progress = {
            "done": 100, "approved": 100,
            "submitted": 80, "in_review": 80,
            "in_progress": 50,
            "not_started": 0, "blocked": 0,
        }.get(status, 0)

    # Parse dates
    start_raw = t.get("start_date") or t.get("created_at") or ""
    due_raw   = t.get("due_date") or ""

    def _parse_date(s):
        if not s:
            return None
        return s.split("T")[0] if "T" in s else s

    desc = t.get("description") or ""
    brief = t.get("brief") or ""
    idea = t.get("idea") or ""
    caption = t.get("caption") or ""
    file_link = t.get("file_link") or ""

    # Parsed pipe-separated values are now handled by notion_store.list_tasks

    return {
        "id":          t.get("notion_id") or t.get("id") or "",
        "title":       t.get("title") or "Untitled Task",
        "status":      status,
        "progress":    progress,
        "assigned_to": t.get("assigned_to") or "",
        "description": desc,
        "start_date":  _parse_date(start_raw),
        "due_date":    _parse_date(due_raw),
        "client_name": t.get("client_name") or "",
        "source":      source,
        "service":     t.get("service") or "",
        "type":        t.get("type") or "",
        "brief":       brief,
        "content":     t.get("content") or "",
        "idea":        idea,
        "scripts_copy": t.get("scripts_copy") or "",
        "caption":     caption,
        "file_link":   file_link,
    }


@ops_bp.route("/api/client-portal/tasks/<task_id>/feedback", methods=["POST"])
def submit_task_feedback(task_id):
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    status = data.get("status")
    comments = data.get("comments", "")
    audio_url = data.get("audio_url", None)
    source = data.get("source", "sqlite")
    
    # Store feedback locally
    try:
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO client_task_feedback (task_id, status, comments, audio_url, updated_at) 
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, comments=excluded.comments, audio_url=excluded.audio_url, updated_at=CURRENT_TIMESTAMP
        """, (str(task_id), status, comments, audio_url))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving task feedback: {e}")
        return jsonify({"error": "Failed to save feedback"}), 500

    _client_activity_bg(
        client, "left feedback on a task",
        f"{status or ''} {comments or ''}".strip(),
    )
    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT PORTAL DEPENDENCIES (Files, Notes, Links)
# ══════════════════════════════════════════════════════════════════════════════

import uuid
from werkzeug.utils import secure_filename

@ops_bp.route("/api/client-portal/dependencies/upload", methods=["POST"])
def upload_dependency():
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    dep_type = request.form.get("type", "docs")

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    original_name = secure_filename(file.filename) or "upload.file"
    ext = os.path.splitext(original_name)[1]
    
    # generate a unique filename
    unique_filename = f"{uuid.uuid4().hex}_{original_name}"
    
    from app import UPLOAD_DIR
    file_path = os.path.join(UPLOAD_DIR, unique_filename)
    file.save(file_path)

    content = f"/uploads/{unique_filename}"

    try:
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO client_dependencies (client_id, type, content, original_name) 
            VALUES (?, ?, ?, ?)
        """, (str(client["id"]), dep_type, content, original_name))
        conn.commit()
        dep_id = cur.lastrowid
        conn.close()
    except Exception as e:
        logger.error(f"Error saving dependency: {e}")
        return jsonify({"error": "Database error"}), 500

    _client_activity_bg(client, "uploaded a file to the portal", original_name)
    return jsonify({"success": True, "id": dep_id, "url": content, "original_name": original_name})


@ops_bp.route("/api/client-portal/dependencies/text", methods=["POST"])
def save_text_dependency():
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    dep_type = data.get("type")
    content = data.get("content")

    if not dep_type or not content:
        return jsonify({"error": "Missing required fields"}), 400

    try:
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO client_dependencies (client_id, type, content) 
            VALUES (?, ?, ?)
        """, (str(client["id"]), dep_type, content))
        conn.commit()
        dep_id = cur.lastrowid
        conn.close()
    except Exception as e:
        logger.error(f"Error saving dependency: {e}")
        return jsonify({"error": "Database error"}), 500

    _client_activity_bg(client, f"added a {dep_type or 'note'} in the portal", content)
    return jsonify({"success": True, "id": dep_id})


@ops_bp.route("/api/client-portal/dependencies", methods=["GET"])
def get_dependencies():
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = _pt_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, type, content, original_name, created_at FROM client_dependencies WHERE client_id = ? ORDER BY created_at ASC", (str(client["id"]),))
        rows = cur.fetchall()
        conn.close()
        
        deps = []
        for r in rows:
            deps.append({
                "id": r[0],
                "type": r[1],
                "content": r[2],
                "original_name": r[3],
                "created_at": r[4]
            })
        return jsonify({"dependencies": deps})
    except Exception as e:
        logger.error(f"Error fetching dependencies: {e}")
        return jsonify({"error": "Database error"}), 500


@ops_bp.route("/api/client-portal/dependencies/<int:dep_id>", methods=["DELETE"])
def delete_dependency(dep_id):
    from routes.auth import _verify_client_session
    token = request.cookies.get("client_session_token", "") or request.headers.get("X-Client-Token", "")
    client = _verify_client_session(token)
    if not client:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = _pt_conn()
        cur = conn.cursor()
        
        # Optionally verify it belongs to this client and get file path
        cur.execute("SELECT client_id, content FROM client_dependencies WHERE id = ?", (dep_id,))
        row = cur.fetchone()
        if not row or str(row[0]) != str(client["id"]):
            conn.close()
            return jsonify({"error": "Not found or unauthorized"}), 404
            
        content_url = row[1]
        
        cur.execute("DELETE FROM client_dependencies WHERE id = ?", (dep_id,))
        conn.commit()
        conn.close()
        
        # If it was an uploaded file, delete it from disk
        if content_url and content_url.startswith("/uploads/"):
            filename = content_url.replace("/uploads/", "")
            from app import UPLOAD_DIR
            file_path = os.path.join(UPLOAD_DIR, filename)
            if os.path.exists(file_path):
                os.remove(file_path)
                
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error deleting dependency: {e}")
        return jsonify({"error": "Database error"}), 500

@ops_bp.route("/api/bet", methods=["GET"])
def get_bet():
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key='bet_question'")
        row = cur.fetchone()
        question = row[0] if row else "Enter bet question here..."
        
        cur.execute("SELECT user_id, vote FROM mohit_bets")
        votes = [{"user_id": r[0], "vote": r[1]} for r in cur.fetchall()]
        conn.close()
        return jsonify({"success": True, "votes": votes, "question": question})
    except Exception as e:
        logger.error(f"Error getting bets: {e}")
        return jsonify({"success": False, "error": str(e)})

@ops_bp.route("/api/bet", methods=["POST"])
def post_bet():
    try:
        data = request.json
        user_id = data.get("user_id")
        vote = data.get("vote")
        if user_id not in ["emp002", "emp003", "emp006", "emp007", "emp008"]:
            return jsonify({"success": False, "error": "Not allowed"}), 403
        
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("INSERT OR REPLACE INTO mohit_bets (user_id, vote) VALUES (?, ?)", (user_id, vote))
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error posting bet: {e}")
        return jsonify({"success": False, "error": str(e)})

@ops_bp.route("/api/bet/question", methods=["POST"])
def post_bet_question():
    try:
        data = request.json
        user_id = data.get("user_id")
        question = data.get("question")
        if user_id not in ["emp002", "emp003", "emp006", "emp007", "emp008"]:
            return jsonify({"success": False, "error": "Not allowed"}), 403
        
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('bet_question', ?)", (question,))
            # Clear previous votes since question changed
            conn.execute("DELETE FROM mohit_bets")
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error posting bet question: {e}")
        return jsonify({"success": False, "error": str(e)})

# ══════════════════════════════════════════════════════════════════════════════
# DISCOVERY QUESTIONNAIRE
# ══════════════════════════════════════════════════════════════════════════════

@ops_bp.route("/api/form-templates", methods=["GET"])
def get_all_form_templates():
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.execute("SELECT id FROM form_templates ORDER BY id ASC")
        rows = cur.fetchall()
        conn.close()
        templates = [r[0] for r in rows]
        return jsonify({"success": True, "templates": templates})
    except Exception as e:
        logger.error(f"Error fetching form templates list: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/form-templates/<template_id>", methods=["GET"])
def get_form_template(template_id):
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.execute("SELECT schema_json FROM form_templates WHERE id=?", (template_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            return jsonify({"success": True, "template": json.loads(row[0])})
        return jsonify({"success": False, "error": "Template not found"}), 404
    except Exception as e:
        logger.error(f"Error fetching form template: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/form-templates/<template_id>", methods=["POST"])
def save_form_template(template_id):
    try:
        data = request.json
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("INSERT OR REPLACE INTO form_templates (id, schema_json) VALUES (?, ?)", (template_id, json.dumps(data.get("template", []))))
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving form template: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/discovery-submissions", methods=["GET"])
def get_discovery_submissions():
    try:
        from db import get_connection
        conn = get_connection()
        cur = conn.execute("SELECT id, form_id, company_name, email, answers_json, submitted_at FROM discovery_submissions ORDER BY submitted_at DESC")
        rows = cur.fetchall()
        conn.close()
        submissions = []
        for r in rows:
            submissions.append({
                "id": r[0],
                "form_id": r[1],
                "company_name": r[2],
                "email": r[3],
                "answers": json.loads(r[4]),
                "submitted_at": r[5]
            })
        return jsonify({"success": True, "submissions": submissions})
    except Exception as e:
        logger.error(f"Error fetching discovery submissions: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@ops_bp.route("/api/discovery-submissions", methods=["POST"])
def save_discovery_submission():
    try:
        data = request.json
        form_id = data.get("form_id", "discovery_global").strip()
        company_name = data.get("company_name", "").strip()
        email = data.get("email", "").strip()
        answers = data.get("answers", {})
        
        if not company_name:
            return jsonify({"success": False, "error": "Company Name is required"}), 400
            
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO discovery_submissions (form_id, company_name, email, answers_json) VALUES (?, ?, ?, ?)", 
                (form_id, company_name, email, json.dumps(answers))
            )
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving discovery submission: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
