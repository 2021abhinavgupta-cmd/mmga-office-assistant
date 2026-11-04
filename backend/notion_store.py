"""
notion_store.py — Notion API Integration
=========================================
Reads/writes clients and tasks to Notion databases.

Setup:
  1. Go to https://www.notion.so/my-integrations and create an Internal Integration.
  2. Copy the "Internal Integration Token" → set as NOTION_TOKEN in config/.env
  3. Create two Notion databases:
       - Clients DB  → share with integration → copy DB ID → set NOTION_CLIENTS_DB_ID
       - Tasks DB    → share with integration → copy DB ID → set NOTION_TASKS_DB_ID
  4. See ARCHITECTURE.md for required database property schemas.

If NOTION_TOKEN is not set, all functions return empty results gracefully (no crash).
"""

import os
import re
import time
import logging
import requests
from typing import Optional

try:
    from notifications import notify_task_status_changed
except ImportError:
    notify_task_status_changed = None

logger = logging.getLogger(__name__)


def _notion_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    Make a Notion API request with 3-attempt exponential backoff.
    Raises requests.HTTPError on final failure.
    """
    last_exc = None
    for attempt in range(1, 4):
        try:
            r = requests.request(method, url, timeout=15, **kwargs)
            if r.status_code == 429:  # Notion rate limit
                wait = 2 ** (attempt - 1)
                logger.warning(f"Notion rate-limited, retrying in {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.exceptions.Timeout as e:
            logger.warning(f"Notion request timed out (attempt {attempt}): {url}")
            last_exc = e
            time.sleep(2 ** (attempt - 1))
        except requests.exceptions.HTTPError as e:
            raise  # non-retryable HTTP errors bubble up immediately
        except Exception as e:
            logger.warning(f"Notion request error (attempt {attempt}): {e}")
            last_exc = e
            time.sleep(2 ** (attempt - 1))
    raise requests.exceptions.ConnectionError(f"Notion request failed after 3 attempts: {url}") from last_exc

NOTION_TOKEN      = os.getenv("NOTION_TOKEN", "")
CLIENTS_DB_ID     = os.getenv("NOTION_CLIENTS_DB_ID", "")
TASKS_DB_ID       = os.getenv("NOTION_TASKS_DB_ID", "")
NOTION_VERSION    = "2022-06-28"

# ── Notion is configured? ────────────────────────────────────────────────────

def is_configured() -> bool:
    """Reads env vars dynamically so Railway hot-deploys take effect without restart."""
    return bool(
        os.getenv("NOTION_TOKEN") and
        os.getenv("NOTION_CLIENTS_DB_ID") and
        os.getenv("NOTION_TASKS_DB_ID")
    )


def _headers() -> dict:
    """Read token dynamically on every call so env var updates are picked up."""
    token = os.getenv("NOTION_TOKEN", "")
    return {
        "Authorization":  f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    }

# Dynamic DB IDs (hot-reload safe)
def _clients_db() -> str:
    return os.getenv("NOTION_CLIENTS_DB_ID", CLIENTS_DB_ID)

def _tasks_db() -> str:
    return os.getenv("NOTION_TASKS_DB_ID", TASKS_DB_ID)


_creation_date_prop_ready = False


def _ensure_creation_date_property() -> bool:
    """One-time, cached schema patch: adds 'Creation Date' (date type) to the
    Tasks DB if it doesn't already exist. Notion rejects an entire page PATCH
    if it references an unknown property, so callers must check this before
    writing to it."""
    global _creation_date_prop_ready
    if _creation_date_prop_ready:
        return True
    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/databases/{_tasks_db()}",
            headers=_headers(),
            json={"properties": {"Creation Date": {"date": {}}}},
        )
        _creation_date_prop_ready = True
        return True
    except Exception:
        logger.exception("Failed to ensure Notion 'Creation Date' property exists")
        return False


_last_edited_prop_ready = False


def _ensure_last_edited_property() -> bool:
    """One-time, cached schema patch: adds 'Last Edited' (rich_text) to the
    Tasks DB if it doesn't already exist. Stores a single pipe-delimited
    string 'ISO_TIMESTAMP|editor name|comma-separated changed fields' -- one
    property instead of three, same tradeoff Notes/other free-text fields in
    this file already make. Not to be confused with Notion's own built-in
    'Last edited time'/'Last edited by' property types: those would always
    attribute the edit to this app's single integration token, not to
    whichever employee actually clicked Save in our UI."""
    global _last_edited_prop_ready
    if _last_edited_prop_ready:
        return True
    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/databases/{_tasks_db()}",
            headers=_headers(),
            json={"properties": {"Last Edited": {"rich_text": {}}}},
        )
        _last_edited_prop_ready = True
        return True
    except Exception:
        logger.exception("Failed to ensure Notion 'Last Edited' property exists")
        return False


def _parse_last_edited(raw: str) -> dict:
    """Splits the 'ISO_TIMESTAMP|editor name|changed fields' string written by
    update_task() back into its three parts. Returns empty strings for legacy
    tasks that predate this property existing."""
    if not raw:
        return {"at": "", "by": "", "summary": ""}
    parts = raw.split("|", 2)
    return {
        "at": parts[0] if len(parts) > 0 else "",
        "by": parts[1] if len(parts) > 1 else "",
        "summary": parts[2] if len(parts) > 2 else "",
    }


# ── Schema health check ──────────────────────────────────────────────────────
# This codebase's most common recurring bug this session was a Notion
# property being renamed/missing/never-created and the code silently
# falling back to empty data instead of erroring (Creation Date, Scripts,
# extra_notes were all this exact bug). This gives a way to catch that
# BEFORE it silently breaks something, instead of after a client complains.

# Properties the code actually depends on. "required" = core functionality
# breaks or silently degrades without it. "optional" = one of several
# fallback names the code already tries — fine if missing, just documents
# what the fallback chain is covering.
_TASKS_DB_EXPECTED = {
    "required": ["Task", "Status", "Due Date", "Assigned To", "Client ID",
                 "Notes", "Progress", "Task Type"],
    "optional_fallbacks": {
        "title (Task)":        ["Post Title", "Post"],
        "client name":         ["Customer Name", "Client Name", "Client", "Brand", "Customer", "Account"],
        "due date":            ["Post Day"],
        "type":                ["Type"],
        "scripts/copy":        ["Scripts/ Copy", "Script/ Copy"],
        "other content":       ["Brief", "Content", "Idea", "Caption"],
        "file link":           ["File (Drive Link)", "File", "Drive Link"],
        "creation date (auto-created on first write)": ["Creation Date"],
    },
}
_CLIENTS_DB_EXPECTED = {
    "required": ["Client", "Contact", "Requirements", "Deadline", "Budget", "Notes", "Status"],
    "optional_fallbacks": {},
}


def _fetch_db_schema(db_id: str) -> dict:
    """Returns {property_name: property_type} for a Notion database, or {} on failure."""
    try:
        r = _notion_request("GET", f"https://api.notion.com/v1/databases/{db_id}", headers=_headers())
        return {k: v.get("type") for k, v in r.json().get("properties", {}).items()}
    except Exception:
        logger.exception(f"Failed to fetch Notion DB schema for {db_id}")
        return {}


def _check_db_schema(db_id: str, expected: dict) -> dict:
    schema = _fetch_db_schema(db_id)
    if not schema:
        return {"reachable": False, "missing_required": [], "present_fallbacks": {}, "properties": {}}

    missing_required = [p for p in expected["required"] if p not in schema]
    present_fallbacks = {}
    for label, names in expected.get("optional_fallbacks", {}).items():
        present = [n for n in names if n in schema]
        if len(present) < len(names):
            present_fallbacks[label] = {"present": present, "missing": [n for n in names if n not in present]}

    return {
        "reachable": True,
        "missing_required": missing_required,
        "present_fallbacks": present_fallbacks,
        "properties": schema,
    }


def get_schema_report() -> dict:
    """
    Compares the live Notion Tasks/Clients DB schemas against what this
    codebase actually reads/writes. Returns a dict with an "ok" flag and
    per-DB details — surface this on an admin page or hit it directly
    (GET /api/notion/schema-check) after editing Notion properties by hand.
    """
    if not is_configured():
        return {"ok": False, "configured": False, "error": "Notion not configured"}

    tasks_report = _check_db_schema(_tasks_db(), _TASKS_DB_EXPECTED)
    clients_report = _check_db_schema(_clients_db(), _CLIENTS_DB_EXPECTED)

    ok = (
        tasks_report["reachable"] and clients_report["reachable"]
        and not tasks_report["missing_required"] and not clients_report["missing_required"]
    )

    return {
        "ok": ok,
        "configured": True,
        "tasks_db": tasks_report,
        "clients_db": clients_report,
    }


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _text(value: str) -> dict:
    """Notion rich_text property value. Notion caps a single text object's
    `content` at 2000 characters and rejects the ENTIRE page-update request
    if it's exceeded (not just that property) -- long composite Sheets notes
    (Content/Idea/Scripts/Caption/Notes joined together) routinely blow past
    this, silently failing the whole save. Chunk into multiple rich_text
    array entries instead; _get_text() already joins all of them back
    together on read, so this is fully symmetric."""
    s = str(value or "")
    if not s:
        return {"rich_text": []}
    chunks = [s[i:i + 2000] for i in range(0, len(s), 2000)]
    return {"rich_text": [{"text": {"content": c}} for c in chunks]}


def _title(value: str) -> dict:
    """Notion title property value."""
    return {"title": [{"text": {"content": str(value or "")}}]}


def _select(value: str) -> dict:
    """Notion select property. Sends None if value is blank (clears the field)."""
    if value and str(value).strip():
        return {"select": {"name": str(value).strip()}}
    return {"select": None}


def _multi_select(value: str) -> dict:
    """Notion multi_select property. Accepts a comma-separated string."""
    if not value or not str(value).strip():
        return {"multi_select": []}
    names = [n.strip() for n in str(value).split(",") if n.strip()]
    return {"multi_select": [{"name": n} for n in names]}


_ASSIGNED_TO_TYPE_CACHE = {"type": None, "ts": 0}
_WORKSPACE_USERS_CACHE = {"users": None, "ts": 0}
_CACHE_TTL = 300  # seconds


def _get_assigned_to_prop_type() -> Optional[str]:
    """
    Reads the Tasks DB schema to find the actual Notion property type of
    "Assigned To" (e.g. "people", "select", "multi_select"). Different
    workspaces configure this column differently, and writing the wrong
    shape makes the ENTIRE page-update request fail (Notion rejects the
    whole PATCH, not just the mismatched property), which used to happen
    silently. Cached for _CACHE_TTL seconds since the schema rarely changes.
    """
    now = time.time()
    if _ASSIGNED_TO_TYPE_CACHE["type"] and (now - _ASSIGNED_TO_TYPE_CACHE["ts"]) < _CACHE_TTL:
        return _ASSIGNED_TO_TYPE_CACHE["type"]
    try:
        r = _notion_request("GET", f"https://api.notion.com/v1/databases/{_tasks_db()}", headers=_headers())
        schema = r.json().get("properties", {})
        ptype = (schema.get("Assigned To") or {}).get("type")
        if ptype:
            _ASSIGNED_TO_TYPE_CACHE["type"] = ptype
            _ASSIGNED_TO_TYPE_CACHE["ts"] = now
        return ptype
    except Exception:
        logger.exception("Failed to read Assigned To property type from Notion schema")
        return None


def _get_workspace_users() -> list:
    """Fetches and caches all Notion workspace users (for resolving names -> person IDs)."""
    now = time.time()
    if _WORKSPACE_USERS_CACHE["users"] is not None and (now - _WORKSPACE_USERS_CACHE["ts"]) < _CACHE_TTL:
        return _WORKSPACE_USERS_CACHE["users"]
    users = []
    try:
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = _notion_request("GET", "https://api.notion.com/v1/users", headers=_headers(), params=params)
            data = r.json()
            for u in data.get("results", []):
                if u.get("type") == "person" and u.get("name"):
                    users.append({"id": u["id"], "name": u["name"]})
            if data.get("has_more"):
                cursor = data.get("next_cursor")
            else:
                break
        _WORKSPACE_USERS_CACHE["users"] = users
        _WORKSPACE_USERS_CACHE["ts"] = now
    except Exception:
        logger.exception("Failed to fetch Notion workspace users")
    return users


def _resolve_people_ids(names_csv: str) -> list:
    """Fuzzy-matches comma-separated display names (e.g. 'Abhinav') against
    real Notion workspace user names (e.g. 'Abhinav Gupta') to get person IDs."""
    if not names_csv or not str(names_csv).strip():
        return []
    users = _get_workspace_users()
    ids = []
    for n in [x.strip() for x in str(names_csv).split(",") if x.strip()]:
        for u in users:
            if n.lower() in u["name"].lower() or u["name"].lower() in n.lower():
                if u["id"] not in ids:
                    ids.append(u["id"])
                break
    return ids


def _assigned_to_prop(value: str) -> dict:
    """
    Builds the correct Notion property payload for "Assigned To" based on
    its actual configured type, instead of assuming multi_select.
    """
    ptype = _get_assigned_to_prop_type()
    if ptype == "people":
        ids = _resolve_people_ids(value)
        if not ids and value:
            logger.warning(f"Could not resolve any Notion user for Assigned To value: {value!r}")
        return {"people": [{"id": i} for i in ids]}
    if ptype == "select":
        return _select(value)
    # Default / "multi_select" / unknown-schema fallback (previous behavior)
    return _multi_select(value)


def _date(value: str) -> dict:
    """Notion date property value (ISO date string or empty)."""
    if value:
        return {"date": {"start": value}}
    return {"date": None}


def _number(value) -> dict:
    return {"number": int(value) if value is not None else 0}


def _get_text(prop: dict) -> str:
    """Extract plain text from a Notion rich_text or title property."""
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(t.get("plain_text", "") for t in items)


def _get_select(prop: dict) -> str:
    sel = prop.get("select") or {}
    return sel.get("name", "")


def _get_multi_select(prop: dict) -> str:
    if "multi_select" in prop:
        items = prop.get("multi_select") or []
        return ", ".join(item.get("name", "") for item in items if item.get("name"))
    elif "people" in prop:
        items = prop.get("people") or []
        return ", ".join(item.get("name", "") for item in items if item.get("name"))
    return ""


_page_title_cache = {}  # page_id -> (title, cached_at)

def _fetch_page_title(page_id: str) -> str:
    cached = _page_title_cache.get(page_id)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]
    try:
        r = _notion_request("GET", f"https://api.notion.com/v1/pages/{page_id}", headers=_headers())
        if r.status_code == 200:
            data = r.json()
            props = data.get("properties", {})
            # Find the title property
            for k, v in props.items():
                if v.get("type") == "title":
                    title = _get_text(v)
                    _page_title_cache[page_id] = (title, time.time())
                    return title
    except: pass
    _page_title_cache[page_id] = ("", time.time())
    return ""

def _get_string_val(prop: dict) -> str:
    if not prop: return ""
    if "rich_text" in prop or "title" in prop: return _get_text(prop)
    if "select" in prop: return _get_select(prop)
    if "multi_select" in prop: return _get_multi_select(prop)
    if "formula" in prop:
        f = prop["formula"]
        if f.get("type") == "string": return f.get("string", "") or ""
    if "rollup" in prop:
        r = prop["rollup"]
        if r.get("type") == "array":
            arr = r.get("array", [])
            if arr: return _get_string_val(arr[0])
    if "relation" in prop:
        rels = prop.get("relation", [])
        if rels: return _fetch_page_title(rels[0]["id"])
    return ""


def _get_date(prop: dict) -> str:
    if "date" in prop:
        d = prop.get("date") or {}
        return d.get("start", "")
    elif "created_time" in prop:
        return prop.get("created_time", "")
    elif "last_edited_time" in prop:
        return prop.get("last_edited_time", "")
    return ""


def _get_number(prop: dict) -> int:
    return prop.get("number") or 0


def _get_relation_ids(prop: dict) -> list:
    return [r["id"] for r in prop.get("relation", [])]


# ══════════════════════════════════════════════════════════════════════════════
# CLIENTS
# ══════════════════════════════════════════════════════════════════════════════

def create_client(name: str, contact: str = "", requirements: str = "",
                  deadline: str = "", budget: str = "", notes: str = "",
                  status: str = "active") -> Optional[dict]:
    """
    Creates a new page in the Clients Notion DB.
    Returns: { notion_id, name } or None on failure.

    Required DB properties:
      Name (title), Contact (rich_text), Requirements (rich_text),
      Deadline (date), Budget (rich_text), Notes (rich_text), Status (select)
    """
    if not is_configured():
        logger.warning("Notion not configured — skipping create_client")
        return None

    payload = {
        "parent": {"database_id": _clients_db()},
        "properties": {
            "Client":       _title(name),
            "Contact":      _text(contact),
            "Requirements": _text(requirements),
            "Deadline":     _date(deadline),
            "Budget":       _text(budget),
            "Notes":        _text(notes),
            "Status":       _select(status),
        },
    }

    try:
        r = _notion_request(
            "POST",
            "https://api.notion.com/v1/pages",
            headers=_headers(),
            json=payload,
        )
        page = r.json()
        logger.info(f"Notion: created client '{name}' — page {page['id']}")
        return {"notion_id": page["id"], "name": name}
    except Exception:
        logger.exception(f"Notion create_client failed for '{name}'")
        return None


def list_clients(status_filter: str = "") -> list:
    """
    Returns all clients from Notion Clients DB.
    Each dict: { notion_id, name, contact, requirements, deadline, budget, notes, status }
    """
    if not is_configured():
        return []

    payload: dict = {"page_size": 100}
    if status_filter:
        payload["filter"] = {
            "property": "Status",
            "select": {"equals": status_filter},
        }

    clients = []
    try:
        has_more = True
        while has_more:
            r = _notion_request(
                "POST",
                f"https://api.notion.com/v1/databases/{_clients_db()}/query",
                headers=_headers(),
                json=payload,
            )
            data = r.json()
            pages = data.get("results", [])
            for p in pages:
                try:
                    props = p.get("properties", {})
                    clients.append({
                        "notion_id":    p["id"],
                        "name":         _get_text(props.get("Client", {})),
                        "contact":      _get_text(props.get("Contact", {})),
                        "requirements": _get_text(props.get("Requirements", {})),
                        "deadline":     _get_date(props.get("Deadline", {})),
                        "budget":       _get_text(props.get("Budget", {})),
                        "notes":        _get_text(props.get("Notes", {})),
                        "status":       _get_select(props.get("Status", {})),
                        "url":          p.get("url", ""),
                    })
                except Exception:
                    logger.exception(f"Notion list_clients: skipping malformed page {p.get('id')}")
            has_more = data.get("has_more", False)
            if has_more:
                payload["start_cursor"] = data.get("next_cursor")
        return clients
    except Exception:
        logger.exception("Notion list_clients failed")
        return []


def append_client_requirements(notion_id: str, extra_text: str) -> bool:
    """Appends text to a client's Requirements field. Notion has no native
    string-append operation, so this fetches the current value and PATCHes
    the concatenated result."""
    if not is_configured() or not notion_id or not extra_text:
        return False
    try:
        r = _notion_request("GET", f"https://api.notion.com/v1/pages/{notion_id}", headers=_headers())
        props = r.json().get("properties", {})
        current = _get_text(props.get("Requirements", {}))
        combined = (current + "\n" + extra_text) if current else extra_text
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=_headers(),
            json={"properties": {"Requirements": _text(combined)}},
        )
        return True
    except Exception:
        logger.exception(f"Notion append_client_requirements failed for {notion_id}")
        return False


def update_client_status(notion_id: str, status: str) -> bool:
    """Update the Status property of a client page."""
    if not is_configured():
        return False
    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=_headers(),
            json={"properties": {"Status": _select(status)}},
        )
        return True
    except Exception:
        logger.exception(f"Notion update_client_status failed for {notion_id}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# TASKS
# ══════════════════════════════════════════════════════════════════════════════

def create_task(title: str, client_name: str, client_notion_id: str,
                assigned_to: str = "", due_date: str = "",
                status: str = "not_started", progress: int = 0,
                service: str = "", notes: str = "", creation_date: str = "") -> Optional[dict]:
    """
    Creates a new page in the Tasks Notion DB.
    Returns: { notion_id, title } or None on failure.

    Required DB properties:
      Title (title), Client (rich_text), ClientID (rich_text),
      AssignedTo (select), DueDate (date), Status (select),
      Progress (number), Service (select)
    """
    if not is_configured():
        return None

    payload = {
        "parent": {"database_id": _tasks_db()},
        "properties": {
            "Task":          _title(title),
            "Customer Name": _text(client_name),
            "Client ID":     _text(client_notion_id),
            "Assigned To":   _assigned_to_prop(assigned_to),
            "Due Date":      _date(due_date),
            "Status":        _select(status),
            "Progress":      _number(progress),
            "Task Type":     _select(service),
        },
    }
    if notes:
        payload["properties"]["Notes"] = _text(notes)
    if creation_date and _ensure_creation_date_property():
        payload["properties"]["Creation Date"] = _date(creation_date)

    try:
        r = _notion_request(
            "POST",
            "https://api.notion.com/v1/pages",
            headers=_headers(),
            json=payload,
        )
        page = r.json()
        logger.info(f"Notion: created task '{title}' for client '{client_name}'")
        return {"notion_id": page["id"], "title": title}
    except Exception:
        logger.exception(f"Notion create_task failed for '{title}'")
        return None


def list_tasks(assigned_to: str = "", client_notion_id: str = "",
               status_filter: str = "") -> list:
    """
    Returns tasks from Notion Tasks DB.
    Filter by assigned_to (employee id/name), client_notion_id, or status.
    """
    if not is_configured():
        return []

    filters = []
    if client_notion_id:
        filters.append({"property": "Client ID", "rich_text": {"equals": client_notion_id}})
    if status_filter:
        if status_filter == "EMPTY":
            filters.append({"property": "Status", "select": {"is_empty": True}})
        else:
            filters.append({"property": "Status", "select": {"equals": status_filter}})

    payload: dict = {
        "page_size": 200,
        "sorts": [{"timestamp": "created_time", "direction": "descending"}]
    }
    if len(filters) == 1:
        payload["filter"] = filters[0]
    elif len(filters) > 1:
        payload["filter"] = {"and": filters}

    tasks = []
    try:
        has_more = True
        while has_more:
            r = _notion_request(
                "POST",
                f"https://api.notion.com/v1/databases/{_tasks_db()}/query",
                headers=_headers(),
                json=payload,
            )
            data = r.json()
            pages = data.get("results", [])
            for p in pages:
                try:
                    props = p.get("properties", {})
                    desc = _get_string_val(props.get("Notes", {}))
                    brief = _get_string_val(props.get("Brief", {}))
                    content = _get_string_val(props.get("Content", {}))
                    idea = _get_string_val(props.get("Idea", {}))
                    scripts_copy = _get_string_val(props.get("Scripts/ Copy", {})) or _get_string_val(props.get("Script/ Copy", {}))
                    caption = _get_string_val(props.get("Caption", {}))
                    file_link = props.get("File (Drive Link)", {}).get("url", "") or props.get("File", {}).get("url", "") or props.get("Drive Link", {}).get("url", "") or _get_text(props.get("File (Drive Link)", {})) or _get_text(props.get("File", {}))

                    # Parse pipe-separated values from description/Notes if present.
                    # NOTE: single-field Notes (e.g. only "Scripts: ...") has no "|" —
                    # desc.split("|") still returns [desc] as one part, so this must
                    # NOT require "|" in desc or single-field saves never get parsed.
                    if not brief and desc:
                        parts = [pt.strip() for pt in desc.split("|")]
                        for pt in parts:
                            pt_lower = pt.lower()
                            if pt_lower.startswith("brief:"): brief = pt[6:].strip()
                            elif pt_lower.startswith("content:"): content = pt[8:].strip()
                            elif pt_lower.startswith("idea:"): idea = pt[5:].strip()
                            elif pt_lower.startswith("scripts:") or pt_lower.startswith("script:"): scripts_copy = pt.split(":", 1)[1].strip()
                            elif pt_lower.startswith("scripts/copy:") or pt_lower.startswith("script/copy:"): scripts_copy = pt.split(":", 1)[1].strip()
                            elif pt_lower.startswith("caption:"): caption = pt[8:].strip()
                            elif pt_lower.startswith("link:"): file_link = pt[5:].strip()
                            elif pt_lower.startswith("file:"): file_link = pt[5:].strip()

                    client_name_val = _get_string_val(props.get("Customer Name")) or _get_string_val(props.get("Client Name")) or _get_string_val(props.get("Client")) or _get_string_val(props.get("Brand")) or _get_string_val(props.get("Customer")) or _get_string_val(props.get("Account"))
                    last_edited = _parse_last_edited(_get_string_val(props.get("Last Edited", {})))

                    tasks.append({
                        "notion_id":   p["id"],
                        "title":        _get_text(props.get("Task", {})) or _get_text(props.get("Post Title", {})) or _get_text(props.get("Post", {})),
                        "client_name":  client_name_val,
                        "client_notion_id": _get_text(props.get("Client ID", {})),
                        "assigned_to":  _get_multi_select(props.get("Assigned To", {})),
                        "due_date":     _get_date(props.get("Due Date", {})) or _get_date(props.get("Post Day", {})),
                        "creation_date":  _get_date(props.get("Creation Date", {})),
                        "status":       _get_select(props.get("Status", {})),
                        "progress":    _get_number(props.get("Progress", {})),
                        "service":     _get_select(props.get("Task Type", {})),
                        "description":  desc,
                        "url":         p.get("url", ""),
                        "type":         _get_select(props.get("Type", {})) or _get_select(props.get("Task Type", {})),
                        "brief":        brief,
                        "content":      content,
                        "idea":         idea,
                        "scripts_copy": scripts_copy,
                        "caption":      caption,
                        "file_link":    file_link,
                        "created_time": p.get("created_time", ""),
                        "last_edited_by":      last_edited["by"],
                        "last_edited_at":      last_edited["at"],
                        "last_edited_summary": last_edited["summary"],
                    })
                except Exception:
                    logger.exception(f"Notion list_tasks: skipping malformed page {p.get('id')}")
            has_more = data.get("has_more", False)
            if has_more:
                payload["start_cursor"] = data.get("next_cursor")
            # If assigned_to was requested, we filter in Python because Notion API fails
        # when attempting to filter a 'people' property with a 'multi_select' condition.
        if assigned_to:
            filtered_tasks = []
            for t in tasks:
                n_assignees = t.get("assigned_to", "")
                if assigned_to.lower() in n_assignees.lower():
                    filtered_tasks.append(t)
            return filtered_tasks
        return tasks
    except Exception:
        logger.exception("Notion list_tasks failed")
        return []


def get_task_type(notion_id: str) -> str:
    """Fetches a single task and returns its Type or Task Type. Falls back to
    detecting a [Story]/[Reel]/etc bracket prefix in the title when neither
    property is populated -- Sheets-created rows don't always get Task Type
    set (same "Task" title as the app's own social-media detection uses),
    so this mirrors that fallback instead of silently returning empty."""
    if not is_configured() or not notion_id:
        return ""
    try:
        r = _notion_request("GET", f"https://api.notion.com/v1/pages/{notion_id}", headers=_headers())
        props = r.json().get("properties", {})
        ptype = _get_select(props.get("Type", {})) or _get_select(props.get("Task Type", {}))
        if ptype:
            return ptype
        title = _get_text(props.get("Task", {})) or _get_text(props.get("Post Title", {})) or _get_text(props.get("Post", {}))
        if title and re.search(r'\[(Story|Static|Reel|Carousel|Post|Video)\]', title, re.IGNORECASE):
            return "Social Media"
        return ""
    except Exception:
        logger.exception(f"Notion get_task_type failed for {notion_id}")
        return ""


def get_task_summary(notion_id: str) -> dict:
    """Fetches a single task page and returns its title/client/content, for
    building a readable standup entry when the caller didn't supply a title."""
    if not is_configured() or not notion_id:
        return {}
    try:
        r = _notion_request("GET", f"https://api.notion.com/v1/pages/{notion_id}", headers=_headers())
        props = r.json().get("properties", {})
        desc = _get_string_val(props.get("Notes", {}))
        content = _get_string_val(props.get("Content", {}))
        if not content and desc:
            for pt in [pt.strip() for pt in desc.split("|")]:
                if pt.lower().startswith("content:"):
                    content = pt[8:].strip()
                    break
        client_name_val = (_get_string_val(props.get("Customer Name")) or _get_string_val(props.get("Client Name"))
                            or _get_string_val(props.get("Client")) or _get_string_val(props.get("Brand"))
                            or _get_string_val(props.get("Customer")) or _get_string_val(props.get("Account")))
        return {
            "title": _get_text(props.get("Task", {})) or _get_text(props.get("Post Title", {})) or _get_text(props.get("Post", {})),
            "client_name": client_name_val,
            "client_notion_id": _get_text(props.get("Client ID", {})),
            "content": content or desc,
            "description": desc,
            "creation_date": _get_date(props.get("Creation Date", {})),
        }
    except Exception:
        logger.exception(f"Notion get_task_summary failed for {notion_id}")
        return {}


def update_task(notion_id: str, status: str = None, progress: int = None,
                submission_note: str = None, assigned_to: str = None,
                new_title: str = None, due_date: str = None, creation_date: str = None,
                task_title: str = "", assignee: str = "", client_name: str = "",
                last_edited: str = None) -> bool:
    """
    Update Status, Progress, SubmissionNote, AssignedTo, Title, DueDate, and/or CreationDate on a task page.
    Pass only the fields you want to change.
    Automatically sends WhatsApp notification on key status changes.
    """
    if not is_configured():
        return False

    props = {}
    if status is not None:
        status_map = {
            "approved": "Approved",
            "blocked": "Blocked",
            "in_review": "Pending Review",
            "pending_review": "Pending Review",
            "in_progress": "In Progress",
            "done": "Done",
            "not_started": "Not Started",
            "need_to_start": "Not Started",
            "posted": "Posted",
            "final": "Final",
            "scheduled": "Scheduled",
            "paused": "Paused",
            "need_for_approval": "Need for approval",
            "need_approval": "Need for approval"
        }
        notion_status_name = status_map.get(status.lower(), status)
        props["Status"] = _select(notion_status_name)
    if progress is not None:
        props["Progress"] = _number(progress)
    if submission_note is not None:
        props["Notes"] = _text(submission_note)
    if assigned_to is not None:
        props["Assigned To"] = _assigned_to_prop(assigned_to)
    if new_title is not None:
        props["Task"] = _title(new_title)
    if due_date is not None:
        props["Due Date"] = _date(due_date)
    if creation_date is not None and _ensure_creation_date_property():
        props["Creation Date"] = _date(creation_date)
    if last_edited is not None and _ensure_last_edited_property():
        props["Last Edited"] = _text(last_edited)

    if not props:
        return True  # nothing to update

    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=_headers(),
            json={"properties": props},
        )

        # ── WhatsApp notification ──
        if status and notify_task_status_changed:
            try:
                notify_task_status_changed(
                    task_title  = task_title  or "(unnamed task)",
                    assignee    = assignee    or "Team member",
                    client_name = client_name or "Unknown client",
                    old_status  = "",
                    new_status  = status,
                )
            except Exception:
                logger.warning("WhatsApp notification failed (non-fatal)", exc_info=True)

        return True
    except Exception:
        logger.exception(f"Notion update_task failed for {notion_id}")
        return False


