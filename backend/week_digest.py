"""
Weekly office-calendar digest + same-day change alerts.

What HR (Noorish) puts on the office calendar -- holidays, work-from-home,
in-person office days, timed events -- is sent to everyone:

  * Every Sunday ~9 PM IST (run_weekly): the coming Mon-Sun schedule as an
    email (HTML, with the week picture and the month picture inline) and as a
    WhatsApp DM to each person (week picture + text, then the month picture).
  * Whenever HR adds / changes / removes a holiday, WFH or office day
    (notify_change): an immediate WhatsApp to everyone about that date only.
    Timed events keep their own announce() in office_events.py.

Pictures are drawn with Pillow, stored in wa_images and served by token at
/api/public/cal-image/<token>.png (the token is the secret) so the email can
<img> them and the laptop can fetch them for WhatsApp.

Everything is best-effort: nothing here raises into the caller.
"""
from __future__ import annotations

import calendar
import io
import logging
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from db import get_connection

logger = logging.getLogger(__name__)

# kind -> (emoji for text, label, hex colour)
KIND = {
    "holiday": ("🎉", "Public holiday", "#2e9e6b"),
    "wfh":     ("🏠", "Work from home", "#3b82f6"),
    "office":  ("🏢", "In-person office", "#9b5de5"),
    "event":   ("📌", "Event", "#e0911b"),
}
_ORDER = {"holiday": 0, "wfh": 1, "office": 2, "event": 3}
_INACTIVE = {"inactive", "disabled", "left", "removed", "archived", "former"}


# ── data ────────────────────────────────────────────────────────────────
def _base_url() -> str:
    return (os.getenv("PUBLIC_BASE_URL") or "https://lumina.mmga.agency").rstrip("/")


def _fmt_time(t: str | None) -> str:
    if not t:
        return ""
    try:
        return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return t


def entries_by_day(start: date, end: date) -> dict:
    """{'YYYY-MM-DD': [entry, ...]} for every day in [start, end]."""
    out = {(start + timedelta(days=i)).isoformat(): []
           for i in range((end - start).days + 1)}
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT kind, title, start_date, end_date, start_time, end_time FROM office_calendar "
            "WHERE start_date <= ? AND end_date >= ? ORDER BY start_date, start_time, id",
            (end.isoformat(), start.isoformat())).fetchall()
    finally:
        conn.close()
    for kind, title, sd, ed, st, et in rows:
        try:
            d0, d1 = date.fromisoformat(sd), date.fromisoformat(ed)
        except ValueError:
            continue
        d = max(d0, start)
        while d <= min(d1, end):
            out[d.isoformat()].append({"kind": kind, "title": (title or "").strip(),
                                       "start_time": st or "", "end_time": et or ""})
            d += timedelta(days=1)
    for v in out.values():
        v.sort(key=lambda e: (_ORDER.get(e["kind"], 9), e["start_time"]))
    return out


def _wa_safe(s: str) -> str:
    """Drop the characters WhatsApp reads as bold/italic/strike/code markers."""
    return re.sub(r"[*_~`]", "", s or "").strip()


def label(e: dict) -> str:
    """Short human label for one entry ('Work from home', '3:00 PM Townhall')."""
    k = e["kind"]
    title = _wa_safe(e["title"])
    if k == "event":
        return (f"{_fmt_time(e['start_time'])} " if e["start_time"] else "") + (title or "Event")
    return title or KIND.get(k, ("", k, ""))[1]


def _img_label(e: dict) -> str:
    """label() for the pictures: the bundled fonts can't draw Devanagari and
    other scripts (they'd show as boxes), so such titles fall back to the kind
    name there. The text message and email still carry the real title."""
    txt = label(e)
    if any(ord(c) > 0x24F and not 0x2000 <= ord(c) <= 0x206F for c in txt):
        t = _fmt_time(e["start_time"]) + " " if e["kind"] == "event" and e["start_time"] else ""
        return t + KIND.get(e["kind"], ("", e["kind"], ""))[1]
    return txt


