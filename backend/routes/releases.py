"""Popup feed for "what's new" (see backend/releases.py)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import releases
from routes.leave import _require_session

releases_bp = Blueprint("releases", __name__)


@releases_bp.route("/api/whatsnew", methods=["GET"])
def whatsnew_list():
    """Releases the logged-in person has not dismissed yet (identity = the session cookie)."""
    uid, err = _require_session()
    if err:
        return err
    return jsonify({"releases": releases.unseen_for(uid)})


@releases_bp.route("/api/whatsnew/all", methods=["GET"])
def whatsnew_all():
    """Full history of announcements (dashboard 'What's new' button)."""
    uid, err = _require_session()
    if err:
        return err
    return jsonify({"releases": releases.all_for(uid)})


@releases_bp.route("/api/whatsnew/seen", methods=["POST"])
def whatsnew_seen():
    uid, err = _require_session()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list):
        ids = [body.get("id")] if body.get("id") else []
    return jsonify({"success": True, "marked": releases.mark_seen(uid, ids)})
