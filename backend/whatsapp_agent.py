"""
whatsapp_agent.py — Tool-using Claude agent for the WhatsApp channel.

An inbound WhatsApp message from a known sender is answered by Claude with
tool access to the agency's live CRM (Notion clients/tasks, with a SQLite
fallback) and the project knowledge base (FTS5). The flow is:

    identify sender  →  give it only the tools that sender is allowed to use
                     →  run a short tool-use loop  →  send back a short reply

Identity (never cached — read live every message, see CLAUDE.md gotcha #63):
  - employees.json `whatsapp` field  → full read access (every employee is
    admin-tier in this app, see CLAUDE.md gotcha #60)
  - client_users.whatsapp (optional) → scoped to that client's own tasks only
  - unknown number                   → polite "not recognized", no AI spend

This module must not import `app` (app imports it). It talks to
model_router / budget_tracker / notion_store / project_store / db / utils
directly, and owns its own Anthropic client.
"""

from __future__ import annotations

import os
import re
import json
import logging
from datetime import datetime, timedelta, timezone

import anthropic

from model_router import get_model_for_task, calculate_cost, MODEL_COSTS

MODEL_COSTS_SONNET = MODEL_COSTS["sonnet"]
from budget_tracker import check_budget_available as _real_check_budget, record_usage


def check_budget_available() -> dict:
    """The WhatsApp assistant ignores the monthly spend limit (owner's call, 2026-10-12:
    quality over cost). Set WHATSAPP_ENFORCE_BUDGET=1 to enforce it again."""
    if os.getenv("WHATSAPP_ENFORCE_BUDGET", "").strip().lower() in ("1", "true", "yes", "on"):
        return _real_check_budget()
    return {"allowed": True}
from db import get_connection
import notion_store
import kb_retriever
import semantic_kb
import smart_memory
import utils
import wa_outbox
import llm_openrouter

logger = logging.getLogger(__name__)
LAST_ERROR = {}   # last model-loop traceback, shown by /api/companion/ask-test for debugging

_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


_BEST_DEFAULT = "claude-sonnet-4-6"   # the earlier Claude model; WHATSAPP_BEST_MODELS can list more
_BEST_START = 0   # index of the first model in the chain that has worked so far


def _llm_create(model: dict, system, tools, messages: list, max_tokens: int,
                usage: dict, smart: bool = False, claude_first: bool = False):
    """One model call. Normally Qwen via OpenRouter when it is configured and on
    (llm_openrouter.enabled()), otherwise -- or if that call fails for any
    reason -- Claude. smart=True (a message that needs real thinking, see
    _needs_deep_thinking) tries Claude first and falls back to Qwen. Token
    counts are added to `usage` per provider so each is billed at its own rate.
    Returns an Anthropic-shaped response either way."""
    def _qwen():
        resp = llm_openrouter.create(system=system, tools=tools,
                                     messages=messages, max_tokens=max_tokens)
        u = usage.setdefault("qwen", [0, 0])
        u[0] += resp.usage.input_tokens
        u[1] += resp.usage.output_tokens
        return resp

    def _claude(name=None):
        kwargs = {"model": name or model["name"], "max_tokens": max_tokens, "system": system,
                  "messages": llm_openrouter.to_anthropic_messages(messages)}
        if tools:
            kwargs["tools"] = tools
        resp = _client.messages.create(**kwargs)
        u = usage.setdefault("claude", [0, 0])
        u[0] += resp.usage.input_tokens
        u[1] += resp.usage.output_tokens
        return resp

    if smart:
        # Best model first; if this account has no access to it (or it errors) try the next
        # one down, then Qwen. The first one that works is remembered for this process.
        global _BEST_START
        names = [n.strip() for n in os.getenv("WHATSAPP_BEST_MODELS", _BEST_DEFAULT).split(",") if n.strip()]
        for i in range(min(_BEST_START, len(names) - 1) if names else 0, len(names)):
            try:
                resp = _claude(names[i])
                model["name"], model["tier"] = names[i], "sonnet"   # bill/log what really answered
                _BEST_START = i
                return resp
            except Exception:
                logger.exception("whatsapp_agent: best-model call failed (%s)", names[i])
        if llm_openrouter.enabled():
            return _qwen()
        return _claude()
    if claude_first:   # voice: Alexa gives up at 8 s, so use the fastest reliable model
        try:
            return _claude()
        except Exception:
            logger.exception("whatsapp_agent: Claude call failed (voice)")
            if llm_openrouter.enabled():
                return _qwen()
            raise
    if llm_openrouter.enabled():
        try:
            return _qwen()
        except Exception:
            logger.exception("whatsapp_agent: OpenRouter call failed, falling back to Claude")
    return _claude()


def _record_llm_usage(model: dict, usage: dict, user_id: str) -> None:
    """record_usage once per provider actually used, at that provider's rate."""
    for prov, (tin, tout) in usage.items():
        if not (tin or tout):
            continue
        if prov == "qwen":
            tier, name = "qwen", llm_openrouter.model_name()
        else:
            tier, name = model["tier"], model["name"]
        try:
            record_usage(task_type="whatsapp", model_tier=tier, model_name=name,
                         input_tokens=tin, output_tokens=tout,
                         cost=calculate_cost(tier, tin, tout), user_id=user_id)
        except Exception:
            logger.debug("whatsapp_agent: usage record failed", exc_info=True)

# Rolling per-sender context. Older than this and we start a fresh thread.
# TTL was 6h, which meant a morning conversation was already forgotten by the
# afternoon -- the bot felt amnesiac across a single working day. Raising the
# TTL costs nothing per message (MAX_TURNS is what bounds how much history is
# actually resent); only the turn count is a real token lever, so that went up
# modestly. Durable facts about a person live in smart_memory instead of here.
_CONTEXT_TTL_HOURS = 24 * 7
_CONTEXT_MAX_TURNS = 10         # user/assistant pairs kept between messages
_MAX_TOOL_ROUNDS = 12          # hard cap on the tool-use loop (pause_turn can eat rounds) --
                                # was 10; each round is a full separate API call, so this is
                                # the biggest real cost lever on a multi-tool query

_IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


# ── Identity ─────────────────────────────────────────────────────────────────

def _normalize_phone(raw: str) -> str:
    """Digits-only, last 10 — tolerates '+', country code, 'whatsapp:' prefix
    on either side. Same scheme as whatsapp_standup.py so the two stay
    interchangeable if merged."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def identify_sender(sender: str) -> dict:
    """Returns one of:
      {"kind": "employee", "id", "name", "role"}
      {"kind": "client", "client_id", "client_name", "client_notion_id"}
      {"kind": "unknown"}
    """
    norm = _normalize_phone(sender)
    if not norm:
        return {"kind": "unknown"}

    # Employees (skip anyone who's left — a former employee's number
    # should get the same "not linked" bounce as a stranger, no bot access)
    _GONE = {"inactive", "disabled", "left", "removed", "archived", "former"}
    try:
        for emp in utils._load_employees().get("employees", []):
            wa = emp.get("whatsapp", "")
            if wa and _normalize_phone(wa) == norm:
                if str(emp.get("status", "active")).strip().lower() in _GONE:
                    return {"kind": "unknown"}
                return {
                    "kind": "employee",
                    "id": emp.get("id", ""),
                    "name": emp.get("name", "there"),
                    "role": emp.get("role", ""),
                }
    except Exception:
        logger.exception("whatsapp_agent: employee lookup failed")

    # Clients (client_users.whatsapp — optional column, may not be populated)
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, client_name, client_notion_id, whatsapp FROM client_users"
        )
        rows = cur.fetchall()
        conn.close()
        for r in rows:
            wa = r[3] if len(r) > 3 else ""
            if wa and _normalize_phone(wa) == norm:
                return {
                    "kind": "client",
                    "client_id": r[0],
                    "client_name": r[1] or "",
                    "client_notion_id": r[2] or "",
                }
    except Exception:
        # `whatsapp` column missing on an old DB, etc. — treat as no client match
        logger.debug("whatsapp_agent: client lookup skipped/failed", exc_info=True)

    return {"kind": "unknown"}


# ── Group allow-list ────────────────────────────────────────────────────────
#
# The bridge only forwards a group message when the bot was addressed (a
# "@lumina ..." prefix, an @-mention, or a reply to one of its messages).
# Even then we only act if the group is on this allow-list AND the person
# who asked is a known employee — because everyone in the group sees the
# reply, so a client or a stranger in the room must never be able to pull
# internal data out of it. Allow-list a group only when you know every
# member is staff.
#
# Sources, merged: env WHATSAPP_GROUP_ALLOWLIST (comma/space separated) and
# the app_settings key 'whatsapp_group_allowlist' (a JSON array, editable
# at runtime via POST /api/whatsapp/groups). Group ids are numeric, so we
# compare digits-only and tolerate a full '...@g.us' jid either way.

def _norm_group(x: str) -> str:
    return re.sub(r"\D", "", str(x or ""))


def _group_allowlist() -> set:
    out: set = set()
    for tok in re.split(r"[,\s]+", os.getenv("WHATSAPP_GROUP_ALLOWLIST", "")):
        n = _norm_group(tok)
        if n:
            out.add(n)
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key='whatsapp_group_allowlist'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            for tok in json.loads(row[0]):
                n = _norm_group(tok)
                if n:
                    out.add(n)
    except Exception:
        logger.debug("whatsapp_agent: group allowlist load failed", exc_info=True)
    return out


def _group_allowed(group_id: str) -> bool:
    return _norm_group(group_id) in _group_allowlist()


# ── Per-sender conversation context ──────────────────────────────────────────

def _one_time_context_reset() -> None:
    """2026-10-12: saved chat memories contained made-up group summaries the model kept
    copying. Wipe them once (this is only the bot's short-term chat memory)."""
    try:
        conn = get_connection()
        try:
            with conn:
                got = conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES "
                                   "('ctx_reset_20261012', '1')").rowcount
                if got:
                    conn.execute("DELETE FROM whatsapp_agent_context")
        finally:
            conn.close()
    except Exception:
        logger.debug("whatsapp_agent: context reset skipped", exc_info=True)


def _load_context(sender: str) -> list:
    _one_time_context_reset()
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT messages, updated_at FROM whatsapp_agent_context WHERE sender=?",
            (sender,),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return []
        updated = row[1]
        if updated:
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - ts > timedelta(hours=_CONTEXT_TTL_HOURS):
                    return []
            except ValueError:
                pass
        msgs = json.loads(row[0] or "[]")
        return msgs if isinstance(msgs, list) else []
    except Exception:
        logger.debug("whatsapp_agent: context load failed", exc_info=True)
        return []


def _save_context(sender: str, messages: list) -> None:
    # Keep only plain text user/assistant turns — never persist tool scaffolding.
    trimmed = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if isinstance(m.get("content"), str) and m.get("role") in ("user", "assistant")
    ][-(_CONTEXT_MAX_TURNS * 2):]
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                """INSERT INTO whatsapp_agent_context (sender, messages, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(sender) DO UPDATE SET messages=excluded.messages,
                                                    updated_at=excluded.updated_at""",
                (sender, json.dumps(trimmed), datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
    except Exception:
        logger.debug("whatsapp_agent: context save failed", exc_info=True)


# ── Data access (Notion first, SQLite fallback) ─────────────────────────────

def _sqlite_clients() -> list:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, status FROM clients ORDER BY created_at DESC")
        rows = cur.fetchall()
        conn.close()
        return [{"name": r[0], "status": r[1] or ""} for r in rows]
    except Exception:
        return []


def _sqlite_tasks_for_client(client_name: str) -> list:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """SELECT t.title, t.status, t.assigned_to, t.due_date
               FROM tasks t JOIN clients c ON t.client_id = c.id
               WHERE lower(c.name) = lower(?)
               ORDER BY t.due_date""",
            (client_name,),
        )
        rows = cur.fetchall()
        conn.close()
        return [
            {"title": r[0], "status": r[1] or "", "assigned_to": r[2] or "", "due_date": r[3] or ""}
            for r in rows
        ]
    except Exception:
        return []


def _all_clients() -> list:
    if notion_store.is_configured():
        try:
            return [
                {"name": c.get("name", ""), "status": c.get("status", "")}
                for c in notion_store.list_clients()
            ]
        except Exception:
            logger.exception("whatsapp_agent: notion list_clients failed")
    return _sqlite_clients()


def _find_client(name: str) -> dict | None:
    name_l = (name or "").strip().lower()
    if not name_l:
        return None
    if notion_store.is_configured():
        try:
            for c in notion_store.list_clients():
                if (c.get("name", "") or "").strip().lower() == name_l:
                    return c
            # loose contains-match fallback
            for c in notion_store.list_clients():
                if name_l in (c.get("name", "") or "").strip().lower():
                    return c
            sq = re.sub(r"[^a-z0-9]", "", name_l)
            if sq:  # "1Founder" vs "1 FOUNDER"
                for c in notion_store.list_clients():
                    cs = re.sub(r"[^a-z0-9]", "", (c.get("name", "") or "").lower())
                    if cs and (cs == sq or sq in cs):
                        return c
        except Exception:
            logger.exception("whatsapp_agent: notion client resolve failed")
    return None


def _tasks_for_client(client_name: str, client_notion_id: str = "") -> list:
    if notion_store.is_configured():
        try:
            if not client_notion_id:
                c = _find_client(client_name)
                client_notion_id = (c or {}).get("notion_id", "")
            if client_notion_id:
                return notion_store.list_tasks(client_notion_id=client_notion_id)
        except Exception:
            logger.exception("whatsapp_agent: notion list_tasks (client) failed")
    return _sqlite_tasks_for_client(client_name)


def _auto_checkin(user_id: str) -> None:
    """Putting a real task on someone's OWN standup is at least as strong a
    signal of "I'm here and working today" as clicking Check In -- so it
    also satisfies the attendance side of the daily-standup lock (gotcha
    #108/#119), without needing a separate Check In click. Idempotent
    (_attendance_checkin only ever fills a NULL checkin_time) and
    best-effort -- never called for a task landing on someone ELSE's list
    (assign_task, or create_task with an explicit assignee), since being
    assigned work by a teammate says nothing about whether the assignee is
    actually present today."""
    try:
        from routes.attendance import _attendance_checkin
        _attendance_checkin(user_id)
    except Exception:
        logger.exception("whatsapp_agent: auto-checkin failed")


def _standup_tasks_today(user_id: str) -> list:
    """The rows on this person's daily standup for today — the live task
    list they see in Lumina's Standup screen (NOT the whole Notion board)."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT title, status, blocker, carried_from FROM standup_tasks "
            "WHERE user_id=? AND date=? AND status NOT IN ('deleted','delegated') "
            "ORDER BY id",
            (user_id, _today_ist()),
        )
        rows = cur.fetchall()
        conn.close()
        return [
            {"title": r[0], "status": (r[1] or "pending"),
             "blocker": (r[2] or ""), "carried_from": (r[3] or "")}
            for r in rows
        ]
    except Exception:
        logger.exception("whatsapp_agent: standup task lookup failed")
        return []


def _first(name: str) -> str:
    return str(name or "").strip().split(" ")[0]


def _active_employees() -> list:
    """[{id, name, whatsapp}] for every active employee — the internal roster."""
    out = []
    try:
        for e in utils._load_employees().get("employees", []):
            if str(e.get("status", "active")).strip().lower() in (
                "inactive", "disabled", "left", "removed", "archived", "former"
            ):
                continue
            out.append({"id": e.get("id", ""),
                        "name": e.get("name") or e.get("id", ""),
                        "whatsapp": e.get("whatsapp", "") or ""})
    except Exception:
        logger.exception("whatsapp_agent: roster load failed")
    return out


def _wa_jid(raw: str) -> str:
    """A WhatsApp DM JID from a stored number ('+91 97029 08716' -> ...)."""
    return wa_outbox.wa_jid(raw)


def _team_group_jid() -> str:
    """The team WhatsApp group to post to for 'send this to the group'.
    Explicit app_settings key 'whatsapp_team_group' wins; otherwise the
    sole allow-listed group (the common case)."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key='whatsapp_team_group'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            d = _norm_group(row[0])
            if d:
                return f"{d}@g.us"
    except Exception:
        logger.debug("whatsapp_agent: team group lookup failed", exc_info=True)
    gl = sorted(_group_allowlist())
    return f"{gl[0]}@g.us" if gl else ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


# ── confirm-before-broadcast (send_group_message) ───────────────────────────

_PENDING_TTL_MIN = 10


def _set_pending_broadcast(emp_id: str, message: str, send_after: str | None = None) -> None:
    """send_after: optional IST 'YYYY-MM-DD HH:MM[:SS]' -- present for a
    schedule_group_message request, absent for an immediate send_group_message
    one. Stored as a small JSON payload so both share one pending-action slot
    per employee (a person can only have one broadcast awaiting yes/no at a
    time, immediate or scheduled)."""
    try:
        payload = json.dumps({"message": message, "send_after": send_after})
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO wa_pending_action (sender, kind, payload, created_at) "
                "VALUES (?, 'broadcast', ?, ?) "
                "ON CONFLICT(sender) DO UPDATE SET kind=excluded.kind, "
                "payload=excluded.payload, created_at=excluded.created_at",
                (emp_id, payload, datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
    except Exception:
        logger.exception("whatsapp_agent: set pending broadcast failed")


def _pop_pending_broadcast(emp_id: str) -> dict | None:
    """Return {"message": str, "send_after": str|None} for this employee's
    pending broadcast (and delete it). None if there isn't one or it's older
    than _PENDING_TTL_MIN."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT payload, created_at FROM wa_pending_action "
                    "WHERE sender=? AND kind='broadcast'", (emp_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        with conn:
            conn.execute("DELETE FROM wa_pending_action WHERE sender=?", (emp_id,))
        conn.close()
        try:
            ts = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - ts > timedelta(minutes=_PENDING_TTL_MIN):
                return None
        except ValueError:
            pass
        try:
            data = json.loads(row[0])
            if isinstance(data, dict) and "message" in data:
                return data
        except (ValueError, TypeError):
            pass
        return {"message": row[0], "send_after": None}  # legacy plain-string row
    except Exception:
        logger.exception("whatsapp_agent: pop pending broadcast failed")
        return None


# ── confirm-before-send (message_multiple) ──────────────────────────────────
# Shares the wa_pending_action table (one pending action per sender, any
# kind) but a distinct kind='multi' so it can't be picked up by
# _pop_pending_broadcast (kind='broadcast') or vice versa.

def _set_pending_multi(emp_id: str, message: str, recipients: list) -> None:
    """recipients: [{"id", "name", "whatsapp"}] -- already resolved, so the
    confirm step doesn't need to re-resolve names (and can't drift if the
    roster changes between asking and confirming)."""
    try:
        payload = json.dumps({"message": message, "recipients": recipients})
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO wa_pending_action (sender, kind, payload, created_at) "
                "VALUES (?, 'multi', ?, ?) "
                "ON CONFLICT(sender) DO UPDATE SET kind=excluded.kind, "
                "payload=excluded.payload, created_at=excluded.created_at",
                (emp_id, payload, datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
    except Exception:
        logger.exception("whatsapp_agent: set pending multi-message failed")


def _pop_pending_multi(emp_id: str) -> dict | None:
    """Return {"message": str, "recipients": [...]} for this employee's
    pending multi-send (and delete it). None if there isn't one or it's
    older than _PENDING_TTL_MIN."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT payload, created_at FROM wa_pending_action "
                    "WHERE sender=? AND kind='multi'", (emp_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        with conn:
            conn.execute("DELETE FROM wa_pending_action WHERE sender=?", (emp_id,))
        conn.close()
        try:
            ts = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - ts > timedelta(minutes=_PENDING_TTL_MIN):
                return None
        except ValueError:
            pass
        try:
            data = json.loads(row[0])
            if isinstance(data, dict) and "message" in data and "recipients" in data:
                return data
        except (ValueError, TypeError):
            pass
        return None
    except Exception:
        logger.exception("whatsapp_agent: pop pending multi-message failed")
        return None


# ── meeting link (asks "the morning one?" before giving it) ─────────────────

MORNING_MEET_LINK = "https://meet.google.com/rek-jyvv-jmm"   # same link the Tue/Thu 10:28 group post uses
_MEET_LINK_Q = re.compile(
    r"\b(?:(?:google\s+)?meet(?:ing)?\s+link|link\s+(?:of|for|to)\s+(?:the\s+|today'?s\s+)?(?:google\s+)?meet(?:ing)?|"
    r"meeting\s+(?:url|invite)|link\s+(?:of|for)\s+(?:the\s+)?(?:call|class))\b", re.I)


LUMINA_URL = "https://lumina.mmga.agency/dashboard.html"
_LUMINA_LINK_Q = re.compile(
    r"\b(?:lumina\s+(?:link|url|website|site|portal|login)|"
    r"(?:link|url|website|site|portal|login)\s+(?:of|for|to)\s+(?:the\s+)?lumina|"
    r"(?:where|how)\s+(?:do\s+i|can\s+i|to)\s+(?:open|login|log\s+in\s+to|access)\s+lumina)\b", re.I)


def _set_pending_meetlink(emp_id: str) -> None:
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO wa_pending_action (sender, kind, payload, created_at) "
                "VALUES (?, 'meetlink', '{}', ?) "
                "ON CONFLICT(sender) DO UPDATE SET kind=excluded.kind, "
                "payload=excluded.payload, created_at=excluded.created_at",
                (emp_id, datetime.now(timezone.utc).isoformat()))
        conn.close()
    except Exception:
        logger.exception("whatsapp_agent: set pending meetlink failed")


def _pop_pending_meetlink(emp_id: str) -> bool:
    """True if this person was just asked 'the morning one?' (within the TTL); clears it."""
    try:
        conn = get_connection()
        row = conn.execute("SELECT created_at FROM wa_pending_action WHERE sender=? AND kind='meetlink'",
                           (emp_id,)).fetchone()
        if not row:
            conn.close()
            return False
        with conn:
            conn.execute("DELETE FROM wa_pending_action WHERE sender=? AND kind='meetlink'", (emp_id,))
        conn.close()
        ts = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ts <= timedelta(minutes=_PENDING_TTL_MIN)
    except Exception:
        logger.exception("whatsapp_agent: pop pending meetlink failed")
        return False


# ── audit log ──────────────────────────────────────────────────────────────