def leave_by_day(start: date, end: date) -> dict:
    """{'YYYY-MM-DD': ['Nupur', 'Happy (half)']} for approved leave on weekdays
    of active employees. Names only -- never the reason."""
    out: dict = {}
    try:
        names = {e.get("id"): (e.get("name") or "").split()[0] for e in _people() if e.get("name")}
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT user_id, start_date, end_date, leave_type FROM employee_leave "
                "WHERE status='approved' AND start_date <= ? AND end_date >= ?",
                (end.isoformat(), start.isoformat())).fetchall()
        finally:
            conn.close()
        for uid, sd, ed, lt in rows:
            if uid not in names:
                continue
            d = max(date.fromisoformat(sd), start)
            while d <= min(date.fromisoformat(ed), end):
                if d.weekday() < 5:
                    out.setdefault(d.isoformat(), []).append(
                        names[uid] + (" (half)" if lt == "half" else ""))
                d += timedelta(days=1)
        for v in out.values():
            v.sort()
    except Exception:
        logger.exception("week_digest: leave lookup failed")
    return out


def _day_line(entries: list, weekend: bool = False) -> str:
    if not entries:
        return "Weekend" if weekend else "Normal working day"
    return "  ·  ".join(f"{KIND[e['kind']][0]} {label(e)}" for e in entries)


def _span(monday: date) -> str:
    sun = monday + timedelta(days=6)
    if monday.month == sun.month:
        return f"{monday.day} – {sun.day} {sun.strftime('%b')}"
    return f"{monday.day} {monday.strftime('%b')} – {sun.day} {sun.strftime('%b')}"


# ── text ────────────────────────────────────────────────────────────────
def week_text(monday: date, bold: str = "*") -> str:
    days = entries_by_day(monday, monday + timedelta(days=6))
    leave = leave_by_day(monday, monday + timedelta(days=6))
    lines = [f"📅 {bold}Week ahead: {_span(monday)}{bold}", ""]
    for i in range(7):
        d = monday + timedelta(days=i)
        ents = days[d.isoformat()]
        if i >= 5 and not ents:
            continue
        lines.append(f"{bold}{d.strftime('%a %d %b')}{bold}  {_day_line(ents, i >= 5)}")
        if leave.get(d.isoformat()):
            lines.append(f"      🌴 On leave: {', '.join(leave[d.isoformat()])}")
    return "\n".join(lines)


