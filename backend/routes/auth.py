import os
import json
import secrets
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
from flask import Blueprint, request, jsonify
import logging
from werkzeug.security import generate_password_hash, check_password_hash

from utils import _load_employees, _save_employees, _is_admin, now_ist, today_ist, IST
from extensions import limiter

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__)

# ── PIN-login lockout (security warning #8) ─────────────────────────────────
_LOCKOUT_THRESHOLD = 5           # wrong PINs before locking
_LOCKOUT_MINUTES = 15


def _lockout_status(user_id: str):
    """Returns (locked: bool, retry_after_seconds: int) for this user_id."""
    conn = _sessions_conn()
    row = conn.execute(
        "SELECT locked_until FROM login_lockout WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return False, 0
    try:
        until = datetime.fromisoformat(row[0])
    except ValueError:
        return False, 0
    now = datetime.utcnow()
    if now >= until:
        return False, 0
    return True, int((until - now).total_seconds())


def _record_login_failure(user_id: str):
    conn = _sessions_conn()
    with conn:
        row = conn.execute(
            "SELECT fail_count FROM login_lockout WHERE user_id=?", (user_id,)
        ).fetchone()
        fail_count = (row[0] if row else 0) + 1
        locked_until = None
        if fail_count >= _LOCKOUT_THRESHOLD:
            locked_until = (datetime.utcnow() + timedelta(minutes=_LOCKOUT_MINUTES)).isoformat()
        conn.execute(
            "INSERT INTO login_lockout (user_id, fail_count, locked_until) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET fail_count=excluded.fail_count, "
            "locked_until=excluded.locked_until",
            (user_id, fail_count, locked_until),
        )
    conn.close()


def _clear_login_failures(user_id: str):
    conn = _sessions_conn()
    with conn:
        conn.execute(
            "INSERT INTO login_lockout (user_id, fail_count, locked_until) VALUES (?, 0, NULL) "
            "ON CONFLICT(user_id) DO UPDATE SET fail_count=0, locked_until=NULL",
            (user_id,),
        )
    conn.close()

def _sessions_conn():
    from db import get_connection
    return get_connection()

def _attendance_conn():
    from db import get_connection
    return get_connection()

def _attendance_checkout(user_id: str):
    """Always updates checkout_time to latest IST logout (UPSERT)."""
    d = today_ist()
    t = now_ist()
    ts = datetime.now(IST).isoformat(timespec="seconds")
    conn = _attendance_conn()
    with conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO daily_attendance (user_id, date, checkout_time, checkout_auto)
               VALUES (?, ?, ?, 0)
               ON CONFLICT(user_id, date) DO UPDATE SET
                   checkout_time = excluded.checkout_time, checkout_auto = 0""",
            (user_id, d, t),
        )
        conn.execute(
            "INSERT INTO attendance (user_id, action, timestamp) VALUES (?, 'out', ?)",
            (user_id, ts),
        )
        cur.execute(
            "SELECT checkout_time FROM daily_attendance WHERE user_id=? AND date=?",
            (user_id, d),
        )
    conn.close()
    return d, t

def _create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"
    conn = _sessions_conn()
    with conn:
        conn.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)",
                     (token, user_id, expires))
    conn.close()
    return token

def _verify_session(token: str) -> Optional[str]:
    """Returns user_id if token is valid and not expired, else None."""
    if not token:
        return None
    conn = _sessions_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    user_id, expires_at = row
    if datetime.utcnow().isoformat() + "Z" > expires_at:
        return None  # Expired
    return user_id

@auth_bp.route("/api/auth/login", methods=["POST"])
@limiter.limit("20 per minute")
def auth_login():
    """PIN login — returns a server-side session token."""
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id", "").strip()
    pin     = body.get("pin", "").strip()

    if not user_id or not pin:
        return jsonify({"error": "user_id and pin required"}), 400

    locked, retry_after = _lockout_status(user_id)
    if locked:
        minutes = max(1, retry_after // 60)
        return jsonify({"error": f"Too many incorrect attempts. Try again in {minutes} min."}), 429

    data = _load_employees()
    emp = next((e for e in data.get("employees", []) if e["id"] == user_id), None)
    if not emp:
        return jsonify({"error": "Employee not found"}), 404

    if emp.get("pin", "0000") != pin:
        _record_login_failure(user_id)
        return jsonify({"error": "Incorrect PIN"}), 401

    _clear_login_failures(user_id)
    token = _create_session(user_id)
    resp = jsonify({
        "success": True,
        "token":   token,
        "user":    {"id": emp["id"], "name": emp["name"], "role": emp["role"],
                    "is_admin": _is_admin(user_id)},
    })
    # Set HttpOnly cookie so JS can't read the token (XSS protection)
    is_secure = os.getenv("FLASK_ENV", "development") != "development"
    resp.set_cookie(
        "session_token", token,
        httponly=True,
        samesite="Lax",
        secure=is_secure,
        max_age=30 * 24 * 60 * 60,  # 30 days
    )
    return resp

@auth_bp.route("/api/auth/verify", methods=["GET"])
def auth_verify():
    """Verify a session token. Used by frontend auth guard."""
    token = (
        request.cookies.get("session_token", "")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
        or request.headers.get("X-Session-Token", "")
        or request.args.get("token", "")
    )
    user_id = _verify_session(token)
    if not user_id:
        return jsonify({"valid": False}), 401
        
    # Implicitly ensure the user is checked "in" for today. This runs on every
    # page load/refresh, so it must only undo a checkout the SYSTEM made
    # (sweep_stale_checkouts: the heartbeat went quiet, the person is clearly
    # back now). It used to clear checkout_time unconditionally, which silently
    # reverted an explicit Check Out / Logout the moment the page was refreshed.
    # A manual checkout (checkout_auto = 0) now sticks until they check in again.
    # The check-in time must follow the work-day rules (nothing before 06:00,
    # nothing earlier than 09:00): a raw page-load time at 01:33 used to become
    # the day's check-in and inflated overtime.
    from routes.attendance import _too_early_for_checkin, _clamp_work_time
    _now = now_ist()
    _cin = None if _too_early_for_checkin(_now) else _clamp_work_time(_now)
    conn = _attendance_conn()
    with conn:
        conn.execute("""
            INSERT INTO daily_attendance (user_id, date, checkin_time)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
                checkin_time  = COALESCE(daily_attendance.checkin_time, excluded.checkin_time),
                checkout_time = CASE WHEN daily_attendance.checkout_auto = 1 THEN NULL ELSE daily_attendance.checkout_time END,
                checkout_auto = CASE WHEN daily_attendance.checkout_auto = 1 THEN 0 ELSE daily_attendance.checkout_auto END
        """, (user_id, today_ist(), _cin))
    conn.close()
    data = _load_employees()
    emp = next((e for e in data.get("employees", []) if e["id"] == user_id), {})
    return jsonify({"valid": True, "user_id": user_id, "name": emp.get("name",""),
                    "role": emp.get("role",""), "is_admin": _is_admin(user_id)})

@auth_bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    """Invalidate a session token and clear the HttpOnly cookie."""
    token = (
        request.cookies.get("session_token", "")
        or (request.get_json(silent=True) or {}).get("token", "")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
    )
    if token:
        user_id = _verify_session(token)
        if user_id:
            _attendance_checkout(user_id)
        conn = _sessions_conn()
        with conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.close()
    resp = jsonify({"success": True})
    resp.delete_cookie("session_token")
    return resp

@auth_bp.route("/api/auth/change_pin", methods=["POST"])
@limiter.limit("10 per minute")
def auth_change_pin():
    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")
    old_pin = body.get("old_pin")
    new_pin = body.get("new_pin")

    # Security warning #8: this used to trust a body-supplied user_id with
    # no session check at all -- knowing someone else's old PIN (even
    # correctly) while logged in as yourself was enough to change theirs.
    # Now the caller's own session must belong to the exact user_id they're
    # changing the PIN for.
    token = request.cookies.get("session_token", "")
    session_user = _verify_session(token)
    if not session_user or session_user != user_id:
        return jsonify({"error": "Unauthorized"}), 403

    if not new_pin or not str(new_pin).strip():
        return jsonify({"error": "new_pin required"}), 400

    data = _load_employees()
    found = False
    for emp in data.get("employees", []):
        if emp.get("id") == user_id:
            if str(emp.get("pin")) != str(old_pin):
                return jsonify({"error": "Incorrect old PIN"}), 401
            emp["pin"] = new_pin
            found = True
            break
            
    if not found:
        return jsonify({"error": "Employee not found"}), 404
        
    try:
        _save_employees(data)
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Failed to save new PIN: {e}")
        return jsonify({"error": "Could not save PIN"}), 500


# ══════════════════════════════════════════════════════════════════════════════
# CLIENT PORTAL AUTH  (separate from employee auth)
# ══════════════════════════════════════════════════════════════════════════════

def _create_client_session(client_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat() + "Z"
    conn = _sessions_conn()
    with conn:
        conn.execute(
            "INSERT INTO client_sessions(token, client_id, expires_at) VALUES(?,?,?)",
            (token, client_id, expires)
        )
    conn.close()
    return token


def _verify_client_session(token: str):
    """Returns client row dict if valid, else None."""
    if not token:
        return None
    conn = _sessions_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT cs.client_id, cs.expires_at, cu.username, cu.client_name, cu.client_notion_id
           FROM client_sessions cs
           JOIN client_users cu ON cu.id = cs.client_id
           WHERE cs.token = ?""",
        (token,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    client_id, expires_at, username, client_name, client_notion_id = row
    if datetime.utcnow().isoformat() + "Z" > expires_at:
        return None
    return {
        "id": client_id,
        "client_id": client_id,
        "username": username,
        "client_name": client_name,
        "client_notion_id": client_notion_id or "",
    }


@auth_bp.route("/api/auth/client_login", methods=["POST"])
def client_login():
    """Username + password login for client portal."""
    body = request.get_json(silent=True) or {}
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()

    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    conn = _sessions_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, password, client_name, client_notion_id FROM client_users WHERE username = ?",
        (username,)
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Invalid username or password"}), 401

    client_id, stored_password, client_name, client_notion_id = row

    # Password check with seamless upgrade for legacy plaintext
    if stored_password == password:
        # Valid plaintext password. Upgrade to hash seamlessly.
        new_hash = generate_password_hash(password)
        conn = _sessions_conn()
        with conn:
            conn.execute("UPDATE client_users SET password=? WHERE id=?", (new_hash, client_id))
        conn.close()
    elif not check_password_hash(stored_password, password):
        return jsonify({"error": "Invalid username or password"}), 401

    token = _create_client_session(client_id)
    is_secure = os.getenv("FLASK_ENV", "development") != "development"

    resp = jsonify({
        "success": True,
        "client": {
            "id": client_id,
            "username": username,
            "client_name": client_name,
            "client_notion_id": client_notion_id or "",
            "is_client": True,
        }
    })
    resp.set_cookie(
        "client_session_token", token,
        httponly=True,
        samesite="Lax",
        secure=is_secure,
        max_age=30 * 24 * 60 * 60,
    )
    return resp


@auth_bp.route("/api/auth/client_verify", methods=["GET"])
def client_verify():
    """Verify a client session token. Used by client-auth.js guard."""
    token = (
        request.cookies.get("client_session_token", "")
        or request.headers.get("X-Client-Token", "")
        or request.args.get("token", "")
    )
    client = _verify_client_session(token)
    if not client:
        return jsonify({"valid": False}), 401
    return jsonify({"valid": True, **client})


@auth_bp.route("/api/auth/client_logout", methods=["POST"])
def client_logout():
    """Invalidate a client session token."""
    token = request.cookies.get("client_session_token", "")
    if token:
        conn = _sessions_conn()
        with conn:
            conn.execute("DELETE FROM client_sessions WHERE token=?", (token,))
        conn.close()
    resp = jsonify({"success": True})
    resp.delete_cookie("client_session_token")
    return resp


@auth_bp.route("/api/auth/clients", methods=["GET"])
def list_client_users():
    """List all client portal accounts. Admin use."""
    token = request.cookies.get("client_admin_token", "")
    if not _verify_admin_session(token):
        return jsonify({"error": "Unauthorized"}), 401
    conn = _sessions_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, username, client_name, client_notion_id, created_at, "
                "COALESCE(whatsapp,'') FROM client_users ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    clients = [
        {"id": r[0], "username": r[1], "client_name": r[2],
         "client_notion_id": r[3] or "", "created_at": r[4], "whatsapp": r[5] or ""}
        for r in rows
    ]
    return jsonify({"clients": clients})


@auth_bp.route("/api/auth/clients", methods=["POST"])
def create_client_user():
    """Create a new client portal account. Admin only."""
    token = request.cookies.get("client_admin_token", "")
    if not _verify_admin_session(token):
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    client_name = body.get("client_name", "").strip()
    client_notion_id = body.get("client_notion_id", "").strip()
    whatsapp = body.get("whatsapp", "").strip()

    if not username or not password or not client_name:
        return jsonify({"error": "username, password, and client_name are required"}), 400

    hashed_password = generate_password_hash(password)

    conn = _sessions_conn()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO client_users (username, password, client_name, client_notion_id, whatsapp) VALUES (?,?,?,?,?)",
                (username, hashed_password, client_name, client_notion_id, whatsapp)
            )
            new_id = cur.lastrowid
        conn.close()
        return jsonify({"success": True, "id": new_id, "username": username, "client_name": client_name}), 201
    except Exception as e:
        conn.close()
        if "UNIQUE" in str(e):
            return jsonify({"error": "Username already exists"}), 409
        logger.error(f"Failed to create client user: {e}")
        return jsonify({"error": "Could not create client account"}), 500


