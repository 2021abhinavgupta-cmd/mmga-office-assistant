"""
wa_outbox.py -- the one place proactive WhatsApp messages are queued.

The Flask app runs on Railway and can't reach the Baileys bridge (loopback
on the laptop), so anything the backend wants to push -- task-assigned
nudges, overdue escalation, ops alerts, client-activity pings -- is written
to the whatsapp_outbox table and the laptop companion
(scripts/laptop_agent.py :: job_wa_outbox) polls + delivers it.

Only `db` is imported at module load. notion_store / utils are imported
lazily inside the functions that need them, so importing this module stays
cheap for callers like task_scheduler and the route blueprints.
"""
from __future__ import annotations

import re
import logging
from datetime import datetime, timezone

from db import get_connection

logger = logging.getLogger(__name__)

_INACTIVE = {"inactive", "disabled", "left", "removed", "archived", "former"}


def client_welcome_text(name: str, username: str = "", password: str = "") -> str:
    """Welcome message for a new client. With a login, shows Username / Password on separate lines.
    password is only known at onboarding; elsewhere (hashed) pass "" and it says it was shared separately."""
    lines = [
        "Dear Valued Client,",
        "",
        "Welcome to MMGA, and thank you for choosing us.",
        f"We have set up *{name or 'your account'}* and our team is excited to get started with you.",
    ]
    if username:
        lines += [
            "",
            "*Your Client Portal Access*",
            "Portal: https://lumina.mmga.agency/client-login.html",
            f"Username: {username}",
            f"Password: {password}" if password else "Password: shared with you separately by your account manager",
        ]
        if password:
            lines += ["", "For your security, please change your password after your first login."]
    lines += [
        "",
        "You can message this number at any time to ask about your tasks and progress.",
        "",
        "Warm regards,",
        "Team MMGA",
    ]
    return "\n".join(lines)


def wa_jid(raw: str) -> str:
    """A WhatsApp DM JID from a stored number ('+91 97029 08716' -> ...)."""
    d = re.sub(r"\D", "", raw or "")
    return f"{d}@s.whatsapp.net" if d else ""


def enqueue(to_jid: str, text: str, send_after: str | None = None,
            image_token: str | None = None) -> bool:
    """Queue one proactive message (DM JID or '<id>@g.us' group JID).

    send_after: optional IST "YYYY-MM-DD HH:MM[:SS]" -- the message sits in
    the queue and companion_whatsapp_outbox() won't hand it to the laptop
    poller until that time has passed. None (the default) means deliver on
    the very next poll, same as before this parameter existed."""
    if not to_jid or not text:
        return False
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO whatsapp_outbox (to_number, body, created_at, send_after, image_token) "
                "VALUES (?, ?, ?, ?, ?)",
                (to_jid, str(text)[:1500], datetime.now(timezone.utc).isoformat(), send_after or None,
                 image_token or None),
            )
        conn.close()
        return True
    except Exception:
        logger.exception("wa_outbox: enqueue failed")
        return False


def enqueue_call(to_jid_or_number: str, message: str) -> bool:
    """Queue one one-way WhatsApp announcement call (see voice-call gotcha
    in CLAUDE.md). Separate table/queue from enqueue() above -- a call needs
    the laptop's bridge to run TTS + baileys-caller, not a plain /send POST,
    so it can't share whatsapp_outbox's delivery path. `to_jid_or_number`
    can be a full JID or a bare/loosely-formatted number; only the digits
    matter to the bridge, so store just those."""
    digits = re.sub(r"\D", "", to_jid_or_number or "")
    message = (message or "").strip()
    if not digits or not message:
        return False
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO wa_call_outbox (to_number, message, created_at) VALUES (?, ?, ?)",
                (digits, message[:800], datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
        return True
    except Exception:
        logger.exception("wa_outbox: enqueue_call failed")
        return False


def _employees() -> list:
    try:
        import utils
        return utils._load_employees().get("employees", [])
    except Exception:
        return []


def jid_for_name(name: str) -> str:
    """First-name / substring match against active employees -> DM JID, '' if none."""
    n = (name or "").strip().lower()
    if not n:
        return ""
    for e in _employees():
        if str(e.get("status", "active")).strip().lower() in _INACTIVE:
            continue
        en = (e.get("name") or "").strip().lower()
        if not en:
            continue
        if en == n or n in en or en.split()[0] == n:
            return wa_jid(e.get("whatsapp", ""))
    return ""


def alert_recipient_jids() -> list:
    """Employee ids in app_settings 'alert_recipient_ids' (default emp003) -> JIDs."""
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
        logger.debug("wa_outbox: alert_recipient_ids lookup failed", exc_info=True)
    by_id = {e.get("id"): e for e in _employees()}
    out = []
    for i in ids:
        e = by_id.get(i)
        if e:
            j = wa_jid(e.get("whatsapp", ""))
            if j and j not in out:
                out.append(j)
    return out


def notify_alert(text: str) -> int:
    """Queue an ops alert to every configured alert recipient. Returns count sent."""
    n = 0
    for j in alert_recipient_jids():
        if enqueue(j, text):
            n += 1
    return n


def notify_client_activity(client_name: str, client_notion_id: str,
                           kind: str, detail: str = "") -> None:
    """A client did something (uploaded a dependency, left task feedback).
    Ping the people assigned to that client's tasks + the alert recipients.
    Safe to call fire-and-forget from a request handler (swallows everything)."""
    try:
        msg = f"*{client_name or 'A client'}* {kind}"
        if detail:
            msg += f":\n{str(detail)[:300]}"
        jids: set = set()
        try:
            import notion_store
            if client_notion_id and notion_store.is_configured():
                names: set = set()
                for t in notion_store.list_tasks(client_notion_id=client_notion_id):
                    for a in re.split(r"[;,]", t.get("assigned_to") or ""):
                        a = a.strip()
                        if a:
                            names.add(a)
                for nm in names:
                    j = jid_for_name(nm)
                    if j:
                        jids.add(j)
        except Exception:
            logger.debug("wa_outbox: client-activity assignee lookup failed", exc_info=True)
        jids.update(alert_recipient_jids())
        for j in jids:
            enqueue(j, msg)
    except Exception:
        logger.exception("wa_outbox: notify_client_activity failed")