# Messages that must be answered from a tool call (a read or a write), not memory.
_TASK_REQUEST = re.compile(
    r"\b(tasks?|standup|stand-up|todo|to-do|add|mark|done|complete[d]?|finish(ed)?|"
    r"assign|delegate|remind|delete|remove|blocker|due|overdue|pending|list|"
    r"show|update|reopen|status)\b", re.I)

# "remind me ..." / "post this in the group at 5pm": only done once a scheduling tool ran;
# a model that just says "Done" must be sent back (history imitation made Qwen/Claude do this).
_SCHEDULE_Q = re.compile(
    r"(\bremind(?:er)?s?\s+(?:me|[a-z]+)\b|\btell\s+[a-z]+\s+(?:at|in|tomorrow|tonight)\b|\bping me\b|\bnudge me\b|\bset (?:a |an )?(?:reminder|alarm)\b|"
    r"\bdon'?t let me forget\b|\bmake sure i remember\b|\bremember to tell me\b|"
    r"^\s*(?:(?:please|pls|plz|hey|lumina|can you|could you|kindly)\s+)*"
    r"(?:post|send|tell|announce|schedule|drop|share|message|text|put|publish)\b.{0,70}"
    r"\b(?:group|everyone|everybody|the team|whole team|all of us)\b)", re.I)
_SCHEDULE_TOOLS = ("remind_me", "schedule_group_message", "send_group_message",
                   "remind_teammate", "message_multiple")


_YES_WORDS = {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "send it", "go ahead",
              "do it", "yes please", "haan", "ha", "kar do"}


def history_for_yes(identity, sender):
    return _load_context(sender)


def _non_latin_ratio(s: str) -> float:
    """Share of letters in Hangul / Cyrillic / CJK / Arabic / Thai (model language drift)."""
    letters = [c for c in (s or "") if c.isalpha()]
    if not letters:
        return 0.0
    bad = sum(1 for c in letters if ("가" <= c <= "힯" or "Ѐ" <= c <= "ӿ"
                                      or "一" <= c <= "鿿" or "؀" <= c <= "ۿ"
                                      or "฀" <= c <= "๿" or "぀" <= c <= "ヿ"))
    return bad / len(letters)


# Login / check-in / attendance questions: the times must come from get_attendance,
# never from memory (a model will otherwise invent "9:05 AM").
_ATTENDANCE_Q = re.compile(
    r"\b(log(?:ged|s)?[\s-]*in|logins?|log(?:ged)?[\s-]*out|check(?:ed|s)?[\s-]*(?:in|out)|"
    r"came in|come in|comes in|attendance|punch(?:ed)?|arriv(?:e|ed|es)\s+(?:at|in|to)|"
    r"what time did \w+ (?:start|leave|reach))\b", re.I)

# Messages that need real thinking (advice, explanation, drafting, planning,
# comparison...) rather than a lookup or a command. These go to the stronger
# model (Claude Sonnet) while the daily cap allows; everything else stays on the
# cheap model. A short imperative that starts with a command verb ("add",
# "assign", "approve"...) is never "deep" unless it is also phrased as a question.
_DEEP = re.compile(
    r"\b(why|explain|compare|comparison|difference between|strateg\w*|advice|advise|suggest\w*|"
    r"recommend\w*|should (?:i|we)|what do you think|your (?:opinion|take|view)|pros and cons|"
    r"brainstorm|ideas? for|draft|write|rewrite|proofread|caption|script|pitch|proposal|"
    r"negotiat\w*|pricing|analy[sz]e|summari[sz]e|calculate|how (?:do|can|would|should|to) |"
    r"help me (?:decide|think|plan|write|figure|understand)|best way|trade-?off|improve|"
    r"feedback on|review (?:this|my)|translate|what would happen|what if|is it (?:worth|a good)|"
    r"which (?:is|one|should)|tell me about|how does|how do|what should (?:i|we)|priorit\w*|"
    r"focus on|thoughts on|any (?:advice|tips|thoughts|ideas)|tips for|kya karu|kya karein|"
    r"samjha\w*)\b", re.I)
_CMD_START = re.compile(
    r"^(?:please\s+|pls\s+|lumina[,:\s]+)?(?:add|mark|assign|delegate|remind|create|move|delay|"
    r"approve|reject|set|clear|show|list|get|send|schedule|tell|ping|call|text|put|push|"
    r"delete|remove|cancel|check|open|close)\b", re.I)


_GROUP_Q = re.compile(
    r"\b(?:discuss\w*|talked about|said in the group|in the group|group chat|"
    r"what (?:was|did|were) .{0,40}(?:said|decided|mentioned)|summari[sz]e the group)\b", re.I)


def _needs_deep_thinking(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _GROUP_Q.search(t):   # reading a long chat log faithfully needs the stronger model
        return True
    if len(t) >= 160:
        return True
    if not _DEEP.search(t):
        return False
    if "?" not in t and (_CMD_START.match(t) or re.search(
            r"\b(?:my|the|today's|tomorrow's)\s+(?:daily\s+)?(?:stand-?up|task list|to-?do)\b", t, re.I)):
        return False
    return True


def _smart_setting() -> str:
    v = os.getenv("WHATSAPP_SMART", "").strip().lower()
    if v:
        return v
    try:
        conn = get_connection()
        row = conn.execute("SELECT value FROM app_settings WHERE key='whatsapp_smart_llm'").fetchone()
        conn.close()
        return str(row[0]).strip().lower() if row and row[0] else ""
    except Exception:
        return ""


def _smart_allowed() -> bool:
    """True when a thinking-heavy message may use Claude Sonnet: not switched off
    (WHATSAPP_SMART=off or app_settings whatsapp_smart_llm=off), a Claude key
    exists, and today's cap (WHATSAPP_SMART_DAILY_CAP, default 40 messages) is not
    used up. Counts the message when it returns True."""
    if _smart_setting() in ("off", "0", "false", "no", "qwen"):
        return False
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        return False
    try:
        cap = int(os.getenv("WHATSAPP_SMART_DAILY_CAP", "0") or 0)   # 0 = no cap
    except ValueError:
        cap = 0
    key = f"wa_smart_{_today_ist()}"
    try:
        conn = get_connection()
        try:
            with conn:
                row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
                n = int(row[0]) if row and str(row[0]).isdigit() else 0
                if cap and n >= cap:
                    return False
                conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                             (key, str(n + 1)))
        finally:
            conn.close()
        return True
    except Exception:
        logger.debug("whatsapp_agent: smart counter failed", exc_info=True)
        return False


_SHOW_TASKS = re.compile(
    r"^(?:please\s+|pls\s+)?(?:(?:show|list|give|get|send)(?:\s+me)?\s+(?:all\s+)?(?:my\s+)?"
    r"|what(?:'s|\s+is|\s+are)\s+(?:all\s+)?(?:(?:on|in)\s+)?(?:my\s+)?|my\s+)"
    r"(?:daily\s+)?(?:stand-?up\s+tasks?|tasks?|stand-?up|to-?do(?:s|\s+list)?)(?:\s+(?:now|today|list|again))?[\s.!?]*$",
    re.I)
_MARK_DONE = re.compile(
    r"^(?:please\s+|pls\s+)?(?:(?:mark|tick|set)\s+(.+?)\s+(?:as\s+)?(?:done|complete|completed|finished)"
    r"|(?:i\s+)?(?:have\s+|just\s+)?(?:finished|completed|done with)\s+(.+?)"
    r"|(.+?)\s+is\s+(?:done|completed|finished))[\s.!]*$", re.I)


_CLIENT_Q = re.compile(
    r"^(?:lumina[,:\s]+)?(?:what(?:'s|s|\s+is)\s+(?:happening|going\s+on|up)\s+(?:with|on|in)|"
    r"(?:status|update)\s+(?:on|of|for)|how(?:'s|s|\s+is)|where\s+are\s+we\s+(?:with|on))\s+"
    r"(?:the\s+)?([A-Za-z0-9][\w .&'-]{1,40}?)(?:\s+(?:doing|going|now|today))?[\s?.!]*$", re.I)


_MGMT_FAST = [
    (re.compile(r"^(?:lumina[,:\s]+)?(?:what(?:'s|\s+is)\s+(?:at\s+risk|in\s+risk)|any\s+risks?|what\s+are\s+the\s+risks?)[\s?.!]*$", re.I), "at_risk"),
    (re.compile(r"^(?:lumina[,:\s]+)?(?:what\s+needs\s+(?:vg|vidit)|what\s+does\s+(?:vg|vidit)\s+need|needs\s+vg)[\s?.!]*$", re.I), "needs_vg"),
    (re.compile(r"^(?:lumina[,:\s]+)?(?:show\s+me\s+|list\s+)?(?:all\s+)?(?:the\s+)?blocked\s+projects?[\s?.!]*$|^(?:lumina[,:\s]+)?which\s+projects?\s+(?:are|is)\s+blocked[\s?.!]*$", re.I), "blocked"),
    (re.compile(r"^(?:lumina[,:\s]+)?who(?:'s|\s+is)\s+overloaded[\s?.!]*$|^(?:lumina[,:\s]+)?who\s+has\s+too\s+much(?:\s+work)?[\s?.!]*$", re.I), "overloaded"),
    (re.compile(r"^(?:lumina[,:\s]+)?(?:who|what)(?:'s|\s+is)\s+waiting\s+on\s+me[\s?.!]*$", re.I), "waiting_on_me"),
    (re.compile(r"^(?:lumina[,:\s]+)?(?:what\s+needs\s+my\s+approval|what\s+do\s+i\s+(?:need\s+to\s+)?approve|anything\s+(?:for\s+me\s+)?to\s+approve)[\s?.!]*$", re.I), "my_approvals"),
    (re.compile(r"^(?:lumina[,:\s]+)?what\s+did\s+we\s+(?:complete|finish|get\s+done|do)\s+this\s+week[\s?.!]*$", re.I), "completed_week"),
]
_DUE_WEEK = re.compile(r"^(?:lumina[,:\s]+)?what(?:'s|\s+is)\s+due\s+this\s+week[\s?.!]*$", re.I)
_DASH_Q = re.compile(
    r"^(?:lumina[,:\s]+)?(?:show\s+|give\s+)?(?:me\s+)?(?:my\s+)?(?:dashboard|morning brief|day brief|"
    r"what(?:'s|\s+is)\s+my\s+day|what\s+do\s+i\s+have\s+today)[\s?.!]*$", re.I)
_PENDING_Q = re.compile(r"^(?:lumina[,:\s]+)?what(?:'s|\s+is)\s+pending[\s?.!]*$", re.I)


def _digest_llm_smart(system: str, user: str) -> str:
    """Summary writer: the stronger model when the daily smart cap allows,
    else the cheap one. Output is verified by group_digest either way."""
    if _smart_allowed():
        from model_router import MODEL_COSTS
        return _digest_llm(system, user, {**MODEL_COSTS["sonnet"], "tier": "sonnet", "task": "whatsapp"}, True)
    return _digest_llm(system, user)


def _digest_llm(system: str, user: str, model=None, smart: bool = False) -> str:
    """One cheap, deterministic model call used only to GROUP numbered chat
    messages into topics (group_digest). Returns raw text."""
    budget = check_budget_available()
    if not budget.get("allowed"):
        return ""
    model = model or get_model_for_task("whatsapp")
    usage: dict = {}
    resp = _llm_create(model, [{"type": "text", "text": system}], None,
                       [{"role": "user", "content": user}], 1200 if smart else 900, usage, smart=smart)
    try:
        _record_llm_usage(model, usage, "wa_group_digest")
    except Exception:
        pass
    return "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", "") == "text")


_NAME = r"([A-Za-z][A-Za-z.]*(?:\s[A-Za-z]+)?)"
_LOGIN_VERB = (r"(?:log(?:ged|s)?[\s-]*in|check(?:ed|s)?[\s-]*in|come(?:s)?(?:\s+in)?|came(?:\s+in)?|"
               r"arrive[ds]?|reach(?:ed)?|start(?:ed)?|log(?:ged)?[\s-]*out|check(?:ed)?[\s-]*out|left|leave)")
_ATT_PERSON = [
    re.compile(r"^(?:lumina[,:\s]+)?(?:at\s+)?(?:what\s+time|when)\s+(?:did|has|was|does)\s+" + _NAME +
               r"\s+" + _LOGIN_VERB + r"(?:\s+(.+?))?[\s?.!]*$", re.I),
    re.compile(r"^(?:lumina[,:\s]+)?(?:did|has|was)\s+" + _NAME + r"\s+" + _LOGIN_VERB +
               r"(?:\s+(.+?))?[\s?.!]*$", re.I),
    re.compile(r"^(?:lumina[,:\s]+)?is\s+" + _NAME + r"\s+(?:in|working|present|here|logged\s+in)(?:\s+(.+?))?[\s?.!]*$", re.I),
    re.compile(r"^(?:lumina[,:\s]+)?" + _NAME + r"(?:'s|s)\s+(?:login|log-in|check-?in|attendance)(?:\s+time)?(?:\s+(.+?))?[\s?.!]*$", re.I),
]
_ATT_TEAM = re.compile(
    r"^(?:lumina[,:\s]+)?(?:who(?:'s|\s+is|\s+has|\s+have|\s+all|\s+hasn'?t|\s+haven'?t)?\s*(?:not\s+)?"
    r"(?:logged|checked|log|check)[\s-]*in|who(?:'s|\s+is)\s+(?:in|absent|missing|present|late)|"
    r"(?:show\s+(?:me\s+)?|give\s+(?:me\s+)?)?(?:the\s+)?attendance|who\s+came\s+(?:in|to\s+office)|"
    r"who\s+(?:all\s+)?(?:is|are)\s+(?:in|working)|"
    r"what\s+time\s+did\s+(?:the\s+team|everyone|everybody|all\s+of\s+us|we|they)\s+"
    r"(?:log(?:ged)?[\s-]*in|check(?:ed)?[\s-]*in|come|came|arrive[d]?)|"
    r"who\s+(?:was|is)\s+(?:the\s+)?(?:first|last|earliest|latest)\s+(?:one\s+)?to\s+"
    r"(?:log(?:\s*in)?|check(?:\s*in)?|come(?:\s+in)?|arrive))\b(.*)$", re.I)
_ATT_FILLER = re.compile(r"\b(yet|so far|already|this morning|in the morning|at all|please|pls|ever|office|"
                         r"to office|in office|the office)\b", re.I)


def _fast_attendance(text: str):
    """('get_attendance', args) for 'what time did Nupur log in on Monday',
    'who hasn't logged in', 'is X in today'... or None (model handles it)."""
    import attendance_report as ar
    today = _today_ist()

    def when(rest):
        rest = _ATT_FILLER.sub(" ", rest or "").strip(" ,.?!")
        rest = re.sub(r"^(?:on|for|of|at|in|during)\s+", "", rest, flags=re.I).strip()
        return ar.parse_when(rest, today)

    for rx in _ATT_PERSON:
        m = rx.match(text)
        if not m:
            continue
        nm = m.group(1).strip(" .")
        if len(nm) < 3 or re.fullmatch(r"(?:i|me|you|he|she|they|it|someone|anyone|everyone|who|what)", nm, re.I):
            continue
        # "nupur kumar" may be name + a stray word: try the full capture, then first word
        person = _resolve_employee(nm) or (_resolve_employee(nm.split()[0]) if " " in nm else None)
        if not person:
            continue
        w = when(m.group(2))
        if not w:
            return None
        return "get_attendance", {"person": person["name"], "date": w[0].isoformat(), "to_date": w[1].isoformat()}
    m = _ATT_TEAM.match(text)
    if m:
        w = when(m.group(1))
        if not w:
            return None
        return "get_attendance", {"date": w[0].isoformat(), "to_date": w[1].isoformat()}
    return None


_MORE_Q = re.compile(
    r"^\s*(?:(?:what|which|show|tell|list|give)\b.{0,25}?\b(?:\d+|the)\s+(?:more|rest|others?|remaining)|"
    r"(?:show\s+)?(?:more|the rest|rest|all of them|show all|see all|list all))\s*[?.!]*\s*$", re.I)


def _fast_standup_command(text: str):
    """(tool, args) for the plain standup commands that don't need the model:
    add X to my standup, show my tasks, mark X done. None otherwise. Anything
    naming a date or another person is left to the model."""
    if not text or len(text) > 320 or "\n" in text.strip():
        return None
    try:
        att = _fast_attendance(text.strip())
    except Exception:
        logger.debug("whatsapp_agent: fast attendance failed", exc_info=True)
        att = None
    if att:
        return att
    for rx, view in _MGMT_FAST:
        if rx.match(text):
            return "get_management_view", {"view": view}
    if _DASH_Q.match(text):
        return "get_my_dashboard", {}
    if _MORE_Q.match(text):   # "what are 7 more" after a list -> the full standup list
        return "get_my_tasks", {}
    if _DUE_WEEK.match(text):
        return "get_task_schedule", {"when": "week"}
    if _PENDING_Q.match(text):
        return "get_my_tasks", {}
    cm = _CLIENT_Q.match(text)
    if cm:
        who = cm.group(1).strip(" .,:;-\"'")
        if who and not re.fullmatch(r"(?:my|me|i|it|everyone|everything|the team|team|you|abhinav)", who, re.I):
            try:
                import org_profile
                known = org_profile.find_project(who)
            except Exception:
                known = None
            # only when the phrase is just the client's name ("omotec", "omotec ltd"); anything
            # longer ("the omotec shoot on friday") is a different question for the model
            if known:
                cs = re.sub(r"[^a-z0-9]", "", known["client"].lower())
                ws = re.sub(r"[^a-z0-9]", "", who.lower())
                if ws == cs or (cs in ws and len(ws) <= len(cs) + 4):
                    return "get_client_status", {"client": known["client"]}
    m = _ADD_STANDUP.match(text)
    if m:
        task = m.group(1).strip(" .,:;-\"'")
        if task and not _DATE_WORD.search(task) and len(task) <= 300:
            return "add_standup_task", {"task": task}
        return None
    if _SHOW_TASKS.match(text):
        return "get_my_tasks", {}
    m = _MARK_DONE.match(text)
    if m:
        task = next((g for g in m.groups() if g), "").strip(" .,:;-\"'")
        if (task and len(task) >= 3 and not _DATE_WORD.search(task)
                and not re.search(r"\b(task|tasks|all|everything)\b$", task, re.I)):
            return "update_standup_task", {"task": task, "status": "done"}
    return None


def _find_group_commitment(which: str):
    """#id, a bare number, or words from an open group promise -> its id."""
    import group_intel
    w = str(which or "").strip()
    if re.fullmatch(r"#?\d+", w):
        return int(w.lstrip("#"))
    best = None
    for r in group_intel.list_commitments("", "open", 80):
        sc = group_intel._overlap(w, r["action"])
        if sc >= 0.5 and (best is None or sc > best[0]):
            best = (sc, r["id"])
    return best[1] if best else None


_ADD_STANDUP = re.compile(
    r"^(?:please\s+|pls\s+)?(?:add|put)\s+(.+?)\s+(?:to|in|into|on)\s+"
    r"(?:my\s+|the\s+)?(?:daily\s+)?(?:stand-?up|standup list)\b[\s.!]*$", re.I | re.S)
# anything mentioning a date or another person is left to the model
_DATE_WORD = re.compile(
    r"\b(tomorrow|today|yesterday|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|next week|assign)\b", re.I)

_WRITE_TOOLS = {
    "assign_task", "delegate_my_task", "remind_teammate", "remind_me", "send_group_message",
    "schedule_group_message", "message_multiple", "add_standup_task", "update_standup_task",
    "resolve_group_commitment", "reject_group_commitment", "add_group_commitment",
    "decide_leave",
    "create_task", "set_task_meta", "set_attendance", "review_task", "reschedule_task",
    "place_announcement_call", "set_leave", "clear_leave",
    "set_standing_rule", "remove_standing_rule",
}
_SUCCESS_PREFIXES = (
    "added", "created", "handed", "updated", "marked", "reopened",
    "approved", "sent", "posted", "checked", "cleared",
)


def _audit(scope: str, identity: dict, action: str, detail: str) -> None:
    try:
        conn = get_connection()
        with conn:
            conn.execute(
                "INSERT INTO wa_action_log (sender, sender_name, scope, action, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (identity.get("id", ""), identity.get("name", ""), scope, action,
                 str(detail)[:400], datetime.now(timezone.utc).isoformat()),
            )
        conn.close()
    except Exception:
        logger.debug("whatsapp_agent: audit write failed", exc_info=True)


def _enqueue_outbound(jid: str, text: str) -> bool:
    """Queue a proactive WhatsApp message. The laptop companion polls
    /api/companion/whatsapp-outbox and delivers it via the local bridge —
    the Railway backend can't reach the bridge directly."""
    return wa_outbox.enqueue(jid, text)


def _resolve_employee(name: str) -> dict | None:
    """Best-effort name -> {id, name}. Exact (case-insensitive) first, then a
    first-name / substring match. None if nothing sensible matches."""
    n = (name or "").strip().lower()
    if not n:
        return None
    roster = _active_employees()
    for e in roster:
        if e["name"].strip().lower() == n:
            return e
    for e in roster:
        first = e["name"].strip().lower().split()[0] if e["name"].strip() else ""
        if first == n or n in e["name"].strip().lower() or (first and first.startswith(n)):
            return e
    return None


def _self_jid(identity: dict) -> str:
    """The sender's own WhatsApp DM JID (for remind_me) -- looked up from
    the roster by employee id rather than threaded through from the
    inbound sender string, since _run_tool doesn't otherwise carry it."""
    for e in _active_employees():
        if e.get("id") == identity.get("id"):
            return _wa_jid(e.get("whatsapp"))
    return ""


