"""
routes/student_routes.py
────────────────────────
CRUD endpoints for the Dissertation Students roster (focal_students.json).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..config import load_students, save_students, upsert_student, update_student_phase, archive_student

bp = Blueprint("students", __name__)

VALID_STATUSES = ["Active", "Graduated", "Transferred", "On Hold"]


@bp.route("/api/students", methods=["GET"])
def api_list_students():
    """Return all students, optionally filtered by ?status=Active."""
    status_filter = request.args.get("status", "").strip()
    students = load_students()
    if status_filter:
        students = [s for s in students if s.get("status", "Active") == status_filter]
    return jsonify({"students": students})


@bp.route("/api/students", methods=["POST"])
def api_upsert_student():
    """Create or update a student record (upsert by student_name)."""
    body = request.json or {}
    name = body.get("student_name", "").strip()
    if not name:
        return jsonify({"error": "student_name is required"}), 400

    record = {
        "student_name":  name,
        "chair":         body.get("chair",         "").strip(),
        "degree":        body.get("degree",        "").strip(),
        "my_role":       body.get("my_role",       "").strip(),
        "program":       body.get("program",       "").strip(),
        "current_phase": body.get("current_phase", "").strip(),
        "status":        body.get("status",        "Active").strip(),
        "notes":         body.get("notes",         "").strip(),
    }
    if record["status"] not in VALID_STATUSES:
        return jsonify({"error": f"status must be one of {VALID_STATUSES}"}), 400

    try:
        upsert_student(record)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/students/update-phase", methods=["POST"])
def api_update_student_phase():
    """Update only current_phase for a student (called after Quick Add with checkbox)."""
    body = request.json or {}
    name  = body.get("student_name",  "").strip()
    phase = body.get("current_phase", "").strip()
    if not name or not phase:
        return jsonify({"error": "student_name and current_phase required"}), 400
    found = update_student_phase(name, phase)
    return jsonify({"ok": True, "updated": found})


@bp.route("/api/students/archive", methods=["POST"])
def api_archive_student():
    """Archive a student: marks as Removed with a required reason. Record is kept forever."""
    body   = request.json or {}
    name   = body.get("student_name", "").strip()
    reason = body.get("reason", "").strip()
    if not name:
        return jsonify({"error": "student_name required"}), 400
    if not reason:
        return jsonify({"error": "reason is required"}), 400
    found = archive_student(name, reason)
    if not found:
        return jsonify({"error": f"Student '{name}' not found"}), 404
    return jsonify({"ok": True})
