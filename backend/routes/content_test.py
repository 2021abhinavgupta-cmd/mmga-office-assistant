"""Content-writing test: the timed, no-paste questionnaire (frontend/content-test.html).

A candidate fills it in on the public page; when it ends (or the page is closed
part-way) the answers are stored here and sent as WhatsApp messages to the leads
who review it (Kshitij and Noorish by default) as an emailed Excel sheet.

  POST /api/content-test              public; one submission per run_id
  GET  /api/content-test-submissions  employee session; list of stored results
"""
import json
import logging
import re
import threading

from flask import Blueprint, jsonify, request

from db import get_connection

logger = logging.getLogger(__name__)
content_test_bp = Blueprint("content_test", __name__)

DEFAULT_RECIPIENTS = ["emp004", "emp009"]   # Kshitij, Noorish
MAX_BODY_CHARS = 200_000
CHUNK = 3400
_STATUS_LABEL = {
    "answered": "answered",
    "timed-out": "time ran out (kept what was typed)",
    "skipped-tab": "SKIPPED: left the tab",
    "skipped-time": "skipped: time ran out, nothing typed",
    "skipped-blank": "skipped: nothing typed",
    "skipped-left": "not reached: page was closed or reloaded",
    "left-page": "page closed or reloaded during this question (kept what was typed)",
}


def _ensure():
    conn = get_connection()
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS content_test_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE,
            form_id TEXT,
            candidate_name TEXT NOT NULL,
            email TEXT NOT NULL,
            answers_json TEXT NOT NULL,
            meta_json TEXT,
            partial INTEGER DEFAULT 0,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.close()


def _recipients():
    ids = list(DEFAULT_RECIPIENTS)
    try:
        conn = get_connection()
        row = conn.execute("SELECT value FROM app_settings WHERE key='content_test_recipient_ids'").fetchone()
        conn.close()
        if row and row[0]:
            ids = [x.strip() for x in str(row[0]).replace(",", " ").split() if x.strip()]
    except Exception:
        logger.debug("content_test: recipient lookup failed", exc_info=True)
    return ids


