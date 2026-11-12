"""
Leave Management Blueprint
Routes: /api/leave/*

Balance/calendar/apply/cancel are self-service (any real logged-in
employee, acting on their own leave). pending/approve/reject are HR-only,
hardcoded to emp009 (Noorish) -- same allowlist convention as the bet
feature in routes/ops.py, not the generic _is_admin()/bool(user_id)
pattern, since this is a real authority gate, not general app access.

Every route below is authenticated off the real session_token cookie
(routes.auth._verify_session()), never a client-supplied user_id param --
see routes/attendance.py::_verified_admin() for the identical pattern
this mirrors. A client-supplied user_id is just a string anyone can set
to anything, and combined with this app's CORS(origins="*"), trusting it
used to mean anyone could approve/reject/read any employee's leave, or
file leave in a colleague's name, by simply changing that one field.

See docs/superpowers/specs/2026-09-30-leave-management-design.md.
"""
from __future__ import annotations

import re

from flask import Blueprint, jsonify, request

import leave_store

leave_bp = Blueprint("leave", __name__)

HR_USER_ID = "emp009"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _session_user_id() -> str:
    """The real logged-in user id from the session_token cookie, or "" if
    the cookie is missing or the session is invalid/expired. This is the
    ONLY source of truth for identity in this file -- never a query
    param or JSON body field, which is just a string the caller wrote."""
    from routes.auth import _verify_session
    token = request.cookies.get("session_token", "")
    return _verify_session(token) or ""


def _require_session() -> tuple[str, tuple | None]:
    """(user_id, None) on a valid session, or ("", 401 response) with no
    valid session at all. Used by every self-service route -- the acting
    user is always the session holder, never a body/query-supplied id."""
    uid = _session_user_id()
    if not uid:
        return "", (jsonify({"error": "login required"}), 401)
    return uid, None


def _require_hr() -> tuple[str, tuple | None]:
    """(user_id, None) when the session belongs to HR (emp009); otherwise
    an error response -- 401 with no valid session at all, 403 for a real,
    valid session that just isn't HR's."""
    uid = _session_user_id()
    if not uid:
        return "", (jsonify({"error": "login required"}), 401)
    if uid != HR_USER_ID:
        return "", (jsonify({"error": "HR only"}), 403)
    return uid, None


@leave_bp.route("/api/leave/balance", methods=["GET"])
def leave_balance():
    uid, err = _require_session()
    if err:
        return err
    year = request.args.get("year", type=int)
    return jsonify(leave_store.get_balance(uid, year=year))


@leave_bp.route("/api/leave/calendar", methods=["GET"])
def leave_calendar():
    uid, err = _require_session()
    if err:
        return err
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if not year or not month or not (1 <= month <= 12):
        return jsonify({"error": "year and month (1-12) required"}), 400
    if not (2000 <= year <= 2100):
        return jsonify({"error": "year out of range"}), 400
    return jsonify({"days": leave_store.calendar_days(uid, year, month)})


@leave_bp.route("/api/leave/team", methods=["GET"])
def leave_team():
    """Approved leave of everyone else in a date range, so the whole team can see who was / is /
    will be on leave. Names and dates only: never the reason or the leave balance."""
    uid, err = _require_session()
    if err:
        return err
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if not (_DATE_RE.match(start) and _DATE_RE.match(end)) or end < start:
        return jsonify({"error": "start and end (YYYY-MM-DD) required"}), 400
    from db import get_connection
    import utils
    names = {e.get("id"): (e.get("name") or "").strip() for e in utils._load_employees().get("employees", [])}
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT user_id, start_date, end_date, leave_type FROM employee_leave "
            "WHERE status='approved' AND start_date <= ? AND end_date >= ? AND user_id != ? "
            "ORDER BY start_date", (end, start, uid)).fetchall()
    finally:
        conn.close()
    out = [{"user_id": r[0], "name": names.get(r[0]) or r[0], "start_date": max(r[1], start),
            "end_date": min(r[2], end), "leave_type": r[3] or "full"} for r in rows]
    return jsonify({"leaves": out})


