"""Popup feed for tasks proposed from the team group chat (backend/group_actions.py).
Identity is always the session cookie, never a body field."""
from __future__ import annotations

from flask import Blueprint, jsonify

import group_actions
from routes.leave import _require_session

group_proposals_bp = Blueprint("group_proposals", __name__)


@group_proposals_bp.route("/api/group-proposals", methods=["GET"])
def proposals_list():
    uid, err = _require_session()
    if err:
        return err
    return jsonify({"proposals": group_actions.pending_for(uid)})


@group_proposals_bp.route("/api/group-proposals/<int:pid>/accept", methods=["POST"])
def proposals_accept(pid: int):
    uid, err = _require_session()
    if err:
        return err
    ok, msg, tid = group_actions.accept(uid, pid)
    return jsonify({"success": ok, "message": msg, "task_id": tid}), (200 if ok else 404 if msg == "not found" else 409)


@group_proposals_bp.route("/api/group-proposals/<int:pid>/reject", methods=["POST"])
def proposals_reject(pid: int):
    uid, err = _require_session()
    if err:
        return err
    ok, msg = group_actions.reject(uid, pid)
    return jsonify({"success": ok, "message": msg}), (200 if ok else 404 if msg == "not found" else 409)