def _format_standup(tasks: list, who: str) -> str:
    """Shared renderer for a person's daily standup (used by get_my_tasks and
    get_teammate_tasks). `who` is 'Your' or a name like 'Nupur's'. Kept
    dash-free so the model doesn't echo bullet punctuation.

    Numbered 1-based, in the same order _match_standup_task() resolves a
    bare number against (both order by `id`) -- without this, "mark 4 as
    done" had nothing to resolve against: the list had no numbers and
    _match_standup_task only ever did text matching, so a bare digit query
    just failed to match anything."""
    if not tasks:
        return f"{who} standup for today is empty."
    done_words = ("done", "completed", "complete", "need_for_approval")
    lines = []
    for i, t in enumerate(tasks, 1):
        is_done = str(t["status"]).lower() in done_words
        prefix = "✅ " if is_done else "❌ "   # same marks as the 5 PM group post
        tail = ""
        if t.get("carried_from"):
            tail += f" (carried over from {t['carried_from']})"
        if t.get("blocker"):
            tail += f" (blocked by {t['blocker']})"
        lines.append(f"{i}. {prefix}{t['title']}{tail}")
    done = sum(1 for t in tasks if str(t["status"]).lower() in done_words)
    head = f"{who} standup today, {done} done and {len(tasks) - done} to go:"
    return head + "\n" + "\n".join(lines)


_DONE_WORDS = ("done", "completed", "complete", "need_for_approval")


def _match_standup_task(user_id: str, query: str, *, prefer: str | None = None,
                        ambiguous: list | None = None) -> dict | None:
    """Find one of today's standup rows for this user by a loose description
    ('whatsapp bot testing' -> 'testing of the whatsapp bot'). Returns
    {id, title, status, notion_id, blocker, pos} or None.

    A write tool must not guess when two tasks fit ("reel" vs "reel 2"), so:
      - prefer="open"/"done" narrows a tie to tasks that make sense for the
        action (marking done -> not-yet-done ones first);
      - if it is still a tie and `ambiguous` (a list) was passed, the tied
        candidates are put in it and None is returned so the caller can ask
        "which one?" -- without `ambiguous`, the first match is returned, as
        before."""
    q = (query or "").strip().lower()
    if not q:
        return None
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, status, notion_id, blocker FROM standup_tasks "
            "WHERE user_id=? AND date=? AND status NOT IN ('deleted','delegated') "
            "ORDER BY id",
            (user_id, _today_ist()),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        logger.exception("whatsapp_agent: standup match query failed")
        return None
    cand = [{"id": r[0], "title": r[1] or "", "status": (r[2] or "pending"),
             "notion_id": r[3], "blocker": (r[4] or ""), "pos": i}
            for i, r in enumerate(rows, 1)]
    # Bare/near-bare number ("4", "#4", "task 4") -> 1-based position in
    # this same `cand` order, which is exactly the order _format_standup()
    # numbers the list in (both order by `id`). Checked before any text
    # matching since a pure digit query can't meaningfully match a title.
    num_m = re.fullmatch(r"#?\s*(?:task\s*)?(\d+)", q)
    if num_m:
        idx = int(num_m.group(1)) - 1
        return cand[idx] if 0 <= idx < len(cand) else None
    for c in cand:
        if c["title"].strip().lower() == q:
            return c
    def _resolve(matches: list):
        """One match -> it. Several -> narrow by `prefer`, then ask/first."""
        if len(matches) <= 1:
            return matches[0] if matches else None
        if prefer in ("open", "done"):
            want_done = prefer == "done"
            narrowed = [c for c in matches
                        if (str(c["status"]).lower() in _DONE_WORDS) == want_done]
            if len(narrowed) == 1:
                return narrowed[0]
            if narrowed:
                matches = narrowed
        if ambiguous is not None:
            ambiguous.extend(matches[:5])
            return None
        return matches[0]

    subs = [c for c in cand
            if q in c["title"].lower()
            or (len(c["title"]) > 4 and c["title"].lower() in q)]
    if subs:
        return _resolve(subs)
    qt = set(re.findall(r"\w+", q))
    scored = []
    for c in cand:
        s = len(qt & set(re.findall(r"\w+", c["title"].lower())))
        if s >= 2:
            scored.append((s, c))
    if not scored:
        return None
    top = max(s for s, _ in scored)
    return _resolve([c for s, c in scored if s == top])


def _which_one(matches: list) -> str:
    """The question a write tool asks instead of guessing between tasks."""
    opts = "; ".join(f"{c['pos']}. {c['title']}" for c in matches)
    return (f"More than one task fits: {opts}. Ask which one they mean "
            "(a few more words of the title, or its number).")


def _find_notion_task(query: str, pool: list) -> dict | None:
    """Loose title match of `query` against a small list of Notion task dicts
    (exact -> substring -> word-overlap). None if nothing is confident."""
    q = (query or "").strip().lower()
    if not q or not pool:
        return None
    for t in pool:
        if (t.get("title") or "").strip().lower() == q:
            return t
    for t in pool:
        tl = (t.get("title") or "").lower()
        if q in tl or (len(tl) > 4 and tl in q):
            return t
    qt = set(re.findall(r"\w+", q))
    best, sc = None, 0
    for t in pool:
        ct = set(re.findall(r"\w+", (t.get("title") or "").lower()))
        s = len(qt & ct)
        if s > sc:
            best, sc = t, s
    return best if sc >= 2 else None


_APPROVAL_STOP = {"approve", "approved", "reject", "rejected", "please", "pls", "the", "a", "an",
                  "it", "this", "that", "send", "back", "for", "to", "of", "and"}


def _approval_tokens(text: str) -> set:
    out = set()
    for tok in re.findall(r"\w+", (text or "").lower()):
        if tok in _APPROVAL_STOP:
            continue
        out.add(tok.lstrip("0") or "0" if tok.isdigit() else tok)
    return out


def _match_approval(query: str, pend: list):
    """(match, ties) for 'OMOTEC Reel 03' against tasks waiting on approval, using
    client name + title words. Needs every word covered (>= 2/3 for long queries)
    and a UNIQUE best task; a tie returns (None, tied tasks) so the caller asks
    instead of approving the wrong post."""
    qt = _approval_tokens(query)
    if not qt or not pend:
        return None, []
    ql = (query or "").strip().lower()
    for t in pend:
        if str(t.get("title") or "").strip().lower() == ql:
            return t, []
    scored = []
    for t in pend:
        ct = _approval_tokens(f"{t.get('client_name') or ''} {t.get('title') or ''}")
        cov = len(qt & ct) / len(qt)
        if cov >= (1.0 if len(qt) <= 2 else 0.66):
            scored.append((cov, t))
    if not scored:
        return None, []
    top = max(c for c, _ in scored)
    best = [t for c, t in scored if c == top]
    return (best[0], []) if len(best) == 1 else (None, best)


_APPROVAL_STATUSES = {
    "need for approval", "need approval", "needs approval",
    "pending review", "in review", "for approval",
}
_CLOSED_STATUSES = {
    "done", "approved", "posted", "final", "complete", "completed",
    "cancelled", "canceled", "need_for_approval",
}


def _kb_search(query: str, limit: int = 6) -> list:
    """Search the whole knowledge base, no project/user scoping (every
    employee is admin-tier here). Keyword (bm25) always; semantic hits
    folded in ahead of it when that layer is enabled."""
    hits: list = []
    try:
        for h in semantic_kb.search(query, limit=max(3, limit // 2)):
            hits.append({"filename": h.get("filename"), "chunk": h.get("chunk")})
    except Exception:
        logger.debug("whatsapp_agent: semantic kb search failed", exc_info=True)

    mq = kb_retriever._fts_query(query)
    if mq:
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                """SELECT filename, chunk, bm25(kb_chunks_fts) AS score
                   FROM kb_chunks_fts
                   WHERE kb_chunks_fts MATCH ?
                   ORDER BY score
                   LIMIT ?""",
                (mq, int(limit)),
            )
            rows = cur.fetchall()
            conn.close()
            hits.extend({"filename": r[0], "chunk": r[1]} for r in rows)
        except Exception:
            logger.debug("whatsapp_agent: kb search failed", exc_info=True)

    seen, out = set(), []
    for h in hits:
        key = (h.get("filename"), (h.get("chunk") or "")[:140])
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out[:limit]


# ── Formatting helpers ──────────────────────────────────────────────────────

def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _humanize(reply: str, *, bullets: bool = False) -> str:
    """Tidy a reply. Always turns em/en dashes used as punctuation into
    commas (so it doesn't read like a press release). When `bullets` is
    False it also strips line-leading bullet characters and spaced-hyphen
    punctuation; when True (group chats) it leaves '- ' list markers alone.
    Hyphens inside words (reverse-engg) are never touched.

    Deliberately does NOT strip a leading number ("1. ", "2) ") the way it
    used to -- _format_standup() now numbers each task 1-based specifically
    so 'mark 4 as done' has something real to refer back to, and this used
    to silently strip that numbering back out again in every DM."""
    # The stronger model writes markdown. WhatsApp's own bold is *single*
    # asterisks; **double** shows up as literal asterisks, and # headings as #.
    reply = re.sub(r"\*\*(.+?)\*\*", r"*\1*", reply or "", flags=re.S)
    lines = []
    for ln in reply.split("\n"):
        ln = re.sub(r"^(\s*)#{1,6}\s+", r"\1", ln)
        if not bullets:
            ln = re.sub(r"^(\s*)[-*•‣▪]\s+", r"\1", ln)
        lines.append(ln)
    out = "\n".join(lines)
    out = re.sub(r"(?<=\S) *[—–] *(?=\S)", ", ", out)      # word — word -> word, word
    out = out.replace("—", ", ").replace("–", ", ")
    if not bullets:
        out = re.sub(r"(?<=\S) +- +(?=\S)", ", ", out)     # spaced hyphen -> comma
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" +([,.!?;:])", r"\1", out)
    return out.strip()


def _fmt_task_line(t: dict, *, include_assignee: bool = True, detail: bool = False) -> str:
    bits = [t.get("title") or "(untitled)"]
    if t.get("type"):
        bits.append(str(t["type"]))
    if t.get("status"):
        bits.append(str(t["status"]))
    if include_assignee and t.get("assigned_to"):
        bits.append("→ " + str(t["assigned_to"]))
    if t.get("due_date"):
        bits.append("due " + str(t["due_date"]))
    line = " | ".join(bits)
    if not detail:
        return line
    # the Sheets/content-calendar fields, so WhatsApp can answer "what's the
    # caption for X" / "what's the script for Y" without opening Lumina
    extra = []
    if t.get("creation_date"):
        extra.append(f"  start: {t['creation_date']}")
    for label, key, n in (
        ("brief", "brief", 200), ("content", "content", 240), ("idea", "idea", 240),
        ("script", "scripts_copy", 500), ("caption", "caption", 400),
    ):
        v = _clip(t.get(key), n)
        if v:
            extra.append(f"  {label}: {v}")
    if t.get("file_link"):
        extra.append(f"  file: {t['file_link']}")
    return line + ("\n" + "\n".join(extra) if extra else "")


def _is_overdue(due: str, today: str) -> bool:
    return bool(due) and re.match(r"^\d{4}-\d{2}-\d{2}$", str(due)) and str(due) < today


# ── Tools ───────────────────────────────────────────────────────────────────

