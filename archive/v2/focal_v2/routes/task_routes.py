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
    MASTER_DB_ID,
    WORK_SESSIONS_DB_ID,
    load_config,
    load_mappings,
    load_sessions_mappings,
    save_sessions_mappings,
    load_students,
    update_student_phase,
)
from ..notion_client import NotionClient, extract, p_title, p_text
from ..sync_engine import deduplicate_work_sessions_global, regenerate_focus_cache
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


# ── Student profile lookup ─────────────────────────────────────────────────────

@bp.route("/api/wbs-student-profile", methods=["POST"])
def api_wbs_student_profile():
    """Return profile for a student from focal_students.json.

    Reads from the local students file — no Notion API call needed.
    Falls back to empty profile if student is not found.
    """
    body         = request.json or {}
    student_name = body.get("student_name", "").strip()
    if not student_name:
        return jsonify({"profile": {}})
    students = load_students()
    record = next((s for s in students if s.get("student_name") == student_name), None)
    if not record:
        return jsonify({"profile": {}})
    profile = {
        "degree":        record.get("degree",        ""),
        "my_role":       record.get("my_role",       ""),
        "chair":         record.get("chair",         ""),
        "program":       record.get("program",       ""),
        "current_phase": record.get("current_phase", ""),
    }
    return jsonify({"profile": profile})


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
    session_end        = body.get("session_end", "")
    session_comment    = body.get("session_comment", "").strip()
    session_status     = body.get("session_status", "").strip()
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
    student_name         = body.get("student_name", "").strip()
    student_name_field   = body.get("student_name_field", "").strip()
    current_phase        = body.get("current_phase", "").strip()
    current_phase_field  = body.get("current_phase_field", "").strip()
    degree               = body.get("degree", "").strip()
    degree_field         = body.get("degree_field", "").strip()
    my_role              = body.get("my_role", "").strip()
    my_role_field        = body.get("my_role_field", "").strip()
    chair                = body.get("chair", "").strip()
    chair_field          = body.get("chair_field", "").strip()
    program              = body.get("program", "").strip()
    program_field        = body.get("program_field", "").strip()
    update_profile       = bool(body.get("update_profile", False))

    if not all([token, source_db_id, project_id, task_name]):
        return jsonify({"error": "token, source_db_id, project_id, and task_name are required"}), 400

    try:
        client = NotionClient(token)
        result = quick_add_task(
            client, source_db_id, project_id, task_name,
            task_name_field, backlink_field, session_start,
            session_end=session_end,
            session_comment=session_comment,
            session_status=session_status,
            due_date=due_date, priority=priority, work_type=work_type,
            planned_end_field=planned_end_field,
            priority_field=priority_field,
            work_type_field=work_type_field,
            category=category, category_field=category_field,
            level=level, level_field=level_field,
            org_division=org_division, org_division_field=org_division_field,
            student_name=student_name, student_name_field=student_name_field,
            current_phase=current_phase, current_phase_field=current_phase_field,
            degree=degree, degree_field=degree_field,
            my_role=my_role, my_role_field=my_role_field,
            chair=chair, chair_field=chair_field,
            program=program, program_field=program_field,
        )
        # If checkbox was checked, update the student's phase in local file
        if update_profile and student_name and current_phase:
            update_student_phase(student_name, current_phase)

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


# ── Existing-task lookup ───────────────────────────────────────────────────────