def set_client_id(notion_id: str, client_notion_id: str) -> bool:
    """Repairs a task's Client ID rich_text property -- the property
    list_tasks(client_notion_id=...) filters on, separate from the
    Customer Name/Client Name/etc properties client_name is derived from.
    A task can display the right client name everywhere while still being
    invisible to per-client views (Sheets tab, dashboard) if Client ID is
    blank or wrong -- see CLAUDE.md gotcha #79's "possible future feature"
    note and its live occurrence on 2026-08-14. One-off manual repair tool,
    not wired into any automatic flow -- confirm the target task/client
    before calling."""
    if not is_configured() or not notion_id or not client_notion_id:
        return False
    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{notion_id}",
            headers=_headers(),
            json={"properties": {"Client ID": _text(client_notion_id)}},
        )
        return True
    except Exception:
        logger.exception(f"Notion set_client_id failed for {notion_id}")
        return False


def is_client_active(client_notion_id: str) -> bool:
    """True only if client_notion_id points at a real, non-archived Notion
    page. Used to refuse connecting a Google Sheet to a client_id that no
    longer exists -- without this, a browser tab left open from before a
    client was deleted could still successfully create a sync link pointing
    at nothing (see CLAUDE.md gotcha #94's Omotec stale-tab incident,
    2026-08-25). Returns False on any lookup failure, not just a genuine
    archived/missing page -- a link creation should fail closed if this
    can't be verified, not assume the client is fine."""
    if not is_configured() or not client_notion_id:
        return False
    try:
        r = _notion_request("GET", f"https://api.notion.com/v1/pages/{client_notion_id}", headers=_headers())
        if r.status_code != 200:
            return False
        return not r.json().get("archived", False)
    except Exception:
        logger.exception(f"Notion is_client_active check failed for {client_notion_id}")
        return False


