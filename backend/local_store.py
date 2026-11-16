"""
local_store.py -- Lumina's own clients/tasks store (SQLite), a drop-in for notion_store.

Every public function here has the SAME name, arguments and return shape as its
notion_store twin, and notion_store hands the call over to this module while the
backend switch (app_settings 'task_backend' = 'local', or env TASK_BACKEND=local)
is on. So the ~120 call sites across the app did not change.

Fidelity rules (the point of this module is that nothing is lost or reshaped):
  * Nothing is ever hard-deleted. "Archive" sets archived=1, exactly like Notion's
    trash; the row (and its original Notion page JSON, kept in notion_raw) stays.
  * Statuses / assignees / dates are stored as the exact strings Notion returned,
    and new writes follow the same conventions notion_store used (e.g. create_task
    stores the raw status, update_task maps 'in_progress' -> 'In Progress').
  * The description text is parsed into brief/content/idea/scripts/caption/link
    with the same rules list_tasks always used.
  * Ids are the original Notion page ids, so standup rows, Sheets links, feedback,
    WhatsApp history etc. that reference them keep working.

Tables are lumina_clients / lumina_tasks. (The older 'tasks'/'clients' tables belong
to the legacy SQLite-mode and are not touched.)
"""
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Optional

from db import get_connection

try:
    from notifications import notify_task_status_changed
except ImportError:  # pragma: no cover
    notify_task_status_changed = None

logger = logging.getLogger(__name__)

STATUS_MAP = {
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
    "need_approval": "Need for approval",
}

