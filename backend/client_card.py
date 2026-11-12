"""
client_card.py -- "what is happening with <client>?" as one fixed card.

    *MELLOW*
    SMM: Active
    This week: 3 posts (1 live)
    Next week: 2 posts planned
    Production: 1 pending
    Client feedback: 2 open
    Blocker: Product images
    DM: Charu
    Needs VG: No

Built from live data, never from the model: Notion tasks (Post Day = due date),
the client portal's feedback table, today's standup blockers and the DM from
org_profile (MMGA OS document). Needs VG = Yes when anything for the client is
waiting for approval OR is overdue (user decision, 2026-10-12).

Definitions (the document's 3-week cycle: publish this week, produce next
week, plan the week after):
  This week      social posts dated Mon-Sun of the current week; "live" = Posted/Final
  Next week      social posts dated next Mon-Sun ("planned")
  Production     next week's posts that are not ready yet (still Need to Start /
                 In Progress / Paused / blank); ready = Scheduled/Approved/Posted/
                 Final/Need for approval
  Overdue        unfinished and due within the last 14 days; anything older is ignored
                 entirely (not shown, does not set Needs VG)
  Client feedback open  portal feedback on the client's still-open tasks that
                 asks for changes or has a comment, not an approval
  SMM: Active    the client has social posts within 14 days back or 30 days ahead
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from db import get_connection

logger = logging.getLogger(__name__)

_SOCIAL_TITLE = re.compile(r"\[(story|static|reel|carousel|post|video)\]", re.I)
_APPROVAL = {"need_for_approval", "need_approval", "needs_approval", "pending_review",
             "in_review", "for_approval"}
_CLOSED = {"done", "approved", "posted", "final", "complete", "completed",
           "cancelled", "canceled"}
_LIVE = {"posted", "final"}
OVERDUE_DAYS = 14   # overdue = due within the last 14 days; older unfinished items are "old backlog"
_READY = _LIVE | {"scheduled", "approved", "complete", "completed", "done"} | _APPROVAL


def _st(t: dict) -> str:
    return re.sub(r"\s+", "_", str(t.get("status") or "").strip().lower())


def _due(t: dict) -> str:
    d = str(t.get("due_date") or "")[:10]
    return d if re.match(r"^\d{4}-\d{2}-\d{2}$", d) else ""


def is_social(t: dict) -> bool:
    return ("social" in str(t.get("service") or "").lower()
            or bool(_SOCIAL_TITLE.search(str(t.get("title") or ""))))


def _today() -> date:
    from utils import today_ist
    return date.fromisoformat(today_ist())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _client_row(name: str):
    import whatsapp_agent as wa
    return wa._find_client(name)


def _tasks(c: dict) -> list:
    import whatsapp_agent as wa
    return wa._tasks_for_client(c.get("name", ""), c.get("notion_id", ""))


def _feedback_open(tasks: list) -> int:
    ids = {str(t.get("notion_id") or t.get("id")): t for t in tasks if (t.get("notion_id") or t.get("id"))}
    if not ids:
        return 0
    try:
        conn = get_connection()
        try:
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT task_id, status, comments FROM client_task_feedback WHERE task_id IN ({marks})",
                list(ids)).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("client_card: feedback lookup failed")
        return 0
    n = 0
    for tid, status, comments in rows:
        t = ids.get(str(tid))
        if not t or _st(t) in _CLOSED:
            continue
        if (status or "") == "changes_requested" or ((status or "") != "approved" and (comments or "").strip()):
            n += 1
    return n


def _blockers(tasks: list, today: date, client_name: str = "") -> list:
    titles = {_norm(t.get("title")) for t in tasks if t.get("title")}
    cname = re.sub(r"[^a-z0-9]", "", (client_name or "").lower())
    if not titles and len(cname) < 4:
        return []
    since = (today - timedelta(days=3)).isoformat()
    out = []
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT title, blocker FROM standup_tasks WHERE date>=? AND blocker IS NOT NULL "
                "AND blocker<>'' AND status NOT IN ('done','deleted')", (since,)).fetchall()
        finally:
            conn.close()
        for tt, bb in rows:
            nt = _norm(tt)
            # a standup task that is not linked to Notion still counts when its title names the client
            names_client = len(cname) >= 4 and cname in re.sub(r"[^a-z0-9]", "", nt)
            if names_client or any(nt == c or (len(c) > 5 and (nt in c or c in nt)) for c in titles):
                b = str(bb).strip()
                if b and b not in out:
                    out.append(b)
    except Exception:
        logger.exception("client_card: blocker lookup failed")
    return out


def gather(client_name: str) -> dict | None:
    """All the numbers for one client (also used by ops_rhythm). None if unknown."""
    c = _client_row(client_name)
    return gather_row(c) if c else None


def gather_row(c: dict) -> dict | None:
    """Same as gather() for an already-resolved Notion client dict."""
    if not c:
        return None
    import org_profile
    today = _today()
    mon = today - timedelta(days=today.weekday())
    wk = (mon.isoformat(), (mon + timedelta(days=6)).isoformat())
    nx = ((mon + timedelta(days=7)).isoformat(), (mon + timedelta(days=13)).isoformat())
    p2 = ((mon + timedelta(days=14)).isoformat(), (mon + timedelta(days=20)).isoformat())
    tasks = _tasks(c)
    social = [t for t in tasks if is_social(t) and _due(t)]
    open_t = [t for t in tasks if _st(t) not in _CLOSED and _st(t) not in _APPROVAL]
    iso_today = today.isoformat()
    in_rng = lambda t, r: r[0] <= _due(t) <= r[1]
    this_w = [t for t in social if in_rng(t, wk)]
    next_w = [t for t in social if in_rng(t, nx)]
    plan_w = [t for t in social if in_rng(t, p2)]
    lo, hi = (today - timedelta(days=14)).isoformat(), (today + timedelta(days=30)).isoformat()
    proj = org_profile.find_project(c.get("name", ""))
    stale_cut = (today - timedelta(days=OVERDUE_DAYS)).isoformat()
    overdue = [t for t in open_t if _due(t) and stale_cut <= _due(t) < iso_today]
    backlog = [t for t in open_t if _due(t) and _due(t) < stale_cut]
    approvals = [t for t in tasks if _st(t) in _APPROVAL]
    due_next = sorted((t for t in open_t if _due(t) >= iso_today), key=_due)
    return {
        "name": c.get("name", ""), "proj": proj, "tasks": tasks, "open": open_t,
        "has_social": bool(social) or bool(proj and (
            "smm" in (proj["service_type"] or "").lower() or "360" in (proj["service_type"] or ""))),
        "smm_active": any(lo <= _due(t) <= hi for t in social),
        "this_week": this_w, "live": [t for t in this_w if _st(t) in _LIVE],
        "next_week": next_w, "plan_week": plan_w,
        "production_pending": [t for t in next_w if _st(t) not in _READY],
        "overdue": overdue, "backlog": backlog, "approvals": approvals, "due_next": due_next,
        "feedback_open": _feedback_open(tasks), "blockers": _blockers(tasks, today, c.get("name", "")),
    }


def _dm_text(proj) -> str:
    if not proj:
        return "not set"
    return proj["dm"] + (f" + {proj['co_dm']}" if proj["co_dm"] else "")


def _needs_vg(g: dict) -> str:
    bits = []
    if g["approvals"]:
        bits.append(f"{len(g['approvals'])} waiting for approval")
    if g["overdue"]:
        bits.append(f"{len(g['overdue'])} overdue")
    return "Yes (" + ", ".join(bits) + ")" if bits else "No"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _fmt_tasks(ts: list, limit: int = 8) -> list:
    out = []
    for t in sorted(ts, key=_due)[:limit]:
        d = _due(t)
        try:
            d = datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")
        except Exception:
            d = d or "no date"
        who = t.get("assigned_to") or "unassigned"
        out.append(f"- {t.get('title')} ({d}, {_st(t).replace('_', ' ') or 'no status'}, {who})")
    if len(ts) > limit:
        out.append(f"- ...and {len(ts) - limit} more")
    return out


def detail(client_name: str) -> str:
    """The card plus the actual tasks behind each number."""
    g = gather(client_name)
    base = card(client_name)
    if not g:
        return base
    sections = [
        ("Posts this week", g["this_week"]),
        ("Posts next week", g["next_week"]),
        ("Production pending", g["production_pending"]),
        ("Waiting for approval", g["approvals"]),
        ("Overdue (last 14 days)", g["overdue"]),
    ]
    lines = [base]
    for title, ts in sections:
        if ts:
            lines += ["", f"*{title}*"] + _fmt_tasks(ts)
    if g["backlog"]:
        lines += ["", f"Old backlog: {len(g['backlog'])} unfinished tasks older than 14 days"]
    if g["blockers"]:
        lines += ["", "*Blockers*"] + [f"- {b}" for b in g["blockers"][:5]]
    return "\n".join(lines)


def card(client_name: str) -> str:
    g = gather(client_name)
    if not g:
        import org_profile
        proj = org_profile.find_project(client_name)
        if proj:  # an MMGA client that has no page in the CRM (Notion) yet
            return (f"*{proj['client'].upper()}*  ({proj['service_type']})\n"
                    "Not in the CRM yet, so there are no tasks to report.\n"
                    f"DM: {_dm_text(proj)}")
        return "I can't find that client. Use get_clients for the list."
    proj = g["proj"]
    lines = [f"*{g['name'].upper()}*"]
    if g["has_social"]:
        lines.append("SMM: " + ("Active" if g["smm_active"] else "No posts scheduled"))
        tw = g["this_week"]
        lines.append(f"This week: {_plural(len(tw), 'post')}"
                     + (f" ({len(g['live'])} live)" if tw else ""))
        lines.append(f"Next week: {_plural(len(g['next_week']), 'post')} planned")
        pp = len(g["production_pending"])
        lines.append(f"Production: {pp} pending" if pp else "Production: nothing pending")
    else:
        kind = proj["service_type"] if proj else "Project"
        lines[0] += f"  ({kind})"
        lines.append(f"Open tasks: {len(g['open'])}"
                     + (f" ({len(g['overdue'])} overdue)" if g["overdue"] else ""))
        if g["due_next"]:
            t = g["due_next"][0]
            d = datetime.strptime(_due(t), "%Y-%m-%d").strftime("%a %d %b")
            lines.append(f"Next deadline: {d}, {t.get('title')}")
    lines.append(f"Client feedback: {g['feedback_open']} open")
    lines.append("Blocker: " + ("; ".join(g["blockers"][:2]) if g["blockers"] else "None"))
    lines.append(f"DM: {_dm_text(proj)}")
    lines.append(f"Needs VG: {_needs_vg(g)}")
    return "\n".join(lines)
