"""WhatsApp groups that belong to a CLIENT (2026-11-12).

The assistant can sit in a client's own WhatsApp group. There it is very formal
and polite, sends no stickers or greetings, answers only when someone says
"Lumina" (the bridge already requires that), reads and remembers the chat, and
may only talk about THAT client's deliverables. Nothing internal is ever shown.

Storage: app_settings key 'whatsapp_client_groups', a JSON object
  { "<group id digits>": {client_name, client_notion_id, group_name, linked_by} }
Groups are linked either by an employee saying "Lumina, this is the Omotec
group" inside the group, or with POST /api/whatsapp/groups {"client_group": ...}.
"""
from __future__ import annotations

import json
import logging
import re

from db import get_connection

logger = logging.getLogger(__name__)

KEY = "whatsapp_client_groups"


def _digits(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def _load() -> dict:
    try:
        conn = get_connection()
        try:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (KEY,)).fetchone()
        finally:
            conn.close()
        d = json.loads(row[0]) if row and row[0] else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        logger.debug("client_groups: load failed", exc_info=True)
        return {}


def _save(d: dict) -> None:
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, json.dumps(d)))
    finally:
        conn.close()


def get(group_id) -> dict | None:
    """The client record for this group, or None if it is not a client group."""
    g = _digits(group_id)
    if not g:
        return None
    return _load().get(g)


def ids() -> set:
    return set(_load().keys())


def listing() -> list:
    return [{"group_id": k, **v} for k, v in sorted(_load().items())]


def link(group_id, client_name: str, client_notion_id: str, group_name: str = "",
         linked_by: str = "") -> dict:
    g = _digits(group_id)
    if not g or not (client_name or "").strip() or not (client_notion_id or "").strip():
        raise ValueError("group id, client name and client id are all required")
    d = _load()
    d[g] = {"client_name": client_name.strip(), "client_notion_id": client_notion_id.strip(),
            "group_name": (group_name or "").strip(), "linked_by": (linked_by or "").strip()}
    _save(d)
    return d[g]


def unlink(group_id) -> bool:
    g = _digits(group_id)
    d = _load()
    if g in d:
        d.pop(g)
        _save(d)
        return True
    return False