@auth_bp.route("/api/auth/clients/<int:client_id>", methods=["DELETE"])
def delete_client_user(client_id):
    """Delete a client portal account."""
    token = request.cookies.get("client_admin_token", "")
    if not _verify_admin_session(token):
        return jsonify({"error": "Unauthorized"}), 401
    conn = _sessions_conn()
    with conn:
        conn.execute("DELETE FROM client_sessions WHERE client_id=?", (client_id,))
        conn.execute("DELETE FROM client_users WHERE id=?", (client_id,))
    conn.close()
    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PORTAL AUTH (agency admin login — separate from employees & clients)
# Password stored as CLIENT_ADMIN_PASSWORD env var (fallback: "admin2024")
# ══════════════════════════════════════════════════════════════════════════════

def _create_admin_session() -> str:
    """Create a client_sessions row tagged as admin (client_id = -1)."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=7)).isoformat() + "Z"
    conn = _sessions_conn()
    with conn:
        # We abuse client_id = -1 as a sentinel for the agency admin
        conn.execute(
            "INSERT OR REPLACE INTO client_sessions(token, client_id, expires_at) VALUES(?,?,?)",
            (token, -1, expires)
        )
    conn.close()
    return token


def _verify_admin_session(token: str) -> bool:
    """Returns True if the token belongs to a valid, non-expired admin session."""
    if not token:
        return False
    conn = _sessions_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT expires_at FROM client_sessions WHERE token=? AND client_id=?",
        (token, -1)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    expires_at = row[0]
    return datetime.utcnow().isoformat() + "Z" <= expires_at


@auth_bp.route("/api/auth/admin_portal_login", methods=["POST"])
def admin_portal_login():
    """Agency admin login for the client portal admin page."""
    body = request.get_json(silent=True) or {}
    password = body.get("password", "").strip()

    correct = os.getenv("CLIENT_ADMIN_PASSWORD", "admin2024")
    if not password or password != correct:
        return jsonify({"error": "Incorrect admin password"}), 401

    token = _create_admin_session()
    is_secure = os.getenv("FLASK_ENV", "development") != "development"

    resp = jsonify({
        "success": True,
        "is_admin": True,
    })
    resp.set_cookie(
        "client_admin_token", token,
        httponly=True,
        samesite="Lax",
        secure=is_secure,
        max_age=7 * 24 * 60 * 60,
    )
    return resp


@auth_bp.route("/api/auth/admin_portal_verify", methods=["GET"])
def admin_portal_verify():
    """Verify admin portal session."""
    token = request.cookies.get("client_admin_token", "")
    if not _verify_admin_session(token):
        return jsonify({"valid": False}), 401
    return jsonify({"valid": True, "is_admin": True})


@auth_bp.route("/api/auth/admin_portal_logout", methods=["POST"])
def admin_portal_logout():
    """Invalidate admin portal session."""
    token = request.cookies.get("client_admin_token", "")
    if token:
        conn = _sessions_conn()
        with conn:
            conn.execute("DELETE FROM client_sessions WHERE token=?", (token,))
        conn.close()
    resp = jsonify({"success": True})
    resp.delete_cookie("client_admin_token")
    return resp


@auth_bp.route("/api/auth/clients/<int:client_id>", methods=["PUT"])
def update_client_user(client_id):
    """Update an existing client portal account (username, password, client_name, notion_id)."""
    # Verify admin session
    token = request.cookies.get("client_admin_token", "")
    if not _verify_admin_session(token):
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    updates = []
    values = []

    if "username" in body:
        updates.append("username=?")
        values.append(body["username"].strip())
    if "password" in body and body["password"].strip():
        updates.append("password=?")
        values.append(generate_password_hash(body["password"].strip()))
    if "client_name" in body:
        updates.append("client_name=?")
        values.append(body["client_name"].strip())
    if "client_notion_id" in body:
        updates.append("client_notion_id=?")
        values.append(body["client_notion_id"].strip())
    if "whatsapp" in body:
        updates.append("whatsapp=?")
        values.append(body["whatsapp"].strip())

    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    values.append(client_id)
    conn = _sessions_conn()
    try:
        with conn:
            conn.execute(
                f"UPDATE client_users SET {', '.join(updates)} WHERE id=?",
                values
            )
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        conn.close()
        if "UNIQUE" in str(e):
            return jsonify({"error": "Username already exists"}), 409
        logger.error(f"Failed to update client user {client_id}: {e}")
        return jsonify({"error": "Could not update client"}), 500