_SCHEMA_READY = False


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def ensure_tables(conn=None) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and conn is None:
        return
    own = conn is None
    if own:
        conn = get_connection()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS lumina_clients (
            id TEXT PRIMARY KEY, seq INTEGER DEFAULT 0,
            name TEXT DEFAULT '', contact TEXT DEFAULT '', requirements TEXT DEFAULT '',
            deadline TEXT DEFAULT '', budget TEXT DEFAULT '', notes TEXT DEFAULT '',
            status TEXT DEFAULT '', url TEXT DEFAULT '', created_time TEXT DEFAULT '',
            archived INTEGER DEFAULT 0, local_modified INTEGER DEFAULT 0,
            notion_raw TEXT DEFAULT '', imported_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS lumina_tasks (
            id TEXT PRIMARY KEY,
            title TEXT DEFAULT '', client_name TEXT DEFAULT '', client_id TEXT DEFAULT '',
            assigned_to TEXT DEFAULT '', due_date TEXT DEFAULT '', creation_date TEXT DEFAULT '',
            status TEXT DEFAULT '', progress INTEGER DEFAULT 0, service TEXT DEFAULT '',
            description TEXT DEFAULT '', url TEXT DEFAULT '', type_prop TEXT DEFAULT '',
            brief_p TEXT DEFAULT '', content_p TEXT DEFAULT '', idea_p TEXT DEFAULT '',
            scripts_p TEXT DEFAULT '', caption_p TEXT DEFAULT '', link_p TEXT DEFAULT '',
            created_time TEXT DEFAULT '', last_edited TEXT DEFAULT '',
            archived INTEGER DEFAULT 0, local_modified INTEGER DEFAULT 0,
            notion_raw TEXT DEFAULT '', imported_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lt_client ON lumina_tasks(client_id, archived)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lt_created ON lumina_tasks(created_time)")
        conn.commit()
        _SCHEMA_READY = True
    finally:
        if own:
            conn.close()


def _conn():
    ensure_tables()
    return get_connection()


def is_configured() -> bool:
    return True


# ── row <-> dict ──────────────────────────────────────────────────────────────

def _parse_last_edited(raw: str) -> dict:
    if not raw:
        return {"at": "", "by": "", "summary": ""}
    parts = raw.split("|", 2)
    return {"at": parts[0] if len(parts) > 0 else "",
            "by": parts[1] if len(parts) > 1 else "",
            "summary": parts[2] if len(parts) > 2 else ""}


_TASK_COLS = ("id,title,client_name,client_id,assigned_to,due_date,creation_date,status,progress,"
              "service,description,url,type_prop,brief_p,content_p,idea_p,scripts_p,caption_p,link_p,"
              "created_time,last_edited")


def _task_dict(r) -> dict:
    (tid, title, client_name, client_id, assigned_to, due_date, creation_date, status, progress,
     service, desc, url, type_prop, brief, content, idea, scripts_copy, caption, file_link,
     created_time, last_edited_raw) = r
    desc = desc or ""
    brief, content, idea = brief or "", content or "", idea or ""
    scripts_copy, caption, file_link = scripts_copy or "", caption or "", file_link or ""
    # Same Notes parsing notion_store.list_tasks has always done.
    if not brief and desc:
        for pt in [p.strip() for p in desc.split("|")]:
            pl = pt.lower()
            if pl.startswith("brief:"): brief = pt[6:].strip()
            elif pl.startswith("content:"): content = pt[8:].strip()
            elif pl.startswith("idea:"): idea = pt[5:].strip()
            elif pl.startswith("scripts:") or pl.startswith("script:"): scripts_copy = pt.split(":", 1)[1].strip()
            elif pl.startswith("scripts/copy:") or pl.startswith("script/copy:"): scripts_copy = pt.split(":", 1)[1].strip()
            elif pl.startswith("caption:"): caption = pt[8:].strip()
            elif pl.startswith("link:"): file_link = pt[5:].strip()
            elif pl.startswith("file:"): file_link = pt[5:].strip()
    le = _parse_last_edited(last_edited_raw or "")
    return {
        "notion_id": tid,
        "title": title or "",
        "client_name": client_name or "",
        "client_notion_id": client_id or "",
        "assigned_to": assigned_to or "",
        "due_date": due_date or "",
        "creation_date": creation_date or "",
        "status": status or "",
        "progress": progress or 0,
        "service": service or "",
        "description": desc,
        "url": url or "",
        "type": (type_prop or service or ""),
        "brief": brief,
        "content": content,
        "idea": idea,
        "scripts_copy": scripts_copy,
        "caption": caption,
        "file_link": file_link,
        "created_time": created_time or "",
        "last_edited_by": le["by"],
        "last_edited_at": le["at"],
        "last_edited_summary": le["summary"],
    }


_CLIENT_COLS = "id,name,contact,requirements,deadline,budget,notes,status,url"


def _client_dict(r) -> dict:
    return {"notion_id": r[0], "name": r[1] or "", "contact": r[2] or "", "requirements": r[3] or "",
            "deadline": r[4] or "", "budget": r[5] or "", "notes": r[6] or "", "status": r[7] or "",
            "url": r[8] or ""}


# ── CLIENTS ───────────────────────────────────────────────────────────────────

def create_client(name: str, contact: str = "", requirements: str = "",
                  deadline: str = "", budget: str = "", notes: str = "",
                  status: str = "active") -> Optional[dict]:
    try:
        cid = str(uuid.uuid4())
        conn = _conn()
        try:
            seq = (conn.execute("SELECT COALESCE(MAX(seq),0) FROM lumina_clients").fetchone()[0] or 0) + 1
            conn.execute(
                "INSERT INTO lumina_clients (id,seq,name,contact,requirements,deadline,budget,notes,status,"
                "created_time,local_modified,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                (cid, seq, str(name or ""), contact or "", requirements or "", deadline or "", budget or "",
                 notes or "", (status or "").strip(), _now_iso(), _now_iso()))
            conn.commit()
        finally:
            conn.close()
        logger.info(f"local_store: created client '{name}' - {cid}")
        return {"notion_id": cid, "name": name}
    except Exception:
        logger.exception(f"local_store create_client failed for '{name}'")
        return None


def list_clients(status_filter: str = "") -> list:
    try:
        conn = _conn()
        try:
            q = f"SELECT {_CLIENT_COLS} FROM lumina_clients WHERE archived=0"
            args = []
            if status_filter:
                q += " AND status=?"
                args.append(status_filter)
            q += " ORDER BY seq ASC, created_time ASC"
            return [_client_dict(r) for r in conn.execute(q, args).fetchall()]
        finally:
            conn.close()
    except Exception:
        logger.exception("local_store list_clients failed")
        return []


def _update(table: str, row_id: str, fields: dict) -> bool:
    if not fields:
        return True
    conn = _conn()
    try:
        exists = conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (row_id,)).fetchone()
        if not exists:
            return False
        sets = ", ".join(f"{k}=?" for k in fields) + ", local_modified=1, updated_at=?"
        conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", [*fields.values(), _now_iso(), row_id])
        conn.commit()
        return True
    finally:
        conn.close()