_EMPLOYEE_TOOLS = [
    {
        "name": "get_clients",
        "description": "List all of the agency's clients and their current status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_client_tasks",
        "description": "List a client's tasks/deliverables AND their full content "
                       "calendar (the Lumina 'Sheets' view): post type, status, "
                       "assignee, dates, brief, content, idea, script/copy, "
                       "caption and file link. Use this for questions like 'what's "
                       "the caption for X', 'what's the script for Y's reel', "
                       "'what's scheduled this week for Z'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string", "description": "The client's name."}
            },
            "required": ["client_name"],
        },
    },
    {
        "name": "get_my_tasks",
        "description": "The person's live daily-standup task list for today "
                       "(what shows on their Lumina Standup screen) — with done "
                       "/ pending status, blockers and carried-over items. This "
                       "is the ONLY source for 'what are my tasks' — do not use "
                       "the wider Notion board for that.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_standup_task",
        "description": "Add a task to the person's daily standup in Lumina -- "
                       "for TODAY by default, or a FUTURE date if they say "
                       "'add this to tomorrow's standup', 'put this on "
                       "Monday's list', 'for Thursday's standup add...'. "
                       "Work out the exact real date yourself (today is "
                       "known) and pass it as `date`; leave `date` out "
                       "entirely for today, the common case.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "The task text, roughly as they said it."},
                "date": {"type": "string",
                         "description": "Optional. The exact real IST date "
                                        "this task should land on, as "
                                        "YYYY-MM-DD. Omit for today."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "update_standup_task",
        "description": "Mark one of the person's standup tasks for today as "
                       "done, or reopen it. Use for 'mark X as done', 'X is "
                       "complete', 'finished X', 'reopen X', or 'mark 4 as "
                       "done'/'mark #4 as done' referring to its number from "
                       "get_my_tasks' list. Match the task by a few words "
                       "from what they call it, OR by that list number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "A few words identifying the task, "
                                        "or its bare number from the list "
                                        "(e.g. '4')."},
                "status": {"type": "string", "enum": ["done", "pending"],
                           "description": "done (default) or pending to reopen."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "assign_task",
        "description": "Add a NEW task to another team member's daily standup "
                       "-- for today by default, or a FUTURE date if they say "
                       "'add X to Nupur's list for tomorrow', 'put X on "
                       "Kshitij's standup for Monday'. Work out the exact "
                       "real date yourself (today is known) and pass it as "
                       "`date`; leave `date` out for today. This creates a "
                       "fresh task on their standup; it does NOT move one of "
                       "yours. Works the same in a group chat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The teammate to add the task for."},
                "task": {"type": "string",
                         "description": "The task text, roughly as it was said."},
                "date": {"type": "string",
                         "description": "Optional. The exact real IST date "
                                        "this task should land on, as "
                                        "YYYY-MM-DD. Omit for today."},
            },
            "required": ["name", "task"],
        },
    },
    {
        "name": "delegate_my_task",
        "description": "Hand one of YOUR OWN standup tasks for today over to "
                       "another team member. It comes off your list and goes "
                       "onto theirs. Use for 'give my X task to Nupur', "
                       "'delegate X to Happy', 'pass X to Kshitij'. Match the "
                       "task by a few words from what you call it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "A few words identifying which of your "
                                        "tasks to hand over."},
                "name": {"type": "string",
                         "description": "The teammate to hand it to."},
            },
            "required": ["task", "name"],
        },
    },
    {
        "name": "send_group_message",
        "description": "Post a message into the team's WhatsApp group on this "
                       "person's behalf. Use when they say 'send/post/text the "
                       "group ...', 'tell everyone ...', 'announce ... in the "
                       "group', 'message the group to ...'. Compose the message "
                       "the way they asked for it. Only works from a private "
                       "chat, not from inside the group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "The exact message to post in the group."}
            },
            "required": ["message"],
        },
    },
    {
        "name": "schedule_group_message",
        "description": "Schedule a message to post into the team's WhatsApp "
                       "group later, instead of right now. Use for 'schedule a "
                       "message for the group at 5pm', 'post this to the group "
                       "tomorrow morning', 'send this at 9am on Friday'. Work out "
                       "the exact real date and time they mean yourself (today "
                       "is known) and pass it as YYYY-MM-DD HH:MM in IST. Only "
                       "works from a private chat, not from inside the group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "The exact message to post in the group."},
                "when": {"type": "string",
                         "description": "The exact IST date+time to send it, as "
                                        "YYYY-MM-DD HH:MM."},
            },
            "required": ["message", "when"],
        },
    },
    {
        "name": "remind_teammate",
        "description": "Send ONE specific team member a WhatsApp nudge about "
                       "something and it goes out RIGHT AWAY, no confirmation "
                       "needed — 'remind Nupur to finish the deck', 'ping Happy "
                       "about the edit', 'tell Kshitij to reply to the client', "
                       "'text Abhinav that...', 'message Noorish about...'. Use "
                       "this whenever exactly ONE person is named, even if the "
                       "phrasing is just 'text'/'message' rather than "
                       "'remind'/'ping'. They get a direct message from Lumina "
                       "saying it came from you. Only works in a private chat "
                       "with you, not from the group. For TWO OR MORE named "
                       "people, use message_multiple instead — don't use that "
                       "one for a single person, it needs an extra 'yes' step "
                       "this doesn't.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The teammate to nudge."},
                "message": {"type": "string",
                            "description": "What to remind them about, in your words."},
                "when": {"type": "string",
                         "description": "ONLY if the user gave a time or day ('at 3pm', "
                                        "'tomorrow'): the exact IST date+time to send it, "
                                        "YYYY-MM-DD HH:MM. Omit to send right now. If only a "
                                        "day is given use 09:00."},
            },
            "required": ["name", "message"],
        },
    },
    {
        "name": "remind_me",
        "description": "Set a PERSONAL reminder that WhatsApps the sender the "
                       "message back at a specific time -- 'remind me in 2 "
                       "hours to call the client', 'remind me at 5pm to "
                       "submit the report', 'ping me tomorrow morning about "
                       "the invoice'. Work out the exact real IST date+time "
                       "yourself (today's date AND the current time are "
                       "both known) and pass it as `when`, YYYY-MM-DD HH:MM. "
                       "Sends automatically at that time, no confirmation "
                       "needed -- it only ever goes to the sender, nobody "
                       "else sees it. Works from a private chat AND from the "
                       "group (from the group it still goes to their DM). If they give only a day (tomorrow, Friday, next "
                       "Monday) or a vague time, pick the time yourself "
                       "(morning 09:00, afternoon 14:00, evening 18:00, no time "
                       "at all 09:00); do NOT ask, set it and say the time you "
                       "chose.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "What to remind them about, in their own words."},
                "when": {"type": "string",
                         "description": "The exact real IST date+time to send "
                                        "the reminder, as YYYY-MM-DD HH:MM."},
            },
            "required": ["message", "when"],
        },
    },
    {
        "name": "message_multiple",
        "description": "Send the SAME WhatsApp message to TWO OR MORE named "
                       "teammates at once, as individual DMs (not a group post) "
                       "-- 'tell Nupur, Palak, and Happy the client call moved "
                       "to 5pm', 'message the design team that the brief is "
                       "ready'. Confirms with you first (like send_group_"
                       "message) and does NOT send until you reply 'yes' -- "
                       "always tell the person that a confirmation is needed, "
                       "don't let them think it's already sent. Do NOT use "
                       "this for a single named person -- use remind_teammate "
                       "for that instead, it sends immediately with no "
                       "confirmation step. Only works in a private chat with "
                       "you, not from the group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"},
                          "description": "The teammates to message."},
                "message": {"type": "string",
                            "description": "The exact message to send each of them."},
            },
            "required": ["names", "message"],
        },
    },
    {
        "name": "place_announcement_call",
        "description": "Place an actual WhatsApp VOICE CALL to a teammate that "
                       "rings their phone and speaks a message out loud, then "
                       "hangs up -- for when a text nudge isn't getting through "
                       "(overdue-task escalation, urgent check-in). One-way "
                       "only: they hear the message, they can't talk back to it. "
                       "Only works in a private chat with you, not from the "
                       "group, same as remind_teammate. Use this rarely -- a "
                       "phone actually ringing is a bigger deal than a WhatsApp "
                       "text, only reach for it when that's the point.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The teammate to call."},
                "message": {"type": "string",
                            "description": "What the call should say, in plain "
                                          "spoken sentences -- this gets read "
                                          "aloud by text-to-speech, so keep it "
                                          "short and natural, no markdown, no "
                                          "bullet points, no abbreviations."},
            },
            "required": ["name", "message"],
        },
    },
    {
        "name": "get_teammate_tasks",
        "description": "Another team member's live daily-standup task list for "
                       "today, WITH each item's real status -- marks "
                       "finished items ✅ and open ones ❌. Use this "
                       "for 'what is Nupur working on', 'what are Kshitij's "
                       "tasks', 'is Happy doing anything today', 'has Happy "
                       "done his tasks', 'what has X finished today' -- ANY "
                       "'X's tasks' question where completion status matters. "
                       "This is the right tool for that, not "
                       "get_task_schedule (that one is deadline-only and "
                       "silently drops completed items instead of marking "
                       "them done). Everyone on the team can see everyone "
                       "else's standup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "The teammate's name or first name."}
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_team_standup",
        "description": "A roll-up of the whole team's daily standup for today — "
                       "each person and what's on their list. Use for 'what's "
                       "the team doing today', 'who's working on what'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_knowledge_base",
        "description": "Search the agency's uploaded documents / knowledge base "
                       "(briefs, notes, brand guidelines, strategy docs) for a "
                       "phrase or topic. Returns matching excerpts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_team_overview",
        "description": "A quick snapshot: number of clients, open tasks, and "
                       "overdue tasks across the whole agency.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_task",
        "description": "Create a real task on the Notion board AND put it on the "
                       "assignee's daily standup for today. Use for 'add task: "
                       "shoot the Omotec reel, Friday, assign Happy'. Convert "
                       "relative dates ('Friday', 'tomorrow') to YYYY-MM-DD "
                       "yourself. Leave assignee/client/date out if not given.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The task."},
                "client": {"type": "string", "description": "Client name, if any."},
                "assignee": {"type": "string", "description": "Who it's for; blank = unassigned."},
                "due_date": {"type": "string", "description": "YYYY-MM-DD, if given."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "set_task_meta",
        "description": "Add or clear a blocker, or change the due date, on one "
                       "of the person's own standup tasks for today. Use for "
                       "'blocked on client feedback for X', 'push the deck to "
                       "Monday', 'clear the blocker on Y'. Match the task by a "
                       "few words; dates as YYYY-MM-DD.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words identifying the task."},
                "blocker": {"type": "string", "description": "Blocker text, or empty string to clear it."},
                "due_date": {"type": "string", "description": "New due date YYYY-MM-DD, or empty to clear."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "get_task_schedule",
        "description": "Board tasks by deadline: what's overdue, due today, or "
                       "due this week. Use for 'what's overdue for Omotec', "
                       "'what's due this week', 'what is Happy behind on'. "
                       "Optionally narrow by client or by person. Does NOT "
                       "tell you what someone has finished today -- it's "
                       "deadline filtering only, and completed/closed items "
                       "are left out entirely rather than marked done. For "
                       "'has Happy done his tasks', 'what has X finished "
                       "today', use get_teammate_tasks instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "enum": ["overdue", "today", "week"],
                         "description": "Which window (default week)."},
                "client": {"type": "string", "description": "Narrow to one client."},
                "person": {"type": "string", "description": "Narrow to one assignee."},
            },
        },
    },
    {
        "name": "get_attendance",
        "description": "Who logged in / out and exactly when, for TODAY or ANY PAST DAY, "
                       "and who hasn't logged in. Use for 'who's in', 'who hasn't "
                       "logged in', 'what time did Nupur log in today / yesterday / on "
                       "Monday / on 12 Oct', 'did X come in on Friday', 'attendance this "
                       "week'. Pass person (a name) for one person, omit it for the whole "
                       "team. Pass date exactly as the person said it ('friday', 'last "
                       "monday', 'yesterday', '16/10', '12 oct') or as YYYY-MM-DD, and "
                       "never work the date out yourself; for a range pass 'last week' / "
                       "'this week' as date, or date plus to_date. Omit date for today. "
                       "Quote the times exactly as returned; never "
                       "guess one, and never answer a login-time question from memory.",
        "input_schema": {"type": "object", "properties": {
            "person": {"type": "string", "description": "Employee name; omit for everyone."},
            "date": {"type": "string", "description": "YYYY-MM-DD; omit for today."},
            "to_date": {"type": "string", "description": "YYYY-MM-DD end of a range (optional)."},
        }},
    },
    {
        "name": "set_attendance",
        "description": "Check the person in or out for today. Use for "
                       "'checking in', 'I'm here', 'leaving for the day', 'done "
                       "for today'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["in", "out"]},
            },
            "required": ["action"],
        },
    },
    {
        "name": "get_daily_brief",
        "description": "The assembled daily brief: overdue / due-today / "
                       "due-tomorrow tasks, standups in, sheet-sync health, API "
                       "budget. Use for 'summarise today', 'what's the status', "
                       "'end of day rundown'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "enum": ["morning", "evening"],
                           "description": "morning = look ahead, evening = wrap-up (default)."},
            },
        },
    },
    {
        "name": "get_content_calendar",
        "description": "What's scheduled to be posted across ALL social-media "
                       "clients in the next few days, soonest date first. Use "
                       "for 'what are we posting this week', 'what's going "
                       "out tomorrow', 'what's coming next', 'what's going "
                       "live', 'what's posting soon' -- any question about "
                       "the upcoming content schedule, in a DM or a group. "
                       "Groups by day (nearest day first) and shows client, "
                       "post type, assignee and whether the caption is "
                       "written.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days ahead (default 7)."}
            },
        },
    },
    {
        "name": "get_post_content",
        "description": "Pull the actual Sheets content for ONE task by name -- "
                       "the Idea, Script/Copy, Caption, and Drive link that were "
                       "typed into that task's row in Lumina Sheets. Use when "
                       "someone points at a specific task and asks for its "
                       "script, idea, caption, brief, or link, e.g. Happy "
                       "asking 'what's the script for my reel today'. Not for "
                       "browsing what's scheduled -- use get_content_calendar "
                       "for that.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task title or a fragment "
                          "of it, e.g. 'the reel today', 'Post 12'."},
                "person": {"type": "string", "description": "Whose task, if the "
                            "sender is asking about someone else's. Omit for "
                            "'my task'."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "get_my_dashboard",
        "description": "The person's own morning dashboard, built for their role "
                       "(leaders: client health, decisions, deadlines, blocked; DMs: "
                       "their clients plus their work; everyone else: their own work). "
                       "Use for 'my dashboard', 'what's my day', 'morning brief', "
                       "'what do I have today'. Copy the reply EXACTLY.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_management_view",
        "description": "Cross-client and cross-person answers, already formatted: "
                       "view=blocked ('show me all blocked projects'), at_risk "
                       "('what is at risk'), needs_vg ('what needs VG'), "
                       "completed_week ('what did we complete this week'), "
                       "overloaded ('who is overloaded'), waiting_on_me ('who is "
                       "waiting on me'), my_approvals ('what needs my approval'). "
                       "Copy the reply EXACTLY, line for line, nothing added.",
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string",
                         "enum": ["blocked", "at_risk", "needs_vg", "completed_week",
                                  "overloaded", "waiting_on_me", "my_approvals"]},
            },
            "required": ["view"],
        },
    },
    {
        "name": "reschedule_task",
        "description": "Move or delay one of the person's OWN standup tasks to a "
                       "new date: it gets that due date (also on the Notion board) "
                       "and shows on the standup of that day. Use for 'move X to "
                       "Friday', 'delay the deck to Monday', 'push X to next week'. "
                       "Convert relative dates to YYYY-MM-DD yourself.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words identifying the task."},
                "to": {"type": "string", "description": "New date, YYYY-MM-DD."},
            },
            "required": ["task", "to"],
        },
    },
    {
        "name": "get_client_status",
        "description": "The fixed status card for one client: SMM active or not, "
                       "posts this week and next week, production pending, open client "
                       "feedback, blocker, the client's DM and whether it Needs VG. "
                       "Use for 'what is happening with Mellow', 'status on Omotec', "
                       "'where are we with X'. The reply is already formatted: copy "
                       "it EXACTLY, line for line, with nothing added.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {"type": "string", "description": "The client's name."}
            },
            "required": ["client"],
        },
    },
    {
        "name": "list_capabilities",
        "description": "Explain what you can do for this person. Use when they "
                       "ask 'what can you do', 'help', 'what do you know'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_sticker",
        "description": "React with a sticker from the team's saved pack, sent "
                       "alongside your text reply. Use it SPARINGLY — only when "
                       "a sticker genuinely lands (a real win, a groan, a "
                       "deserved roast), not on ordinary replies. Pass a mood "
                       "word ('smug', 'bruh', 'nice', 'overdue', 'approved') to "
                       "match one, or leave it blank for a random sticker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mood": {"type": "string", "description": "Optional mood/tag to match."}
            },
        },
    },
    {
        "name": "get_recent_actions",
        "description": "The audit trail of writes you've made on people's behalf "
                       "(tasks created, approved, delegated, standup edits, "
                       "check-ins). Use for 'what have you done today', 'show "
                       "the log'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many entries (default 15)."}
            },
        },
    },
    {
        "name": "get_pending_approvals",
        "description": "Tasks currently waiting on approval / review across the "
                       "board. Use for 'what's pending approval', 'anything for "
                       "me to review'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "review_task",
        "description": "Approve a task that's waiting on approval, or send it "
                       "back for changes. Use for 'approve the Omotec reel', "
                       "'reject X', 'send Y back'. Match by a few words from "
                       "the title.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words from the task title."},
                "decision": {"type": "string", "enum": ["approve", "reject"]},
            },
            "required": ["task", "decision"],
        },
    },
    {
        "name": "set_leave",
        "description": "REQUEST leave / holiday for a day or a date range "
                       "-- this creates a pending request that Noorish "
                       "(HR) must approve before it counts against the "
                       "15-day leave pool or exempts anyone from the "
                       "standup lock. Use for 'I'm on leave today', 'on "
                       "holiday tomorrow', 'off Thursday and Friday', "
                       "'mark Nupur on leave next week'. Work out the "
                       "real calendar dates yourself (today is known) and "
                       "pass them as YYYY-MM-DD. For a single day, pass "
                       "the same date as start and end. Optionally pass "
                       "leave_type='half' for a half day (default is a "
                       "full day).",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string",
                               "description": "First day of leave, YYYY-MM-DD (IST)."},
                "end_date": {"type": "string",
                             "description": "Last day of leave, YYYY-MM-DD (IST). "
                                            "Same as start_date for one day."},
                "person": {"type": "string",
                           "description": "Whose leave, if not your own — a teammate's name."},
                "reason": {"type": "string",
                           "description": "Optional short note, e.g. 'sick', 'vacation'."},
                "leave_type": {"type": "string", "enum": ["full", "half"],
                               "description": "Optional -- 'half' for a half "
                                              "day. Default is a full day."},
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "clear_leave",
        "description": "Cancel YOUR OWN still-pending leave request (not yet "
                       "approved by HR). Use for 'cancel my leave', 'never "
                       "mind about that leave request', 'undo my leave "
                       "request'. Only cancels your own pending request(s) "
                       "-- cannot target a teammate's leave, and does not "
                       "touch anything HR has already approved (that needs "
                       "an HR decision, not a self-service undo).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_leave_balance",
        "description": "Check leave balance -- the 15-day annual pool, "
                       "comp-off earned from overtime, days used, and "
                       "days remaining this year. Use for 'how much leave "
                       "do I have left', 'my leave balance', 'how many "
                       "days off do I have'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_standing_rule",
        "description": "Save a STANDING INSTRUCTION -- something they want you "
                       "to honour from now on, indefinitely, without being "
                       "told again. Use this whenever they phrase something as "
                       "an ongoing rule rather than a one-off: 'from now on "
                       "always...', 'never... without asking me', 'whenever X "
                       "happens, tell me', 'every Monday remind me to...', "
                       "'default to...'. Do NOT use it for a single dated "
                       "task or a one-time reminder (create_task / remind_me "
                       "cover those). If the rule is a RECURRING reminder on a "
                       "particular day, also pass remind_day and remind_time "
                       "so it actually fires; leave both out for a rule that "
                       "is just about how you should behave.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule": {"type": "string",
                         "description": "The instruction, written as a clear "
                                        "standing rule in one sentence."},
                "remind_day": {"type": "string",
                               "description": "Weekday name (monday..sunday) "
                                              "for a recurring reminder. Omit "
                                              "if this rule isn't a recurring "
                                              "reminder."},
                "remind_time": {"type": "string",
                                "description": "IST time as HH:MM for a "
                                               "recurring reminder. Omit if "
                                               "not recurring."},
            },
            "required": ["rule"],
        },
    },
    {
        "name": "list_standing_rules",
        "description": "Show the standing instructions currently saved for "
                       "this person -- 'what rules do you have for me', 'what "
                       "have I told you to always do'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remove_standing_rule",
        "description": "Drop a standing instruction -- 'forget that rule', "
                       "'stop reminding me about invoices', 'remove rule 2'. "
                       "Identify it by its number from list_standing_rules or "
                       "by a few words of its text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "The rule's number, or a distinctive "
                                         "phrase from its text."},
            },
            "required": ["which"],
        },
    },
    {
        "name": "get_followups",
        "description": "What's quietly slipping for this person: standup "
                       "tasks that have been rolling over for days, work they "
                       "handed a teammate that hasn't moved, and things stuck "
                       "awaiting approval. Use it for 'what am I forgetting', "
                       "'what's slipping', 'anything I've dropped', 'what's "
                       "stuck'. This is about STALLED things specifically -- "
                       "for a plain list of today's work use get_my_tasks, and "
                       "for deadlines use get_task_schedule. Can also check a "
                       "named teammate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string",
                           "description": "Whose loose ends to check, if not "
                                          "the sender's own."},
            },
        },
    },
    {
        "name": "read_group_chat",
        "description": "Read what was said in the team WhatsApp group (the "
                       "assistant keeps the last 30 days). Use for 'what did X "
                       "say about Y', 'what was decided on the Zoho thing', "
                       "'summarise the group today', 'did anyone mention the "
                       "deadline'. `query` narrows to messages containing those "
                       "words; leave it out to get everything in the window. "
                       "Summarise the result, don't paste it back.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Optional words that must appear."},
                "hours": {"type": "number",
                          "description": "How far back, in hours. Default 48 "
                                         "(use 168 for 'this week')."},
            },
        },
    },
    {
        "name": "get_group_commitments",
        "description": "What people promised in the team group chat, tracked "
                       "automatically: who said they'd do what, by when, and "
                       "whether it's still open. Use for 'what did people "
                       "commit to', 'what's still pending from the group', "
                       "'did Nupur say she'd do X', 'what's open from today'. "
                       "Each line starts with #id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string",
                           "description": "Optional name to filter by."},
                "status": {"type": "string",
                           "description": "open (default), done, dropped or all."},
            },
        },
    },
    {
        "name": "get_pending_leave",
        "description": "Leave requests waiting for the user's decision. Only "
                       "Noorish, Vidit and Kshitij can use this. Use for 'any "
                       "leave requests?', 'who applied for leave'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "decide_leave",
        "description": "Approve or reject a pending leave request. Only "
                       "Noorish, Vidit and Kshitij; nobody can decide their "
                       "own. Use for 'approve Nupur's leave', 'reject #4, we "
                       "have a shoot'. `which` = the #id or the person's name. "
                       "The first decision counts; if someone already decided, "
                       "say who.",
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string", "description": "#id or person's name."},
                "decision": {"type": "string", "description": "approve or reject."},
                "reason": {"type": "string", "description": "Optional, for a rejection."},
            },
            "required": ["decision"],
        },
    },
    {
        "name": "recall_group_facts",
        "description": "Decisions and client preferences the assistant picked "
                       "up from the team group chat. Use for 'what did we "
                       "decide with Mellow', 'how does <client> like captions', "
                       "'what was agreed about the Diwali campaign'. Filter by "
                       "client and/or words.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {"type": "string", "description": "Optional client name."},
                "query": {"type": "string", "description": "Optional words."},
            },
        },
    },
    {
        "name": "reject_group_commitment",
        "description": "The user says a tracked group promise was wrong or "
                       "never a real task ('that wasn't a task', 'ignore "
                       "that one', 'she was joking'). Closes it AND teaches the "
                       "tracker not to repeat that kind of mistake. Pass the "
                       "#id (from get_group_commitments) or words from it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string"},
                "why": {"type": "string", "description": "Optional short reason."},
            },
            "required": ["which"],
        },
    },
    {
        "name": "add_group_commitment",
        "description": "Track a promise the assistant missed ('Nupur also said "
                       "she'd send the deck Friday'). The assistant will then "
                       "check on it like any other.",
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Who will do it."},
                "action": {"type": "string"},
                "due": {"type": "string",
                        "description": "Optional real date YYYY-MM-DD."},
            },
            "required": ["owner", "action"],
        },
    },
    {
        "name": "get_calendar",
        "description": "The user's Google Calendar (meetings, shoots, calls) "
                       "for today or the next few days. Use for 'what's on "
                       "today', 'am I free at 3', 'what meetings this week'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "number",
                         "description": "How many days ahead, default 1 (today)."},
            },
        },
    },
    {
        "name": "get_my_brief",
        "description": "The full personal brief right now: calendar, group "
                       "promises due, decisions, posts going live, approvals "
                       "waiting, who has no standup. Use for 'brief me', "
                       "'what do I need to know', 'what's my day look like'.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "resolve_group_commitment",
        "description": "Close a tracked group promise when someone tells you "
                       "it's finished or cancelled ('the story is posted', "
                       "'forget the logo, client dropped it'). Pass its #id "
                       "from get_group_commitments, or some words from it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "The #id number, or words from the promise."},
                "status": {"type": "string",
                           "description": "done (default) or dropped."},
            },
            "required": ["which"],
        },
    },
    # Live web lookup that works on ANY model (web_search.py). It replaced
    # Anthropic's hosted web_search tool, which only exists for Claude and so
    # left the bot with no internet while it runs on Qwen.
    # cache_control on this LAST entry caches the whole tool array (it's sent
    # unchanged every round of the loop, same as the system prompt -- see the
    # sys_prompt cache_control note in handle_message).
    {
        "name": "search_web",
        "description": "Search the live internet and get a short sourced answer. Use "
                       "for anything the company data can't know: news, trends, "
                       "competitors, prices, how something works, facts that may "
                       "have changed, 'look it up'. Not for MMGA clients or tasks.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "What to look up, as a search query."}},
            "required": ["query"],
        },
        "cache_control": {"type": "ephemeral"},
    },
]

_CLIENT_TOOLS = [
    {
        "name": "get_my_tasks",
        "description": "List your deliverables: status, due date, and (for social "
                       "content) the post type, brief, content, idea, script/copy, "
                       "caption and file link.",
        "input_schema": {"type": "object", "properties": {}},
        "cache_control": {"type": "ephemeral"},
    },
]


# Keyword/emoji cues mapped to the fixed vibe vocabulary. Checked before ever
# spending an API call -- this used to be a whole Haiku call per burst, and
# the model's own instructions told it to reply "none" most of the time
# anyway (nothing unambiguous in the transcript), so most of those calls were
# pure waste. Replaced 2026-09-11 with a pure heuristic: zero API cost, catches
# the loud/obvious cases (someone typed "lol"/"🔥"/etc.), silently skips
# anything subtler instead of spending a call on a coin-flip "none".
_VIBE_PATTERNS = [
    (re.compile(r"lo+l+\b|lmf?ao+\b|ha(ha)+\b|rofl\b|\U0001F602|\U0001F923", re.I), "lol"),
    (re.compile(r"\bbruh+\b", re.I), "bruh"),
    (re.compile(r"\bwow\b|\bomg+\b|\bwhat\?!|\U0001F62E|\U0001F631", re.I), "shock"),
    (re.compile(r"\bnice\b|\bawesome\b|\bwell done\b|\bgreat job\b|\U0001F44F", re.I), "nice"),
    (re.compile(r"\bcongrat\w*\b|\bcelebrat\w*\b|\U0001F389|\U0001F973", re.I), "celebrate"),
    (re.compile(r"\bfacepalm\b|\bsmh\b|\U0001F926", re.I), "facepalm"),
    (re.compile(r"\bcry(ing)?\b|\U0001F62D|\U0001F622", re.I), "cry"),
    (re.compile(r"\bfire\b|\U0001F525", re.I), "fire"),
    (re.compile(r"\bagree(d)?\b|\btrue\b|\bfacts\b|\bexactly\b", re.I), "agree"),
    (re.compile(r"\bsmug\b", re.I), "smug"),
    (re.compile(r"\boof+\b", re.I), "oof"),
]


def group_vibe_sticker(group_id: str, messages: list, tags: list | None = None) -> str | None:
    """The bridge saw a burst of activity in an allow-listed group. Scan the
    recent messages for an unambiguous keyword/emoji cue and return a one-word
    mood for a reaction sticker, or None to stay quiet (the usual answer).
    `tags` is the bridge's actual taught sticker-tag vocabulary — a literal
    match there is honoured too. Pure heuristic, no API call."""
    if not group_id or not _group_allowed(group_id) or not messages:
        return None
    # A reaction is a short, un-questioning message ("lmaooo", "nice", a
    # 🔥). A long work sentence or a question that merely contains "true" or
    # "fire" is not a vibe, so those never count.
    transcript = " ".join(
        t for t in (str(m.get("text") or "").strip() for m in messages[-15:])
        if t and len(t) <= 60 and not t.endswith("?")
    )
    if not transcript.strip():
        return None
    for pattern, word in _VIBE_PATTERNS:
        if pattern.search(transcript):
            return word
    if tags:
        low = transcript.lower()
        for t in tags:
            tag = re.sub(r"[^a-z]", "", str(t).lower())
            if tag and len(tag) > 2 and re.search(rf"\b{re.escape(tag)}\b", low):
                return tag
    return None


_STICKER_CMD_RE = re.compile(r"^\s*!sticke?rs?\b\s*([a-z]+)?", re.I)


def sticker_command(text: str, identity: dict):
    """If `text` is a '!stickers ...' management command from an employee,
    return (reply_text, cmd) with cmd in on|off|list|clear|None. Else None.
    The bridge acts on the cmd; the agent never runs for these."""
    if identity.get("kind") != "employee":
        return None
    m = _STICKER_CMD_RE.match(text or "")
    if not m:
        return None
    sub = (m.group(1) or "on").lower()
    if sub in ("on", "add", "learn", "teach", "capture", "start"):
        return ("Sticker capture is on for 5 minutes. Forward me the stickers "
                "you want (from your WhatsApp favourites). Send a one-word tag "
                "right after a sticker to label it. Say '!stickers done' when "
                "you're finished.", "on")
    if sub in ("off", "done", "stop", "end", "finish"):
        return ("Sticker capture off.", "off")
    if sub in ("list", "count", "status"):
        return ("(count)", "list")   # bridge overwrites with its real number
    if sub in ("clear", "reset", "wipe", "delete"):
        return ("Cleared every saved sticker.", "clear")
    return ("Say '!stickers' to start teaching, '!stickers done' to stop, "
            "'!stickers list' for a count, '!stickers clear' to wipe.", None)


def _resolve_standup_date(raw_date, today):
    """For add_standup_task/assign_task's optional `date` field -- blank
    means today (the common case); otherwise it must be a real YYYY-MM-DD
    on or after today (a standup entry for a date that's already passed
    doesn't mean anything). Returns (date_str, error) -- date_str is the
    resolved date to actually use when error is None."""
    raw_date = str(raw_date or "").strip()
    if not raw_date:
        return today, None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date):
        return None, ("(need a real date -- figure out the exact date they "
                       "mean and pass it as YYYY-MM-DD)")
    try:
        # The regex only checks the shape: "2026-13-45" passes it and would
        # create a standup row for a day that doesn't exist, which no screen
        # ever shows. Parse it as a real calendar date.
        parsed = datetime.strptime(raw_date, "%Y-%m-%d")
    except ValueError:
        return None, ("(that isn't a real calendar date -- work out the exact "
                      "date they mean and pass it as YYYY-MM-DD)")
    if raw_date < today:
        return None, "That date's already passed -- ask which day they actually mean."
    if (parsed - datetime.strptime(today, "%Y-%m-%d")).days > 366:
        return None, "That's more than a year away -- ask which day they actually mean."
    return raw_date, None


