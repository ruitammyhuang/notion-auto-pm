"""
routes/work_type_routes.py
──────────────────────────
Work type management: list, create, update color, deprecate.

All changes persist to work_types.json and are immediately pushed to Notion.

Endpoints:
  GET  /api/work-types/full      -- active types with name+color+description
  POST /api/work-types/create    -- create new type, push to Notion
  POST /api/work-types/update    -- update color/description, push to Notion
  POST /api/work-types/deprecate -- soft-delete a type, push to Notion
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..config import WORK_SESSIONS_DB_ID, load_config
from ..notion_client import NotionClient
from ..work_type_manager import (
    NOTION_COLORS,
    deprecate_work_type,
    get_work_type_options,
    get_work_types,
    save_work_type,
    update_work_type,
)

bp = Blueprint("work_types", __name__)


def _merge_options(client: NotionClient, db_id: str, col_name: str,
                   new_options: list[dict]) -> list[dict]:
    """Return a merged options list safe to PATCH.

    Existing options are identified by their Notion id (which prevents a 400
    when Notion rejects color changes on already-existing options).
    New options (not yet in the DB) are appended with their specified color.
    """
    try:
        schema  = client.get_db_schema(db_id)
        col     = schema.get("properties", {}).get(col_name, {})
        existing = {o["name"]: o for o in col.get("select", {}).get("options", [])}
    except Exception:
        existing = {}

    merged = []
    for opt in new_options:
        if opt["name"] in existing:
            # Keep the stored option as-is (id + name + existing color)
            merged.append(existing[opt["name"]])
        else:
            merged.append({"name": opt["name"], "color": opt["color"]})
    return merged


def _push_to_notion(client: NotionClient, cfg: dict) -> dict:
    """Push current work_types.json options to Work Sessions and all WBS DBs."""
    new_options = get_work_type_options()
    ok_count    = 0
    fail_count  = 0
    errors: list[str] = []

    ws_options  = _merge_options(client, WORK_SESSIONS_DB_ID, "Work Type", new_options)
    ws_payload  = {"properties": {"Work Type": {"select": {"options": ws_options}}}}
    r = client.patch_database(WORK_SESSIONS_DB_ID, ws_payload)
    if not r.ok:
        errors.append(f"Work Sessions DB: HTTP {r.status_code}")

    for db_id, src in cfg.get("sources", {}).items():
        col_name = src.get("field_map", {}).get("work_type", "")
        if not col_name:
            continue
        merged  = _merge_options(client, db_id, col_name, new_options)
        payload = {"properties": {col_name: {"select": {"options": merged}}}}
        r = client.patch_database(db_id, payload)
        if r.ok:
            ok_count += 1
        else:
            fail_count += 1
            errors.append(f"{src.get('db_title', db_id[:8])}: HTTP {r.status_code}")

    return {"ok_count": ok_count, "fail_count": fail_count, "errors": errors}


@bp.route("/api/work-types/push-all", methods=["POST"])
def api_work_types_push_all():
    """Push current work_types.json to all Notion databases without changing any types."""
    body  = request.json or {}
    token = body.get("token", "").strip() or load_config().get("token", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400
    cfg         = load_config()
    client      = NotionClient(token)
    notion_push = _push_to_notion(client, cfg)
    return jsonify({"ok": True, "notion_push": notion_push})


@bp.route("/api/work-types/full", methods=["GET"])
def api_work_types_full():
    """Return active work types with name, color, and description."""
    return jsonify({
        "work_types":    get_work_types(include_deprecated=False),
        "notion_colors": NOTION_COLORS,
    })


@bp.route("/api/work-types/create", methods=["POST"])
def api_work_types_create():
    """
    Create a new work type and push to all Notion databases.

    Body: { name, color, description? }
    Returns: { ok, work_type, notion_push }
    """
    body        = request.json or {}
    token       = body.get("token", "").strip() or load_config().get("token", "").strip()
    name        = body.get("name", "").strip()
    color       = body.get("color", "").strip()
    description = body.get("description", "").strip()

    if not token:
        return jsonify({"error": "No token"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not color:
        return jsonify({"error": "color is required"}), 400

    try:
        new_type = save_work_type(name=name, color=color, description=description)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    cfg         = load_config()
    client      = NotionClient(token)
    notion_push = _push_to_notion(client, cfg)

    return jsonify({"ok": True, "work_type": new_type, "notion_push": notion_push})


@bp.route("/api/work-types/update", methods=["POST"])
def api_work_types_update():
    """
    Update color or description for an existing work type, then push to Notion.

    Body: { name, color?, description? }
    Returns: { ok, work_type, notion_push }
    """
    body        = request.json or {}
    token       = body.get("token", "").strip() or load_config().get("token", "").strip()
    name        = body.get("name", "").strip()
    color       = body.get("color", "").strip()
    description = body.get("description")

    if not token:
        return jsonify({"error": "No token"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    fields: dict = {}
    if color:
        fields["color"] = color
    if description is not None:
        fields["description"] = description.strip()

    if not fields:
        return jsonify({"error": "Nothing to update — provide color or description"}), 400

    try:
        updated = update_work_type(name, **fields)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    cfg         = load_config()
    client      = NotionClient(token)
    notion_push = _push_to_notion(client, cfg)

    return jsonify({"ok": True, "work_type": updated, "notion_push": notion_push})


@bp.route("/api/work-types/deprecate", methods=["POST"])
def api_work_types_deprecate():
    """
    Soft-delete a work type (marks deprecated=true) and push updated list to Notion.

    Body: { name }
    Returns: { ok, notion_push }
    """
    body  = request.json or {}
    token = body.get("token", "").strip() or load_config().get("token", "").strip()
    name  = body.get("name", "").strip()

    if not token:
        return jsonify({"error": "No token"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    try:
        deprecate_work_type(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    cfg         = load_config()
    client      = NotionClient(token)
    notion_push = _push_to_notion(client, cfg)

    return jsonify({"ok": True, "notion_push": notion_push})
