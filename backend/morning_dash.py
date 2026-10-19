"""
morning_dash.py -- the personal morning dashboard, different for each role.

Replaces the one-size brief. Built in code from live data (no model), so every
number is exact:

  leader  (VG, KG, Abhinav): client health (red/amber/green), meetings, decisions
                             waiting, today's client deadlines, blocked clients
  DM      (anyone who owns a client): the same, but only for their own clients, plus
                             their own tasks
  member  (designers, editors, everyone else): their own tasks, nothing about the
                             business

Sent once per weekday 09:00-11:00 IST by the scheduler to every active employee
who has a WhatsApp number (skipping weekends, public holidays and approved leave).
Also available on demand: "my dashboard".
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
LEADERS = ("emp001", "emp004", "emp003")
HR_ID = "emp009"
OVERDUE_DAYS = 14


def _today(now: datetime | None = None):
    return (now or datetime.now(_IST)).date()


def _first(name: str) -> str:
    return re.split(r"[\s(]", (name or "").strip())[0]


def _role(emp: dict, dm_clients: list) -> str:
    if emp.get("id") in LEADERS:
        return "leader"
    return "dm" if dm_clients else "member"


# ---------- pieces -------------------------------------------------------
def _entries(day) -> list:
    try:
        import week_digest
        return week_digest.entries_by_day(day, day).get(day.isoformat(), [])
    except Exception:
        return []


def _meetings(day) -> list:
    import week_digest
    out = []
    for e in _entries(day):
        if e["kind"] == "event":
            t = week_digest._fmt_time(e["start_time"]) if e["start_time"] else ""
            out.append(f"{t} {e['title'] or 'Event'}".strip())
    wd = day.weekday()
    if wd == 0:
        out.append("4 PM Weekly Planning")
    elif wd in (2, 4):
        out.append("4 PM Team meeting")
    elif wd in (1, 3):
        out.append("10:30 AM Class meeting")
    return out


def _work_mode(day) -> str:
    kinds = {e["kind"]: e["title"] for e in _entries(day)}
    if "holiday" in kinds:
        return f"Public holiday: {kinds['holiday']}"
    bits = []
    if "wfh" in kinds:
        bits.append("Work from home")
    if "office" in kinds:
        bits.append("In-person office")
    return " / ".join(bits)


def _state(g: dict) -> str:
    import mgmt_views
    score, _ = mgmt_views._risk_reasons(g)
    if score >= 8 or (g["blockers"] and g["overdue"]):
        return "red"
    return "amber" if score > 0 else "green"


def _client_rows(emp: dict, role: str) -> list:
    """Client snapshots this person should see: all for a leader, their own for a DM."""
    import mgmt_views
    import ops_rhythm
    import org_profile
    rows = mgmt_views._all_clients()
    if role == "leader":
        return rows
    mine = {r["client"] for r in ops_rhythm._clients_by_person(
        mgmt_views._roster()).get(emp.get("id"), [])}
    out = []
    for g in rows:
        p = org_profile.find_project(g["name"])
        if p and p["client"] in mine:
            out.append(g)
    return out


def _due_today(rows: list, today) -> list:
    iso = today.isoformat()
    out = []
    for g in rows:
        for t in g["open"]:
            if str(t.get("due_date") or "")[:10] == iso:
                out.append(f"{g['name']}: {t.get('title') or 'Untitled'}")
    return out


def _decisions(emp: dict, rows: list) -> list:
    out = []
    for g in rows:
        if g["approvals"]:
            out.append(f"{g['name']}: {len(g['approvals'])} awaiting approval")
    try:
        import leave_store
        if emp.get("id") in leave_store.APPROVER_IDS:
            n = len(leave_store.list_pending(emp.get("id")))
            if n:
                out.append(f"Leave: {n} request{'' if n == 1 else 's'} to decide")
    except Exception:
        pass
    return out


_TASKS: dict = {"at": 0.0, "data": []}


def _all_tasks() -> list:
    """Every Notion task (including ones under no client), cached for a minute so a
    batch of dashboards reads Notion once."""
    import time
    import notion_store
    if _TASKS["data"] and time.time() - _TASKS["at"] < 60:
        return _TASKS["data"]
    try:
        data = notion_store.list_tasks() if notion_store.is_configured() else []
    except Exception:
        data = []
    if data:
        _TASKS["data"], _TASKS["at"] = data, time.time()
    return data


def _my_tasks(emp: dict, today) -> dict:
    """Notion tasks assigned to this person (any client, same rule as 'who is overloaded')
    plus their standup."""
    import client_card as cc
    import mgmt_views
    first = _first(emp.get("name", "")).lower()
    iso = today.isoformat()
    cut = (today - timedelta(days=OVERDUE_DAYS)).isoformat()
    wk_end = (today + timedelta(days=7)).isoformat()
    mine = []
    for t in _all_tasks():
        if cc._st(t) in cc._CLOSED or cc._st(t) in cc._APPROVAL:
            continue
        if first and first in mgmt_views._owners(t):
            mine.append((t.get("client_name") or "No client", t))
    over = [(c, t) for c, t in mine if cut <= str(t.get("due_date") or "")[:10] < iso]
    due = [(c, t) for c, t in mine if str(t.get("due_date") or "")[:10] == iso]
    soon = [(c, t) for c, t in mine if iso < str(t.get("due_date") or "")[:10] <= wk_end]
    done = pending = 0
    blockers = []
    urgent, risk, fine = [], [], []
    try:
        conn = get_connection()
        try:
            for st, bl, title, cf, dd in conn.execute(
                    "SELECT status, blocker, title, carried_from, due_date FROM standup_tasks "
                    "WHERE user_id=? AND date=? AND status NOT IN ('deleted','delegated')",
                    (emp.get("id"), iso)).fetchall():
                if st == "done":
                    done += 1
                    continue
                pending += 1
                if (bl or "").strip():
                    blockers.append(f"{title}: {bl.strip()}")
                late = 0   # days this task has been hanging: rolled over from an earlier day, or past its due date
                for d in (cf, dd):
                    try:
                        late = max(late, (today - datetime.strptime(str(d)[:10], "%Y-%m-%d").date()).days)
                    except Exception:
                        pass
                if late >= 2:
                    urgent.append((late, title))
                elif late == 1:
                    risk.append((late, title))
                else:
                    fine.append((late, title))
        finally:
            conn.close()
    except Exception:
        logger.debug("morning_dash: standup read failed", exc_info=True)
    return {"over": over, "due": due, "soon": soon, "done": done, "pending": pending, "blockers": blockers,
            "urgent": sorted(urgent, reverse=True), "risk": risk, "fine": fine}


def _list(items: list, n: int = 4) -> list:
    out = [f"  - {x}" for x in items[:n]]
    if len(items) > n:
        out.append(f"  - ...and {len(items) - n} more")
    return out


def _title(ct) -> str:
    c, t = ct
    return f"{c}: {t.get('title') or 'Untitled'}"


# ---------- the dashboards -----------------------------------------------
def build(emp: dict, now: datetime | None = None) -> str:
    today = _today(now)
    import mgmt_views
    import ops_rhythm
    dm_clients = ops_rhythm._clients_by_person(mgmt_views._roster()).get(emp.get("id"), [])
    role = _role(emp, dm_clients)
    rows = _client_rows(emp, role) if role in ("leader", "dm") else []
    name = _first(emp.get("name", "")) or "there"
    lines = [f"Good morning, {name}."]
    mode = _work_mode(today)
    if mode:
        lines.append(f"Today: {mode}")

    if role in ("leader", "dm"):
        st = [_state(g) for g in rows]
        lines += ["", "Today's priorities:" if role == "leader" else "Your clients today:",
                  f"🔴 {st.count('red')} urgent", f"🟡 {st.count('amber')} at risk",
                  f"🟢 {st.count('green')} on track"]
        if role == "leader":
            worst = [f"{g['name']}" for g, s in zip(rows, st) if s == "red"]
            if worst:
                lines.append("  Urgent: " + ", ".join(worst))

    meets = _meetings(today)
    lines += ["", "Your meetings"] + ([f"  {m}" for m in meets] if meets else ["  None today"])

    if role in ("leader", "dm"):
        dec = _decisions(emp, rows)
        lines += ["", "Your decisions", f"  {len(dec)} item{'' if len(dec) == 1 else 's'} waiting"
                  if dec else "  Nothing waiting"] + _list(dec, 8)
        dl = _due_today(rows, today)
        lines += ["", "Today's client deadlines", f"  {len(dl)}"] + _list(dl, 4)
        bl = [f"{g['name']}: {g['blockers'][0]}" for g in rows if g["blockers"]]
        lines += ["", "Blocked", f"  {len(bl)}"] + _list(bl, 3)

    # everyone who does hands-on work (members, DMs, and Abhinav) gets their own work
    if role in ("member", "dm") or emp.get("id") == "emp003":
        t = _my_tasks(emp, today)
        n = t["pending"]
        lines += ["", "Your standup today",
                  f"  {n} to do, {t['done']} done",
                  f"🔴 {len(t['urgent'])} urgent (hanging more than a day)",
                  f"🟡 {len(t['risk'])} at risk (rolled over from yesterday)",
                  f"🟢 {len(t['fine'])} on track"]
        if t["urgent"]:
            lines.append("  Urgent:")
            lines += _list([f"{ti} ({d} days)" for d, ti in t["urgent"]], 5)
        if t["risk"]:
            lines.append("  At risk:")
            lines += _list([ti for _, ti in t["risk"]], 4)
        if t["over"] or t["due"]:
            lines += ["", f"On the board: {len(t['due'])} due today, {len(t['over'])} overdue (last {OVERDUE_DAYS} days)"]
            lines += _list([_title(x) for x in t["due"]] + [_title(x) for x in t["over"]], 15)
        if role == "member" and t["blockers"]:
            lines += ["", "Blocked"] + _list(t["blockers"], 3)

    if emp.get("id") == HR_ID or role == "leader":
        try:
            import leave_store
            import utils
            ids = leave_store.on_leave_ids(today.isoformat())
            names = {e.get("id"): e.get("name") for e in utils._load_employees().get("employees", [])}
            away = [names.get(i, i) for i in sorted(ids)]
            if away:
                lines += ["", "On leave today", "  " + ", ".join(away)]
        except Exception:
            pass
    if emp.get("id") == HR_ID:
        try:
            import leave_store
            n = len(leave_store.list_pending(HR_ID))
            lines += ["", "Your decisions", f"  {n} leave request{'' if n == 1 else 's'} waiting" if n else "  Nothing waiting"]
        except Exception:
            pass
    return "\n".join(lines)


# ---------- sending ------------------------------------------------------
def _claim(uid: str, day: str) -> bool:
    conn = get_connection()
    try:
        with conn:
            return conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                                (f"morning_dash_{day}_{uid}", datetime.utcnow().isoformat())).rowcount > 0
    finally:
        conn.close()


def _release(uid: str, day: str) -> None:
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM app_settings WHERE key=?", (f"morning_dash_{day}_{uid}",))
    finally:
        conn.close()


def _remember(jid: str, text: str) -> None:
    """Put the dashboard into the person's chat memory so a reply like
    'what are the 7 more' is understood (the bot otherwise never saw it)."""
    try:
        import whatsapp_agent as wa
        key = str(jid).split("@")[0]
        hist = wa._load_context(key)
        if hist and hist[-1].get("role") != "assistant":
            hist = []
        wa._save_context(key, hist + [{"role": "user", "content": "morning dashboard"},
                                      {"role": "assistant", "content": text}])
    except Exception:
        logger.debug("morning_dash: remember failed", exc_info=True)


def run_daily(now: datetime | None = None, force: bool = False, only_ids: list | None = None) -> dict:
    """One dashboard per person per weekday, 09:00-11:00 IST. `force` skips the
    window and the once-a-day claim (testing; needs only_ids)."""
    now = now or datetime.now(_IST)
    day = now.date().isoformat()
    if not force:
        if now.weekday() >= 5 or not (9 <= now.hour < 11):
            return {"skipped": "outside window"}
        if any(e["kind"] == "holiday" for e in _entries(now.date())):
            return {"skipped": "public holiday"}
    elif not only_ids:
        return {"skipped": "force needs only_ids"}
    import wa_outbox
    import week_digest
    try:
        import leave_store
        away = set(leave_store.on_leave_ids(day))
    except Exception:
        away = set()
    sent = 0
    for emp in week_digest._people(only_ids=only_ids):
        uid = emp.get("id")
        if (uid in away and not force) or not str(emp.get("whatsapp") or "").strip():
            continue
        if not force and not _claim(uid, day):
            continue
        try:
            text = build(emp, now)
            jid = wa_outbox.wa_jid(emp["whatsapp"])
            if wa_outbox.enqueue(jid, text):
                sent += 1
                _remember(jid, text)
            elif not force:
                _release(uid, day)
        except Exception:
            logger.exception("morning_dash: failed for %s", uid)
            if not force:
                _release(uid, day)   # retry on the next 10-minute tick
    return {"sent": sent}