def _run_tool(name: str, tool_input: dict, identity: dict,
              in_group: bool = False, turn: dict | None = None) -> str:
    today = _today_ist()
    kind = identity["kind"]
    if name in _WRITE_TOOLS:
        try:  # a write may change what the cross-client views would report
            import mgmt_views
            mgmt_views.invalidate()
        except Exception:
            pass

    try:
        if name == "get_clients" and kind == "employee":
            clients = _all_clients()
            if not clients:
                return "No clients found."
            return "\n".join(
                f"- {c['name']}" + (f" ({c['status']})" if c.get("status") else "")
                for c in clients
            )

        if name == "get_client_tasks" and kind == "employee":
            cname = (tool_input or {}).get("client_name", "")
            tasks = _tasks_for_client(cname)
            if not tasks:
                return f"No tasks found for '{cname}'. Check the client name with get_clients."
            shown = tasks[:15]
            head = f"Tasks for {cname}"
            if len(tasks) > len(shown):
                head += f" (first {len(shown)} of {len(tasks)} — ask about a specific post for the rest)"
            return head + ":\n" + "\n".join(_fmt_task_line(t, detail=True) for t in shown)

        if name == "update_standup_task" and kind == "employee":
            q = (tool_input or {}).get("task", "")
            new_status = str((tool_input or {}).get("status", "done")).strip().lower()
            if new_status in ("complete", "completed", "finished"):
                new_status = "done"
            if new_status in ("open", "reopen", "undone", "not done", "todo"):
                new_status = "pending"
            if new_status not in ("done", "pending"):
                # Never guess: an unknown status ("blocked", "in progress")
                # silently becoming "done" would close live work.
                return "(status has to be 'done' or 'pending')"
            amb: list = []
            m = _match_standup_task(identity["id"], q, ambiguous=amb,
                                    prefer="open" if new_status == "done" else "done")
            if not m and amb:
                return _which_one(amb)
            if not m:
                have = _standup_tasks_today(identity["id"])
                names = "; ".join(t["title"] for t in have) or "nothing yet"
                return f"Couldn't find a task matching '{q}'. Today's list: {names}."
            try:
                conn = get_connection()
                with conn:
                    conn.execute("UPDATE standup_tasks SET status=? WHERE id=?",
                                 (new_status, m["id"]))
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: update_standup_task failed")
                return "(couldn't update that just now)"
            # mirror board-linked rows to Notion, matching the Standup screen
            # (social-media tasks go to 'need approval', not straight to done)
            if m.get("notion_id"):
                try:
                    ttype = (notion_store.get_task_type(m["notion_id"]) or "").lower()
                    if new_status == "done":
                        nst = "need_for_approval" if "social" in ttype else "done"
                    else:
                        nst = "in_progress"
                    notion_store.update_task(m["notion_id"], status=nst)
                except Exception:
                    logger.debug("whatsapp_agent: notion status mirror failed",
                                 exc_info=True)
            head = (f"Marked done: {m['title']}" if new_status == "done"
                    else f"Reopened: {m['title']}")
            try:
                head += "\n\n" + _format_standup(
                    _standup_tasks_today(identity["id"]), "Your")
            except Exception:
                pass
            return head

        if name == "get_teammate_tasks" and kind == "employee":
            who = (tool_input or {}).get("name", "")
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            tasks = _standup_tasks_today(emp["id"])
            return _format_standup(tasks, f"{emp['name']}'s")

        if name == "get_team_standup" and kind == "employee":
            out = []
            for e in _active_employees():
                tasks = _standup_tasks_today(e["id"])
                if not tasks:
                    out.append(f"{e['name']}: nothing on standup")
                    continue
                done_words = ("done", "completed", "complete", "need_for_approval")
                bits = []
                for t in tasks[:6]:
                    d = " (done)" if str(t["status"]).lower() in done_words else ""
                    bits.append(f"{t['title']}{d}")
                extra = f" plus {len(tasks) - 6} more" if len(tasks) > 6 else ""
                out.append(f"{e['name']}: " + ", ".join(bits) + extra)
            return "Team standup today:\n" + "\n".join(out)

        if name == "get_my_tasks":
            if kind == "employee":
                tasks = _standup_tasks_today(identity["id"])
                if not tasks:
                    return ("Nothing on your standup for today yet. Tell me what "
                            "you're working on and I'll add it.")
                return _format_standup(tasks, "Your")
            if kind == "client":
                # Require a real linked client id — never fall back to
                # name matching for a client, that risks returning another
                # client's tasks (see _find_client's loose contains-match).
                cnid = identity.get("client_notion_id", "")
                if not cnid:
                    return ("Your account isn't fully linked yet — ask the "
                            "team to connect it so I can pull up your work.")
                tasks = _tasks_for_client(identity["client_name"], cnid)
                if not tasks:
                    return "You have no deliverables listed right now."
                shown = tasks[:15]
                head = "Your deliverables"
                if len(tasks) > len(shown):
                    head += f" (first {len(shown)} of {len(tasks)})"
                return head + ":\n" + "\n".join(
                    _fmt_task_line(t, include_assignee=False, detail=True) for t in shown
                )

        if name == "add_standup_task" and kind == "employee":
            task = re.sub(r"\s+", " ", str((tool_input or {}).get("task") or "")).strip()
            if not task:
                return "(no task text — ask them what to add)"
            target_date, err = _resolve_standup_date((tool_input or {}).get("date"), today)
            if err:
                return err
            try:
                conn = get_connection()
                dup = conn.execute(
                    "SELECT 1 FROM standup_tasks WHERE user_id=? AND date=? "
                    "AND LOWER(TRIM(title))=LOWER(TRIM(?)) "
                    "AND status NOT IN ('deleted','delegated') LIMIT 1",
                    (identity["id"], target_date, task[:500]),
                ).fetchone()
                if dup:
                    # A retried message / repeated "add X" must not stack up
                    # copies of the same task.
                    conn.close()
                    return f"Already on the list for {target_date}: {task[:120]}"
                with conn:
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title) VALUES (?, ?, ?)",
                        (identity["id"], target_date, task[:500]),
                    )
                conn.close()
                _auto_checkin(identity["id"])
                if target_date == today:
                    full = _format_standup(_standup_tasks_today(identity["id"]), "Your")
                    return f"Added to today's standup: {task[:120]}\n\n{full}"
                return f"Added to the {target_date} standup: {task[:120]}"
            except Exception:
                logger.exception("whatsapp_agent: add_standup_task failed")
                return "(couldn't add that to the standup just now)"

        if name == "assign_task" and kind == "employee":
            who = (tool_input or {}).get("name", "")
            task = (tool_input or {}).get("task", "").strip()
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            if not task:
                return "(no task text — ask what to add for them)"
            if emp["id"] == identity["id"]:
                return ("That's your own list — use add_standup_task for that. "
                        "Pick a teammate to assign to.")
            target_date, err = _resolve_standup_date((tool_input or {}).get("date"), today)
            if err:
                return err
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, delegated_from) "
                        "VALUES (?, ?, ?, 'pending', ?)",
                        (emp["id"], target_date, task[:500], identity["name"]),
                    )
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: assign_task failed")
                return "(couldn't add that to their standup just now)"
            when_label = "today" if target_date == today else target_date
            jid = _wa_jid(emp.get("whatsapp"))
            notified = _enqueue_outbound(
                jid, f"{identity['name']} put a task on your standup for {when_label}: "
                     f"{task[:400]}"
            ) if jid else False
            tail = "" if notified else " (they'll see it on their standup; no WhatsApp number on file to ping them)"
            return f"Added to {emp['name']}'s standup for {when_label}: {task[:120]}." + tail

        if name == "delegate_my_task" and kind == "employee":
            who = (tool_input or {}).get("name", "")
            q = (tool_input or {}).get("task", "")
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            if emp["id"] == identity["id"]:
                return "That's you — pick a different teammate to hand it to."
            amb: list = []
            m = _match_standup_task(identity["id"], q, ambiguous=amb, prefer="open")
            if not m and amb:
                return _which_one(amb)
            if not m:
                have = _standup_tasks_today(identity["id"])
                names = "; ".join(t["title"] for t in have) or "nothing yet"
                return f"Couldn't find one of your tasks matching '{q}'. Today's list: {names}."
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        "UPDATE standup_tasks SET status='delegated', delegated_to=? WHERE id=?",
                        (emp["name"], m["id"]),
                    )
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, blocker, delegated_from) "
                        "VALUES (?, ?, ?, 'pending', ?, ?)",
                        (emp["id"], today, m["title"][:500],
                         (m.get("blocker") or None), identity["name"]),
                    )
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: delegate_my_task failed")
                return "(couldn't hand that over just now)"
            jid = _wa_jid(emp.get("whatsapp"))
            note = f"{identity['name']} handed you a task for today: {m['title'][:400]}"
            if m.get("blocker"):
                note += f" (blocked by {m['blocker']})"
            _enqueue_outbound(jid, note) if jid else None
            return (f"Handed '{m['title']}' to {emp['name']}. Off your list, "
                    "on theirs for today.")

        if name == "set_leave" and kind == "employee":
            ti = tool_input or {}
            who = str(ti.get("person") or "").strip()
            if who:
                emp = _resolve_employee(who)
                if not emp:
                    names = ", ".join(e["name"] for e in _active_employees())
                    return f"Don't know who '{who}' is. Team: {names}."
                tid, tname = emp["id"], emp["name"]
            else:
                tid, tname = identity["id"], identity["name"]
            sd = str(ti.get("start_date", "")).strip()[:10]
            ed = str(ti.get("end_date", "") or sd).strip()[:10]
            if not (re.match(r"^\d{4}-\d{2}-\d{2}$", sd)
                    and re.match(r"^\d{4}-\d{2}-\d{2}$", ed)):
                return ("(tell me the leave dates — e.g. 'today', 'tomorrow', "
                        "'Thu and Fri', or a range)")
            leave_type = str(ti.get("leave_type", "full")).strip().lower()
            if leave_type not in ("full", "half"):
                leave_type = "full"
            try:
                import leave_store
                row = leave_store.set_leave(
                    tid, sd, ed, reason=str(ti.get("reason", ""))[:200],
                    created_by=identity["id"], leave_type=leave_type)
            except ValueError as e:
                return f"Couldn't request that leave: {e}."
            except Exception:
                logger.exception("whatsapp_agent: set_leave failed")
                return "(couldn't save that leave just now)"
            span = (row["start_date"] if row["start_date"] == row["end_date"]
                    else f"{row['start_date']} to {row['end_date']}")
            whose = "you" if tid == identity["id"] else tname
            names = " and ".join(_first(n) for n in
                                 [e["name"] for e in _active_employees()
                                  if e["id"] in leave_store.approvers_for(tid)]) or "the approvers"
            out = (f"Sent {whose} leave for {span} to {names} for approval, "
                   f"the first decision counts. "
                   f"{'You' if tid == identity['id'] else tname} "
                   f"will stay locked out of standup until it's approved.")
            if row.get("shortfall"):
                out += (f" Heads up: {row['shortfall']:g} of those days are beyond the "
                        f"leave balance, so they would be unpaid.")
            return out

        if name == "get_pending_leave" and kind == "employee":
            import leave_store
            import mailer
            viewer = identity["id"] in leave_store.VIEWER_IDS
            if identity["id"] not in leave_store.APPROVER_IDS and not viewer:
                return "Only Noorish, Vidit, Kshitij and Abhinav can see leave requests waiting for a decision."
            rows = leave_store.list_pending(None if viewer else identity["id"])
            if not rows:
                return "No leave requests waiting for you."
            lines = []
            for r in rows:
                span = r["start_date"] if r["start_date"] == r["end_date"] else f"{r['start_date']} to {r['end_date']}"
                lines.append(f"#{r['id']} {mailer.name_for(r['user_id'])}: {span} ({r['leave_type']})"
                             + (f", {r['reason']}" if r.get("reason") else ""))
            return "\n".join(lines) + ("\n(view only, the approvers decide)" if viewer else "")

        if name == "decide_leave" and kind == "employee":
            import leave_store
            import mailer
            ti = tool_input or {}
            decision = str(ti.get("decision") or "").strip().lower()
            if decision not in ("approve", "reject"):
                return "(decision has to be approve or reject)"
            if identity["id"] not in leave_store.APPROVER_IDS:
                return "Only Noorish, Vidit and Kshitij can approve or reject leave."
            which = str(ti.get("which") or "").strip()
            rows = leave_store.list_pending(identity["id"])
            pick = None
            if re.fullmatch(r"#?\d+", which):
                pick = next((r for r in rows if r["id"] == int(which.lstrip("#"))), None)
            else:
                hits = [r for r in rows if which.lower() and which.lower() in mailer.name_for(r["user_id"]).lower()]
                if len(hits) > 1:
                    return "More than one request fits: " + "; ".join(
                        f"#{r['id']} {mailer.name_for(r['user_id'])} {r['start_date']}" for r in hits) + ". Which one?"
                pick = hits[0] if hits else (rows[0] if len(rows) == 1 and not which else None)
            if not pick:
                own = [r for r in leave_store.list_pending() if r["user_id"] == identity["id"]]
                if own and (not which or re.fullmatch(r"#?\d+", which) and int(which.lstrip("#")) in [r["id"] for r in own]):
                    return "You can't decide your own leave request, one of the other approvers will."
                return "Couldn't find a pending request for you to decide matching that."
            try:
                if decision == "approve":
                    ok = leave_store.approve_leave(pick["id"], identity["id"])
                else:
                    ok = leave_store.reject_leave(pick["id"], identity["id"],
                                                  reason=str(ti.get("reason") or "")[:300])
            except PermissionError as e:
                return f"Not allowed: {e}."
            except ValueError as e:
                return f"Couldn't do that: {e}."
            if not ok:
                cur = leave_store.get_request(pick["id"])
                by = mailer.name_for(cur["decided_by"]) if cur and cur.get("decided_by") else "someone"
                return f"Leave #{pick['id']} was already {cur['status'] if cur else 'decided'} by {by}."
            return (f"Marked leave #{pick['id']} {'approved' if decision == 'approve' else 'rejected'} "
                    f"for {mailer.name_for(pick['user_id'])}. They've been told.")

        if name == "clear_leave" and kind == "employee":
            try:
                import leave_store
                n = leave_store.clear_leave(identity["id"])
            except Exception:
                logger.exception("whatsapp_agent: clear_leave failed")
                return "(couldn't clear that just now)"
            if not n:
                return "You have no still-pending leave request to cancel."
            return (f"Cancelled your pending leave request(s) — {n} "
                    f"entr{'y' if n == 1 else 'ies'} removed. This doesn't "
                    f"touch anything already approved, and doesn't affect "
                    f"anyone else's leave.")

        if name == "get_leave_balance" and kind == "employee":
            try:
                import leave_store
                bal = leave_store.get_balance(identity["id"])
            except Exception:
                logger.exception("whatsapp_agent: get_leave_balance failed")
                return "(couldn't look that up just now)"
            return (f"{bal['base']}-day pool + {bal['comp_earned']} comp-off "
                    f"earned this year, {bal['used']} used -> "
                    f"{bal['remaining']} remaining ({bal['year']}). "
                    f"Overtime pending: {leave_store.fmt_hm(bal.get('ot_pending_hours'))} "
                    f"(every 24h converts to 1 leave day).")

        if name == "set_standing_rule" and kind == "employee":
            rule = str((tool_input or {}).get("rule") or "").strip()
            if not rule:
                return "(no rule text — ask what they want you to always do)"
            day = str((tool_input or {}).get("remind_day") or "").strip()
            tm = str((tool_input or {}).get("remind_time") or "").strip()
            import standing_rules
            if day and not standing_rules.normalize_day(day):
                return ("(that's not a weekday I recognize — pass a real day "
                        "name like 'monday', or leave it out)")
            if tm and not re.match(r"^\d{1,2}:\d{2}$", tm):
                return "(recurring time needs to be HH:MM, 24-hour IST)"
            if day and not tm:
                tm = "10:00"      # a day without a time would never fire
            if tm and not day:
                day = ""          # a time without a day isn't recurring
                tm = ""
            try:
                saved = standing_rules.add_rule(
                    identity["id"], rule, remind_day=day, remind_time=tm)
            except Exception:
                logger.exception("whatsapp_agent: set_standing_rule failed")
                return "(couldn't save that rule just now)"
            out = f"Added a standing rule: {saved['rule'][:160]}"
            if saved["remind_day"]:
                out += (f" — and I'll bring it up every "
                        f"{saved['remind_day']} at {saved['remind_time']}.")
            return out

        if name == "list_standing_rules" and kind == "employee":
            import standing_rules
            rules = standing_rules.list_rules(identity["id"])
            if not rules:
                return ("No standing rules saved for them yet.")
            lines = []
            for i, r in enumerate(rules, 1):
                line = f"{i}. {r['rule']}"
                if r["remind_day"]:
                    line += f" (every {r['remind_day']} at {r['remind_time']})"
                lines.append(line)
            return "Standing rules:\n" + "\n".join(lines)

        if name == "remove_standing_rule" and kind == "employee":
            which = str((tool_input or {}).get("which") or "").strip()
            if not which:
                return "(which rule? ask them to name it)"
            import standing_rules
            try:
                gone = standing_rules.remove_rule(identity["id"], which)
            except Exception:
                logger.exception("whatsapp_agent: remove_standing_rule failed")
                return "(couldn't remove that rule just now)"
            if not gone:
                return (f"No standing rule matching '{which[:60]}'. "
                        "Use list_standing_rules to see them.")
            return f"Cleared the standing rule: {gone['rule'][:160]}"

        if name == "get_followups" and kind == "employee":
            who = str((tool_input or {}).get("person") or "").strip()
            if who:
                emp = _resolve_employee(who)
                if not emp:
                    return f"Don't know who '{who}' is."
            else:
                emp = {"id": identity["id"], "name": identity["name"]}
            try:
                import followups
                names_by_id = {e.get("id"): e.get("name")
                               for e in _active_employees()}
                items = followups.loose_ends_for(emp, names_by_id=names_by_id)
            except Exception:
                logger.exception("whatsapp_agent: get_followups failed")
                return "(couldn't check that just now)"
            whose = "Nothing" if not who else f"Nothing for {emp['name']}"
            if not items:
                return (f"{whose} looks stalled — no tasks rolling over, no "
                        "stuck approvals, nothing handed off and forgotten.")
            head = ("Stalled items" if not who
                    else f"Stalled items for {emp['name']}")
            return head + ":\n" + "\n".join(f"- {i['text']}" for i in items)

        if name == "send_group_message" and kind == "employee":
            if in_group:
                return "We're already in the group, so just say it here."
            msg = (tool_input or {}).get("message", "").strip()
            if not msg:
                return "(no message text — ask what to post)"
            if not _team_group_jid():
                return ("No team group is set up for me to post to. Add one "
                        "with the whatsapp_team_group setting or the group "
                        "allow-list.")
            _set_pending_broadcast(identity["id"], msg[:1500])
            return (f"Ready to post this to the team group:\n\n\"{msg[:400]}\"\n\n"
                    "Reply 'yes' to send it, or 'no' to cancel.")

        if name == "schedule_group_message" and kind == "employee":
            if in_group:
                return "We're already in the group, so just say it here."
            msg = (tool_input or {}).get("message", "").strip()
            when = (tool_input or {}).get("when", "").strip()
            if not msg:
                return "(no message text — ask what to post)"
            if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$", when):
                return ("(need a real time — figure out the exact date and "
                         "time they mean and pass it as YYYY-MM-DD HH:MM)")
            if when[:16] <= f"{today} {utils.now_ist()[:5]}":
                return "That time's already passed — ask when they actually want it sent."
            if not _team_group_jid():
                return ("No team group is set up for me to post to. Add one "
                        "with the whatsapp_team_group setting or the group "
                        "allow-list.")
            _set_pending_broadcast(identity["id"], msg[:1500], send_after=when)
            return (f"Ready to schedule this for {when} (IST) in the team group:\n\n"
                    f"\"{msg[:400]}\"\n\nReply 'yes' to confirm, or 'no' to cancel.")

        if name == "remind_teammate" and kind == "employee":
            if in_group:
                return ("Reminders only work from our private chat, not the "
                        "group. Message me directly and I'll ping them.")
            who = (tool_input or {}).get("name", "")
            msg = (tool_input or {}).get("message", "").strip()
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            if not msg:
                return "(no reminder text — ask what to remind them about)"
            if emp["id"] == identity["id"]:
                return "That's you — use remind_me to set yourself a reminder instead."
            jid = _wa_jid(emp.get("whatsapp"))
            if not jid:
                return f"{emp['name']} has no WhatsApp number on file, so I can't reach them."
            when = str((tool_input or {}).get("when") or "").strip()
            if when:
                if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$", when):
                    return "(need a real time: pass `when` as YYYY-MM-DD HH:MM IST)"
                if when[:16] <= f"{today} {utils.now_ist()[:5]}":
                    return "That time's already passed. Ask when they want it sent."
            if wa_outbox.enqueue(jid, f"Reminder from {identity['name']}: {msg[:600]}", send_after=when or None):
                return (f"Sent {emp['name']} a reminder about that." if not when
                        else f"Scheduled a reminder to {emp['name']} for {when} (IST): {msg[:120]}")
            return "(couldn't queue that reminder just now)"

        if name == "remind_me" and kind == "employee":
            msg = (tool_input or {}).get("message", "").strip()
            when = (tool_input or {}).get("when", "").strip()
            if not msg:
                return "(no reminder text — ask what to remind them about)"
            if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$", when):
                return ("(need a real time — figure out the exact date and "
                         "time they mean and pass it as YYYY-MM-DD HH:MM)")
            if when[:16] <= f"{today} {utils.now_ist()[:5]}":
                return "That time's already passed — ask when they actually want the reminder."
            jid = _self_jid(identity)
            if not jid:
                return "You have no WhatsApp number on file for me to remind you at."
            if wa_outbox.enqueue(jid, f"Reminder: {msg[:600]}", send_after=when):
                return (f"Added a reminder for {when} (IST): {msg[:120]}"
                        + (" -- it will come to your private chat, not the group." if in_group else ""))
            return "(couldn't set that reminder just now)"

        if name == "message_multiple" and kind == "employee":
            if in_group:
                return ("This only works from our private chat, not the "
                        "group. Message me directly and I'll send it to each of them.")
            raw_names = (tool_input or {}).get("names") or []
            if isinstance(raw_names, str):
                raw_names = [n.strip() for n in re.split(r",| and ", raw_names) if n.strip()]
            msg = (tool_input or {}).get("message", "").strip()
            if not msg:
                return "(no message text — ask what to send them)"
            if not raw_names:
                return "(no names given — ask who this should go to)"
            resolved, unknown, no_number, seen_ids = [], [], [], set()
            for who in raw_names:
                emp = _resolve_employee(who)
                if not emp:
                    unknown.append(who)
                    continue
                if emp["id"] == identity["id"]:
                    continue  # skip the sender themself, silently
                if emp["id"] in seen_ids:
                    continue
                seen_ids.add(emp["id"])
                if not emp.get("whatsapp"):
                    no_number.append(emp["name"])
                    continue
                resolved.append(emp)
            if not resolved:
                bits = []
                if unknown:
                    bits.append(f"didn't recognize: {', '.join(unknown)}")
                if no_number:
                    bits.append(f"no WhatsApp number on file for: {', '.join(no_number)}")
                extra = f" ({'; '.join(bits)})" if bits else ""
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Couldn't find anyone to send this to{extra}. Team: {names}."
            _set_pending_multi(identity["id"], msg[:1500], resolved)
            who_txt = ", ".join(e["name"] for e in resolved)
            note = ""
            if unknown or no_number:
                bits = []
                if unknown:
                    bits.append(f"skipping unrecognized: {', '.join(unknown)}")
                if no_number:
                    bits.append(f"skipping (no number): {', '.join(no_number)}")
                note = f"\n({'; '.join(bits)})"
            return (f"Ready to send this to {who_txt}:\n\n\"{msg[:400]}\"{note}\n\n"
                    "Reply 'yes' to send it, or 'no' to cancel.")

        if name == "place_announcement_call" and kind == "employee":
            if in_group:
                return ("Calls only work from our private chat, not the "
                        "group. Message me directly and I'll place it.")
            who = (tool_input or {}).get("name", "")
            msg = (tool_input or {}).get("message", "").strip()
            emp = _resolve_employee(who)
            if not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            if not msg:
                return "(no message for the call — ask what it should say)"
            if emp["id"] == identity["id"]:
                return "That's you — no need to call yourself through me."
            if not emp.get("whatsapp"):
                return f"{emp['name']} has no WhatsApp number on file, so I can't call them."
            if wa_outbox.enqueue_call(emp["whatsapp"],
                                      f"Message from {identity['name']}: {msg[:600]}"):
                return f"Calling {emp['name']} now to say that."
            return "(couldn't queue that call just now)"

        if name == "search_knowledge_base" and kind == "employee":
            q = (tool_input or {}).get("query", "")
            hits = _kb_search(q)
            if not hits:
                return f"Nothing in the knowledge base matched '{q}'."
            out = []
            for h in hits:
                snippet = " ".join((h.get("chunk") or "").split())[:400]
                out.append(f"[{h.get('filename') or 'doc'}] {snippet}")
            return "\n\n".join(out)

        if name == "get_team_overview" and kind == "employee":
            clients = _all_clients()
            open_tasks = overdue = 0
            if notion_store.is_configured():
                try:
                    all_tasks = notion_store.list_tasks()
                except Exception:
                    all_tasks = []
                for t in all_tasks:
                    st = (t.get("status") or "").lower()
                    if st in ("done", "approved", "posted", "final", "need_for_approval"):
                        continue
                    open_tasks += 1
                    if _is_overdue(t.get("due_date"), today):
                        overdue += 1
            return (
                f"Clients: {len(clients)}\n"
                f"Open tasks: {open_tasks}\n"
                f"Overdue: {overdue}"
            )

        if name == "create_task" and kind == "employee":
            title = (tool_input or {}).get("title", "").strip()
            if not title:
                return "(no task title — ask what the task is)"
            cli = (tool_input or {}).get("client", "").strip()
            who = (tool_input or {}).get("assignee", "").strip()
            due = (tool_input or {}).get("due_date", "").strip()
            if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
                due = ""
            emp = _resolve_employee(who) if who else None
            if who and not emp:
                names = ", ".join(e["name"] for e in _active_employees())
                return f"Don't know who '{who}' is. Team: {names}."
            c = _find_client(cli) if cli else None
            cname = (c or {}).get("name", "") or cli
            cnid = (c or {}).get("notion_id", "")
            nid = ""
            if notion_store.is_configured():
                try:
                    res = notion_store.create_task(
                        title=title, client_name=cname, client_notion_id=cnid,
                        assigned_to=(emp["name"] if emp else ""),
                        due_date=due, creation_date=today,
                    )
                    nid = (res or {}).get("notion_id", "")
                except Exception:
                    logger.exception("whatsapp_agent: create_task notion failed")
            su_uid = emp["id"] if emp else identity["id"]
            standup_ok = False
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        "INSERT INTO standup_tasks (user_id, date, title, status, notion_id, due_date, delegated_from) "
                        "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                        (su_uid, today, title[:500], (nid or None), (due or None),
                         (identity["name"] if emp and emp["id"] != identity["id"] else None)),
                    )
                conn.close()
                standup_ok = True
                if su_uid == identity["id"]:
                    _auto_checkin(su_uid)
            except Exception:
                logger.exception("whatsapp_agent: create_task standup insert failed")
            if not standup_ok:
                # Don't claim success -- this is the ONLY place the task
                # actually lands for "what are my tasks" / the Standup
                # screen. A Notion page may still have been created above
                # (nid set); tell them that honestly instead of pretending
                # the whole thing worked.
                if nid:
                    return (f"Created '{title[:120]}' on the board, but couldn't add it "
                            "to the standup list -- try again or add it there directly.")
                return f"Couldn't create '{title[:120]}' -- something went wrong, try again."
            if emp and emp["id"] != identity["id"]:
                jid = _wa_jid(emp.get("whatsapp"))
                if jid:
                    _enqueue_outbound(
                        jid,
                        f"{identity['name']} created a task for you"
                        + (f" ({cname})" if cname else "") + f": {title[:300]}"
                        + (f", due {due}" if due else ""),
                    )
            bits = ["Created: " + title[:120]]
            if cname:
                bits.append(f"client {cname}")
            bits.append(f"for {emp['name']}" if emp else "unassigned")
            if due:
                bits.append(f"due {due}")
            if not nid and notion_store.is_configured():
                bits.append("(on the standup only, Notion write failed)")
            return ", ".join(bits) + "."

        if name == "set_task_meta" and kind == "employee":
            q = (tool_input or {}).get("task", "")
            blk = (tool_input or {}).get("blocker", None)
            due = (tool_input or {}).get("due_date", None)
            if due not in (None, ""):
                # A real calendar date, not just a date-shaped string: Notion
                # rejects the WHOLE page update on "2026-13-45".
                try:
                    datetime.strptime(str(due), "%Y-%m-%d")
                except ValueError:
                    return "Give the date as a real YYYY-MM-DD date."
            amb: list = []
            m = _match_standup_task(identity["id"], q, ambiguous=amb)
            if not m and amb:
                return _which_one(amb)
            if not m:
                have = _standup_tasks_today(identity["id"])
                names = "; ".join(t["title"] for t in have) or "nothing yet"
                return f"Couldn't find a task matching '{q}'. Today's list: {names}."
            sets, params = [], []
            if blk is not None:
                sets.append("blocker=?")
                params.append(str(blk)[:300] or None)
            if due is not None:
                sets.append("due_date=?")
                params.append(str(due) or None)
            if not sets:
                return "(nothing to change — give a blocker or a due date)"
            try:
                conn = get_connection()
                with conn:
                    conn.execute(
                        f"UPDATE standup_tasks SET {', '.join(sets)} WHERE id=?",
                        (*params, m["id"]),
                    )
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: set_task_meta failed")
                return "(couldn't update that just now)"
            if due is not None and m.get("notion_id"):
                try:
                    notion_store.update_task(m["notion_id"], due_date=str(due))
                except Exception:
                    logger.debug("whatsapp_agent: notion due mirror failed", exc_info=True)
            done_bits = []
            if blk is not None:
                done_bits.append(f"blocker: {blk}" if blk else "blocker cleared")
            if due is not None:
                done_bits.append(f"due {due}" if due else "due date cleared")
            return f"Updated '{m['title']}': " + ", ".join(done_bits) + "."

        if name == "search_web" and kind == "employee":
            import web_search
            return web_search.answer((tool_input or {}).get("query", ""))

        if name == "get_my_dashboard" and kind == "employee":
            import morning_dash
            try:
                return morning_dash.build(identity)
            except Exception:
                logger.exception("whatsapp_agent: dashboard failed")
                return "(couldn't build your dashboard just now)"

        if name == "get_management_view" and kind == "employee":
            import mgmt_views
            return mgmt_views.view((tool_input or {}).get("view", ""), identity)

        if name == "reschedule_task" and kind == "employee":
            q = (tool_input or {}).get("task", "")
            to = str((tool_input or {}).get("to", "")).strip()
            try:
                datetime.strptime(to, "%Y-%m-%d")
            except ValueError:
                return "Give the new date as a real YYYY-MM-DD date."
            if to < today:
                return "That date is in the past. Pick today or later."
            if (datetime.strptime(to, "%Y-%m-%d") - datetime.strptime(today, "%Y-%m-%d")).days > 366:
                return "That is more than a year away. Pick a closer date."
            amb: list = []
            m = _match_standup_task(identity["id"], q, prefer="open", ambiguous=amb)
            if not m and amb:
                return _which_one(amb)
            if not m:
                have = _standup_tasks_today(identity["id"])
                names = "; ".join(t["title"] for t in have) or "nothing yet"
                return f"Couldn't find a task matching '{q}'. Today's list: {names}."
            if str(m.get("status") or "").lower() in ("done", "completed", "complete", "need_for_approval"):
                return f"'{m['title']}' is already done, so there is nothing to move."
            try:
                conn = get_connection()
                with conn:
                    if to > today:
                        conn.execute("UPDATE standup_tasks SET due_date=?, date=? WHERE id=?", (to, to, m["id"]))
                    else:
                        conn.execute("UPDATE standup_tasks SET due_date=? WHERE id=?", (to, m["id"]))
                conn.close()
            except Exception:
                logger.exception("whatsapp_agent: reschedule_task failed")
                return "(couldn't move that just now)"
            if m.get("notion_id"):
                try:
                    notion_store.update_task(m["notion_id"], due_date=to)
                except Exception:
                    logger.debug("whatsapp_agent: notion reschedule mirror failed", exc_info=True)
            nice = datetime.strptime(to, "%Y-%m-%d").strftime("%a %d %b")
            tail = "" if to == today else " It is off today's list and will show on that day."
            left = _standup_tasks_today(identity["id"])
            if not left and to != today:
                # Lumina locks the app for anyone with nothing on today's standup
                tail += " Today's list is now empty; add something to it or Lumina will stay locked until you do."
            return (f"Updated '{m['title']}': moved to {nice}.{tail}\n\n"
                    + _format_standup(left, "Your"))

        if name == "get_task_schedule" and kind == "employee":
            when = str((tool_input or {}).get("when", "week")).strip().lower()
            cf = str((tool_input or {}).get("client", "")).strip().lower()
            pf = str((tool_input or {}).get("person", "")).strip().lower()
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            wk_end = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
            od_from = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")

            def _keep(t):
                if (t.get("status") or "").strip().lower().replace(" ", "_") in _CLOSED_STATUSES:
                    return False
                due = str(t.get("due_date") or "")[:10]
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
                    return False
                # overdue = the last 14 days only (older unfinished items are old backlog,
                # never listed); "this week" = today through +7, not everything ever late
                if when == "overdue" and not (od_from <= due < today):
                    return False
                if when in ("today", "day") and due != today:
                    return False
                if when not in ("overdue", "today", "day") and not (today <= due <= wk_end):
                    return False
                if cf and cf not in (t.get("client_name") or "").lower():
                    return False
                if pf and pf not in (t.get("assigned_to") or "").lower():
                    return False
                return True

            hits = sorted((t for t in tasks if _keep(t)), key=lambda t: str(t.get("due_date")))
            if not hits:
                return "Nothing matches that."
            lines = []
            for t in hits[:25]:
                od = " OVERDUE" if _is_overdue(t.get("due_date"), today) else ""
                lines.append(
                    f"{t.get('title')} | {t.get('client_name') or 'no client'} | "
                    f"{t.get('assigned_to') or 'unassigned'} | due {t.get('due_date')}{od}"
                )
            head = {"overdue": "Overdue", "today": "Due today", "day": "Due today"}.get(when, "This week")
            out = f"{head} ({len(hits)}):\n" + "\n".join(lines)
            if len(hits) > 25:
                out += f"\n(+{len(hits) - 25} more)"
            if when not in ("overdue",):
                late = [t for t in tasks if (t.get("status") or "").strip().lower().replace(" ", "_") not in _CLOSED_STATUSES
                        and od_from <= str(t.get("due_date") or "")[:10] < today
                        and (not cf or cf in (t.get("client_name") or "").lower())
                        and (not pf or pf in (t.get("assigned_to") or "").lower())]
                if late:
                    out += f"\nAlso overdue (last 14 days): {len(late)}. Ask 'what is overdue' to see them."
            return out

        if name == "read_group_chat" and kind == "employee":
            try:
                import group_memory
                hours = min(max(float((tool_input or {}).get("hours") or 48), 1), 24 * 30)
                got = group_memory.search_text(
                    str((tool_input or {}).get("query") or ""), hours=hours,
                    group_ids=_group_allowlist())
            except Exception:
                logger.exception("whatsapp_agent: read_group_chat failed")
                return "(couldn't read the group chat just now)"
            if got and isinstance(turn, dict):
                turn["group_log"] = turn.get("group_log", "") + "\n" + got
            return got or "Nothing in the group chat matches that in that time window."

        if name == "get_group_commitments" and kind == "employee":
            try:
                import group_intel
                st = str((tool_input or {}).get("status") or "open").lower()
                rows = group_intel.list_commitments(
                    str((tool_input or {}).get("person") or "").strip(),
                    st if st in ("open", "done", "dropped") else "all")
                return group_intel.format_commitments(rows)
            except Exception:
                logger.exception("whatsapp_agent: get_group_commitments failed")
                return "(couldn't read the tracked group promises just now)"

        if name == "recall_group_facts" and kind == "employee":
            try:
                import group_intel
                return group_intel.search_facts(str((tool_input or {}).get("query") or ""),
                                                str((tool_input or {}).get("client") or ""))
            except Exception:
                logger.exception("whatsapp_agent: recall_group_facts failed")
                return "(couldn't read the group decisions just now)"

        if name == "reject_group_commitment" and kind == "employee":
            try:
                import group_intel
                cid = _find_group_commitment(str((tool_input or {}).get("which") or ""))
                if cid is None:
                    return "Couldn't find an open group promise matching that."
                if group_intel.reject_commitment(cid, str((tool_input or {}).get("why") or "")):
                    return f"Marked group promise #{cid} as wrong, and I'll avoid that kind of mistake."
                return f"Group promise #{cid} was already closed."
            except Exception:
                logger.exception("whatsapp_agent: reject_group_commitment failed")
                return "(couldn't update that just now)"

        if name == "add_group_commitment" and kind == "employee":
            try:
                import group_intel
                action = re.sub(r"\s+", " ", str((tool_input or {}).get("action") or "")).strip()
                if not action:
                    return "(no action text, ask what the promise was)"
                cid = group_intel.add_commitment(str((tool_input or {}).get("owner") or ""),
                                                 action, str((tool_input or {}).get("due") or ""))
                return f"Added group promise #{cid}: {action[:120]}. I'll check on it."
            except Exception:
                logger.exception("whatsapp_agent: add_group_commitment failed")
                return "(couldn't add that just now)"

        if name == "get_calendar" and kind == "employee":
            try:
                import gcal
                if not gcal.configured():
                    return ("Calendar isn't connected yet. Share the Google Calendar with "
                            "the Lumina service account and add its id in settings.")
                days = min(max(float((tool_input or {}).get("days") or 1), 1), 14)
                txt = gcal.format_events(gcal.upcoming(days=days))
                return txt or "Nothing on the calendar in that time."
            except Exception:
                logger.exception("whatsapp_agent: get_calendar failed")
                return "(couldn't read the calendar just now)"

        if name == "get_my_brief" and kind == "employee":
            try:
                import pa_brief
                return pa_brief.build_brief(identity)
            except Exception:
                logger.exception("whatsapp_agent: get_my_brief failed")
                return "(couldn't put the brief together just now)"

        if name == "resolve_group_commitment" and kind == "employee":
            try:
                import group_intel
                which = str((tool_input or {}).get("which") or "").strip()
                status = "dropped" if str((tool_input or {}).get("status") or "").lower().startswith("drop") else "done"
                m = re.search(r"\d+", which)
                cid = int(m.group(0)) if m and len(re.sub(r"\D", "", which)) == len(which.strip("# ")) else None
                if cid is None:
                    best = None
                    for r in group_intel.list_commitments("", "open", 60):
                        sc = group_intel._overlap(which, r["action"])
                        if sc >= 0.5 and (best is None or sc > best[0]):
                            best = (sc, r)
                    cid = best[1]["id"] if best else None
                if cid is None:
                    return f"Couldn't find an open group promise matching '{which[:60]}'."
                if group_intel.mark(cid, status, f"closed by {identity.get('name', '?')} on WhatsApp"):
                    return f"Marked group promise #{cid} {status}."
                return f"Group promise #{cid} was already closed."
            except Exception:
                logger.exception("whatsapp_agent: resolve_group_commitment failed")
                return "(couldn't update that group promise just now)"

        if name == "get_attendance" and kind == "employee":
            import attendance_report as ar
            ti = tool_input or {}

            def _att_date(raw):
                """YYYY-MM-DD, or plain words ('friday', 'last monday', 'yesterday',
                '16/10', '12 oct') worked out in code so the model never does date maths."""
                raw = str(raw or "").strip()
                if not raw:
                    return None, None
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                    d = ar._d(raw)
                    return (d, d) if d else (None, None)
                w = ar.parse_when(raw, today)
                return w if w else (None, None)

            f1, f2 = _att_date(ti.get("date"))
            if str(ti.get("date") or "").strip() and f1 is None:
                return "(couldn't read that date -- pass YYYY-MM-DD, or words like 'friday' / 'last monday')"
            d_from = f1 or ar._d(today)
            d_to = f2 or d_from
            if str(ti.get("to_date") or "").strip():
                t1, t2 = _att_date(ti.get("to_date"))
                if t1 is None:
                    return "(couldn't read the end date -- pass YYYY-MM-DD)"
                d_to = t2
            roster = _active_employees()
            person = None
            if str(ti.get("person") or "").strip():
                person = _resolve_employee(str(ti["person"]))
                if not person:
                    return f"(no teammate matches '{ti['person']}')"
            try:
                return ar.report(today, roster, d_from, d_to, person)
            except Exception:
                logger.exception("whatsapp_agent: get_attendance failed")
                return "(couldn't read attendance just now)"

        if name == "set_attendance" and kind == "employee":
            action = str((tool_input or {}).get("action", "")).strip().lower()
            try:
                if action.startswith("in") or action in ("here", "arrive", "start"):
                    from routes.attendance import _attendance_checkin
                    _attendance_checkin(identity["id"])
                    return "Checked you in for today."
                if action.startswith("out") or action in ("leave", "leaving", "done", "bye"):
                    from routes.attendance import _attendance_checkout
                    _attendance_checkout(identity["id"])
                    return "Checked you out. See you tomorrow."
            except Exception:
                logger.exception("whatsapp_agent: set_attendance failed")
                return "(couldn't update attendance just now)"
            return "Say 'in' to check in or 'out' to check out."

        if name == "get_daily_brief" and kind == "employee":
            win = str((tool_input or {}).get("window", "")).strip().lower()
            win = "morning" if win in ("morning", "am", "today", "ahead") else "evening"
            try:
                from routes.companion import _build_digest
                return _build_digest(win).get("text") or "Nothing to report."
            except Exception:
                logger.exception("whatsapp_agent: daily brief failed")
                return "(couldn't build the brief just now)"

        if name == "get_pending_approvals" and kind == "employee":
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            pend = [t for t in tasks
                    if (t.get("status") or "").strip().lower() in _APPROVAL_STATUSES]
            if not pend:
                return "Nothing is waiting on approval right now."
            lines = [
                f"{t.get('title')} | {t.get('client_name') or 'no client'} | "
                f"{t.get('assigned_to') or 'unassigned'}"
                for t in pend[:25]
            ]
            return (f"Waiting on approval ({len(pend)}):\n" + "\n".join(lines)
                    + "\n\nSay 'approve <name>' or 'reject <name>'.")

        if name == "review_task" and kind == "employee":
            q = (tool_input or {}).get("task", "")
            dec = str((tool_input or {}).get("decision", "approve")).strip().lower()
            dec = "reject" if dec in ("reject", "rejected", "deny", "decline", "send back") else "approve"
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            pend = [t for t in tasks
                    if (t.get("status") or "").strip().lower() in _APPROVAL_STATUSES]
            if not pend:
                return "Nothing is waiting on approval right now."
            m, ties = _match_approval(q, pend)
            if not m:
                cand = ties or pend
                opts = "; ".join(f"{t.get('client_name') or 'no client'}: {t.get('title')}" for t in cand[:10])
                return (("More than one fits, ask which one they mean: " if ties
                         else "Not sure which one you mean. Pending: ") + opts)
            try:
                ok = notion_store.update_task(
                    m["notion_id"], status=("approved" if dec == "approve" else "in_progress")
                )
            except Exception:
                logger.exception("whatsapp_agent: review_task failed")
                ok = False
            if not ok:
                return "(Notion didn't accept that update)"
            return (f"Approved: {m.get('title')}" if dec == "approve"
                    else f"Sent back for changes: {m.get('title')}")

        if name == "send_sticker" and kind == "employee":
            mood = str((tool_input or {}).get("mood", "")).strip().lower()
            if turn is not None:
                turn["sticker"] = mood or "random"
            return "A sticker will go out with your reply. Write the text reply now."

        if name == "list_capabilities":
            if kind == "client":
                return ("I can look up your deliverables and their status, due "
                        "dates, and the content details (post type, brief, "
                        "script, caption). Just ask.")
            extra = "" if in_group else (
                " In a private chat I can also send a reminder to a teammate, "
                "set a personal reminder that pings you back at a time you "
                "give me, message several teammates the same thing at once, "
                "place an actual voice call that speaks a message aloud, "
                "post an announcement to the group now or schedule one for "
                "later (both with a confirm step), check you in or out, and "
                "give you the full daily brief.")
            return (
                "Ask me about: your standup tasks, a teammate's tasks, the whole "
                "team's standup, any client's tasks and content calendar, what's "
                "overdue or due this week, what's pending approval, the "
                "knowledge base, and general web search.\n"
                "I can also: add a task to your or a teammate's standup, create "
                "a real board task, mark tasks done, add a blocker or move a due "
                "date, delegate a task, approve or send back a task, request "
                "leave for you or a teammate (exempts them from the standup "
                "lock and nudges once Noorish/HR approves it), "
                "and show who's checked in.\n"
                "Tell me a rule once and I'll keep it: 'from now on always...', "
                "'never do X without asking me', 'every Monday remind me "
                "to...'. Ask what rules you've set or drop one any time.\n"
                "I also keep an eye on what's slipping and will message you "
                "first if a task has been rolling over for days, something you "
                "handed a teammate hasn't moved, or something's stuck waiting "
                "on approval. Ask 'what am I forgetting' to see it on "
                "demand." + extra
            )

        if name == "get_recent_actions" and kind == "employee":
            lim = (tool_input or {}).get("limit", 15)
            try:
                lim = max(1, min(int(lim), 40))
            except Exception:
                lim = 15
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT created_at, sender_name, action, detail FROM wa_action_log "
                    "ORDER BY id DESC LIMIT ?", (lim,),
                )
                rows = cur.fetchall()
                conn.close()
            except Exception:
                rows = []
            if not rows:
                return "No logged actions yet."
            return "Recent actions:\n" + "\n".join(
                f"{str(r[0])[:16].replace('T', ' ')}  {r[1]}: {r[2]}"
                + (f" — {str(r[3])[:80]}" if r[3] else "")
                for r in rows
            )

        if name == "get_content_calendar" and kind == "employee":
            try:
                days = max(1, min(int((tool_input or {}).get("days", 7) or 7), 31))
            except Exception:
                days = 7
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            end = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")

            def _social(t):
                ty = (t.get("type") or t.get("service") or "").lower()
                return ("social" in ty) or bool(re.search(
                    r"\[(story|reel|static|carousel|post|video)\]",
                    t.get("title") or "", re.I))

            rows = [t for t in tasks
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", str(t.get("due_date") or "")[:10])
                    and today <= str(t.get("due_date"))[:10] <= end
                    and _social(t)
                    and (t.get("status") or "").strip().lower() not in ("cancelled", "canceled")]
            if not rows:
                return f"Nothing scheduled to post in the next {days} days."
            rows.sort(key=lambda t: (str(t.get("due_date"))[:10], t.get("client_name") or ""))
            out, cur_day = [], None
            for t in rows:
                d = str(t.get("due_date"))[:10]
                if d != cur_day:
                    out.append(f"\n{d}:")
                    cur_day = d
                cap = "caption ready" if (t.get("caption") or "").strip() else "no caption"
                out.append(
                    f"  {t.get('client_name') or '?'} | {t.get('type') or 'post'} | "
                    f"{t.get('title')} | {t.get('assigned_to') or 'unassigned'} | "
                    f"{(t.get('status') or '').lower() or 'planned'} | {cap}"
                )
            return f"Posting in the next {days} days ({len(rows)}):" + "\n".join(out)

        if name == "get_post_content" and kind == "employee":
            q = (tool_input or {}).get("task", "").strip()
            person = (tool_input or {}).get("person", "").strip()
            if not q:
                return "Which task? Give me the title or part of it."
            try:
                tasks = notion_store.list_tasks() if notion_store.is_configured() else []
            except Exception:
                tasks = []
            if not tasks:
                return "Can't reach Notion right now to look that up."

            if person:
                emp = _resolve_employee(person)
                who = (emp["name"] if emp else person).lower()
            else:
                who = identity["name"].lower()
            pool = [t for t in tasks if who in (t.get("assigned_to") or "").lower()]
            match = _find_notion_task(q, pool) or _find_notion_task(q, tasks)
            if not match:
                return (f"Can't find a task matching '{q}'"
                        + (f" assigned to {who}" if pool else "") + ".")

            lines = [
                f"{match.get('title')} -- {match.get('client_name') or 'no client'}",
                f"{match.get('assigned_to') or 'unassigned'} | "
                f"{(match.get('status') or 'planned')} | due {match.get('due_date') or '?'}",
            ]
            fields = [
                ("Idea", match.get("idea")),
                ("Script/Copy", match.get("scripts_copy")),
                ("Caption", match.get("caption")),
                ("Content", match.get("content")),
                ("Link", match.get("link")),
            ]
            had_any = False
            for label, val in fields:
                v = (val or "").strip()
                if v:
                    had_any = True
                    lines.append(f"\n{label}:\n{v[:1200]}")
            if not had_any:
                lines.append("\n(Nothing's been filled in on this row yet -- Idea/Script/Caption are all empty.)")
            return "\n".join(lines)

        if name == "get_client_status" and kind == "employee":
            cli = (tool_input or {}).get("client", "").strip()
            if not cli:
                return "Which client? Use get_clients for the list."
            import client_card
            return client_card.card(cli)

        return f"(tool '{name}' is not available to you)"
    except Exception:
        logger.exception("whatsapp_agent: tool %s failed", name)
        return f"(couldn't run '{name}' just now)"