# ── pictures ────────────────────────────────────────────────────────────
def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    names = (["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"] if bold
             else ["DejaVuSans.ttf", "arial.ttf", "Arial.ttf"])
    for n in names:
        for base in ("", "/usr/share/fonts/truetype/dejavu/", "C:/Windows/Fonts/"):
            try:
                return ImageFont.truetype(base + n, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow: fixed-size bitmap font
        return ImageFont.load_default()


def _fit(draw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def _chip(draw, x, y, text, colour, font, max_w, fill=True, pad=8, h=26) -> int:
    """Draw a rounded pill; returns the x where the next chip may start."""
    text = _fit(draw, text, font, max_w - 2 * pad)
    w = int(draw.textlength(text, font=font)) + 2 * pad
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2,
                           fill=colour if fill else "#ffffff", outline=colour, width=2)
    draw.text((x + pad, y + h // 2), text, font=font,
              fill="#ffffff" if fill else colour, anchor="lm")
    return x + w + 8


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_week_png(monday: date) -> bytes:
    from PIL import Image, ImageDraw
    days = entries_by_day(monday, monday + timedelta(days=6))
    leave = leave_by_day(monday, monday + timedelta(days=6))
    W, head, row = 1100, 120, 96
    img = Image.new("RGB", (W, head + 7 * row + 30), "#ffffff")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, head), fill="#1f2937")
    d.text((36, 44), "Week ahead", font=_font(40, True), fill="#ffffff", anchor="lm")
    d.text((36, 90), _span(monday) + f"  {monday.year}", font=_font(26), fill="#cbd5e1", anchor="lm")
    x = W - 36
    for k in ("event", "office", "wfh", "holiday"):
        t = KIND[k][1]
        w = int(d.textlength(t, font=_font(18))) + 38
        x -= w
        d.ellipse((x, 56, x + 14, 70), fill=KIND[k][2])
        d.text((x + 22, 63), t, font=_font(18), fill="#e5e7eb", anchor="lm")
    f_day, f_num, f_chip, f_dim = _font(30, True), _font(22), _font(22, True), _font(22)
    for i in range(7):
        dd = monday + timedelta(days=i)
        y = head + i * row
        weekend = i >= 5
        d.rectangle((0, y, W, y + row), fill="#f8fafc" if weekend else "#ffffff")
        d.line((0, y + row, W, y + row), fill="#e5e7eb", width=2)
        d.text((36, y + 34), dd.strftime("%a"), font=f_day, fill="#111827", anchor="lm")
        d.text((36, y + 66), dd.strftime("%d %b"), font=f_num, fill="#6b7280", anchor="lm")
        ents = days[dd.isoformat()]
        lv = leave.get(dd.isoformat())
        top = y + (14 if lv else row // 2 - 17)
        if not ents:
            d.text((190, top + 17), "Weekend" if weekend else "Normal working day",
                   font=f_dim, fill="#9ca3af", anchor="lm")
        cx = 190
        for e in ents:
            if cx > W - 120:
                break
            cx = _chip(d, cx, top, _img_label(e), KIND[e["kind"]][2], f_chip,
                       W - 36 - cx, h=34, pad=14)
        if lv:
            d.text((190, y + row - 22), _fit(d, "On leave: " + ", ".join(lv), _font(20), W - 230),
                   font=_font(20), fill="#b45309", anchor="lm")
    return _png(img)


def render_month_png(monday: date) -> bytes:
    """The month containing `monday`, the Mon-Sun week of `monday` highlighted."""
    from PIL import Image, ImageDraw
    first = monday.replace(day=1)
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    start, end = weeks[0][0], weeks[-1][-1]
    days = entries_by_day(start, end)
    W, head, hdr, cw, ch = 1120, 100, 40, 156, 132
    gx = (W - 7 * cw) // 2
    img = Image.new("RGB", (W, head + hdr + len(weeks) * ch + 70), "#ffffff")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, head), fill="#1f2937")
    d.text((36, 50), first.strftime("%B %Y"), font=_font(42, True), fill="#ffffff", anchor="lm")
    d.text((W - 36, 50), f"highlighted: {_span(monday)}", font=_font(22), fill="#fcd34d", anchor="rm")
    for c, n in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        d.text((gx + c * cw + cw // 2, head + hdr // 2), n, font=_font(20, True),
               fill="#6b7280", anchor="mm")
    f_num, f_chip = _font(22, True), _font(16, True)
    for r, wk in enumerate(weeks):
        y0 = head + hdr + r * ch
        if wk[0] == monday:
            d.rectangle((gx - 8, y0, gx + 7 * cw + 8, y0 + ch), fill="#fff7d6", outline="#f59e0b", width=3)
        for c, dd in enumerate(wk):
            x0 = gx + c * cw
            d.rectangle((x0, y0, x0 + cw, y0 + ch), outline="#e5e7eb", width=1)
            in_month = dd.month == first.month
            d.text((x0 + 10, y0 + 18), str(dd.day), font=f_num,
                   fill="#111827" if in_month else "#cbd5e1", anchor="lm")
            ents = days[dd.isoformat()]
            for n, e in enumerate(ents[:3]):
                if n == 2 and len(ents) > 3:
                    d.text((x0 + 10, y0 + 38 + n * 28 + 12), f"+{len(ents) - 2} more",
                           font=f_chip, fill="#6b7280", anchor="lm")
                    break
                _chip(d, x0 + 6, y0 + 38 + n * 28, _img_label(e), KIND[e["kind"]][2], f_chip,
                      cw - 12, fill=in_month, pad=6, h=24)
    return _png(img)


def store_image(png: bytes) -> str:
    """Save a PNG, return its unguessable token. Old pictures are dropped."""
    token = secrets.token_urlsafe(18)
    conn = get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM wa_images WHERE created_at < ?",
                         ((datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),))
            conn.execute("INSERT INTO wa_images (token, png, created_at) VALUES (?, ?, ?)",
                         (token, png, datetime.now(timezone.utc).isoformat()))
    finally:
        conn.close()
    return token


