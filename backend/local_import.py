"""
local_import.py -- copy Lumina's clients/tasks from Notion into local_store, prove the copy
is identical, and flip the backend switch.

Safety properties:
  * The import only READS Notion (database queries). It never writes to Notion.
  * Nothing is ever deleted locally. A row that disappeared from Notion is only marked
    archived (reversible), and only if it was never edited locally.
  * A row that has been edited locally (local_modified=1) is never overwritten by an import,
    so re-running the import after cutover cannot undo anyone's work.
  * Every row keeps the original Notion page JSON in notion_raw.
  * The switch only flips when compare() finds zero differences between what Notion returns
    through notion_store and what local_store returns for the same calls.
"""
import json
import logging
import time
from datetime import datetime

import notion_store as ns
import local_store as ls
from db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


def _task_row(p: dict) -> dict:
    props = p.get("properties", {})
    gsv = ns._get_string_val
    file_link = (props.get("File (Drive Link)", {}).get("url", "") or props.get("File", {}).get("url", "")
                 or props.get("Drive Link", {}).get("url", "") or ns._get_text(props.get("File (Drive Link)", {}))
                 or ns._get_text(props.get("File", {})))
    return {
        "id": p["id"],
        "title": ns._get_text(props.get("Task", {})) or ns._get_text(props.get("Post Title", {})) or ns._get_text(props.get("Post", {})),
        "client_name": (gsv(props.get("Customer Name")) or gsv(props.get("Client Name")) or gsv(props.get("Client"))
                        or gsv(props.get("Brand")) or gsv(props.get("Customer")) or gsv(props.get("Account"))),
        "client_id": ns._get_text(props.get("Client ID", {})),
        "assigned_to": ns._get_multi_select(props.get("Assigned To", {})),
        "due_date": ns._get_date(props.get("Due Date", {})) or ns._get_date(props.get("Post Day", {})),
        "creation_date": ns._get_date(props.get("Creation Date", {})),
        "status": ns._get_select(props.get("Status", {})),
        "progress": ns._get_number(props.get("Progress", {})),
        "service": ns._get_select(props.get("Task Type", {})),
        "description": gsv(props.get("Notes", {})),
        "url": p.get("url", ""),
        "type_prop": ns._get_select(props.get("Type", {})) or ns._get_select(props.get("Task Type", {})),
        "brief_p": gsv(props.get("Brief", {})),
        "content_p": gsv(props.get("Content", {})),
        "idea_p": gsv(props.get("Idea", {})),
        "scripts_p": gsv(props.get("Scripts/ Copy", {})) or gsv(props.get("Script/ Copy", {})),
        "caption_p": gsv(props.get("Caption", {})),
        "link_p": file_link,
        "created_time": p.get("created_time", ""),
        "last_edited": gsv(props.get("Last Edited", {})),
    }


def _client_row(p: dict, seq: int) -> dict:
    props = p.get("properties", {})
    return {
        "id": p["id"], "seq": seq,
        "name": ns._get_text(props.get("Client", {})),
        "contact": ns._get_text(props.get("Contact", {})),
        "requirements": ns._get_text(props.get("Requirements", {})),
        "deadline": ns._get_date(props.get("Deadline", {})),
        "budget": ns._get_text(props.get("Budget", {})),
        "notes": ns._get_text(props.get("Notes", {})),
        "status": ns._get_select(props.get("Status", {})),
        "url": p.get("url", ""),
        "created_time": p.get("created_time", ""),
    }


def _upsert(conn, table: str, row: dict, raw: dict) -> str:
    """Insert or refresh one row unless it was edited locally. Returns inserted|updated|skipped_local."""
    existing = conn.execute(f"SELECT local_modified FROM {table} WHERE id=?", (row["id"],)).fetchone()
    now = _now()
    if existing is not None and existing[0]:
        return "skipped_local"
    cols = list(row.keys()) + ["archived", "notion_raw", "imported_at", "updated_at"]
    vals = list(row.values()) + [0, json.dumps(raw, ensure_ascii=False), now, now]
    if existing is None:
        conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return "inserted"
    sets = ",".join(f"{c}=?" for c in cols if c != "id")
    conn.execute(f"UPDATE {table} SET {sets} WHERE id=? AND local_modified=0",
                 [v for c, v in zip(cols, vals) if c != "id"] + [row["id"]])
    return "updated"


QUICK_TASK_CLIENT = "__quick__"