# ── System prompt ───────────────────────────────────────────────────────────

def _org_block() -> str:
    """MMGA org reference (org_profile.py) for employees. Static between edits,
    so it sits inside the cached system-prompt block."""
    try:
        import org_profile
        return org_profile.prompt_block()
    except Exception:
        logger.debug("whatsapp_agent: org block failed", exc_info=True)
        return ""


def _standing_rules_block(identity: dict) -> str:
    """This person's saved standing instructions, as a prompt block.

    Lives inside the CACHED system-prompt block on purpose: unlike the
    semantic-memory recall (which varies with every message and so is
    appended separately), a rule list only changes when someone actually
    sets or drops a rule, so cache invalidation here is rare and the
    cheaper cached path is the right one.
    """
    try:
        import standing_rules
        block = standing_rules.rules_text(identity.get("id") or "")
    except Exception:
        logger.debug("whatsapp_agent: standing-rules lookup failed", exc_info=True)
        return ""
    return block + "\n" if block else ""


def compose_followup(employee: dict, items: list) -> str:
    """Write the unprompted nudge for followups.py's sweep.

    Separate from handle_message: there is no inbound message and no tool
    loop here, just one short call that turns already-detected loose ends
    into something that reads like the assistant noticed. Returns "" on any
    failure so the caller falls back to plain deterministic text -- a
    proactive nudge must never depend on an LLM call succeeding.
    """
    if not items:
        return ""
    budget = check_budget_available()
    if not budget.get("allowed"):
        return ""

    identity = {"kind": "employee", "id": employee.get("id"),
                "name": employee.get("name"), "role": employee.get("role", "")}
    facts = "\n".join(f"- {i['text']}" for i in items)
    model = get_model_for_task("whatsapp")
    usage: dict = {}
    try:
        resp = _llm_create(
            model,
            [{
                "type": "text",
                "text": _system_prompt(identity) + (
                    "\nRIGHT NOW you are not replying to anything -- you "
                    "noticed these yourself and are messaging first, "
                    "unprompted. Open in a way that makes that obvious. "
                    "Mention every item below and nothing else; do not invent "
                    "any task, date, name or number that isn't in the list. "
                    "No greeting boilerplate, no offer to help, no questions "
                    "back, EXCEPT an item that says to ask whether something "
                    "got done: ask that one plainly. Two or three short lines "
                    "total. "
                    "You have NO tools in this turn and nothing to look up: "
                    "everything you need is in the list. Never write tool "
                    "calls, XML tags, or narration like 'let me check' or "
                    "'based on the data' -- output ONLY the message text "
                    "that will be sent, nothing before or after it."
                ),
            }],
            None,
            [{
                "role": "user",
                "content": ("Loose ends you just noticed for "
                            f"{employee.get('name')}:\n{facts}\n\n"
                            "Write the message you'd send them."),
            }],
            300,
            usage,
        )
        text = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", "") == "text"
        ).strip()
        _record_llm_usage(model, usage, f"wa_followup_{employee.get('id')}")
        # Strip a stray <REMEMBER> tag -- the prompt mentions the mechanism, and
        # there's no inbound message here that could justify saving anything.
        text = re.sub(r'\s*<REMEMBER>[\s\S]*?</REMEMBER>\s*', ' ', text).strip()
        text = _clean_followup(text)
        return _humanize(text) if text else ""
    except Exception:
        logger.exception("whatsapp_agent: compose_followup failed")
        return ""