def image_url(token: str) -> str:
    return f"{_base_url()}/api/public/cal-image/{token}.png"


# ── recipients ──────────────────────────────────────────────────────────
def _people(only_ids: list | None = None, exclude_id: str = "") -> list:
    try:
        import utils
        people = [e for e in utils._load_employees().get("employees", [])
                  if str(e.get("status", "active")).strip().lower() not in _INACTIVE]
    except Exception:
        logger.exception("week_digest: roster load failed")
        return []
    if only_ids:
        people = [e for e in people if e.get("id") in only_ids]
    if exclude_id:
        people = [e for e in people if e.get("id") != exclude_id]
    return people


# ── email ───────────────────────────────────────────────────────────────
def _email_html(monday: date, week_url: str, month_url: str) -> str:
    days = entries_by_day(monday, monday + timedelta(days=6))
    leave = leave_by_day(monday, monday + timedelta(days=6))
    rows = []
    for i in range(7):
        dd = monday + timedelta(days=i)
        ents = days[dd.isoformat()]
        if i >= 5 and not ents:
            continue
        if ents:
            pills = " ".join(
                f'<span style="display:inline-block;background:{KIND[e["kind"]][2]};color:#fff;'
                f'border-radius:12px;padding:3px 10px;font-size:13px;margin:2px 4px 2px 0">'
                f'{KIND[e["kind"]][0]} {_esc(label(e))}</span>' for e in ents)
        else:
            pills = f'<span style="color:#9ca3af">{"Weekend" if i >= 5 else "Normal working day"}</span>'
        if leave.get(dd.isoformat()):
            pills += ('<div style="color:#b45309;font-size:13px;margin-top:4px">🌴 On leave: '
                      + _esc(", ".join(leave[dd.isoformat()])) + '</div>')
        rows.append(
            f'<tr><td style="padding:10px 14px;border-bottom:1px solid #eef0f3;width:110px;'
            f'font-weight:600;color:#111827">{dd.strftime("%a %d %b")}</td>'
            f'<td style="padding:10px 14px;border-bottom:1px solid #eef0f3">{pills}</td></tr>')
    legend = " &nbsp; ".join(f'{v[0]} {v[1]}' for v in KIND.values())
    return f"""<!doctype html><html><body style="margin:0;background:#f3f4f6;font-family:Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;background:#fff;border-radius:14px;overflow:hidden">
<tr><td style="background:#1f2937;padding:24px 28px">
<div style="color:#fcd34d;font-size:13px;letter-spacing:1px">📅 WEEK AHEAD</div>
<div style="color:#fff;font-size:26px;font-weight:700;margin-top:4px">{_span(monday)} {monday.year}</div></td></tr>
<tr><td style="padding:22px 28px 6px;color:#374151;font-size:15px">Hi team 👋<br>Here's what's planned at the office this week.</td></tr>
<tr><td style="padding:8px 14px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{''.join(rows)}</table></td></tr>
<tr><td style="padding:6px 28px;color:#6b7280;font-size:12px">{legend}</td></tr>
<tr><td style="padding:14px 28px 4px"><img src="{week_url}" alt="This week" width="584" style="width:100%;max-width:584px;border-radius:10px;border:1px solid #e5e7eb"></td></tr>
<tr><td style="padding:14px 28px 4px;color:#111827;font-weight:600">🗓️ The whole month</td></tr>
<tr><td style="padding:6px 28px 18px"><img src="{month_url}" alt="This month" width="584" style="width:100%;max-width:584px;border-radius:10px;border:1px solid #e5e7eb"></td></tr>
<tr><td style="padding:0 28px 26px;color:#6b7280;font-size:13px">Plans can change - if HR updates the calendar mid-week you'll get a WhatsApp for that day.<br>
<a href="{_base_url()}/leave.html" style="color:#2563eb">Open the calendar in Lumina</a></td></tr>
</table></td></tr></table></body></html>"""