@bp.route("/api/project-tasks", methods=["POST"])
def api_project_tasks():
    """Return non-completed tasks for a project from Master WBS Tasks.

    Body: {token, project_id}
    Returns: {tasks: [{id, url, name, priority, work_type, work_sessions, total_sessions, completed_sessions}]}
    """
    body       = request.json or {}
    token      = body.get("token", "").strip()
    project_id = body.get("project_id", "").strip()

    if not token or not project_id:
        return jsonify({"error": "token and project_id required"}), 400

    try:
        client      = NotionClient(token)
        filter_body = {"property": "Project", "relation": {"contains": project_id}}
        pages       = client.query_db(MASTER_DB_ID, filter_body)

        tasks = []
        for page in pages:
            if page.get("archived") or page.get("in_trash"):
                continue
            props = page.get("properties", {})

            # Skip tasks where all sessions are completed
            completed_sessions = int(
                props.get("Completed Sessions", {}).get("rollup", {}).get("number") or 0
            )
            total_sessions = int(
                props.get("Total Sessions", {}).get("rollup", {}).get("number") or 0
            )
            if total_sessions > 0 and completed_sessions >= total_sessions:
                continue

            task_name = extract(props.get("Task Name", {})) or ""
            if not task_name:
                continue

            pid_clean = page["id"].replace("-", "")
            ws_entries = [
                {
                    "id":  r["id"],
                    "url": f"https://app.notion.com/p/{r['id'].replace('-', '')}",
                }
                for r in props.get("Work Sessions", {}).get("relation", [])
            ]

            tasks.append({
                "id":                 page["id"],
                "url":                f"https://app.notion.com/p/{pid_clean}",
                "name":               task_name,
                "priority":           extract(props.get("Priority", {})) or "Normal",
                "work_type":          extract(props.get("Work Type", {})) or "",
                "work_sessions":      ws_entries,
                "total_sessions":     total_sessions,
                "completed_sessions": completed_sessions,
            })

        tasks.sort(key=lambda t: t["name"].lower())
        return jsonify({"tasks": tasks})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/log-session", methods=["POST"])
def api_log_session():
    """Create a Work Session linked to an existing Master WBS task.

    Body: {token, master_task_id, project_id, task_name?,
           session_start?, session_end?, session_status?, session_comment?}
    Returns: {ok: true, ws_url}
    """
    body            = request.json or {}
    token           = body.get("token", "").strip()
    master_task_id  = body.get("master_task_id", "").strip()
    project_id      = body.get("project_id", "").strip()
    task_name       = body.get("task_name", "").strip()
    session_start   = body.get("session_start", "")
    session_end     = body.get("session_end", "")
    session_status  = body.get("session_status", "").strip()
    session_comment = body.get("session_comment", "").strip()

    if not all([token, master_task_id, project_id]):
        return jsonify({"error": "token, master_task_id, and project_id required"}), 400

    try:
        client   = NotionClient(token)
        ws_props = {
            "Session Name": p_title(task_name or "Work Session"),
            "Task":    {"relation": [{"id": master_task_id}]},
            "Project": {"relation": [{"id": project_id}]},
        }
        if session_start:
            ws_props["Session Start"] = {"date": {"start": session_start}}
        if session_end:
            ws_props["Session End"]   = {"date": {"start": session_end}}
        if session_comment:
            ws_props["Comment"]       = p_text(session_comment)
        if session_status:
            ws_props["Status"]        = {"select": {"name": session_status}}

        ws_page = client.create_page({"database_id": WORK_SESSIONS_DB_ID}, ws_props)
        ws_id   = ws_page["id"]

        # Update sessions mapping so the focus cache stays consistent
        sessions_mappings = load_sessions_mappings()
        sessions_mappings[master_task_id] = ws_id
        save_sessions_mappings(sessions_mappings)

        regenerate_focus_cache(client)

        return jsonify({"ok": True, "ws_url": ws_page.get("url", "")})
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        msg  = e.response.text[:300] if e.response is not None else str(e)
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


# ── Push changes back to WBS ───────────────────────────────────────────────────