def append_client_requirements(notion_id: str, extra_text: str) -> bool:
    if not notion_id or not extra_text:
        return False
    try:
        conn = _conn()
        try:
            row = conn.execute("SELECT requirements FROM lumina_clients WHERE id=?", (notion_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        current = row[0] or ""
        combined = (current + "\n" + extra_text) if current else extra_text
        return _update("lumina_clients", notion_id, {"requirements": combined})
    except Exception:
        logger.exception(f"local_store append_client_requirements failed for {notion_id}")
        return False


def update_client(notion_id: str, name=None, contact=None, requirements=None,
                  deadline=None, budget=None, notes=None, status=None) -> bool:
    if not notion_id:
        return False
    f = {}
    if name is not None: f["name"] = str(name)
    if contact is not None: f["contact"] = contact
    if requirements is not None: f["requirements"] = requirements
    if deadline is not None: f["deadline"] = deadline
    if budget is not None: f["budget"] = budget
    if notes is not None: f["notes"] = notes
    if status: f["status"] = str(status).strip()
    try:
        return _update("lumina_clients", notion_id, f)
    except Exception:
        logger.exception(f"local_store update_client failed for {notion_id}")
        return False


def update_client_status(notion_id: str, status: str) -> bool:
    try:
        return _update("lumina_clients", notion_id, {"status": (status or "").strip()})
    except Exception:
        logger.exception(f"local_store update_client_status failed for {notion_id}")
        return False


# ── TASKS ─────────────────────────────────────────────────────────────────────

def create_task(title: str, client_name: str, client_notion_id: str,
                assigned_to: str = "", due_date: str = "",
                status: str = "not_started", progress: int = 0,
                service: str = "", notes: str = "", creation_date: str = "") -> Optional[dict]:
    try:
        tid = str(uuid.uuid4())
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO lumina_tasks (id,title,client_name,client_id,assigned_to,due_date,creation_date,"
                "status,progress,service,description,created_time,local_modified,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (tid, str(title or ""), client_name or "", client_notion_id or "", assigned_to or "",
                 due_date or "", creation_date or "", (status or "").strip(),
                 int(progress) if progress is not None else 0, (service or "").strip(), notes or "",
                 _now_iso(), _now_iso()))
            conn.commit()
        finally:
            conn.close()
        logger.info(f"local_store: created task '{title}' for client '{client_name}'")
        return {"notion_id": tid, "title": title}
    except Exception:
        logger.exception(f"local_store create_task failed for '{title}'")
        return None


def list_tasks(assigned_to: str = "", client_notion_id: str = "",
               status_filter: str = "") -> list:
    try:
        q = f"SELECT {_TASK_COLS} FROM lumina_tasks WHERE archived=0"
        args = []
        if client_notion_id:
            q += " AND client_id=?"
            args.append(client_notion_id)
        if status_filter:
            if status_filter == "EMPTY":
                q += " AND (status IS NULL OR status='')"
            else:
                q += " AND status=?"
                args.append(status_filter)
        q += " ORDER BY created_time DESC, rowid DESC"
        conn = _conn()
        try:
            tasks = [_task_dict(r) for r in conn.execute(q, args).fetchall()]
        finally:
            conn.close()
        if assigned_to:
            return [t for t in tasks if assigned_to.lower() in (t.get("assigned_to", "") or "").lower()]
        return tasks
    except Exception:
        logger.exception("local_store list_tasks failed")
        return []


def _fetch_task_row(notion_id: str):
    conn = _conn()
    try:
        return conn.execute(f"SELECT {_TASK_COLS} FROM lumina_tasks WHERE id=?", (notion_id,)).fetchone()
    finally:
        conn.close()


def get_task_type(notion_id: str) -> str:
    if not notion_id:
        return ""
    try:
        r = _fetch_task_row(notion_id)
        if r is None:
            return ""
        ptype = (r[12] or "") or (r[9] or "")           # Type, else Task Type (service)
        if ptype:
            return ptype
        title = r[1] or ""
        if title and re.search(r'\[(Story|Static|Reel|Carousel|Post|Video)\]', title, re.IGNORECASE):
            return "Social Media"
        return ""
    except Exception:
        logger.exception(f"local_store get_task_type failed for {notion_id}")
        return ""


def get_task_summary(notion_id: str) -> dict:
    if not notion_id:
        return {}
    try:
        r = _fetch_task_row(notion_id)
        if r is None:
            return {}
        desc = r[10] or ""
        content = r[14] or ""
        if not content and desc:
            for pt in [p.strip() for p in desc.split("|")]:
                if pt.lower().startswith("content:"):
                    content = pt[8:].strip()
                    break
        return {
            "title": r[1] or "",
            "client_name": r[2] or "",
            "client_notion_id": r[3] or "",
            "content": content or desc,
            "description": desc,
            "creation_date": r[6] or "",
        }
    except Exception:
        logger.exception(f"local_store get_task_summary failed for {notion_id}")
        return {}


def update_task(notion_id: str, status: str = None, progress: int = None,
                submission_note: str = None, assigned_to: str = None,
                new_title: str = None, due_date: str = None, creation_date: str = None,
                task_title: str = "", assignee: str = "", client_name: str = "",
                last_edited: str = None) -> bool:
    f = {}
    if status is not None:
        f["status"] = STATUS_MAP.get(status.lower(), status)
        f["status"] = str(f["status"]).strip()
    if progress is not None:
        f["progress"] = int(progress)
    if submission_note is not None:
        f["description"] = submission_note
    if assigned_to is not None:
        f["assigned_to"] = assigned_to
    if new_title is not None:
        f["title"] = str(new_title)
    if due_date is not None:
        f["due_date"] = due_date
    if creation_date is not None:
        f["creation_date"] = creation_date
    if last_edited is not None:
        f["last_edited"] = last_edited
    if not f:
        return True
    try:
        if not _update("lumina_tasks", notion_id, f):
            return False
        if status and notify_task_status_changed:
            try:
                notify_task_status_changed(
                    task_title=task_title or "(unnamed task)",
                    assignee=assignee or "Team member",
                    client_name=client_name or "Unknown client",
                    old_status="", new_status=status)
            except Exception:
                logger.warning("WhatsApp notification failed (non-fatal)", exc_info=True)
        return True
    except Exception:
        logger.exception(f"local_store update_task failed for {notion_id}")
        return False


def set_client_id(notion_id: str, client_notion_id: str) -> bool:
    if not notion_id or not client_notion_id:
        return False
    try:
        return _update("lumina_tasks", notion_id, {"client_id": client_notion_id})
    except Exception:
        logger.exception(f"local_store set_client_id failed for {notion_id}")
        return False


def is_client_active(client_notion_id: str) -> bool:
    if not client_notion_id:
        return False
    try:
        conn = _conn()
        try:
            r = conn.execute("SELECT archived FROM lumina_clients WHERE id=?", (client_notion_id,)).fetchone()
        finally:
            conn.close()
        return bool(r) and not r[0]
    except Exception:
        logger.exception(f"local_store is_client_active failed for {client_notion_id}")
        return False


def _set_archived(page_id: str, value: int) -> bool:
    if not page_id:
        return False
    try:
        for table in ("lumina_tasks", "lumina_clients"):
            if _update(table, page_id, {"archived": value}):
                return True
        return False
    except Exception:
        logger.exception(f"local_store archive({value}) failed for {page_id}")
        return False


def archive_notion_page(page_id: str) -> bool:
    return _set_archived(page_id, 1)


def unarchive_notion_page(page_id: str) -> bool:
    return _set_archived(page_id, 0)


def get_schema_report() -> dict:
    conn = _conn()
    try:
        t = conn.execute("SELECT COUNT(*) FROM lumina_tasks WHERE archived=0").fetchone()[0]
        c = conn.execute("SELECT COUNT(*) FROM lumina_clients WHERE archived=0").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "configured": True, "backend": "local", "tasks": t, "clients": c}


def counts() -> dict:
    conn = _conn()
    try:
        g = lambda q: conn.execute(q).fetchone()[0]
        return {"tasks_active": g("SELECT COUNT(*) FROM lumina_tasks WHERE archived=0"),
                "tasks_archived": g("SELECT COUNT(*) FROM lumina_tasks WHERE archived=1"),
                "clients_active": g("SELECT COUNT(*) FROM lumina_clients WHERE archived=0"),
                "clients_archived": g("SELECT COUNT(*) FROM lumina_clients WHERE archived=1"),
                "tasks_locally_modified": g("SELECT COUNT(*) FROM lumina_tasks WHERE local_modified=1"),
                "clients_locally_modified": g("SELECT COUNT(*) FROM lumina_clients WHERE local_modified=1")}
    finally:
        conn.close()


def export_all() -> dict:
    """Everything, including archived rows and the original Notion JSON, for backup."""
    conn = _conn()
    try:
        out = {}
        for table in ("lumina_clients", "lumina_tasks"):
            cur = conn.execute(f"SELECT * FROM {table}")
            cols = [d[0] for d in cur.description]
            out[table] = [dict(zip(cols, row)) for row in cur.fetchall()]
        return out
    finally:
        conn.close()