def _esc(s: str) -> str:
    import html
    return html.escape(s or "")


# ── weekly send ─────────────────────────────────────────────────────────
def _ist_now() -> datetime:
    try:
        from utils import now_ist, today_ist
        return datetime.strptime(f"{today_ist()} {now_ist()}", "%Y-%m-%d %H:%M:%S")
    except Exception:  # never lose a calendar alert over a clock helper
        from utils import IST
        return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def _drop_pending_in_week(monday: date) -> None:
    """The digest already shows the current calendar, so a held change that falls
    inside that week would only repeat it -- drop those."""
    import json
    lo, hi = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    conn = get_connection()
    try:
        with conn:
            for eid, bj, aj in conn.execute("SELECT entry_id, before_json, after_json FROM cal_change_pending").fetchall():
                for js in (bj, aj):
                    ev = json.loads(js) if js else None
                    if ev and ev["start_date"] <= hi and ev["end_date"] >= lo:
                        conn.execute("DELETE FROM cal_change_pending WHERE entry_id=?", (eid,))
                        break
    finally:
        conn.close()


def run_weekly(now: datetime | None = None, force: bool = False,
               only_ids: list | None = None, this_week: bool = False) -> dict:
    """Send the coming week's schedule. Scheduler calls it every 10 min; it only
    acts on Sunday 21:00-23:00 IST and only once per week. force=True skips the
    window/once-only guard (used for previews, optionally to only_ids)."""
    claim_key = ""
    try:
        now = now or _ist_now()
        if not force and not (now.weekday() == 6 and 21 <= now.hour < 23):
            return {"sent": False, "reason": "outside window"}
        monday = (now.date() - timedelta(days=now.weekday()) if this_week
                  else now.date() + timedelta(days=7 - now.weekday()))
        if not force:
            conn = get_connection()
            claim_key = f"week_digest_sent_{monday}"
            try:
                with conn:
                    claimed = conn.execute(
                        "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
                        (claim_key, now.isoformat())).rowcount
            finally:
                conn.close()
            if not claimed:
                claim_key = ""
                return {"sent": False, "reason": "already sent"}
        people = _people(only_ids)
        wk_tok = store_image(render_week_png(monday))
        mo_tok = store_image(render_month_png(monday))
        text = week_text(monday)
        emailed = whatsapped = 0
        try:
            import mailer
            to = [a for a in (mailer.email_for(e.get("id", "")) for e in people) if a]
            if to and mailer.send(to, f"📅 Week ahead: {_span(monday)} — office schedule",
                                  week_text(monday, bold="") + f"\n\nPictures: {image_url(wk_tok)}\n{image_url(mo_tok)}",
                                  html=_email_html(monday, image_url(wk_tok), image_url(mo_tok))):
                emailed = len(to)
        except Exception:
            logger.exception("week_digest: email failed")
        try:
            import wa_outbox
            cap2 = f"🗓️ {monday.strftime('%B %Y')} — {_span(monday)} highlighted"
            for e in people:
                jid = wa_outbox.wa_jid(e.get("whatsapp", ""))
                if not jid:
                    continue
                ok1 = wa_outbox.enqueue(jid, text, image_token=wk_tok)
                wa_outbox.enqueue(jid, cap2, image_token=mo_tok)
                whatsapped += 1 if ok1 else 0
        except Exception:
            logger.exception("week_digest: WhatsApp enqueue failed")
        logger.info(f"week_digest: {monday} -> {emailed} email(s), {whatsapped} WhatsApp(s)")
        _drop_pending_in_week(monday)
        return {"sent": True, "week": monday.isoformat(), "emailed": emailed, "whatsapped": whatsapped}
    except Exception:
        logger.exception("week_digest: run_weekly failed")
        if claim_key:  # release the once-a-week claim so the next 10-min tick retries
            try:
                c = get_connection()
                with c:
                    c.execute("DELETE FROM app_settings WHERE key=?", (claim_key,))
                c.close()
            except Exception:
                pass
        return {"sent": False, "reason": "error"}


