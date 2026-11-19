"""Discovery questionnaire -> formatted Excel, saved in the DB and mailed.

build_xlsx() makes a two-sheet workbook (client summary + question/answer
list grouped by section). finalize_submission() runs after a client submits:
builds the file, stores it on the row (discovery_submissions.xlsx_blob so it
can be downloaded later) and emails it to Vidit and Kshitij. All best-effort:
a failure here never affects the client's submission.
"""
import io
import json
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

RECIPIENT_IDS = ("emp001", "emp004")  # Vidit, Kshitij
ORANGE = "FF6A00"
DARK = "1A1A1F"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _template(form_id: str) -> list:
    try:
        from db import get_connection
        conn = get_connection()
        row = conn.execute("SELECT schema_json FROM form_templates WHERE id=?", (form_id,)).fetchone()
        conn.close()
        return json.loads(row[0]) if row else []
    except Exception:
        logger.exception("discovery_export: template load failed")
        return []


def safe_name(company: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", company or "client").strip("_") or "client"
    return f"Discovery_{base[:40]}.xlsx"


def build_xlsx(company: str, email: str, answers: dict, form_id: str, submitted_at: str = "") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    template = _template(form_id)
    answers = dict(answers or {})
    when = submitted_at or datetime.now().strftime("%Y-%m-%d %H:%M")

    thin = Side(style="thin", color="D9D9D9")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    orange = PatternFill("solid", fgColor=ORANGE)
    dark = PatternFill("solid", fgColor=DARK)
    band = PatternFill("solid", fgColor="FFF1E6")
    wrap = Alignment(wrap_text=True, vertical="top")

    wb = Workbook()

    # ── Sheet 1: summary ──
    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 56
    ws.merge_cells("A1:B1")
    ws["A1"] = "MMGA Lumina  |  Discovery Questionnaire"
    ws["A1"].font = Font(name="Calibri", size=16, bold=True, color="FFFFFF")
    ws["A1"].fill = dark
    ws["A1"].alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[1].height = 34
    answered = sum(1 for v in answers.values() if str(v or "").strip())
    rows = [("Company", company), ("Email", email), ("Submitted", when),
            ("Questionnaire", form_id), ("Questions answered", f"{answered} of {len(template) or len(answers)}")]
    for i, (k, v) in enumerate(rows, start=3):
        a, b = ws.cell(i, 1, k), ws.cell(i, 2, v)
        a.font = Font(bold=True, color="FFFFFF")
        a.fill = orange
        a.border = b.border = box
        b.alignment = wrap
        ws.row_dimensions[i].height = 22

    # ── Sheet 2: answers grouped by section ──
    wa = wb.create_sheet("Answers")
    wa.sheet_view.showGridLines = False
    wa.column_dimensions["A"].width = 6
    wa.column_dimensions["B"].width = 46
    wa.column_dimensions["C"].width = 84
    for c, h in enumerate(("#", "Question", "Answer"), start=1):
        cell = wa.cell(1, c, h)
        cell.font = Font(bold=True, color="FFFFFF", size=12)
        cell.fill = dark
        cell.alignment = Alignment(vertical="center", indent=1)
    wa.row_dimensions[1].height = 26
    wa.freeze_panes = "A2"

    r, n, section = 2, 0, None
    known = set()
    for q in template:
        known.add(q.get("id"))
        sec = q.get("section") or "General"
        if sec != section:
            section = sec
            wa.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
            c = wa.cell(r, 1, sec)
            c.font = Font(bold=True, color="FFFFFF", size=12)
            c.fill = orange
            c.alignment = Alignment(vertical="center", indent=1)
            wa.row_dimensions[r].height = 24
            r += 1
        n += 1
        ans = str(answers.get(q.get("id")) or "").strip()
        vals = (n, q.get("label", ""), ans or "(not answered)")
        for c, v in enumerate(vals, start=1):
            cell = wa.cell(r, c, v)
            cell.border = box
            cell.alignment = wrap
            if n % 2 == 0:
                cell.fill = band
        wa.cell(r, 2).font = Font(bold=True)
        if not ans:
            wa.cell(r, 3).font = Font(italic=True, color="999999")
        wa.row_dimensions[r].height = max(22, min(300, 16 * (1 + len(ans) // 80 + ans.count("\n"))))
        r += 1

    # answers whose question was later removed from the template
    left = [(k, v) for k, v in answers.items() if k not in known and str(v or "").strip()]
    if left:
        wa.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        c = wa.cell(r, 1, "Other answers")
        c.font = Font(bold=True, color="FFFFFF", size=12)
        c.fill = orange
        r += 1
        for k, v in left:
            n += 1
            for c, val in enumerate((n, f"{k} (archived question)", str(v)), start=1):
                cell = wa.cell(r, c, val)
                cell.border = box
                cell.alignment = wrap
            r += 1

    wa.page_setup.orientation = "landscape"
    wa.page_setup.fitToWidth = 1
    wa.sheet_properties.pageSetUpPr.fitToPage = True
    wa.page_setup.fitToHeight = 0

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def finalize_submission(sub_id: int, company: str, email: str, answers: dict, form_id: str) -> None:
    """Build + store the workbook, then email it to Vidit and Kshitij."""
    try:
        data = build_xlsx(company, email, answers, form_id)
    except Exception:
        logger.exception("discovery_export: build failed")
        return
    try:
        from db import get_connection
        conn = get_connection()
        with conn:
            conn.execute("UPDATE discovery_submissions SET xlsx_blob=? WHERE id=?", (data, sub_id))
        conn.close()
    except Exception:
        logger.exception("discovery_export: store failed")
    try:
        import mailer
        to = [e for e in (mailer.email_for(i) for i in RECIPIENT_IDS) if e]
        if not to:
            return
        fn = safe_name(company)
        base = "https://lumina.mmga.agency"
        body = (f"{company} has submitted the Discovery Questionnaire.\n\n"
                f"Company: {company}\nEmail:   {email or '(not given)'}\n"
                f"Answered: {sum(1 for v in (answers or {}).values() if str(v or '').strip())} questions\n\n"
                f"The answers are attached as an Excel file ({fn}).\n"
                f"You can also download it any time from {base}/client-forms-db.html")
        mailer.send(to, f"New discovery questionnaire: {company}", body,
                    reply_to=email or "", from_name="Lumina",
                    attachments=[(fn, data, XLSX_MIME)])
    except Exception:
        logger.exception("discovery_export: mail failed")