def archive_notion_page(page_id: str) -> bool:
    """
    Archives a Notion page (moves it to trash). Used for deleting clients/tasks.
    """
    if not is_configured() or not page_id:
        return False
    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=_headers(),
            json={"archived": True},
        )
        logger.info(f"Notion: archived page {page_id}")
        return True
    except Exception:
        logger.exception(f"Notion archive_notion_page failed for {page_id}")
        return False

def unarchive_notion_page(page_id: str) -> bool:
    """Brings an archived Notion page back out of the trash (standup recycle bin)."""
    if not is_configured() or not page_id:
        return False
    try:
        _notion_request(
            "PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=_headers(),
            json={"archived": False},
        )
        logger.info(f"Notion: restored page {page_id}")
        return True
    except Exception:
        logger.exception(f"Notion unarchive_notion_page failed for {page_id}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD AGGREGATE — all clients + their tasks
# ══════════════════════════════════════════════════════════════════════════════

def get_dashboard_data() -> dict:
    """
    Returns { clients: [ { ...client, tasks: [...] } ] }
    Used by /api/notion/dashboard to power projects.html
    """
    if not is_configured():
        return {"configured": False, "clients": []}

    clients = list_clients()

    # Per-client filtered queries (server-side "Client ID equals" filter),
    # not one big unfiltered fetch-everything-then-match-in-Python. This
    # workspace has 350+ tasks -- a single unfiltered list_tasks() call
    # paginates at 200/page, and Notion's cursor pagination has no snapshot
    # isolation: a task created/edited by someone else while page 2 is being
    # fetched can shift the sort order enough to skip a row entirely near
    # the page boundary. That looked exactly like "a task silently
    # disappeared from Lumina" for a real task on 2026-08-14, in a workspace
    # that's edited live throughout the day. Each per-client query here
    # returns far fewer rows (usually well under 200, single page), so it's
    # not exposed to that race the way one giant cross-client fetch is.
    assigned_task_ids = set()
    for client in clients:
        client["tasks"] = list_tasks(client_notion_id=client["notion_id"])
        assigned_task_ids.update(t["notion_id"] for t in client["tasks"])

    # Still need one unfiltered pass to find genuinely unassigned tasks
    # (no client_notion_id at all) -- this bucket keeps the same residual
    # pagination-race exposure the per-client lists no longer have, but it's
    # a much smaller blast radius (a misc bucket, not every client's task list).
    all_tasks = list_tasks()
    unassigned_tasks = [t for t in all_tasks if t["notion_id"] not in assigned_task_ids]
    if unassigned_tasks:
        clients.append({
            "notion_id": "unassigned",
            "name": "Daily Standup Tasks",
            "url": "",
            "tasks": unassigned_tasks,
            "deadline": "",
        })

    return {"configured": True, "clients": clients}