# ── mid-week change alert (held 60 min, merged per entry, sent as one) ───
CHANGE_HOLD_MIN = 30        # quiet time after the LAST edit; every new edit restarts it
_SNAP = ("kind", "title", "start_date", "end_date")
_TS = "%Y-%m-%d %H:%M:%S"


def _snap(ev):
    return {k: (ev.get(k) or "") for k in _SNAP} if ev else None


def queue_change(entry_id: int, before: dict | None, after: dict | None,
                 editor_id: str = "") -> None:
    """Hold a holiday/WFH/office change. Further edits to the same entry are
    merged (original 'before' kept, latest 'after' wins) so a mistaken change
    that is corrected sends one message about the final state, or none if it
    ends up as it was. Every queued change slides the timer of ALL held changes
    (so a sitting of many edits goes out as one message)."""
    import json
    try:
        now = _ist_now()
        conn = get_connection()
        try:
            with conn:
                row = conn.execute("SELECT before_json, first_at FROM cal_change_pending WHERE entry_id=?",
                                   (entry_id,)).fetchone()
                b = json.loads(row[0]) if row and row[0] else (None if row else _snap(before))
                a = _snap(after)
                if (b is None and a is None) or (b is not None and a is not None and b == a):
                    conn.execute("DELETE FROM cal_change_pending WHERE entry_id=?", (entry_id,))
                    return
                first = (row[1] if row and row[1] else now.strftime(_TS))
                conn.execute(
                    "INSERT OR REPLACE INTO cal_change_pending "
                    "(entry_id, before_json, after_json, editor_id, due_at, first_at) VALUES (?,?,?,?,?,?)",
                    (entry_id, json.dumps(b) if b else None, json.dumps(a) if a else None,
                     editor_id, now.strftime(_TS), first))
                conn.execute("UPDATE cal_change_pending SET due_at=?",
                             ((now + timedelta(minutes=CHANGE_HOLD_MIN)).strftime(_TS),))
        finally:
            conn.close()
    except Exception:
        logger.exception("week_digest: queue_change failed")


def flush_changes(now: datetime | None = None) -> int:
    """Send the held changes whose time is up -- one message per person however
    many changes there are. A removal that exactly matches an addition (the
    entry was deleted and re-created) cancels out. Returns changes announced."""
    import json
    try:
        now = now or _ist_now()
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT entry_id, before_json, after_json, editor_id FROM cal_change_pending "
                "WHERE due_at <= ?", (now.strftime(_TS),)).fetchall()
            with conn:
                for r in rows:  # claim first so an overlapping sweep can't double-send
                    conn.execute("DELETE FROM cal_change_pending WHERE entry_id=?", (r[0],))
        finally:
            conn.close()
        items = [(json.loads(bj) if bj else None, json.loads(aj) if aj else None, ed or "")
                 for _id, bj, aj, ed in rows]
        # delete + re-create of the same thing = no change
        for rem in [i for i in items if i[1] is None]:
            for add in [i for i in items if i[0] is None]:
                if rem in items and add in items and rem[0] == add[1]:
                    items.remove(rem)
                    items.remove(add)
                    break
        if not items:
            return 0
        if len(items) == 1:
            b, a, ed = items[0]
            notify_change(b, a, "added" if b is None else "removed" if a is None else "updated", ed)
        else:
            notify_batch(items)
        return len(items)
    except Exception:
        logger.exception("week_digest: flush_changes failed")
        return 0