def _clean(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def build_messages(name, email, attempt, answers, meta, partial):
    """The report, split into WhatsApp-sized messages."""
    qs = (meta or {}).get("questions") or []
    answered = sum(1 for q in qs if q.get("status") in ("answered", "timed-out"))
    tabs = sum(1 for q in qs if q.get("status") == "skipped-tab")
    pastes = sum(int(q.get("pastes") or 0) for q in qs)
    used = sum(int(q.get("used") or 0) for q in qs)
    head = [f"*Content writing test: {name}*", f"Email: {email}"]
    if attempt > 1:
        head.append(f"Attempt number {attempt} for this email")
    head.append(f"Answered {answered} of {len(qs)} | left the tab {tabs} time(s) | paste/copy attempts {pastes} | "
                f"time used {used // 60}m {used % 60}s")
    if partial:
        head.append(f"INCOMPLETE: {(meta or {}).get('reason') or 'the page was closed before the end'}")
    blocks = ["\n".join(head)]
    for i, q in enumerate(qs, 1):
        text = str((answers or {}).get(q.get("id"), "") or "").strip() or "(no answer)"
        st = _STATUS_LABEL.get(q.get("status"), q.get("status") or "")
        extra = f", paste attempts {q.get('pastes')}" if q.get("pastes") else ""
        blocks.append(f"*{i}. {q.get('label') or q.get('id')}*\n[{st}; {q.get('used', 0)}s of {q.get('seconds', 0)}s{extra}]\n{text}")
    out, cur = [], ""
    for b in blocks:
        while len(b) > CHUNK:               # one huge answer: cut on a space
            cut = b.rfind(" ", 0, CHUNK) or CHUNK
            piece, b = b[:cut], b[cut:].lstrip()
            if cur:
                out.append(cur); cur = ""
            out.append(piece)
        if cur and len(cur) + len(b) + 2 > CHUNK:
            out.append(cur); cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        out.append(cur)
    n = len(out)
    return [f"({i}/{n})\n{m}" if n > 1 else m for i, m in enumerate(out, 1)]


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_xlsx(name, email, attempt, answers, meta, partial):
    """One workbook: a summary block, then one row per question."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    qs = (meta or {}).get("questions") or []
    wb = Workbook()
    ws = wb.active
    ws.title = "Test result"
    orange = PatternFill("solid", fgColor="FF6A00")
    side = Side(style="thin", color="D9D9D9")
    box = Border(left=side, right=side, top=side, bottom=side)
    wrap = Alignment(wrap_text=True, vertical="top")
    answered = sum(1 for q in qs if q.get("status") in ("answered", "timed-out", "left-page"))
    tabs = sum(1 for q in qs if q.get("status") == "skipped-tab")
    pastes = sum(int(q.get("pastes") or 0) for q in qs)
    used = sum(int(q.get("used") or 0) for q in qs)

    import content_test_key as key
    by_id = {q.get("id"): q for q in qs}
    a_pts = sum(key.auto_points(i, (by_id.get(i) or {}).get("fields")) or 0 for i in key.A_KEY)
    wpm, acc = key.typing_speed((answers or {}).get("Q0"), (by_id.get("Q0") or {}).get("used"))
    c4 = key.auto_points("C4", (by_id.get("C4") or {}).get("fields"))

    ws.merge_cells("A1:I1")
    ws["A1"] = f"Content writing test: {name}"
    ws["A1"].font = Font(bold=True, size=15, color="FFFFFF")
    ws["A1"].fill = orange
    ws.row_dimensions[1].height = 28
    info = [("Email", email), ("Attempt", attempt),
            ("Answered", f"{answered} of {len(qs)}"),
            ("Times left the tab", tabs), ("Paste / copy attempts", pastes),
            ("Time used", f"{used // 60}m {used % 60}s"),
            ("Attitude (Section A, automatic)", f"{a_pts} of 15 = {round(a_pts / 15 * 100)}%  (below 60% is a no-hire on attitude)"),
            ("C4 hook pick, automatic", "n/a" if c4 is None else f"{c4} of 2 (the reason needs a person: 0 or 1)"),
            ("Typing speed (Q0)", "n/a" if wpm is None else f"{wpm} words per minute, {acc}% accurate"),
            ("How to score the rest", "Sections B and C need a person. " + key.SCALE),
            ("Finished", "Incomplete: " + ((meta or {}).get("reason") or "page was closed") if partial else "Yes, submitted")]
    r = 2
    for k, v in info:
        ws.cell(r, 1, k).font = Font(bold=True)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=9)
        ws.cell(r, 2, v)
        r += 1
    r += 1
    heads = ["#", "Question", "Status", "Seconds allowed", "Seconds used", "Paste attempts", "Answer", "Auto points", "Rubric (for the scorer)"]
    for c, t in enumerate(heads, 1):
        cell = ws.cell(r, c, t)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1A1A1F")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = box
    r += 1
    for i, q in enumerate(qs, 1):
        text = str((answers or {}).get(q.get("id"), "") or "").strip()
        row = [i, q.get("label") or q.get("id"), _STATUS_LABEL.get(q.get("status"), q.get("status") or ""),
               q.get("seconds", 0), q.get("used", 0), q.get("pastes", 0), text or "(no answer)",
               ("" if key.auto_points(q.get("id"), q.get("fields")) is None else key.auto_points(q.get("id"), q.get("fields"))),
               key.RUBRIC.get(q.get("id"), "")]
        for c, v in enumerate(row, 1):
            cell = ws.cell(r, c, v)
            cell.alignment = wrap
            cell.border = box
        if q.get("status") == "skipped-tab":
            ws.cell(r, 3).font = Font(bold=True, color="D92D20")
        ws.row_dimensions[r].height = max(30, min(400, 15 * (1 + len(text) // 70 + text.count("\n"))))
        r += 1
    for col, w in zip("ABCDEFGHI", (5, 42, 30, 12, 12, 12, 80, 10, 55)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def safe_name(name):
    base = re.sub(r"[^A-Za-z0-9]+", "_", name or "candidate").strip("_") or "candidate"
    return f"Content_test_{base[:40]}.xlsx"


def notify(name, email, attempt, answers, meta, partial):
    """Email the result sheet to the reviewers (no WhatsApp, no client mail)."""
    try:
        import mailer
        to = [e for e in (mailer.email_for(i) for i in _recipients()) if e]
        if not to:
            logger.warning("content_test: no reviewer email addresses")
            return
        data = build_xlsx(name, email, attempt, answers, meta, partial)
        qs = (meta or {}).get("questions") or []
        done = sum(1 for q in qs if q.get("status") in ("answered", "timed-out", "left-page"))
        tabs = sum(1 for q in qs if q.get("status") == "skipped-tab")
        pastes = sum(int(q.get("pastes") or 0) for q in qs)
        body = (f"{name} has taken the content writing test.\n\n"
                f"Email: {email}\n"
                f"Attempt: {attempt}\n"
                f"Answered: {done} of {len(qs)}\n"
                f"Left the tab: {tabs} time(s)\n"
                f"Paste / copy attempts: {pastes}\n"
                + (f"\nINCOMPLETE: {(meta or {}).get('reason') or 'the page was closed before the end'}\n" if partial else "")
                + "\nThe full answers are in the attached Excel sheet.")
        mailer.send(to, f"Content writing test: {name}" + (" (incomplete)" if partial else ""), body,
                    reply_to=email, from_name="Lumina",
                    attachments=[(safe_name(name), data, XLSX_MIME)])
        logger.info("content_test: emailed result for %s to %s", name, to)
    except Exception:
        logger.exception("content_test: notify failed")


@content_test_bp.route("/api/content-test", methods=["POST"])
def submit_content_test():
    try:
        raw = request.get_data(cache=True) or b""
        if len(raw) > MAX_BODY_CHARS * 2:
            return jsonify({"success": False, "error": "Submission too large"}), 413
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
        name, email = _clean(data.get("name"), 120), _clean(data.get("email"), 160)
        run_id = _clean(data.get("run_id"), 64)
        if not name or not email or "@" not in email:
            return jsonify({"success": False, "error": "Name and a valid email are required"}), 400
        answers = {str(k)[:40]: str(v or "")[:20000] for k, v in (data.get("answers") or {}).items()}
        meta = data.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        partial = bool(data.get("partial"))
        form_id = _clean(data.get("form_id"), 60) or "content_writing_test"
        _ensure()
        conn = get_connection()
        try:
            if run_id and conn.execute("SELECT 1 FROM content_test_submissions WHERE run_id=?", (run_id,)).fetchone():
                return jsonify({"success": True, "duplicate": True})
            attempt = conn.execute(
                "SELECT COUNT(*) FROM content_test_submissions WHERE lower(email)=lower(?) AND form_id=?",
                (email, form_id)).fetchone()[0] + 1
            with conn:
                conn.execute(
                    "INSERT INTO content_test_submissions (run_id, form_id, candidate_name, email, answers_json, meta_json, partial) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (run_id or None, form_id, name, email, json.dumps(answers), json.dumps(meta), 1 if partial else 0))
        finally:
            conn.close()
        threading.Thread(target=notify, args=(name, email, attempt, answers, meta, partial), daemon=True).start()
        return jsonify({"success": True})
    except Exception:
        logger.exception("content_test submit failed")
        return jsonify({"success": False, "error": "Could not save the test"}), 500


@content_test_bp.route("/api/content-test-submissions", methods=["GET"])
def list_content_test_submissions():
    from routes.auth import _verify_session
    if not _verify_session(request.cookies.get("session_token", "")):
        return jsonify({"error": "Unauthorized"}), 403
    _ensure()
    conn = get_connection()
    rows = conn.execute("SELECT id, form_id, candidate_name, email, answers_json, meta_json, partial, submitted_at "
                        "FROM content_test_submissions ORDER BY id DESC").fetchall()
    conn.close()
    import content_test_key as key
    out = []
    for r in rows:
        meta = json.loads(r[5] or "{}")
        answers = json.loads(r[4] or "{}")
        qs = meta.get("questions") or []
        by_id = {q.get("id"): q for q in qs}
        for q in qs:                                   # points and rubric are added here, never stored
            q["auto_points"] = key.auto_points(q.get("id"), q.get("fields"))
            q["rubric"] = key.RUBRIC.get(q.get("id"), "")
        a_pts = sum(key.auto_points(i, (by_id.get(i) or {}).get("fields")) or 0 for i in key.A_KEY)
        wpm, acc = key.typing_speed(answers.get("Q0"), (by_id.get("Q0") or {}).get("used"))
        summary = {"attitude_points": a_pts, "attitude_pct": round(a_pts / 15 * 100),
                   "c4_points": key.auto_points("C4", (by_id.get("C4") or {}).get("fields")),
                   "wpm": wpm, "accuracy": acc,
                   "answered": sum(1 for q in qs if q.get("status") in ("answered", "timed-out", "left-page")),
                   "total": len(qs),
                   "tab_leaves": sum(1 for q in qs if q.get("status") == "skipped-tab"),
                   "pastes": sum(int(q.get("pastes") or 0) for q in qs)}
        out.append({"id": r[0], "form_id": r[1], "name": r[2], "email": r[3], "answers": answers,
                    "meta": meta, "partial": bool(r[6]), "submitted_at": r[7], "summary": summary})
    return jsonify({"success": True, "submissions": out})


@content_test_bp.route("/api/content-test-submissions/<int:sub_id>/xlsx", methods=["GET"])
def download_content_test_xlsx(sub_id):
    from flask import Response
    from routes.auth import _verify_session
    if not _verify_session(request.cookies.get("session_token", "")):
        return jsonify({"error": "Unauthorized"}), 403
    _ensure()
    conn = get_connection()
    row = conn.execute("SELECT candidate_name, email, answers_json, meta_json, partial, form_id FROM content_test_submissions WHERE id=?", (sub_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    attempt = conn.execute("SELECT COUNT(*) FROM content_test_submissions WHERE lower(email)=lower(?) AND form_id=? AND id<=?",
                           (row[1], row[5], sub_id)).fetchone()[0]
    conn.close()
    data = build_xlsx(row[0], row[1], attempt, json.loads(row[2] or "{}"), json.loads(row[3] or "{}"), bool(row[4]))
    return Response(data, mimetype=XLSX_MIME,
                    headers={"Content-Disposition": f"attachment;filename={safe_name(row[0])}"})