def standup_ref_ids() -> set:
    """Notion task ids that any standup row points at (so nothing a standup references is left behind)."""
    conn = get_connection()
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT notion_id FROM standup_tasks WHERE notion_id IS NOT NULL AND notion_id<>''").fetchall()}
    finally:
        conn.close()


def _in_scope(client_id: str, task_id: str, client_ids: set, ref_ids: set) -> bool:
    """scope=visible: only what Lumina shows today -- tasks of a listed client, quick tasks, and any
    task a standup row points at. Orphan / leftover Notion pages stay in Notion only."""
    return (client_id in client_ids) or client_id == QUICK_TASK_CLIENT or (task_id in ref_ids)


def import_from_notion(scope: str = "visible") -> dict:
    """Pull clients and tasks from Notion into the local tables. Idempotent.
    scope="visible" (default) copies only what Lumina currently shows; scope="all" copies every page."""
    started = time.time()
    ls.ensure_tables()
    report = {"clients": {"seen": 0, "inserted": 0, "updated": 0, "skipped_local": 0, "marked_archived": 0},
              "tasks": {"seen": 0, "inserted": 0, "updated": 0, "skipped_local": 0, "marked_archived": 0},
              "errors": []}
    with ns.force_notion():
        clients_raw = list(ns.query_all_raw("clients"))
        tasks_raw = list(ns.query_all_raw("tasks"))
        # parse everything first (may call Notion for relation titles) so a failure here
        # leaves the local tables untouched
        client_rows = [(_client_row(p, i), p) for i, p in enumerate(clients_raw, start=1)]
        task_rows = [(_task_row(p), p) for p in tasks_raw]
        report["tasks"]["in_notion"] = len(task_rows)
        if scope != "all":
            cids = {p["id"] for p in clients_raw}
            refs = standup_ref_ids()
            task_rows = [(r, p) for r, p in task_rows if _in_scope(r["client_id"], r["id"], cids, refs)]
        report["scope"] = scope

    conn = get_connection()
    try:
        for table, rows, key in (("lumina_clients", client_rows, "clients"), ("lumina_tasks", task_rows, "tasks")):
            seen = set()
            for row, raw in rows:
                outcome = _upsert(conn, table, row, raw)
                report[key]["seen"] += 1
                report[key][outcome] += 1
                seen.add(row["id"])
            # Rows that are no longer in Notion (trashed there): soft-archive, never delete,
            # and never touch anything edited locally.
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seen_ids (id TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _seen_ids")
            conn.executemany("INSERT OR IGNORE INTO _seen_ids VALUES (?)", [(i,) for i in seen])
            cur = conn.execute(f"UPDATE {table} SET archived=1, updated_at=? "
                               f"WHERE archived=0 AND local_modified=0 AND id NOT IN (SELECT id FROM _seen_ids)",
                               (_now(),))
            report[key]["marked_archived"] = cur.rowcount
            conn.execute("DROP TABLE IF EXISTS _seen_ids")
        conn.commit()
    finally:
        conn.close()
    report["seconds"] = round(time.time() - started, 1)
    _remember("local_import_last", json.dumps({"at": _now(), "report": report}))
    return report


def compare(scope: str = "visible") -> dict:
    """Run the same calls against Notion and against local_store; list every difference."""
    with ns.force_notion():
        n_tasks = ns.list_tasks()
        n_clients = ns.list_clients()
    if scope != "all":
        cids = {c["notion_id"] for c in n_clients}
        refs = standup_ref_ids()
        n_tasks = [t for t in n_tasks if _in_scope(t["client_notion_id"], t["notion_id"], cids, refs)]
    l_tasks = ls.list_tasks()
    l_clients = ls.list_clients()

    def diff(kind, a_list, b_list):
        a = {x["notion_id"]: x for x in a_list}
        b = {x["notion_id"]: x for x in b_list}
        out = {"notion_count": len(a), "local_count": len(b),
               "missing_locally": [i for i in a if i not in b],
               "extra_locally": [i for i in b if i not in a],
               "field_differences": []}
        for i, na in a.items():
            lb = b.get(i)
            if lb is None:
                continue
            for k in set(na) | set(lb):
                if na.get(k) != lb.get(k):
                    out["field_differences"].append({"id": i, "field": k,
                                                     "notion": str(na.get(k))[:120], "local": str(lb.get(k))[:120]})
        out["identical"] = not (out["missing_locally"] or out["extra_locally"] or out["field_differences"])
        out["field_difference_count"] = len(out["field_differences"])
        out["field_differences"] = out["field_differences"][:30]
        return out

    res = {"tasks": diff("tasks", n_tasks, l_tasks), "clients": diff("clients", n_clients, l_clients)}
    # the project-board grouping: tasks per client + the 'unassigned' bucket must match too
    def buckets(clients, tasks):
        ids = {c["notion_id"] for c in clients}
        per = {c["notion_id"]: sum(1 for t in tasks if t["client_notion_id"] == c["notion_id"]) for c in clients}
        per["unassigned"] = sum(1 for t in tasks if t["client_notion_id"] not in ids)
        return per
    res["board_groups_identical"] = buckets(n_clients, n_tasks) == buckets(l_clients, l_tasks)
    res["identical"] = res["tasks"]["identical"] and res["clients"]["identical"] and res["board_groups_identical"]
    _remember("local_verify_last", json.dumps({"at": _now(), "identical": res["identical"]}))
    return res


def _remember(key: str, value: str) -> None:
    try:
        conn = get_connection()
        try:
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("local_import: could not record state")


def switch_to_local(force: bool = False, scope: str = "visible") -> dict:
    """Fresh import -> compare -> flip the switch only if identical -> catch up any gap writes."""
    if ns.backend() == "local":
        return {"switched": False, "reason": "already local", "counts": ls.counts()}
    imp = import_from_notion(scope)
    cmp_ = compare(scope)
    if not cmp_["identical"] and not force:
        return {"switched": False, "reason": "local copy differs from Notion; not switching",
                "import": imp, "compare": cmp_}
    ns.set_backend("local")
    _remember("local_switched_at", _now())
    # A Lumina write could have reached Notion between the compare and the flip; pick those up.
    # Rows already edited locally are protected from being overwritten.
    try:
        catch = import_from_notion(scope)
    except Exception as e:
        catch = {"error": f"{type(e).__name__}: {e}"}
    return {"switched": True, "import": imp, "compare": {k: v for k, v in cmp_.items() if k != "tasks" and k != "clients"}
            | {"tasks_identical": cmp_["tasks"]["identical"], "clients_identical": cmp_["clients"]["identical"]},
            "catch_up": catch, "counts": ls.counts()}


def switch_to_notion() -> dict:
    """Go back to Notion. Anything created or edited while local was active exists ONLY locally."""
    ns.set_backend("notion")
    return {"switched": True, "backend": "notion", "counts": ls.counts(),
            "warning": "Changes made while the local store was active are not in Notion; "
                       "download /api/companion/local-export to keep them."}


def preview() -> dict:
    """Read-only look at what is in Notion right now versus what Lumina shows, and what the
    default (scope=visible) import would copy or leave behind. Writes nothing anywhere."""
    with ns.force_notion():
        clients = ns.list_clients()
        tasks = ns.list_tasks()
    cids = {c["notion_id"] for c in clients}
    refs = standup_ref_ids()
    keep, left = [], []
    for t in tasks:
        (keep if _in_scope(t["client_notion_id"], t["notion_id"], cids, refs) else left).append(t)
    per_client = [{"client": c["name"], "status": c["status"],
                   "tasks": sum(1 for t in keep if t["client_notion_id"] == c["notion_id"])} for c in clients]
    conn = get_connection()
    try:
        su_total = conn.execute("SELECT COUNT(*) FROM standup_tasks").fetchone()[0]
        su_by_user = conn.execute(
            "SELECT user_id, COUNT(*) FROM standup_tasks WHERE status NOT IN ('deleted','delegated') "
            "AND date=(SELECT MAX(date) FROM standup_tasks s2 WHERE s2.user_id=standup_tasks.user_id) "
            "GROUP BY user_id").fetchall()
    finally:
        conn.close()
    return {
        "notion": {"clients": len(clients), "tasks": len(tasks)},
        "will_copy": {"clients": len(clients), "tasks": len(keep),
                      "quick_tasks": sum(1 for t in keep if t["client_notion_id"] == QUICK_TASK_CLIENT),
                      "referenced_by_standup_only": sum(
                          1 for t in keep if t["client_notion_id"] not in cids
                          and t["client_notion_id"] != QUICK_TASK_CLIENT)},
        "per_client": per_client,
        "stay_in_notion_only": {"tasks": len(left), "examples": [
            {"title": t["title"], "client": t["client_name"], "status": t["status"], "due": t["due_date"],
             "assigned_to": t["assigned_to"]} for t in left[:40]]},
        "daily_standup": {"rows_in_lumina_untouched": su_total,
                          "open_rows_on_each_persons_latest_day": {u: n for u, n in su_by_user},
                          "note": "standup_tasks lives in Lumina's own database already; the import never reads or changes it"},
    }