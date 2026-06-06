"""
routes/task_routes.py
──────────────────────
Quick-add task creation, WBS field option lookups, deduplication,
and project-page-link management.
"""

from __future__ import annotations

import requests
from flask import Blueprint, jsonify, request

from ..config import (
    load_sessions_mappings,
    save_sessions_mappings,
)
from ..notion_client import NotionClient, extract
from ..sync_engine import deduplicate_work_sessions_global
from ..tasks import quick_add_task

bp = Blueprint("tasks", __name__)


# ── WBS field option helpers ───────────────────────────────────────────────────

@bp.route("/api/wbs-categories", methods=["POST"])
def api_wbs_categories():
    """Return select options for a WBS category field (used by Quick Start dropdown)."""
    body           = request.json or {}
    token          = body.get("token", "").strip()
    db_id          = body.get("db_id", "").strip()
    category_field = body.get("category_field", "").strip()

    if not token or not db_id:
        return jsonify({"error": "token and db_id required"}), 400
    if not category_field:
        return jsonify({"options": []})

    try:
        client = NotionClient(token)
        schema = client.get_db_schema(db_id)
        field  = schema.get("properties", {}).get(category_field, {})
        raw    = field.get("select", {}).get("options", [])
        return jsonify({
            "options":    [{"name": o["name"], "color": o.get("color", "default")} for o in raw],
            "field_type": field.get("type", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/wbs-text-options", methods=["POST"])
def api_wbs_text_options():
    """Return unique non-empty text values from a WBS field (e.g. org/division history)."""
    body       = request.json or {}
    token      = body.get("token", "").strip()
    db_id      = body.get("db_id", "").strip()
    field_name = body.get("field_name", "").strip()

    if not token or not db_id or not field_name:
        return jsonify({"options": []})
    try:
        client = NotionClient(token)
        pages  = client.query_db(db_id)
        seen   = set()
        for page in pages:
            val = extract(page.get("properties", {}).get(field_name, {}))
            if val and val.strip():
                seen.add(val.strip())
        return jsonify({"options": sorted(seen)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Quick-add ──────────────────────────────────────────────────────────────────

@bp.route("/api/quick-add", methods=["POST"])
def api_quick_add():
    """Create a task in Project WBS + Master WBS + Work Sessions in one call."""
    body               = request.json
    token              = body.get("token", "").strip()
    source_db_id       = body.get("source_db_id", "").strip()
    project_id         = body.get("project_id", "").strip()
    task_name          = body.get("task_name", "").strip()
    task_name_field    = body.get("task_name_field", "Task")
    backlink_field     = body.get("backlink_field", "Master WBS")
    session_start      = body.get("session_start", "")
    due_date           = body.get("due_date", "")
    priority           = body.get("priority", "Normal")
    work_type          = body.get("work_type", "")
    planned_end_field  = body.get("planned_end_field", "")
    priority_field     = body.get("priority_field", "")
    work_type_field    = body.get("work_type_field", "")
    category           = body.get("category", "").strip()
    category_field     = body.get("category_field", "").strip()
    level              = body.get("level", "").strip()
    level_field        = body.get("level_field", "").strip()
    org_division       = body.get("org_division", "").strip()
    org_division_field = body.get("org_division_field", "").strip()

    if not all([token, source_db_id, project_id, task_name]):
        return jsonify({"error": "token, source_db_id, project_id, and task_name are required"}), 400

    try:
        client = NotionClient(token)
        result = quick_add_task(
            client, source_db_id, project_id, task_name,
            task_name_field, backlink_field, session_start,
            due_date=due_date, priority=priority, work_type=work_type,
            planned_end_field=planned_end_field,
            priority_field=priority_field,
            work_type_field=work_type_field,
            category=category, category_field=category_field,
            level=level, level_field=level_field,
            org_division=org_division, org_division_field=org_division_field,
        )
        return jsonify({"ok": True, **result, "task_name": task_name})
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        msg  = e.response.text[:300] if e.response is not None else str(e)
        if code == 404:
            msg = ("Database not accessible (404). Make sure the Project WBS, "
                   "Master WBS Tasks, and Work Sessions databases are all shared "
                   "with your integration.")
        return jsonify({"error": f"Notion API {code}: {msg}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Deduplication ──────────────────────────────────────────────────────────────

@bp.route("/api/deduplicate", methods=["POST"])
def api_deduplicate():
    """Deduplicate Work Sessions globally, then reconcile sessions_mappings."""
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400
    try:
        client = NotionClient(token)
        result = deduplicate_work_sessions_global(client)
        sessions_mappings = load_sessions_mappings()
        sessions_mappings.update(result["updated_mappings"])
        save_sessions_mappings(sessions_mappings)
        result.pop("updated_mappings")  # don't send large dict to browser
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Project page link ──────────────────────────────────────────────────────────

def _extract_page_id(url_or_id: str) -> str | None:
    """Extract a clean UUID from a Notion URL or raw ID string."""
    if not url_or_id:
        return None
    raw     = url_or_id.split("?")[0].split("#")[0].rstrip("/")
    segment = raw.split("/")[-1]
    hex_only = segment.replace("-", "")
    if len(hex_only) >= 32:
        raw_id = hex_only[-32:]
        return f"{raw_id[:8]}-{raw_id[8:12]}-{raw_id[12:16]}-{raw_id[16:20]}-{raw_id[20:]}"
    return segment


def _add_project_page_link(
    client: NotionClient,
    project_entry_id: str,
    hub_page_url: str,
) -> dict:
    """Append a 'Project Page' heading + link_to_page block to a Projects DB entry.
    Idempotent: skips if the page already contains that heading."""
    hub_page_id = _extract_page_id(hub_page_url)
    if not hub_page_id:
        raise ValueError(f"Could not parse hub page ID from: {hub_page_url}")

    blocks = client.get_block_children(project_entry_id)
    for block in blocks:
        if block.get("type") == "heading_1":
            texts    = block.get("heading_1", {}).get("rich_text", [])
            combined = "".join(t.get("plain_text", "") for t in texts)
            if "project page" in combined.lower():
                return {"status": "already_exists"}

    client.append_block_children(project_entry_id, [
        {
            "type": "heading_1",
            "heading_1": {
                "rich_text": [{"type": "text", "text": {"content": "Project Page"}}]
            },
        },
        {
            "type": "link_to_page",
            "link_to_page": {"type": "page_id", "page_id": hub_page_id},
        },
    ])
    return {"status": "added", "hub_page_id": hub_page_id}


@bp.route("/api/add-project-page-link", methods=["POST"])
def api_add_project_page_link():
    data             = request.json or {}
    token            = data.get("token", "").strip()
    project_entry_id = data.get("project_entry_id", "").strip()
    hub_page_url     = data.get("hub_page_url", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400
    if not project_entry_id or not hub_page_url:
        return jsonify({"error": "project_entry_id and hub_page_url required"}), 400
    try:
        client = NotionClient(token)
        result = _add_project_page_link(client, project_entry_id, hub_page_url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