@bp.route("/api/push-to-wbs", methods=["POST"])
def api_push_to_wbs():
    """
    Push an updated task name and/or artifact URL from a Work Session back to
    the corresponding Master WBS task and Project WBS task.

    Body: {
      token?,            # falls back to saved config
      work_session_id,   # Notion URL or page ID of the Work Session
      new_task_name?,    # override; if blank, reads Session Name from Notion
      artifact_url?,     # external URL to append as a bookmark block to Project WBS task
      artifact_label?,   # display label for the bookmark (default: "Resource")
    }
    """
    body           = request.json or {}
    token          = body.get("token", "").strip()
    if not token:
        token = load_config().get("token", "").strip()
    session_url    = body.get("work_session_id", "").strip()
    new_task_name  = body.get("new_task_name", "").strip()
    artifact_url   = body.get("artifact_url", "").strip()
    artifact_label = body.get("artifact_label", "").strip() or "Resource"

    if not token:
        return jsonify({"error": "No token — save one in the Setup tab first"}), 400
    if not session_url:
        return jsonify({"error": "work_session_id required"}), 400

    session_id = _extract_page_id(session_url)
    if not session_id:
        return jsonify({"error": f"Could not parse a page ID from: {session_url}"}), 400

    client = NotionClient(token)

    # 1. Fetch the Work Session to read Session Name + Task relation
    r = client.get_page(session_id)
    if not r.ok:
        return jsonify({"error": f"Cannot fetch Work Session ({r.status_code}). "
                                 "Make sure the Work Sessions DB is shared with your integration."}), 400

    ws_props  = r.json().get("properties", {})
    sess_name = extract(ws_props.get("Session Name", {})) or ""
    task_name = new_task_name or sess_name
    if not task_name:
        return jsonify({"error": "Could not determine task name — Work Session has no Session Name."}), 400

    task_rels = ws_props.get("Task", {}).get("relation", [])
    if not task_rels:
        return jsonify({"error": "Work Session has no linked Task (Master WBS). "
                                 "Link it in Notion first, then push."}), 400

    master_id = task_rels[0]["id"]

    # 2. Update Master WBS task name
    try:
        mr = client.patch_page(master_id, {"properties": {"Task Name": p_title(task_name)}})
        mr.raise_for_status()
    except Exception as e:
        return jsonify({"error": f"Failed to update Master WBS task: {e}"}), 500

    master_url = f"https://app.notion.com/p/{master_id.replace('-', '')}"

    # 3. Find the Project WBS task via inverted focal_mappings
    mappings = load_mappings()
    config   = load_config()
    sources  = config.get("sources", {})

    inverted: dict[str, dict] = {}
    for src_page, info in mappings.items():
        mid = info.get("master_id", "")
        if mid:
            inverted[mid] = {"source_page_id": src_page, "db_id": info.get("db", "")}

    wbs_url       = None
    artifact_saved = False

    wbs_info = inverted.get(master_id)
    if wbs_info:
        src_page_id     = wbs_info["source_page_id"]
        db_id           = wbs_info["db_id"]
        task_name_field = sources.get(db_id, {}).get("field_map", {}).get("task_name", "Task")

        # Update Project WBS task name
        try:
            wr = client.patch_page(src_page_id,
                                   {"properties": {task_name_field: p_title(task_name)}})
            wr.raise_for_status()
            wbs_url = f"https://app.notion.com/p/{src_page_id.replace('-', '')}"
        except Exception as e:
            return jsonify({"error": f"Updated Master WBS but failed on Project WBS: {e}",
                            "master_url": master_url}), 500

        # Append artifact URL as a bookmark block to the Project WBS task body
        if artifact_url:
            try:
                caption = ([{"type": "text", "text": {"content": artifact_label}}]
                           if artifact_label != "Resource" else [])
                client.append_block_children(src_page_id, [{
                    "type": "bookmark",
                    "bookmark": {"url": artifact_url, "caption": caption},
                }])
                artifact_saved = True
            except Exception as e:
                # Non-fatal — names updated, just note the bookmark failure
                return jsonify({
                    "ok": True,
                    "task_name":     task_name,
                    "master_url":    master_url,
                    "wbs_url":       wbs_url,
                    "artifact_saved": False,
                    "warning": f"Names updated, but could not save bookmark: {e}",
                })
    else:
        # Master WBS updated but no mapping found for Project WBS
        return jsonify({
            "ok": True,
            "task_name":     task_name,
            "master_url":    master_url,
            "wbs_url":       None,
            "artifact_saved": False,
            "warning": ("Master WBS task name updated. Project WBS task not found in mappings — "
                        "run a Sync first so the mapping file is populated."),
        })

    return jsonify({
        "ok":            True,
        "task_name":     task_name,
        "master_url":    master_url,
        "wbs_url":       wbs_url,
        "artifact_saved": artifact_saved,
    })


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
