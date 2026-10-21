"""Group chat -> CRM actions.

group_intel.py reads the team group and finds things in it. This module acts on
the clear ones, right away, and tells the person it affects:

  * ASK  ("Charu, make the Omotec reel by Friday")  -> a task on that person's
    Daily Standup (delegated_from = who asked) + an immediate WhatsApp DM to them.
  * CLIENT FEEDBACK (a client's comment on work)    -> saved as a fact and the
    client's DM(s) are told at once.

Rails (owner decision 2026-10-21: create automatically, notify the owner):
  - only an explicit ask aimed at a real teammate becomes a task; a promise someone
    makes about their own work is tracked, not turned into a task;
  - nothing is created for an ask to oneself, and an identical task already on that
    person's list for the day is not duplicated;
  - at most MAX_TASKS_PER_PASS tasks per pass, every action is written to
    wa_action_log, and the person can undo it ("that wasn't a task" -> the
    reject_group_commitment tool also removes the standup row);
  - messages outside 09:00-21:00 IST are held until 09:30;
  - switch off with app_settings key group_auto_actions = off.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
MAX_TASKS_PER_PASS = 6


def enabled() -> bool:
    try:
        conn = get_connection()
        row = conn.execute("SELECT value FROM app_settings WHERE key='group_auto_actions'").fetchone()
        conn.close()
        return not (row and str(row[0]).strip().lower() in ("off", "0", "false", "no"))
    except Exception:
        return True


def _send_after() -> str | None:
    """None = send now; outside 09:00-21:00 IST hold until 09:30."""
    now = datetime.now(_IST)
    if 9 <= now.hour < 21:
        return None
    day = now if now.hour < 9 else now + timedelta(days=1)
    return day.strftime("%Y-%m-%d") + " 09:30"


def _audit(who: str, action: str, detail: str) -> None:
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO wa_action_log (sender, sender_name, scope, action, detail, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("group-intel", who, "group", action, detail[:300],
                 datetime.now(timezone.utc).isoformat()))
        conn.close()
    except Exception:
        logger.debug("group_actions: audit failed", exc_info=True)


def _emp(emp_id: str) -> dict:
    import week_digest
    for e in week_digest._people(only_ids=[emp_id]):
        return e
    return {}


def _dm(emp_id: str, text: str) -> bool:
    import wa_outbox
    e = _emp(emp_id)
    jid = wa_outbox.wa_jid(e.get("whatsapp", "")) if e else ""
    return bool(jid and wa_outbox.enqueue(jid, text, send_after=_send_after()))


def _client_dm_ids(client: str) -> list:
    """Employee ids of the DM / co-DM of this client (Vidit if none is set)."""
    try:
        import org_profile
        import ops_rhythm
        import group_intel
        proj = org_profile.find_project(client)
        if proj:
            roster = group_intel._roster()
            ids = [eid for eid, rows in ops_rhythm._clients_by_person(roster).items()
                   if any(r.get("client") == proj.get("client") for r in rows)]
            if ids:
                return ids
    except Exception:
        logger.debug("group_actions: dm lookup failed", exc_info=True)
    return ["emp001"]


def _handle_ask(a: dict) -> str:
    import group_intel
    owner_id, requester_id = a["owner_id"], a.get("requester_id") or ""
    if not owner_id or owner_id == requester_id:
        return "skipped"
    today = datetime.now(_IST).strftime("%Y-%m-%d")
    title = a["action"][:500]
    note = ""
    task_id = None
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT id, title FROM standup_tasks WHERE user_id=? AND date=? "
            "AND status NOT IN ('deleted','delegated')", (owner_id, today)).fetchall()
        dup = next((r for r in existing if group_intel._overlap(title, r[1] or "") >= 0.7), None)
        if dup:
            note = "It was already on your list, so I did not add it twice."
        else:
            with conn:
                cur = conn.execute(
                    "INSERT INTO standup_tasks (user_id, date, title, status, delegated_from, due_date) "
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (owner_id, today, title, a.get("requester_name") or "the group", a.get("due") or None))
                task_id = cur.lastrowid
                conn.execute("UPDATE group_commitments SET auto_task_id=? WHERE id=?",
                             (task_id, a["commit_id"]))
    finally:
        conn.close()
    who = a.get("requester_name") or "Someone"
    lines = ["ðŸ”” *New task from the group*", "", f"{who} asked you: {title}"]
    if a.get("due"):
        lines.append(f"Due: {a['due']}")
    if a.get("quote"):
        lines.append(f"_\"{a['quote'][:140]}\"_")
    lines.append("")
    lines.append(note or "Added to your Daily Standup.")
    lines.append(f"If this is not a real task, reply: that wasn't a task #{a['commit_id']}")
    _dm(owner_id, "\n".join(lines))
    _audit(who, "auto_task", f"{a.get('owner_name')}: {title} (commitment #{a['commit_id']}, standup #{task_id})")
    return "created" if task_id else "duplicate"


def _handle_feedback(f: dict) -> str:
    client = f.get("client") or ""
    if not client:
        return "skipped"
    ids = _client_dm_ids(client)
    lines = [f"ðŸ’¬ *Client feedback: {client}*", "", f["text"]]
    if f.get("quote"):
        lines.append(f"_\"{f['quote'][:160]}\"_")
    lines += ["", "Picked up from the team group chat."]
    sent = 0
    for eid in ids:
        if _dm(eid, "\n".join(lines)):
            sent += 1
    _audit("client", "feedback_notified", f"{client}: {f['text']} -> {','.join(ids)}")
    return f"notified {sent}"


def apply(actions: list) -> dict:
    """Run the actions collected by group_intel.extract_group (after its DB
    transaction has committed). Never raises."""
    out = {"tasks": 0, "feedback": 0, "skipped": 0}
    if not actions or not enabled():
        return out
    made = 0
    for a in actions:
        try:
            if a["type"] == "ask":
                if made >= MAX_TASKS_PER_PASS:
                    out["skipped"] += 1
                    continue
                if _handle_ask(a) == "created":
                    out["tasks"] += 1
                    made += 1
            elif a["type"] == "feedback":
                if _handle_feedback(a).startswith("notified"):
                    out["feedback"] += 1
        except Exception:
            logger.exception("group_actions: action failed: %s", a.get("type"))
    return out


def undo_for_commitment(conn, cid: int) -> bool:
    """Remove the standup row an ask created (called when the commitment is rejected)."""
    row = conn.execute("SELECT auto_task_id FROM group_commitments WHERE id=?", (cid,)).fetchone()
    if row and row[0]:
        conn.execute("UPDATE standup_tasks SET status='deleted' WHERE id=? AND status='pending'", (row[0],))
        return True
    return False