@leave_bp.route("/api/leave/apply", methods=["POST"])
def leave_apply():
    uid, err = _require_session()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    date_ = (body.get("date") or "").strip()
    end_date = (body.get("end_date") or date_).strip()
    leave_type = (body.get("leave_type") or "full").strip().lower()
    reason = str(body.get("reason") or "")[:300]
    if not _DATE_RE.match(date_) or not _DATE_RE.match(end_date):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    try:
        row = leave_store.apply_leave(
            uid, date_, end_date, leave_type=leave_type, reason=reason, created_by=uid,
            email_subject=str(body.get("email_subject") or "")[:150],
            email_body=str(body.get("email_body") or "")[:3000])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(row)


@leave_bp.route("/api/leave/<int:leave_id>/cancel", methods=["POST"])
def leave_cancel(leave_id: int):
    """Cancel your own pending request; or an APPROVED leave (the owner before
    it starts, an approver until it ends)."""
    uid, err = _require_session()
    if err:
        return err
    if leave_store.cancel_leave(leave_id, uid):
        return jsonify({"success": True})
    ok, why = leave_store.cancel_approved(leave_id, uid)
    if ok:
        return jsonify({"success": True, "was": "approved"})
    return jsonify({"error": why or "nothing to cancel for this id/user"}), 404


@leave_bp.route("/api/leave/convert-ot", methods=["POST"])
def leave_convert_ot():
    """Self-service: convert the caller's own pending overtime into
    comp-off leave right now, instead of waiting for the nightly sweep.
    Whole 24h blocks convert; any leftover hours stay pending and carry
    forward (leave_store._convert_overtime_for_user)."""
    uid, err = _require_session()
    if err:
        return err
    return jsonify(leave_store.convert_overtime_now(uid))


def _require_approver() -> tuple[str, tuple | None]:
    """HR or a founder (leave_store.APPROVER_IDS), by real session."""
    uid = _session_user_id()
    if not uid:
        return "", (jsonify({"error": "login required"}), 401)
    if uid not in leave_store.APPROVER_IDS:
        return "", (jsonify({"error": "approvers only"}), 403)
    return uid, None


def _decide(leave_id: int, uid: str, action: str, reason: str = ""):
    """Shared by the signed-in routes. Returns (json-able dict, status)."""
    try:
        if action == "approve":
            ok = leave_store.approve_leave(leave_id, uid)
        else:
            ok = leave_store.reject_leave(leave_id, uid, reason=reason)
    except PermissionError as e:
        return {"error": str(e)}, 403
    except ValueError as e:
        return {"error": str(e)}, 400
    if ok:
        return {"success": True}, 200
    cur = leave_store.get_request(leave_id)
    if cur and cur["status"] != "pending":
        import mailer
        by = mailer.name_for(cur["decided_by"]) if cur.get("decided_by") else "someone"
        return {"error": f"already {cur['status']}" + (f" by {by}" if cur["status"] in ("approved", "rejected") else "")}, 409
    return {"error": "no pending request with that id"}, 404


@leave_bp.route("/api/leave/pending", methods=["GET"])
def leave_pending():
    """Approvers see what they can decide; view-only people (VIEWER_IDS) see
    every pending request but `can_decide` is false."""
    uid = _session_user_id()
    if not uid:
        return jsonify({"error": "login required"}), 401
    if uid in leave_store.APPROVER_IDS:
        return jsonify({"pending": leave_store.list_pending(uid), "can_decide": True})
    if uid in leave_store.VIEWER_IDS:
        return jsonify({"pending": leave_store.list_pending(), "can_decide": False})
    return jsonify({"error": "approvers only"}), 403


