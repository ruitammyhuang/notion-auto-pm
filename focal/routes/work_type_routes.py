"""
routes/work_type_routes.py
──────────────────────────
Work type management: list, create, inspect.

Endpoints:
  GET  /api/work-types/full    -- all active types with name+color+description
  GET  /api/work-types/all     -- all types including deprecated (for health check)
  POST /api/work-types/create  -- create a new work type and push to Notion
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..config import load_config
from ..work_type_manager import (
    get_work_types,
    get_valid_names,
    save_work_type,
    NOTION_COLORS,
)

bp = Blueprint("work_types", __name__)


@bp.route("/api/work-types/full", methods=["GET"])
def api_work_types_full():
    """Return active work types with full metadata (name, color, description, examples)."""
    return jsonify({"work_types": get_work_types(include_deprecated=False)})


@bp.route("/api/work-types/all", methods=["GET"])
def api_work_types_all():
    """Return all work types including deprecated (used by health check analysis)."""
    return jsonify({"work_types": get_work_types(include_deprecated=True)})


@bp.route("/api/work-types/create", methods=["POST"])
def api_work_types_create():
    """
    Create a new work type, persist it to work_types.json, and push options
    to all Notion databases.

    Body (JSON):
        name        str  required  -- display name, should include leading emoji
        color       str  required  -- Notion palette color
        description str  optional  -- short description of when to use this type
        examples    list optional  -- example task names
        context     str  optional  -- why this type is needed (logged only)

    Returns:
        { ok, work_type: {name, color, description, ...}, notion_push: {ok_count, fail_count, errors} }
    """
    body        = request.json or {}
    name        = body.get("name", "").strip()
    color       = body.get("color", "").strip()
    description = body.get("description", "").strip()
    examples    = body.get("examples", [])
    context     = body.get("context", "").strip()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not color:
        return jsonify({"error": "color is required"}), 400
    if color not in NOTION_COLORS:
        return jsonify({"error": f"color must be one of: {', '.join(NOTION_COLORS)}"}), 400

    # Save to work_types.json
    try:
        new_type = save_work_type(
            name=name,
            color=color,
            description=description,
            examples=examples if isinstance(examples, list) else [],
            context=context,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Push updated options to all Notion databases
    cfg = load_config()
    token = cfg.get("token", "").strip()
    notion_result = {"ok_count": 0, "fail_count": 0, "errors": ["No Notion token configured"]}

    if token:
        try:
            from sync_work_type_options import push_to_all_dbs
            notion_result = push_to_all_dbs(token=token, cfg=cfg, verbose=False)
        except Exception as e:
            notion_result = {"ok_count": 0, "fail_count": 0, "errors": [str(e)]}

    return jsonify({
        "ok":          True,
        "work_type":   new_type,
        "work_types":  get_work_types(include_deprecated=False),
        "notion_push": notion_result,
    })


@bp.route("/api/work-types/names", methods=["GET"])
def api_work_types_names():
    """Return only the name strings (used by dropdowns that just need the list)."""
    return jsonify({"work_types": get_valid_names()})