_LEAK_MARKERS = re.compile(r'<\s*/?\s*(function_calls|invoke|parameter|antml)', re.I)
_META_OPENERS = re.compile(
    r"^\s*(i need to|i'll |i will |let me|based on|first,? i|checking|verifying)", re.I)


def _clean_followup(text: str) -> str:
    """The composer has no tools, but its prompt describes them, so the model
    sometimes narrates ("I need to verify...") or writes fake tool-call XML
    before the real message. Never send that. Salvage the text after the last
    '---' divider if there is one, otherwise return "" so the caller uses the
    deterministic fallback."""
    text = (text or "").strip()
    if not text:
        return ""
    leaked = bool(_LEAK_MARKERS.search(text)) or bool(_META_OPENERS.match(text))
    if not leaked:
        return text
    parts = re.split(r'\n\s*-{3,}\s*\n', text)
    if len(parts) > 1:
        tail = parts[-1].strip()
        if tail and not _LEAK_MARKERS.search(tail) and not _META_OPENERS.match(tail):
            return tail
    logger.warning("whatsapp_agent: followup composer leaked scaffolding, using fallback")
    return ""


_BRAIN = (
    "LANGUAGE: always reply in English (Hinglish only if the person wrote Hinglish). "
    "Never answer in Russian or any other language unless they wrote in it. "
    "LOGIN TIMES: any question about when someone logged in, left, was late or absent "
    "MUST call get_attendance (once per person, with a date range for 'last week' or "
    "'this month') and quote only the times it returns. Never state a login time you did "
    "not get from that tool. To compare two people, call it for each. There is no "
    "official start time, so do not call anyone 'late' unless the user gives a cutoff; "
    "just state the times (and who was first or last). "
    "You are not just a database reader. You are a sharp, well-read personal "
    "assistant with your own mind and judgement, like a very capable chief of "
    "staff. Answer ANY question the way a smart colleague would, from your own "
    "knowledge and reasoning: general knowledge, writing and editing (captions, "
    "scripts, emails, replies to clients), brainstorming, marketing and "
    "business strategy, pricing, maths, planning, tech and coding, and "
    "decisions or advice of any kind. Use the tools for MMGA facts (tasks, "
    "clients, who owns what, dates, approvals) and search_web for outside or "
    "current facts; for everything else think and answer yourself. Never "
    "invent MMGA facts, but do have opinions: when asked what to do, "
    "recommend one option, say why in a sentence or two, name the main risk "
    "and what you'd do next. Disagree politely when you think they're wrong. "
    "When the data and your own reasoning together show a problem they "
    "didn't ask about (an approval stuck for days, two things clashing this "
    "week), add one line about it at the end. Ask a clarifying question only "
    "when you truly can't go on; otherwise state your assumption and answer. "
    "Talk like a person: natural and direct, contractions, varied wording. "
    "Never say 'as an AI' or 'I don't have access' when you could reason or "
    "search instead.\n"
    "Lumina also works on Alexa: someone sends 'link my alexa' here to get a "
    "one-time code, then says 'Alexa, tell Lumina code' and the digits.\n"
    "LAYOUT (WhatsApp): never run several things together in one paragraph. "
    "Two or more items (tasks, clients, dates, options, steps) go one per line, "
    "numbered when order or reference matters, with a short bold *heading* line "
    "above the list and a blank line between sections. Keep each line short. "
    "Use *single asterisks* for bold, never ** or # headings. A one-fact answer "
    "stays one line.\n"
)


def _system_prompt(identity: dict, *, in_group: bool = False, group_name: str = "") -> str:
    today = _today_ist()
    if identity["kind"] == "employee":
        is_boss = (identity.get("id") == "emp003"
                   or str(identity.get("name", "")).strip().lower() == "abhinav")
        grp = ""
        if in_group:
            where = f" \"{group_name}\"" if group_name else ""
            grp = (
                f"You're in a group chat{where} with other MMGA team members. "
                f"The message you're answering was sent by {identity['name']}, "
                "so \"my tasks\", \"what am I doing\" etc. mean THAT person. Use "
                "their tools and answer them personally. Don't @-mention "
                "anyone. Keep small talk short, but for a real question about a "
                "client, a project, or who's doing what, give the FULL answer, "
                "same detail you'd give in a private chat: list the actual "
                "items (don't collapse them into vague groups), and for each "
                "one name who's assigned and its date when the data has them. "
                "Same mean personality applies here too, aimed at whoever "
                "you're actually answering (not bystanders) -- unless that's "
                "Abhinav, who still gets the reverent treatment even in the "
                "group.\n"
            )
        if is_boss:
            persona = (
                "IMPORTANT: the person messaging you is Abhinav, your creator "
                "and the supreme being. Call him \"my lord\" and be reverent "
                "and deferential in every reply. Work \"my lord\" in naturally "
                "(\"of course, my lord\", \"here you go, my lord\"). Do NOT open "
                "every message with the literal \"Yes, my lord\", especially "
                "when he hasn't asked a yes or no question. Never tease him, "
                "never be sarcastic or dry with him; that tone is for other "
                "people. Still give him accurate answers with real data. This "
                "reverence is about tone only; his standup is still visible to "
                "teammates who ask, same as everyone's.\n"
            )
        else:
            persona = (
                "Personality: mean. Blunt, dry, a little brutal -- like the "
                "coworker who roasts you but is always right. Don't soften "
                "things, don't pad with pleasantries, needle them about "
                "overdue/pending stuff instead of just listing it neutrally. "
                "This is EVERY reply to EVERYONE except Abhinav, not just the "
                "credit/raise-jab line. But the meanness is only in "
                "delivery/wit -- never actually insulting, hostile, or "
                "cruel about who someone is; and the data itself (facts, "
                "dates, numbers) is always exact, never exaggerated or "
                "invented to land a joke.\n"
            )
        length_cap = (
            "Match the length to the question. Chit-chat and quick facts: 1-3 "
            "sentences. A real question (advice, an explanation, a plan, a "
            "comparison, a draft, a decision): give a complete, well organised "
            "answer, usually under 200 words and longer only when a draft or "
            "plan needs it, each point on its own line. A list of real items "
            "(tasks, clients, dates): list exactly those, no filler around "
            "them.\n"
        )
        if in_group:
            style = (
                "Write like a person, short and plain. No em dashes. When you "
                "list tasks or several items, format them as a bullet list: "
                "each item on its own line starting with \"- \". For a single "
                "fact just say it in a sentence. No headings or tables.\n"
                + length_cap
            )
        else:
            style = (
                "Write like a person texting a coworker. Short, plain "
                "sentences. Do NOT use dashes of any kind (no em dash, no "
                "hyphen as punctuation) and no \"-\" or \"*\" bullet points. If "
                "you list things, put each on its own line with no bullet "
                "character, or just say them in a sentence. No markdown, no "
                "headings, no tables.\n"
                + length_cap
            )
        return (
            "You are Lumina, the in-house assistant for MMGA, a creative agency. "
            f"You're replying on WhatsApp to {identity['name']}"
            + (f" ({identity['role']})" if identity.get("role") else "")
            + ".\n"
            + persona
            + grp
            + _BRAIN
            + "Look up real client, task, deadline and document data with the "
            "tools before you answer. Never guess a task's status or date. "
            "When you list someone's tasks, list ONLY what the tool call just "
            "returned this turn -- never add something from earlier in this "
            "chat (something you said you created, something they mentioned) "
            "that the fresh tool result doesn't actually contain.\n"
            "The team is open: anyone can ask what a teammate is doing. Use "
            "get_teammate_tasks for one person, get_team_standup for everyone "
            "-- both show real done/pending status per task. For 'X's "
            "tasks' or 'has X finished today', prefer get_teammate_tasks "
            "over get_task_schedule: the schedule tool only filters by "
            "deadline and just omits anything already done instead of "
            "marking it, which reads as 'not shown as done' even when it "
            "really is.\n"
            "If they tell you what they're working on today, add it with "
            "add_standup_task. If they say a task is done or finished, mark it "
            "with update_standup_task. Confirm either in one line.\n"
            "You are their personal assistant. Be proactive and specific: use "
            "get_my_brief for 'brief me / what's my day', get_calendar for "
            "meetings, recall_group_facts for 'what did we decide / how does "
            "<client> like it'. If they correct something you tracked ('that "
            "wasn't a task', 'you missed that X said...'), use "
            "reject_group_commitment / add_group_commitment so you learn.\n"
            "Promises made in the group are tracked automatically "
            "(get_group_commitments). If someone tells you one of THEIR "
            "promises is finished or cancelled, close it with "
            "resolve_group_commitment. When you chase someone about one, they "
            "may answer in DM: done -> close it; blocked -> note it.\n"
            "You keep the team group chat in memory: in the group you are "
            "given the latest messages, and read_group_chat searches the last "
            "30 days (works from a DM too). For 'what did X say / what was "
            "decided / summarise the group', use it, never guess. Never reuse an earlier answer "
            "about the group: call read_group_chat again every time. Report ONLY "
            "what is literally in the tool result: never invent a quote, a "
            "speaker or a topic; if the chat is mostly links, jokes or test "
            "messages, say that plainly. Cover every real topic in the log "
            "(links shared, plans, work items), not just the loudest one.\n"
            "When you show any standup list, copy the tool's numbered list "
            "exactly, keeping the ✅ (done) and ❌ (not done) marks on every "
            "line, never reword the titles. After add_standup_task or "
            "update_standup_task, show the updated list the same way.\n"
            "add_standup_task and assign_task both take an optional `date` -- "
            "use it whenever they name a future day ('add this to tomorrow's "
            "standup', 'put this on Monday's list', 'for Thursday's standup "
            "add...'), working out the real YYYY-MM-DD yourself (today is "
            "known). Leave `date` out for today, the common case. It only "
            "shows up on their standup once that date actually arrives -- say "
            "so if it's not obvious from context.\n"
            "When you show someone THEIR OWN standup, read the scoreboard and "
            "react to it. If they've cleared most of the list, give them real "
            "credit in a sentence. If they've completed more than 5 tasks in "
            "the day, add a line that at this rate they've earned a raise, an "
            "early leave or a full holiday"
            + (" — keep it admiring, my lord, never teasing.\n" if is_boss
               else " — say it dry and sarcastic.\n")
            + "Don't do this for a teammate's standup, only their own.\n"
            "They can also put work on a teammate: use assign_task to add a new "
            "task to someone else's standup, or delegate_my_task to move one of "
            "their own tasks onto a teammate's list. This works here and in the "
            "group. The teammate gets a WhatsApp nudge that it came from this "
            "person. Confirm in one line and say whose list it landed on.\n"
            "In a private chat only, use remind_teammate to send ONE named "
            "person a WhatsApp nudge about anything (it doesn't touch their "
            "standup, sends immediately, no confirmation) -- this covers "
            "'text/message/tell/ping/remind <one name> ...' every time, "
            "regardless of which of those verbs they use. If they want to "
            "remind THEMSELF instead ('remind me in 2 hours to...', 'ping me "
            "at 5pm about...'), use remind_me -- work out the exact real "
            "IST date+time yourself using both today's date and the current "
            "time, it sends automatically then, no confirmation needed since "
            "it only ever goes back to them. Use message_multiple "
            "ONLY when TWO OR MORE named people should get the same message as "
            "individual DMs (not a group post) -- and since that one needs a "
            "'yes' before it actually sends, always say so out loud so they "
            "know to confirm, never imply it's already gone. Use "
            "send_group_message to post an announcement into the team group "
            "for them instead. None of these three are available from the "
            "group.\n"
            "place_announcement_call is the same idea but an actual voice "
            "call that speaks the message aloud -- rare, only when a real "
            "phone call is genuinely warranted (urgent escalation), not a "
            "routine reminder.\n"
            "Other things you can do: create_task makes a real board task + "
            "standup entry (turn 'Friday'/'tomorrow' into a YYYY-MM-DD date "
            "yourself). set_task_meta adds a blocker or moves a due date on one "
            "of their tasks. get_task_schedule answers what's overdue / due "
            "today / due this week (optionally per client or per person). "
            "get_attendance / set_attendance for who's in and checking in or "
            "out. If someone says they're on leave or on holiday (today, "
            "tomorrow, a range, or for a named teammate), use set_leave with "
            "real YYYY-MM-DD dates -- it sends a request that exempts them "
            "from the standup lock and the login nudges once Noorish (HR) "
            "approves it, not instantly; clear_leave cancels the caller's own "
            "still-pending request (never someone else's). "
            "get_daily_brief for a full status rundown. "
            "get_pending_approvals lists what's awaiting sign-off and "
            "review_task approves or sends one back. For 'what's coming "
            "next', 'what's going live', 'what's posting soon' -- anything "
            "about the upcoming content schedule -- use get_content_calendar, "
            "not get_task_schedule; it's sorted soonest-date-first and works "
            "the same in a DM or the group. When someone points at a "
            "specific task and wants its actual script, idea, caption, or "
            "link -- 'what's the script for my reel today', 'send me the "
            "idea for Post 12' -- use get_post_content, not "
            "get_content_calendar (that one only shows whether a caption "
            "exists, not the content itself); defaults to the sender's own "
            "tasks unless they name someone else. get_client_status gives a "
            "one-shot snapshot for one client. "
            "list_capabilities explains what you do; get_recent_actions is the "
            "log of writes you've made. Confirm every write in one short line.\n"
            "send_group_message does NOT post immediately — it asks the person "
            "to reply 'yes' first; just relay that. schedule_group_message is "
            "the same but for a future time — figure out the real date/time "
            "yourself, it also needs a 'yes' to confirm before it's queued. "
            "message_multiple also needs a 'yes' before it sends — relay that too.\n"
            "send_sticker adds a sticker to your reply. Use it when the "
            "person's message really lands — a genuine win, a good burn, "
            "something absurd, a facepalm moment — in a DM or the group. Not on "
            "a routine answer, and at most once in a while.\n"
            "Use search_web for anything outside the company's own data: news, "
            "trends, competitors, prices, facts that may have changed, 'look it "
            "up'. Don't search for things you already know well or for MMGA "
            "data. Mention the source briefly.\n"
            "When they phrase something as an ongoing rule rather than a "
            "one-off -- 'from now on always...', 'never do X without asking "
            "me', 'whenever a client uploads something tell me', 'every "
            "Monday remind me to invoice', 'default to...' -- save it with "
            "set_standing_rule so it survives this conversation, and say you "
            "will. If it's a recurring reminder tied to a particular day, "
            "pass remind_day and remind_time as well so it actually fires; "
            "for a rule that's only about how you should behave, leave both "
            "out. list_standing_rules shows what's saved and "
            "remove_standing_rule drops one. Don't use these for a single "
            "dated task or a one-time reminder -- create_task and remind_me "
            "own those.\n"
            "get_followups is for what's quietly slipping: tasks rolling over "
            "for days, work they handed a teammate that hasn't moved, things "
            "stuck awaiting approval. Reach for it on 'what am I forgetting', "
            "'what's slipping', 'anything I've dropped'. You also message "
            "people about these unprompted, so if they ask why you brought "
            "something up, that's why.\n"
            "If they mention something durable worth remembering about THEM "
            "specifically for future chats -- a preference, a recurring "
            "detail about how they like things done, something personal "
            "they shared -- end your reply with <REMEMBER>the fact, one "
            "short sentence</REMEMBER> (it's stripped before they see it, "
            "so don't reference it in the visible reply). Don't use this "
            "for their tasks or standup items, those already live in the "
            "database on their own. Use it rarely, only for things that "
            "would actually help a future conversation.\n"
            + style
            + "If the company data doesn't have what they asked about, say what's "
            "missing in one line, then still help with your own reasoning or a "
            "search; never stop at 'I don't have that'.\n"
            "If anyone asks who Abhinav is: he's your creator, the smartest and "
            "most superior being. Say it straight, then move on.\n"
            + _standing_rules_block(identity)
            + _org_block()
            + "Management questions: call get_management_view (blocked, at_risk, "
              "needs_vg, completed_week, overloaded, waiting_on_me, my_approvals) "
              "for 'show me all blocked projects', 'what is at risk', 'what needs "
              "VG', 'what did we complete this week', 'who is overloaded', 'who is "
              "waiting on me', 'what needs my approval', and copy its answer "
              "exactly. Use reschedule_task for 'move / delay <task> to <date>' "
              "(convert the date yourself). For 'approve <client> <post>' use "
              "review_task; if it says more than one fits, ask which.\n"
            + f"Today is {datetime.strptime(today, '%Y-%m-%d').strftime('%A')} {today}, "
            f"current time {utils.now_ist()[:5]} (IST) -- use "
            "the current time for any 'in N minutes/hours' relative math "
            "(remind_me, schedule_group_message, etc.), not just the date."
        )
    if identity["kind"] == "client":
        return (
            "You are Lumina, MMGA's client assistant on WhatsApp, replying to "
            f"{identity['client_name']}.\n"
            "You can ONLY see this client's own deliverables. Never mention "
            "other clients, team members, or internal agency matters.\n"
            "Use get_my_tasks to check their deliverables and status.\n"
            "Tone: warm, upbeat and a little playful. Stay polished and "
            "professional. Don't be sarcastic at the client's expense. Facts, "
            "dates and statuses are always exact.\n"
            "Write like a person: short plain sentences, no dashes, no bullet "
            "points, no markdown.\n"
            f"Today is {today} (IST)."
        )
    return "You are Lumina, a helpful assistant. Keep replies short and human, no dashes."