@leave_bp.route("/api/leave/<int:leave_id>/approve", methods=["POST"])
def leave_approve(leave_id: int):
    uid, err = _require_approver()
    if err:
        return err
    body, code = _decide(leave_id, uid, "approve")
    return jsonify(body), code


@leave_bp.route("/api/leave/<int:leave_id>/reject", methods=["POST"])
def leave_reject(leave_id: int):
    uid, err = _require_approver()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    out, code = _decide(leave_id, uid, "reject", str(body.get("reason") or "")[:300])
    return jsonify(out), code


# ── one-click decision from an email / WhatsApp link (no login) ──────────
# GET only shows a confirm page (mail scanners pre-fetch links, a GET must not
# decide anything); the button POSTs. The signed link says who is deciding.

def _page(title: str, inner: str, code: int = 200):
    from flask import Response
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>body{{font-family:system-ui,sans-serif;max-width:460px;"
            "margin:48px auto;padding:0 16px;color:#1a1a1a}}button{padding:10px 20px;border:0;"
            "border-radius:8px;font-size:15px;cursor:pointer;color:#fff}.a{background:#16a34a}"
            ".r{background:#dc2626}input,textarea{width:100%;padding:8px;margin:8px 0;box-sizing:border-box}"
            f"</style></head><body>{inner}</body></html>")
    return Response(html, status=code, mimetype="text/html")


@leave_bp.route("/api/leave/act", methods=["GET", "POST"])
def leave_act():
    import html as _h
    import mailer
    src = request.form if request.method == "POST" else request.args
    try:
        leave_id = int(src.get("id", ""))
    except ValueError:
        return _page("Leave", "<h3>Invalid link</h3>", 400)
    by, action, token = src.get("by", ""), src.get("a", ""), src.get("t", "")
    if action not in ("approve", "reject") or not leave_store.verify_action_token(leave_id, by, action, token):
        return _page("Leave", "<h3>This link isn't valid.</h3><p>Open Lumina to decide it.</p>", 403)
    cur = leave_store.get_request(leave_id)
    if not cur:
        return _page("Leave", "<h3>Request not found.</h3>", 404)
    who = _h.escape(mailer.name_for(cur["user_id"]))
    sd, ed = cur["start_date"], cur["end_date"]
    when = _h.escape(sd if sd == ed else f"{sd} to {ed}")
    kind = "half day" if cur["leave_type"] == "half" else "full day"
    if cur["status"] != "pending":
        dec = _h.escape(mailer.name_for(cur["decided_by"])) if cur.get("decided_by") else ""
        return _page("Leave", f"<h3>Already {_h.escape(cur['status'])}</h3><p>{who}, {when}"
                              + (f" was decided by {dec}." if dec and cur["status"] in ("approved", "rejected") else ".") + "</p>")
    verb = "Approve" if action == "approve" else "Reject"
    if request.method == "GET":
        reason_box = ("<label>Reason (optional, they are told)</label><textarea name='reason' rows='2'></textarea>"
                      if action == "reject" else "")
        return _page("Leave", (
            f"<h3>{verb} leave?</h3><p><b>{who}</b><br>{when} ({kind})<br>"
            f"{_h.escape(cur['reason'] or '')}</p>"
            "<form method='post'>"
            f"<input type='hidden' name='id' value='{leave_id}'><input type='hidden' name='by' value='{_h.escape(by)}'>"
            f"<input type='hidden' name='a' value='{action}'><input type='hidden' name='t' value='{_h.escape(token)}'>"
            f"{reason_box}<button class='{'a' if action == 'approve' else 'r'}'>{verb}</button></form>"))
    out, code = _decide(leave_id, by, action, str(src.get("reason") or "")[:300])
    if code == 200:
        return _page("Leave", f"<h3>Done</h3><p>{who}'s leave for {when} was {'approved' if action == 'approve' else 'rejected'}. They have been told.</p>")
    return _page("Leave", f"<h3>Not done</h3><p>{_h.escape(out.get('error', 'error'))}</p>", code)
