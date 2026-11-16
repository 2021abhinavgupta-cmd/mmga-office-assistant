"""
Outbound email: leave-request mail to HR, event announcements/reminders.

Two transports, picked automatically (first one configured wins):

  1. Brevo HTTPS API  -- PREFERRED. Railway blocks outbound SMTP on some plans
     (diagnosed live 2026-10-08: ports 587/465 time out), but HTTPS works.
       BREVO_API_KEY   from Brevo > SMTP & API > API Keys
       BREVO_SENDER    verified sender address, default marketing@mmga.agency
       SMTP_FROM_NAME  optional display name, default "Lumina"
  2. SMTP -- only useful where outbound SMTP isn't blocked:
  SMTP_HOST   e.g. smtp.gmail.com            (Google Workspace)
  SMTP_PORT   587 (STARTTLS, default) or 465 (SSL)
  SMTP_USER   the mailbox that sends, e.g. lumina@mmga.agency
  SMTP_PASS   that mailbox's app password (Google: Account > Security > App passwords)
  SMTP_FROM_NAME  optional display name, default "Lumina"

Unconfigured -> every send is a logged no-op, never an error, so nothing
that calls this can fail because email isn't set up.

Employee addresses are <first name>@mmga.agency (e.g. abhinav@mmga.agency),
unless employees.json gives the person an explicit "email" field.
"""
from __future__ import annotations

import logging
import os
import smtplib
import threading
from email.message import EmailMessage
from email.utils import formataddr

logger = logging.getLogger(__name__)

EMAIL_DOMAIN = "mmga.agency"
last_error = ""   # most recent send failure (shown by /api/companion/email-status)
HR_USER_ID = "emp009"
# Always copied on leave-request mails to HR (Vidit, Kshitij).
LEAVE_CC_USER_IDS = ("emp001", "emp004")


def _brevo_key() -> str:
    return (os.getenv("BREVO_API_KEY") or "").strip()


def provider() -> str:
    """'brevo', 'smtp', or '' when email isn't configured."""
    if _brevo_key():
        return "brevo"
    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"):
        return "smtp"
    return ""


def is_configured() -> bool:
    return bool(provider())


def _employee(user_id: str) -> dict:
    try:
        from utils import _load_employees
        for e in _load_employees().get("employees", []):
            if e.get("id") == user_id:
                return e
    except Exception:
        logger.exception("mailer: could not read employees.json")
    return {}


def email_for(user_id: str) -> str:
    e = _employee(user_id)
    if e.get("email"):
        return str(e["email"]).strip()
    first = str(e.get("name") or "").strip().split(" ")[0].lower()
    first = "".join(ch for ch in first if ch.isalnum())
    return f"{first}@{EMAIL_DOMAIN}" if first else ""


def name_for(user_id: str) -> str:
    return str(_employee(user_id).get("name") or user_id)


def _send_brevo(to: list[str], subject: str, body: str, reply_to: str = "",
                from_name: str = "", cc: list[str] | None = None, html: str = "") -> bool:
    """One API call per recipient, so people never see each other's
    addresses and "reply all" can't spray the whole company."""
    import requests
    global last_error
    sender = (os.getenv("BREVO_SENDER") or os.getenv("SMTP_USER") or "marketing@mmga.agency").strip()
    name = from_name or os.getenv("SMTP_FROM_NAME", "Lumina")
    ok_all = True
    for addr in to:
        payload = {"sender": {"name": name, "email": sender},
                   "to": [{"email": addr}], "subject": subject, "textContent": body}
        if html:
            payload["htmlContent"] = html
        if reply_to:
            payload["replyTo"] = {"email": reply_to}
        if cc:
            payload["cc"] = [{"email": c} for c in cc if c and c != addr]
        try:
            r = requests.post("https://api.brevo.com/v3/smtp/email", json=payload,
                              headers={"api-key": _brevo_key(), "accept": "application/json"},
                              timeout=20)
            if r.status_code >= 300:
                ok_all = False
                last_error = f"Brevo HTTP {r.status_code}: {r.text}"[:400]
                logger.warning(f"mailer: brevo rejected '{subject}' to {addr}: {last_error}")
            else:
                logger.info(f"mailer: brevo sent '{subject}' to {addr}")
        except Exception as e:
            ok_all = False
            last_error = f"{type(e).__name__}: {e}"[:400]
            logger.warning(f"mailer: brevo send failed to {addr}: {e}")
    return ok_all


