"""Who logged in, and when -- exact answers from daily_attendance for any day.

Used by the WhatsApp/Alexa assistant (`get_attendance` tool + a no-model fast
path). Everything is built in code from the stored check-in / check-out times,
so a time is never guessed. A day with no check-in is explained when we know
why (weekend, public holiday, approved leave, work-from-home day).
"""
import logging
import re
from datetime import date as _date, datetime, timedelta

from db import get_connection

logger = logging.getLogger(__name__)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
MAX_RANGE_DAYS = 31


def _d(s) -> _date | None:
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _label(d: _date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _hm(t) -> str:
    return str(t)[:5]


def _span(ci, co) -> str:
    try:
        a = datetime.strptime(str(ci)[:8], "%H:%M:%S")
        b = datetime.strptime(str(co)[:8], "%H:%M:%S")
        mins = int((b - a).total_seconds() // 60)
        return f"{mins // 60}h {mins % 60:02d}m" if mins > 0 else ""
    except Exception:
        return ""


def _rows(d_from: _date, d_to: _date) -> dict:
    """{(user_id, 'YYYY-MM-DD'): (checkin, checkout)} for real check-ins only
    (a heartbeat-only row has checkin_time NULL and is not a login)."""
    out = {}
    conn = get_connection()
    try:
        for uid, day, ci, co in conn.execute(
                "SELECT user_id, date, checkin_time, checkout_time FROM daily_attendance "
                "WHERE date >= ? AND date <= ? AND checkin_time IS NOT NULL AND checkin_time != ''",
                (d_from.isoformat(), d_to.isoformat())).fetchall():
            out[(uid, day)] = (ci, co)
    finally:
        conn.close()
    return out


def _calendar(d_from: _date, d_to: _date) -> list:
    try:
        conn = get_connection()
        try:
            return conn.execute(
                "SELECT kind, title, start_date, end_date FROM office_calendar "
                "WHERE kind IN ('holiday','wfh') AND end_date >= ? AND start_date <= ?",
                (d_from.isoformat(), d_to.isoformat())).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def _day_notes(cal: list, d: _date) -> tuple:
    """(holiday_name or '', is_wfh)"""
    iso, hol, wfh = d.isoformat(), "", False
    for kind, title, s, e in cal:
        if s <= iso <= e:
            if kind == "holiday":
                hol = title or "public holiday"
            elif kind == "wfh":
                wfh = True
    return hol, wfh


def _on_leave(uid: str, d: _date) -> bool:
    try:
        import leave_store
        return uid in set(leave_store.on_leave_ids(d.isoformat()))
    except Exception:
        return False


def _why_absent(uid: str, d: _date, cal: list, today: _date) -> str:
    if d > today:
        return "that day hasn't happened yet"
    if d.weekday() >= 5:
        return "weekend"
    hol, _ = _day_notes(cal, d)
    if hol:
        return f"public holiday ({hol})"
    if _on_leave(uid, d):
        return "on approved leave"
    return "no check-in recorded"


def _person_day(name: str, uid: str, d: _date, rec, cal: list, today: _date) -> str:
    _, wfh = _day_notes(cal, d)
    tag = " (work from home)" if wfh else ""
    if rec:
        ci, co = rec
        if co:
            sp = _span(ci, co)
            return f"{name} on {_label(d)}: in {_hm(ci)}, left {_hm(co)}" + (f" ({sp})" if sp else "") + tag + "."
        if d == today:
            return f"{name} on {_label(d)}: in {_hm(ci)}, still in (no check-out yet)" + tag + "."
        return f"{name} on {_label(d)}: in {_hm(ci)}, no check-out recorded" + tag + "."
    return f"{name} on {_label(d)}: not logged in ({_why_absent(uid, d, cal, today)})."


def report(today_str: str, roster: list, d_from: _date, d_to: _date, person: dict | None = None) -> str:
    """roster = [{id, name}] of active employees. person = one of them, or None
    for the whole team."""
    today = _d(today_str) or _date.today()
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        return f"(ask for at most {MAX_RANGE_DAYS} days at a time)"
    rows = _rows(d_from, d_to)
    cal = _calendar(d_from, d_to)
    days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]

    if person:
        lines, gone = [], []
        for d in days:
            rec = rows.get((person["id"], d.isoformat()))
            if len(days) > 1 and d.weekday() >= 5 and not rec:
                continue   # skip empty weekends in a range
            if len(days) > 7 and not rec:
                if d <= today:
                    gone.append(f"{d.day} {d.strftime('%b')}" +
                                ("" if _why_absent(person["id"], d, cal, today) == "no check-in recorded"
                                 else f" ({_why_absent(person['id'], d, cal, today)})"))
                continue   # long range: list the days present, then the absences in one line
            lines.append(_person_day(person["name"], person["id"], d, rec, cal, today))
        if gone:
            lines.append(f"No login on {len(gone)} weekday(s): " + ", ".join(gone) + ".")
        return "\n".join(lines) or f"No weekdays in that range for {person['name']}."

    if len(days) == 1:
        d = days[0]
        if d > today:
            return f"{_label(d)} hasn't happened yet."
        ins, outs, miss = [], [], []
        for e in roster:
            rec = rows.get((e["id"], d.isoformat()))
            if rec:
                ci, co = rec
                if co:
                    outs.append((str(ci), f"{e['name']} {_hm(ci)} (left {_hm(co)})"))
                else:
                    ins.append((str(ci), f"{e['name']} {_hm(ci)}" + (" (in now)" if d == today else "")))
            else:
                why = _why_absent(e["id"], d, cal, today)
                miss.append(e["name"] + ("" if why == "no check-in recorded" else f" ({why})"))
        got = sorted(ins + outs)
        head = f"*Logins, {_label(d)}*"
        hol, wfh = _day_notes(cal, d)
        if hol:
            head += f" (public holiday: {hol})"
        elif wfh:
            head += " (work from home day)"
        parts = [head]
        if got:
            first, last = got[0][1].split(" (")[0], got[-1][1].split(" (")[0]
            parts.append(f"First in: {first}. Last in: {last}.")
        parts.append(f"Logged in ({len(got)}): " + (", ".join(t for _, t in got) if got else "nobody"))
        parts.append(f"Not logged in ({len(miss)}): " + (", ".join(sorted(miss)) if miss else "nobody"))
        return "\n".join(parts)

    # team over several days: one line per person with days present / total weekdays
    work = [d for d in days if d.weekday() < 5 and d <= today and not _day_notes(cal, d)[0]]
    lines = [f"*Attendance, {_label(d_from)} to {_label(d_to)}* ({len(work)} working days)"]
    for e in roster:
        present = [d for d in work if (e["id"], d.isoformat()) in rows]
        absent = [d for d in work if (e["id"], d.isoformat()) not in rows and not _on_leave(e["id"], d)]
        line = f"{e['name']}: {len(present)}/{len(work)} days"
        if absent:
            line += " (missed " + ", ".join(_label(d) for d in absent[:6]) + (" ..." if len(absent) > 6 else "") + ")"
        lines.append(line)
    return "\n".join(lines)


# ---------- reading a date out of a phrase --------------------------------
def parse_when(phrase: str, today_str: str):
    """(from, to) dates from words like today / yesterday / on monday /
    last friday / 12 oct / 12/10 / this week. None when nothing recognisable
    is in the phrase."""
    p = (phrase or "").strip().lower()
    today = _d(today_str) or _date.today()
    if not p or re.search(r"\btoday\b", p):
        return today, today
    if re.search(r"\btomorrow\b", p):
        d = today + timedelta(days=1)
        return d, d
    if re.search(r"day before yesterday", p):
        d = today - timedelta(days=2)
        return d, d
    if re.search(r"\byesterday\b", p):
        d = today - timedelta(days=1)
        return d, d
    if re.search(r"\bthis month\b", p):
        return today.replace(day=1), today
    if re.search(r"\blast month\b", p):
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if re.search(r"\bthis week\b", p):
        return today - timedelta(days=today.weekday()), today
    if re.search(r"\blast week\b", p):
        mon = today - timedelta(days=today.weekday() + 7)
        return mon, mon + timedelta(days=6)
    m = re.search(r"\b(?:(last|this|on)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", p)
    if m:
        target = _WEEKDAYS.index(m.group(2))
        back = (today.weekday() - target) % 7     # 0 = it is that weekday today
        if m.group(1) == "last" and back == 0:
            back = 7
        d = today - timedelta(days=back)
        return d, d
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+(\d{4}))?\b", p)
    if m:
        try:
            d = _date(int(m.group(3) or today.year), _MONTHS[m.group(2)], int(m.group(1)))
            if d > today and not m.group(3):
                d = _date(d.year - 1, d.month, d.day)
            return d, d
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", p)
    if m:
        try:
            y = int(m.group(3)) if m.group(3) else today.year
            y += 2000 if y < 100 else 0
            d = _date(y, int(m.group(2)), int(m.group(1)))
            return d, d
        except ValueError:
            return None
    return None