def notify_batch(items: list) -> int:
    """One WhatsApp per person listing several calendar changes. items: [(before, after, editor_id)]."""
    try:
        from utils import today_ist
        today = today_ist()
        lines = []
        editors = set()
        for b, a, ed in items:
            ev = a or b or {}
            if ev.get("kind") not in ("holiday", "wfh", "office") or (ev.get("end_date") or "") < today:
                continue
            verb = "added" if b is None else "removed" if a is None else "updated"
            lines.append(f"{_desc(ev)} {verb} — {_when(ev)}")
            if ed:
                editors.add(ed)
        if not lines:
            return 0
        shown = lines[:15]
        if len(lines) > 15:
            shown.append(f"…and {len(lines) - 15} more")
        who = ""
        if len(editors) == 1:
            try:
                import mailer
                who = mailer.name_for(next(iter(editors)))
            except Exception:
                pass
        text = "\n".join(["🔔 *Calendar updates*", ""] + shown
                         + ["", "Open the Calendar in Lumina for the full picture."]
                         + ([f"_Updated by {who}_"] if who else []))
        import wa_outbox
        n = 0
        for e in _people():  # editor included too (owner request 2026-10-21)
            jid = wa_outbox.wa_jid(e.get("whatsapp", ""))
            if jid and wa_outbox.enqueue(jid, text):
                n += 1
        logger.info(f"week_digest: batch of {len(lines)} change(s) -> {n} WhatsApp(s)")
        return n
    except Exception:
        logger.exception("week_digest: notify_batch failed")
        return 0

def _when(ev: dict) -> str:
    def f(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").strftime("%a, %d %b %Y")
        except ValueError:
            return s
    sd, ed = ev.get("start_date", ""), ev.get("end_date", "") or ev.get("start_date", "")
    return f(sd) if sd == ed else f"{f(sd)} to {f(ed)}"


def _desc(ev: dict) -> str:
    k = ev.get("kind", "")
    return f"{KIND.get(k, ('', k, ''))[0]} *{label(ev)}*"


def notify_change(before: dict | None, after: dict | None, action: str,
                  editor_id: str = "") -> int:
    """WhatsApp everyone (except the editor) right away about one holiday / WFH /
    office-day change, for that date only. action: added | updated | removed.
    Returns how many messages were queued."""
    try:
        from utils import today_ist
        ev = after or before or {}
        if ev.get("kind") not in ("holiday", "wfh", "office"):
            return 0
        if (ev.get("end_date") or ev.get("start_date") or "") < today_ist():
            return 0
        who = ""
        if editor_id:
            try:
                import mailer
                who = mailer.name_for(editor_id)
            except Exception:
                pass
        lines = ["🔔 *Calendar update*", ""]
        if action == "added":
            lines.append(f"{_desc(ev)} added")
        elif action == "removed":
            lines.append(f"{_desc(ev)} removed")
        else:
            lines.append(f"{_desc(ev)} updated")
        lines.append(f"📆 {_when(ev)}")
        if action == "updated" and before and (
                before.get("start_date") != ev.get("start_date") or before.get("end_date") != ev.get("end_date")
                or before.get("kind") != ev.get("kind") or label(before) != label(ev)):
            lines.append(f"Was: {_desc(before)} on {_when(before)}")
        # The full picture for a single day, so nobody has to guess what else is on.
        sd = ev.get("start_date", "")
        if sd and sd == (ev.get("end_date") or sd):
            try:
                ents = entries_by_day(date.fromisoformat(sd), date.fromisoformat(sd))[sd]
                wk = date.fromisoformat(sd).weekday() >= 5
                lines.append(f"That day now: {_day_line(ents, wk)}")
            except Exception:
                pass
        if who:
            lines += ["", f"_Updated by {who}_"]
        text = "\n".join(lines)
        import wa_outbox
        n = 0
        for e in _people():  # editor included too (owner request 2026-10-21)
            jid = wa_outbox.wa_jid(e.get("whatsapp", ""))
            if jid and wa_outbox.enqueue(jid, text):
                n += 1
        logger.info(f"week_digest: change '{action}' {ev.get('kind')} {ev.get('start_date')} -> {n} WhatsApp(s)")
        return n
    except Exception:
        logger.exception("week_digest: notify_change failed")
        return 0