def _send_now(to: list[str], subject: str, body: str, reply_to: str = "",
              from_name: str = "", cc: list[str] | None = None, html: str = "") -> bool:
    if provider() == "brevo":
        return _send_brevo(to, subject, body, reply_to, from_name, cc, html)
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587") or 587)
    user = os.getenv("SMTP_USER", "")
    pwd = os.getenv("SMTP_PASS", "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name or os.getenv("SMTP_FROM_NAME", "Lumina"), user))
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = reply_to
    cc = [c for c in (cc or []) if c and c not in to]
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as srv:
                srv.login(user, pwd)
                srv.send_message(msg, to_addrs=to + cc)
        else:
            with smtplib.SMTP(host, port, timeout=15) as srv:
                srv.starttls()
                srv.login(user, pwd)
                srv.send_message(msg, to_addrs=to + cc)
        logger.info(f"mailer: sent '{subject}' to {to}")
        return True
    except Exception as e:
        global last_error
        last_error = f"{type(e).__name__}: {e}"[:400]
        logger.warning(f"mailer: send failed for '{subject}' to {to}: {e}")
        return False


def send(to, subject: str, body: str, reply_to: str = "", from_name: str = "",
         wait: bool = False, cc=None, html: str = "") -> bool:
    """Send an email. Returns False (and logs) when no transport is configured.
    By default sends on a background thread so a slow mail server never
    delays the HTTP request; wait=True sends inline and returns the result."""
    to = [t for t in ([to] if isinstance(to, str) else list(to or [])) if t]
    if not to:
        return False
    if not is_configured():
        logger.info(f"mailer: email not configured, skipping '{subject}' to {to}")
        return False
    cc = [c for c in ([cc] if isinstance(cc, str) else list(cc or [])) if c]
    if wait:
        return _send_now(to, subject, body, reply_to, from_name, cc, html)
    threading.Thread(target=_send_now, args=(to, subject, body, reply_to, from_name, cc, html),
                     daemon=True).start()
    return True


def notify_leave_decision(row: dict, decision: str, decided_by: str, reason: str = "") -> bool:
    """Email the applicant that HR approved/rejected their leave. Reply-To is
    the person who decided, so a reply reaches HR."""
    try:
        uid = row.get("user_id", "")
        to = email_for(uid)
        by = name_for(decided_by)
        sd, ed = row.get("start_date", ""), row.get("end_date", "")
        when = sd if sd == ed else f"{sd} to {ed}"
        verb = "approved" if decision == "approved" else "rejected"
        lines = [f"Hi {name_for(uid)},", "",
                 f"Your leave request for {when} has been {verb} by {by}."]
        if decision == "rejected" and (reason or "").strip():
            lines += ["", f"Reason: {reason.strip()}"]
        base = (os.getenv("PUBLIC_BASE_URL") or "https://lumina.mmga.agency").rstrip("/")
        lines += ["", f"See your calendar and balance: {base}/leave.html",
                  "", f"Reply to this email to reach {by}."]
        return send(to, f"Leave {verb}: {when}", "\n".join(lines),
                    reply_to=email_for(decided_by), from_name=f"{by} via Lumina")
    except Exception:
        logger.exception("mailer: notify_leave_decision failed")
        return False


def notify_leave_request(row: dict, subject: str = "", message: str = "") -> bool:
    """Email HR about a new pending leave request. Reply-To is the applicant,
    so HR can answer them straight from the mail. If the applicant wrote their
    own subject/message in the Apply box, that exact text is what HR receives
    (plus a one-line approve link); otherwise a standard summary is used."""
    try:
        import leave_store
        uid = row.get("user_id", "")
        who = name_for(uid)
        applicant = email_for(uid)
        sd, ed = row.get("start_date", ""), row.get("end_date", "")
        when = sd if sd == ed else f"{sd} to {ed}"
        kind = "Half day" if row.get("leave_type") == "half" else "Full day"
        reason = (row.get("reason") or "").strip() or "(none given)"
        base = (os.getenv("PUBLIC_BASE_URL") or "https://lumina.mmga.agency").rstrip("/")
        subj = (subject or "").strip() or f"Leave request: {who} ({when})"
        approvers = leave_store.approvers_for(uid)
        # ONE email: first approver in To, the rest visibly in Cc (so everyone can
        # see who else was told). Each approver has their own one-click links,
        # labelled by name; the first decision counts.
        recipients = [(aid, email_for(aid)) for aid in approvers]
        recipients = [(aid, em) for aid, em in recipients if em and em != applicant]
        if not recipients:
            return False
        names = ", ".join(name_for(a) for a, _ in recipients)
        note = ""
        if row.get("shortfall"):
            note = (f"\nNote: {row['shortfall']:g} of these days are beyond their leave "
                    "balance, so they would be unpaid.\n")
        link_lines = []
        for aid, _ in recipients:
            link_lines.append(f"{name_for(aid)}:\n"
                              f"  Approve: {leave_store.action_url(row['id'], aid, 'approve')}\n"
                              f"  Reject:  {leave_store.action_url(row['id'], aid, 'reject')}")
        links = ("\n" + "\n".join(link_lines) +
                 f"\n\n(or open {base}/leave.html). Sent to {names}; the first decision counts. "
                 "Please use your own links only.\n")
        if (message or "").strip():
            body = (f"{message.strip()}\n{note}\n-- {links}"
                    f"Reply to this email to reach {who} directly.")
        else:
            body = (f"{who} has applied for leave.\n\n"
                    f"Dates:  {when}\nType:   {kind}\nReason: {reason}\n{note}{links}\n"
                    f"Reply to this email to reach {who} directly.")
        return send(recipients[0][1], subj, body, reply_to=applicant,
                    from_name=f"{who} via Lumina", cc=[em for _, em in recipients[1:]])
    except Exception:
        logger.exception("mailer: notify_leave_request failed")
        return False