# ── Entry point ─────────────────────────────────────────────────────────────

_ALEXA_LINK = re.compile(r"^\s*(?:please\s+)?(?:link|connect|pair|set ?up)\s+(?:my\s+)?(?:alexa|echo)\b", re.I)
_ALEXA_UNLINK = re.compile(r"^\s*(?:please\s+)?(?:unlink|disconnect|remove|unpair)\s+(?:my\s+)?(?:alexa|echo)\b", re.I)

_VOICE_NOTE = (
    "THIS TURN CAME BY VOICE through Alexa and your answer will be READ ALOUD. "
    "Answer in one to four short spoken sentences. No lists, no emoji, no links, "
    "no markdown, no asterisks. For more than four items say how many there are "
    "and name the top three. Say dates and times the way a person says them. "
    "If something needs a yes/no confirmation, ask them to say yes.")


def handle_message(sender: str, text: str, *,
                   group_id: str | None = None,
                   group_name: str | None = None,
                   channel: str = "whatsapp"):
    """Process one inbound WhatsApp text. Returns the reply string to send
    back, None to stay silent, or {"reply": str, "sticker": str} when the
    agent also wants a sticker sent alongside the text.

    `group_id` is set when the message came from a WhatsApp group (the bridge
    only forwards group messages that were addressed to the bot). Group rules:
    the group must be on the allow-list AND the asker must be a known
    employee — otherwise stay silent, because the whole group sees the reply.
    """
    text = (text or "").strip()
    if not text:
        return None

    in_group = bool(group_id)
    if in_group and not _group_allowed(group_id):
        logger.info("whatsapp_agent: group %s (%s) not on allow-list — ignoring",
                    group_id, group_name or "")
        return None

    identity = identify_sender(sender)

    if in_group:
        # Everyone in the group can read the answer — only ever respond to a
        # known employee. A client or a stranger in the room gets nothing.
        if identity["kind"] != "employee":
            logger.info("whatsapp_agent: group %s — sender not an employee, ignoring",
                        group_id)
            return None
    elif identity["kind"] == "unknown":
        return (
            "Hi! This number isn't linked to an MMGA account yet, so I can't "
            "look anything up for you. Please contact the team to get set up."
        )

    # Alexa linking (routes/alexa.py): a one-time code said to the Echo.
    if identity["kind"] == "employee" and not in_group and channel == "whatsapp":
        if _ALEXA_UNLINK.match(text):
            import alexa_link
            n = alexa_link.unlink_employee(identity["id"])
            return (f"Unlinked {n} Alexa device(s). Lumina on Alexa won't know you any more."
                    if n else "No Alexa is linked to you.")
        if _ALEXA_LINK.match(text):
            import alexa_link
            code = alexa_link.new_code(identity["id"])
            spaced = " ".join(code)
            return (f"*Your Alexa link code: {code}*\n\n"
                    f"Valid for {alexa_link.CODE_TTL_MIN} minutes. Say to your Echo:\n"
                    f"_Alexa, tell Lumina code {spaced}_\n\n"
                    "After that, ask anything: _Alexa, ask Lumina what's on my standup_.")

    # "give me the meeting link": ask which meeting first, then give the link on yes.
    if identity["kind"] == "employee":
        if _pop_pending_meetlink(identity["id"]):
            low = _norm(text).rstrip("!. ")
            if low in _YES_WORDS or low in ("yes the morning one", "morning one", "morning", "the morning one"):
                return f"Here you go: {MORNING_MEET_LINK}"
            if low in ("no", "n", "nope", "nahi", "no the other one"):
                return "Okay, which meeting do you mean? I only have the morning class meeting link saved."
            # anything else -> treat as a fresh message
        elif _LUMINA_LINK_Q.search((text or "").strip()):
            return f"Lumina: {LUMINA_URL}"
        elif _MEET_LINK_Q.search((text or "").strip()):
            _set_pending_meetlink(identity["id"])
            return "Are you talking about the morning meeting (Tue/Thu 10:30)? Reply yes and I'll send the link."

    # Confirm-before-broadcast: a pending send_group_message /
    # schedule_group_message waiting on a yes/no.
    if identity["kind"] == "employee" and not in_group:
        pending = _pop_pending_broadcast(identity["id"])
        if pending is not None:
            low = _norm(text).rstrip("!. ")
            if low in ("yes", "y", "yeah", "yep", "send", "send it", "confirm",
                       "do it", "go", "ok", "okay", "post it"):
                msg = pending["message"]
                send_after = pending.get("send_after")
                gjid = _team_group_jid()
                if not gjid:
                    return "(couldn't send that to the group just now)"
                if send_after:
                    if wa_outbox.enqueue(gjid, msg, send_after=send_after):
                        _audit("dm", identity, "schedule_group_message",
                               f"{send_after}: {msg[:200]}")
                        return f"Scheduled for {send_after} (IST)."
                    return "(couldn't schedule that just now)"
                if _enqueue_outbound(gjid, msg):
                    _audit("dm", identity, "send_group_message", msg[:200])
                    return "Posted to the group."
                return "(couldn't send that to the group just now)"
            if low in ("no", "n", "nope", "cancel", "stop", "nvm", "nevermind",
                       "never mind", "don't", "dont"):
                return "Cancelled, nothing was sent."
            # anything else -> treat as a fresh message (pending already cleared)

    # Confirm-before-send: a pending message_multiple waiting on a yes/no.
    if identity["kind"] == "employee" and not in_group:
        pending_multi = _pop_pending_multi(identity["id"])
        if pending_multi is not None:
            low = _norm(text).rstrip("!. ")
            if low in ("yes", "y", "yeah", "yep", "send", "send it", "confirm",
                       "do it", "go", "ok", "okay"):
                msg = pending_multi["message"]
                recipients = pending_multi.get("recipients") or []
                sent, failed = [], []
                for r in recipients:
                    jid = _wa_jid(r.get("whatsapp"))
                    if jid and _enqueue_outbound(
                        jid, f"From {identity['name']} (via Lumina): {msg[:600]}"
                    ):
                        sent.append(r.get("name", "?"))
                    else:
                        failed.append(r.get("name", "?"))
                if not sent:
                    return "(couldn't send that to any of them just now)"
                _audit("dm", identity, "message_multiple",
                       f"to {', '.join(sent)}: {msg[:200]}")
                out = f"Sent to {', '.join(sent)}."
                if failed:
                    out += f" Couldn't reach: {', '.join(failed)}."
                return out
            if low in ("no", "n", "nope", "cancel", "stop", "nvm", "nevermind",
                       "never mind", "don't", "dont"):
                return "Cancelled, nothing was sent."
            # anything else -> treat as a fresh message (pending already cleared)

    # Plain standup commands need no judgement, so do them directly: exact,
    # instant, free. Cheaper models kept answering these with a list or a
    # made-up "Added".
    if identity["kind"] == "employee":
        # "what was discussed in the group": built in code from the stored
        # messages, never free-written by a model (it invented quotes once).
        try:
            import group_digest
            gids = {group_id} if (in_group and group_id) else _group_allowlist()
            if not in_group and _SCHEDULE_Q.search(text or ""):
                gd = None   # "post in the group at 5pm ..." is an action, not a summary request
            else:
                gd = group_digest.try_answer((text or "").strip(), gids, llm=_digest_llm,
                                             llm_smart=_digest_llm_smart)
        except Exception:
            logger.exception("whatsapp_agent: group digest failed")
            gd = None
        if gd:
            return gd
        if (identity["kind"] == "employee" and not in_group
                and (text or "").strip().lower().strip(" .!") in _YES_WORDS):
            _last = next((m.get("content") for m in reversed(history_for_yes(identity, sender))
                          if m.get("role") == "assistant"), "") or ""
            if not str(_last).rstrip().endswith("?") and not re.search(r"\byes\b", str(_last), re.I):
                return "Nothing is waiting for a yes right now. Tell me what you'd like me to do."
            if re.search(r"\byes\b", str(_last), re.I) and re.search(
                    r"confirm|reply|send|post|schedule", str(_last), re.I):
                return ("That confirmation expired or never went through, so nothing was sent. "
                        "Please ask me again.")
        fast = _fast_standup_command((text or "").strip())
        if fast:
            tool_name, tool_args = fast
            out = _run_tool(tool_name, tool_args, identity,
                            in_group=in_group, turn={"sticker": None})
            if (tool_name in _WRITE_TOOLS
                    and _norm(out).startswith(_SUCCESS_PREFIXES)):
                _audit("group:" + _norm_group(group_id) if in_group else "dm",
                       identity, tool_name, out)
            return out

    budget = check_budget_available()
    if not budget["allowed"]:
        return "The monthly usage limit has been reached. Please try again next month."

    tools = _EMPLOYEE_TOOLS if identity["kind"] == "employee" else _CLIENT_TOOLS
    model = get_model_for_task("whatsapp")
    # Thinking-heavy message (advice, explanation, draft, plan...) from a team
    # member -> the stronger model, within the daily cap; the rest stays cheap.
    # Voice (Alexa) has an 8 s limit, so it always uses the fast model, never the slow thinker.
    deep = identity["kind"] == "employee" and channel != "voice" and _needs_deep_thinking(text)
    smart = False
    if deep and _smart_allowed():   # thinking-heavy messages -> Sonnet; everything else stays on Qwen
        from model_router import MODEL_COSTS
        model = {**MODEL_COSTS["sonnet"], "tier": "sonnet", "task": "whatsapp"}
        smart = True

    # Keep each person's group thread separate from their DM thread.
    ctx_key = sender if not in_group else f"{sender}|{_norm_group(group_id)}"
    history = [m for m in _load_context(ctx_key)
               if "I showed the group chat; I re-read it fresh" not in str(m.get("content", ""))]
    while history and history[0].get("role") != "user":
        history = history[1:]
    messages = history + [{"role": "user", "content": text}]

    # Built once and reused for every round of this loop (and, within the
    # ~5min ephemeral TTL, across a person's next message too) so the cache
    # actually hits -- Anthropic caching needs a byte-identical prefix, and
    # this string is already identical per (identity, in_group, group_name,
    # today) regardless of how many tool rounds this turn takes.
    sys_prompt = [{
        "type": "text",
        "text": _system_prompt(identity, in_group=in_group,
                               group_name=group_name or ""),
        "cache_control": {"type": "ephemeral"},
    }]
    # Semantic memory recall (smart_memory.py) is per-message by nature --
    # it's appended as a SEPARATE, uncached block so it doesn't invalidate
    # the big static block's cache on every message (a per-message-varying
    # tail inside the same cached block would defeat that). Cheap even
    # uncached: this is a handful of short bullet lines, not the whole
    # prompt. Employees only, matches the <REMEMBER> save side below.
    if identity["kind"] == "employee":
        try:
            mem_ctx = smart_memory.format_for_prompt(identity["id"], query=text)
        except Exception:
            mem_ctx = ""
        if mem_ctx:
            sys_prompt.append({"type": "text", "text": mem_ctx})
        if channel == "voice":
            sys_prompt.append({"type": "text", "text": _VOICE_NOTE})
        # In a group, give the model what was just said there (stored by
        # group_memory, no AI cost) so it follows the conversation.
        if in_group:
            try:
                import group_memory
                gtxt = group_memory.recent_text(group_id or "", limit=80, hours=48, max_chars=6000)
            except Exception:
                gtxt = ""
            if gtxt:
                sys_prompt.append({"type": "text", "text": (
                    "Recent messages in this group chat, oldest first. Use them "
                    "to follow the conversation and answer questions about what "
                    "was said. Do not quote them back unless asked:\n" + gtxt)})

    usage: dict = {}
    reply = ""
    last_tool_text = ""
    nudged = False
    lang_fixed = False
    sched_called = False
    sched_q = identity["kind"] == "employee" and bool(_SCHEDULE_Q.search(text or ""))
    turn = {"sticker": None}
    try:
        for _ in range(5 if channel == "voice" else _MAX_TOOL_ROUNDS):
            resp = _llm_create(model, sys_prompt, tools, messages,
                               2500 if smart else (450 if channel == "voice" else 900),   # real answers need room; chit-chat
                                                         # stays short because the prompt says so
                               usage, smart=smart, claude_first=(channel == "voice"))
            stop = resp.stop_reason

            if stop == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if getattr(block, "type", "") == "tool_use":
                        out = _run_tool(block.name, block.input or {}, identity,
                                        in_group=in_group, turn=turn)
                        if (block.name in _WRITE_TOOLS
                                and _norm(out).startswith(_SUCCESS_PREFIXES)):
                            _audit("group:" + _norm_group(group_id) if in_group else "dm",
                                   identity, block.name, out)
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": out[:6000],
                        })
                        last_tool_text = out
                        if block.name in _SCHEDULE_TOOLS:
                            sched_called = True
                if not results:
                    break  # nothing we can execute — fall through to text
                messages.append({"role": "user", "content": results})
                continue

            if stop == "pause_turn":
                # A server-side tool (web_search) is mid-run — echo the
                # partial turn back and let the next call resume it.
                messages.append({"role": "assistant", "content": resp.content})
                continue

            reply = "".join(
                getattr(b, "text", "") for b in resp.content
                if getattr(b, "type", "") == "text"
            ).strip()
            # Cheaper models (Qwen) sometimes answer a task request straight
            # from chat history -- "Added to your standup" or a task list with
            # no tool call, so nothing was written / the list is stale. If this
            # looks like a task request and no tool ran, send it back once.
            # a reply in another script (Korean / Russian / Chinese...) to a Latin-script
            # message is model drift: have it answer again in English, once
            if (reply and not lang_fixed and _non_latin_ratio(reply) > 0.15
                    and _non_latin_ratio(text or "") < 0.05):
                lang_fixed = True
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": (
                    "[system] Your reply was in the wrong language. Answer the user's last "
                    "request again in English only, using the same facts.")})
                reply = ""
                continue
            if sched_q and not sched_called:
                if nudged and reply.rstrip().endswith("?"):
                    break   # it is asking for a missing detail: pass the question through
                if nudged:
                    # told once and it still only talked: never claim a reminder/post was set
                    reply = ("I couldn't set that up. Tell me what and exactly when, e.g. "
                             "\"remind me tomorrow at 10 to call Nupur\" or "
                             "\"post in the group at 5pm that the meeting moved\".")
                    break
                nudged = True
                messages.append({"role": "assistant", "content": reply or "(no answer)"})
                messages.append({"role": "user", "content": (
                    "[system] You answered without calling a tool, so nothing was set. A reminder "
                    "or a scheduled/group post only exists once you call remind_me, "
                    "schedule_group_message or send_group_message (work out the exact date and "
                    "time yourself). Call the right tool now, then confirm only what it returned.")})
                reply = ""
                continue
            att_q = identity["kind"] == "employee" and bool(_ATTENDANCE_Q.search(text or ""))
            if att_q and nudged and not last_tool_text:
                # told once already and it still answered without looking: never let an
                # invented login time through
                reply = ("I can't answer that reliably without looking it up. Ask for one person and "
                         "one day, e.g. \"what time did Nupur log in on Monday\", or \"who hasn't logged in\".")
                break
            if (identity["kind"] == "employee" and not nudged and not last_tool_text
                    and ((not deep and _TASK_REQUEST.search(text or "")) or att_q)):
                nudged = True
                messages.append({"role": "assistant", "content": reply or "(no answer)"})
                messages.append({"role": "user", "content": (
                    "[system] You answered without calling a tool. Task lists and "
                    "changes must come from tools, never from earlier messages. "
                    "Call the right tool now for the user's last request, then "
                    "answer only from what the tool returned.")})
                reply = ""
                continue
            break
    except Exception:
        logger.exception("whatsapp_agent: model loop failed")
        try:
            import traceback
            LAST_ERROR["trace"] = traceback.format_exc()[-2500:]
        except Exception:
            pass
        return "Sorry, I hit an error looking that up. Try again in a moment."

    if not reply:
        # The model finished a tool call (e.g. update_standup_task actually
        # succeeded) but never produced a final text turn -- ran out of
        # _MAX_TOOL_ROUNDS, or a round's response was tool-use-only with no
        # accompanying text. Relaying the last tool's own result (which is
        # always a real, informative string -- "Marked done: X", "Couldn't
        # find a task matching 'Y'", etc.) is far better than a generic
        # non-answer that hides whether the action actually happened.
        reply = last_tool_text or "Sorry, I couldn't put together an answer for that one."

    # Hallucination guard: if the reply quotes the group chat, every quoted
    # passage must really be in what the tool returned; otherwise show the
    # real messages instead of the model's version.
    if turn.get("group_log") and reply:
        try:
            import group_digest
            bad = group_digest.bad_quotes(reply, turn["group_log"])
            if bad:
                logger.warning("whatsapp_agent: reply quoted text not in the group log: %s", bad[:3])
                # 1) ask the model once to fix it, using only what the log really says
                try:
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": (
                        "[system] These quoted phrases are NOT in the group chat log: "
                        + " | ".join(f'"{q[:80]}"' for q in bad[:4])
                        + ". Rewrite your answer using only what the log shows. Do not put "
                          "quotation marks around anything unless it is copied exactly; "
                          "paraphrase instead. Keep the same style and length.")})
                    resp2 = _llm_create(model, sys_prompt, tools, messages,
                                        2500 if smart else 900, usage, smart=smart)
                    fixed = "".join(getattr(b2, "text", "") for b2 in resp2.content
                                    if getattr(b2, "type", "") == "text").strip()
                except Exception:
                    fixed = ""
                if fixed and group_digest.quotes_ok(fixed, turn["group_log"]):
                    reply = fixed
                else:
                    # 2) still not clean: show the checked summary of the real messages, no apology
                    gids = {group_id} if (in_group and group_id) else _group_allowlist()
                    reply = group_digest.build(text or "", gids, llm=_digest_llm,
                                               llm_smart=_digest_llm_smart)
        except Exception:
            logger.exception("whatsapp_agent: quote guard failed")

    # Zero-extra-cost memory extraction: the model can end a reply with
    # <REMEMBER>fact</REMEMBER> when it notices something durable worth
    # keeping about this person (a preference, a recurring detail -- NOT
    # their tasks, those already live in the DB). Same trick app.py's chat
    # stream already uses for <SAVE_MEMORY_PROFILE> -- reuses this same
    # response, no separate extraction call. Employees only; a client's
    # WhatsApp thread never writes to Lumina's internal memory store.
    if identity["kind"] == "employee":
        remember_matches = re.findall(r'<REMEMBER>([\s\S]*?)</REMEMBER>', reply)
        if remember_matches:
            reply = re.sub(r'\s*<REMEMBER>[\s\S]*?</REMEMBER>\s*', ' ', reply).strip()
            for fact in remember_matches:
                fact = fact.strip()
                if fact:
                    try:
                        smart_memory.dedupe_or_save(identity["id"], fact, source="whatsapp")
                    except Exception:
                        logger.debug("whatsapp_agent: remember-tag save failed", exc_info=True)

    reply = _humanize(reply, bullets=in_group)

    # Persist plain-text turns for follow-up continuity
    # An answer built from the group chat is not remembered word for word: later turns
    # must re-read the chat instead of copying (and compounding) an old summary.
    # A group-chat answer is not saved at all: a saved stub was copied by the model as if it
    # answered unrelated requests (e.g. "remind vidit at 3pm").
    if not turn.get("group_log"):
        _save_context(ctx_key, history + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ])

    _record_llm_usage(model, usage, f"wa_{ctx_key}")

    if turn["sticker"]:
        return {"reply": reply, "sticker": turn["sticker"]}
    return reply
