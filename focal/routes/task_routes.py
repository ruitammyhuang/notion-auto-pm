"""
routes/task_routes.py  (focal — two-layer)
───────────────────────────────────────────
Quick-add task creation, WBS field lookups, session logging, and push-to-WBS.

Key changes from v2:
  - api_quick_add: no backlink_field (no Master WBS backlink)
  - api_project_tasks: queries Work Sessions instead of Master WBS Tasks
  - api_log_session: patches an existing Work Session (takes ws_id)
  - api_push_to_wbs: WS → WBS directly via inverted sessions_mappings
  - api_deduplicate: removed (three-layer dedup complexity gone)
"""

from __future__ import annotations

import re
import requests
from flask import Blueprint, jsonify, request

from ..config import (
    WORK_SESSIONS_DB_ID,
    VALID_WORK_TYPES,
    load_config,
    load_sessions_mappings,
    save_sessions_mappings,
    load_students,
    update_student_phase,
)
from ..notion_client import NotionClient, extract, p_title, p_text, p_date
from ..sync_engine import regenerate_focus_cache
from ..tasks import quick_add_task, delete_task_cascade, _next_continuation_name

bp = Blueprint("tasks", __name__)


# ── WBS field option helpers ───────────────────────────────────────────────────

@bp.route("/api/wbs-categories", methods=["POST"])
def api_wbs_categories():
    """Return select options for a WBS category field."""
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
    """Return unique non-empty text values from a WBS field."""
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
    body         = request.json or {}
    student_name = body.get("student_name", "").strip()
    if not student_name:
        return jsonify({"profile": {}})
    students = load_students()
    record = next((s for s in students if s.get("student_name") == student_name), None)
    if not record:
        return jsonify({"profile": {}})
    return jsonify({"profile": {
        "degree":        record.get("degree",        ""),
        "my_role":       record.get("my_role",       ""),
        "chair":         record.get("chair",         ""),
        "program":       record.get("program",       ""),
        "current_phase": record.get("current_phase", ""),
    }})


# ── Quick-add ──────────────────────────────────────────────────────────────────

@bp.route("/api/quick-add", methods=["POST"])
def api_quick_add():
    """Create a task in Project WBS + Master WBS Tasks relay + Work Session."""
    body               = request.json or {}
    token              = body.get("token", "").strip()
    source_db_id       = body.get("source_db_id", "").strip()
    project_id         = body.get("project_id", "").strip()
    task_name          = body.get("task_name", "").strip()
    task_name_field    = body.get("task_name_field", "Task")
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
    project_name         = body.get("project_name", "").strip()
    backlink_field       = body.get("backlink_field", "").strip()

    if not all([token, source_db_id, project_id, task_name]):
        return jsonify({"error": "token, source_db_id, project_id, and task_name required"}), 400

    # Read auto_calc_planned_start and planned_start_field from source config
    src_cfg        = load_config().get("sources", {}).get(source_db_id, {})
    _acps_raw      = src_cfg.get("auto_calc_planned_start", 0)
    auto_calc_ps   = (7 if _acps_raw is True
                      else 0 if _acps_raw is False
                      else int(_acps_raw or 0))
    planned_start_field = src_cfg.get("field_map", {}).get("planned_start", "")

    try:
        client = NotionClient(token)
        result = quick_add_task(
            client, source_db_id, project_id, task_name,
            task_name_field, session_start,
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
            project_name=project_name,
            backlink_field=backlink_field,
            auto_calc_planned_start=auto_calc_ps,
            planned_start_field=planned_start_field,
        )
        if update_profile and student_name and current_phase:
            update_student_phase(student_name, current_phase)
        return jsonify({"ok": True, **result, "task_name": task_name})
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        msg  = e.response.text[:300]  if e.response is not None else str(e)
        if code == 404:
            msg = ("Database not accessible (404). Make sure the Project WBS "
                   "and Work Sessions databases are shared with your integration.")
        return jsonify({"error": f"Notion API {code}: {msg}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Existing-task lookup (for Log Session dropdown) ────────────────────────────

@bp.route("/api/project-tasks", methods=["POST"])
def api_project_tasks():
    """
    Return active Work Sessions for a project (excludes Completed and
    Session Done — once a session is wrapped up, it shouldn't appear as a
    loggable existing task; the continuation session created by the
    Session-Done reverse-sync pass is what should be picked instead).

    In two-layer model, Work Sessions ARE the task tracker.
    Body: {token, project_id}
    Returns: {tasks: [{id (ws_id), url, name, priority, work_type, status}]}
    """
    body       = request.json or {}
    token      = body.get("token", "").strip()
    project_id = body.get("project_id", "").strip()
    if not token or not project_id:
        return jsonify({"error": "token and project_id required"}), 400

    try:
        client = NotionClient(token)
        filter_body = {"property": "Project", "relation": {"contains": project_id}}
        pages = client.query_db(WORK_SESSIONS_DB_ID, filter_body)

        # Build a ws_id → mapping lookup for planned_end
        mappings     = load_sessions_mappings()
        ws_to_info   = {
            info.get("ws_id"): info
            for info in mappings.values()
            if isinstance(info, dict) and info.get("ws_id")
        }

        tasks = []
        for page in pages:
            if page.get("archived") or page.get("in_trash"):
                continue
            props  = page.get("properties", {})
            status = extract(props.get("Status", {})) or ""
            # Hide fully completed sessions and ones already marked Session
            # Done — those are closed out; only their continuation (created
            # by the reverse-sync pass) should remain selectable.
            if status in ("Completed", "Session Done"):
                continue

            name = extract(props.get("Session Name", {})) or ""
            if not name:
                continue

            ws_id     = page["id"]
            pid_clean = ws_id.replace("-", "")
            info      = ws_to_info.get(ws_id, {})

            _ss = extract(props.get("Session Start", {}))
            _se = extract(props.get("Session End", {}))

            tasks.append({
                "id":            ws_id,
                "url":           f"https://app.notion.com/p/{pid_clean}",
                "name":          name,
                "priority":      info.get("priority") or extract(props.get("Priority", {})) or "Normal",
                "work_type":     extract(props.get("Work Type", {})) or info.get("work_type") or "",
                "planned_end":   info.get("planned_end", ""),
                "status":        status,
                "session_start": _ss["start"] if isinstance(_ss, dict) else "",
                "session_end":   _se["start"] if isinstance(_se, dict) else "",
            })

        tasks.sort(key=lambda t: t["name"].lower())
        return jsonify({"tasks": tasks})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Log a session against an existing Work Session ─────────────────────────────

@bp.route("/api/log-session", methods=["POST"])
def api_log_session():
    """
    Update an existing Work Session with session times.

    In two-layer model, the Work Session already exists (created by sync or
    quick-add).  Logging fills in Session Start, Session End, and Status.
    A new Work Session is also accepted via ws_id=None (creates standalone).

    Body: {token, ws_id?, project_id, task_name?,
           session_start?, session_end?, session_status?, session_comment?}
    """
    body            = request.json or {}
    token           = body.get("token", "").strip()
    # Accept both ws_id and master_task_id (JS historically sent master_task_id)
    ws_id           = (body.get("ws_id") or body.get("master_task_id") or "").strip()
    project_id      = body.get("project_id", "").strip()
    task_name       = body.get("task_name", "").strip()
    session_start   = body.get("session_start", "")
    session_end     = body.get("session_end", "")
    session_status  = body.get("session_status", "").strip()
    session_comment = body.get("session_comment", "").strip()

    if not token or not project_id:
        return jsonify({"error": "token and project_id required"}), 400

    try:
        client = NotionClient(token)

        patch: dict = {}
        if session_start:
            patch["Session Start"] = {"date": {"start": session_start}}
        if session_end:
            patch["Session End"]   = {"date": {"start": session_end}}
        if session_comment:
            patch["Notes"]         = p_text(session_comment)
        if session_status:
            patch["Status"]        = {"select": {"name": session_status}}

        if ws_id:
            # Patch existing Work Session
            r = client.patch_page(ws_id, {"properties": patch})
            r.raise_for_status()
            ws_url = f"https://app.notion.com/p/{ws_id.replace('-', '')}"

            # Update status in local sessions_mappings cache
            if session_status:
                mappings = load_sessions_mappings()
                for wbs_id, info in mappings.items():
                    if isinstance(info, dict) and info.get("ws_id") == ws_id:
                        info["status"] = session_status
                        break
                save_sessions_mappings(mappings)

            # ── Session Done: auto-create a continuation Work Session ──────────
            continuation_ws_url  = None
            continuation_ws_name = None

            if session_status == "Session Done":
                # Fetch the current WS to read its Task relation and Project
                ws_data   = client.get_page(ws_id).json()
                ws_props  = ws_data.get("properties", {})
                task_rels = ws_props.get("Task", {}).get("relation", [])
                proj_rels = ws_props.get("Project", {}).get("relation", [])
                sess_name = extract(ws_props.get("Session Name", {})) or task_name

                task_id_for_cont = task_rels[0]["id"] if task_rels else None

                cont_name = _next_continuation_name(client, sess_name,
                                                     task_id_for_cont) if task_id_for_cont \
                            else re.sub(r'-\d+$', '', sess_name) + "-2"

                cont_props: dict = {
                    "Session Name": p_title(cont_name),
                    "Project":      {"relation": proj_rels} if proj_rels else
                                    {"relation": [{"id": project_id}]},
                    "Status":       {"select": {"name": "In Progress"}},
                }
                if task_id_for_cont:
                    cont_props["Task"] = {"relation": [{"id": task_id_for_cont}]}
                # Copy Work Type if present
                wt = extract(ws_props.get("Work Type", {}))
                if wt:
                    cont_props["Work Type"] = {"select": {"name": wt}}

                cont_page            = client.create_page({"database_id": WORK_SESSIONS_DB_ID},
                                                           cont_props)
                continuation_ws_url  = f"https://app.notion.com/p/{cont_page['id'].replace('-', '')}"
                continuation_ws_name = cont_name

        else:
            # Create a new standalone Work Session (no existing WS selected)
            ws_props = {
                "Session Name": p_title(task_name or "Work Session"),
                "Project":      {"relation": [{"id": project_id}]},
                **patch,
            }
            ws_page = client.create_page({"database_id": WORK_SESSIONS_DB_ID}, ws_props)
            ws_url  = ws_page.get("url", "")
            continuation_ws_url  = None
            continuation_ws_name = None

        regenerate_focus_cache(client)
        response = {"ok": True, "ws_url": ws_url}
        if continuation_ws_url:
            response["continuation_ws_url"]  = continuation_ws_url
            response["continuation_ws_name"] = continuation_ws_name
        return jsonify(response)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        msg  = e.response.text[:300]  if e.response is not None else str(e)
        return jsonify({"error": f"Notion API {code}: {msg}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Push changes back to WBS ───────────────────────────────────────────────────

@bp.route("/api/push-to-wbs", methods=["POST"])
def api_push_to_wbs():
    """
    Push an updated task name and/or artifact URL from a Work Session back
    to the corresponding Project WBS task.

    Two-layer simplification: no Master WBS hop.  Uses inverted
    sessions_mappings to find the WBS page ID directly.

    Body: {token?, work_session_id, new_task_name?, artifact_url?, artifact_label?}
    """
    body           = request.json or {}
    token          = body.get("token", "").strip() or load_config().get("token", "").strip()
    session_url    = body.get("work_session_id", "").strip()
    new_task_name  = body.get("new_task_name", "").strip()
    artifact_url   = body.get("artifact_url", "").strip()
    artifact_label = body.get("artifact_label", "").strip() or "Resource"

    if not token:
        return jsonify({"error": "No token — save one in the Setup tab first"}), 400
    if not session_url:
        return jsonify({"error": "work_session_id required"}), 400

    ws_id = _extract_page_id(session_url)
    if not ws_id:
        return jsonify({"error": f"Could not parse a page ID from: {session_url}"}), 400

    client = NotionClient(token)

    # Fetch the Work Session to read current Session Name
    r = client.get_page(ws_id)
    if not r.ok:
        return jsonify({"error": f"Cannot fetch Work Session ({r.status_code})."}), 400

    ws_props  = r.json().get("properties", {})
    sess_name = extract(ws_props.get("Session Name", {})) or ""
    task_name = new_task_name or sess_name
    if not task_name:
        return jsonify({"error": "Could not determine task name."}), 400

    # Find the WBS page via inverted sessions_mappings
    mappings = load_sessions_mappings()
    wbs_page_id = None
    source_db_id = None
    for wbs_id, info in mappings.items():
        if isinstance(info, dict) and info.get("ws_id") == ws_id:
            wbs_page_id  = wbs_id
            source_db_id = info.get("source_db_id", "")
            break

    if not wbs_page_id:
        return jsonify({
            "ok": True, "task_name": task_name, "wbs_url": None,
            "warning": ("Work Session not found in local mappings — run a Sync first."),
        })

    # Find task_name_field from config
    config  = load_config()
    sources = config.get("sources", {})
    task_name_field = sources.get(source_db_id, {}).get("field_map", {}).get("task_name", "Task")

    try:
        wr = client.patch_page(wbs_page_id,
                               {"properties": {task_name_field: p_title(task_name)}})
        wr.raise_for_status()
    except Exception as e:
        return jsonify({"error": f"Failed to update WBS task: {e}"}), 500

    wbs_url       = f"https://app.notion.com/p/{wbs_page_id.replace('-', '')}"
    artifact_saved = False

    if artifact_url:
        try:
            caption = ([{"type": "text", "text": {"content": artifact_label}}]
                       if artifact_label != "Resource" else [])
            client.append_block_children(wbs_page_id, [{
                "type": "bookmark",
                "bookmark": {"url": artifact_url, "caption": caption},
            }])
            artifact_saved = True
        except Exception as e:
            return jsonify({
                "ok": True, "task_name": task_name, "wbs_url": wbs_url,
                "artifact_saved": False,
                "warning": f"WBS task name updated, but bookmark failed: {e}",
            })

    # Also update Session Name on the Work Session to stay in sync
    try:
        client.patch_page(ws_id, {"properties": {"Session Name": p_title(task_name)}})
    except Exception:
        pass

    # Update local mapping cache
    if wbs_page_id in mappings and isinstance(mappings[wbs_page_id], dict):
        mappings[wbs_page_id]["name"] = task_name
        save_sessions_mappings(mappings)

    return jsonify({
        "ok":            True,
        "task_name":     task_name,
        "wbs_url":       wbs_url,
        "artifact_saved": artifact_saved,
    })


# ── Project page link ──────────────────────────────────────────────────────────

def _extract_page_id(url_or_id: str) -> str | None:
    if not url_or_id:
        return None
    raw     = url_or_id.split("?")[0].split("#")[0].rstrip("/")
    segment = raw.split("/")[-1]
    hex_only = segment.replace("-", "")
    if len(hex_only) >= 32:
        raw_id = hex_only[-32:]
        return f"{raw_id[:8]}-{raw_id[8:12]}-{raw_id[12:16]}-{raw_id[16:20]}-{raw_id[20:]}"
    return segment


@bp.route("/api/add-project-page-link", methods=["POST"])
def api_add_project_page_link():
    """Append a Project Page heading + link_to_page block to a Projects DB entry."""
    data             = request.json or {}
    token            = data.get("token", "").strip()
    project_entry_id = data.get("project_entry_id", "").strip()
    hub_page_url     = data.get("hub_page_url", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400
    if not project_entry_id or not hub_page_url:
        return jsonify({"error": "project_entry_id and hub_page_url required"}), 400

    hub_page_id = _extract_page_id(hub_page_url)
    if not hub_page_id:
        return jsonify({"error": f"Could not parse hub page ID from: {hub_page_url}"}), 400

    try:
        client = NotionClient(token)
        blocks = client.get_block_children(project_entry_id)
        for block in blocks:
            if block.get("type") == "heading_1":
                texts = block.get("heading_1", {}).get("rich_text", [])
                if "project page" in "".join(t.get("plain_text", "") for t in texts).lower():
                    return jsonify({"status": "already_exists"})

        client.append_block_children(project_entry_id, [
            {"type": "heading_1",
             "heading_1": {"rich_text": [{"type": "text", "text": {"content": "Project Page"}}]}},
            {"type": "link_to_page",
             "link_to_page": {"type": "page_id", "page_id": hub_page_id}},
        ])
        return jsonify({"status": "added", "hub_page_id": hub_page_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400



# ── Cascade-delete an accidentally created task ────────────────────────────────

@bp.route("/api/delete-task", methods=["POST"])
def api_delete_task():
    """
    Archive a Work Session and every record that was created alongside it:
    the Master WBS Tasks relay entry and the Project WBS source row.

    Use this to undo an accidental Quick Add.  Each archive step is
    attempted independently so a partial failure is still reported clearly.

    Body:  { ws_id: "<Work Session page ID or URL>" }
    Returns: { ok, task_name, archived, skipped, warnings }
    """
    body  = request.json or {}
    ws_id = body.get("ws_id", "").strip()
    if not ws_id:
        return jsonify({"error": "ws_id required"}), 400

    # Accept full Notion URLs as well as raw page IDs
    ws_id = _extract_page_id(ws_id) or ws_id

    cfg   = load_config()
    token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No Notion token — save one in the Setup tab first"}), 400

    try:
        client = NotionClient(token)
        result = delete_task_cascade(client, ws_id)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/work-types", methods=["GET"])
def api_work_types():
    """Return the list of valid Work Type values."""
    return jsonify({"work_types": VALID_WORK_TYPES})


@bp.route("/api/set-work-type", methods=["POST"])
def api_set_work_type():
    """
    Set Work Type on a Work Session and propagate it back to the source WBS row.

    Body: { ws_id: "<page-id>", work_type: "🔵 Deep Work" }

    Steps:
      1. Write "Work Type" select on the Work Session page directly
         (dashboard reflects immediately on next load).
      2. Look up the WBS source page via inverted sessions_mappings (ws_id → wbs_key).
      3. Write work_type to the WBS source row using the field_map's work_type_field
         (survives future syncs — WBS is the canonical source).

    Returns: { ok, wbs_updated, warning? }
    """
    body      = request.json or {}
    ws_id     = body.get("ws_id", "").strip()
    work_type = body.get("work_type", "").strip()

    if not ws_id:
        return jsonify({"error": "ws_id required"}), 400
    if not work_type:
        return jsonify({"error": "work_type required"}), 400
    if work_type not in VALID_WORK_TYPES:
        return jsonify({"error": f"Invalid work_type. Valid values: {VALID_WORK_TYPES}"}), 400

    cfg   = load_config()
    token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No Notion token — save one in the Setup tab first"}), 400

    client = NotionClient(token)

    # ── Step 1: Update Work Session directly ──────────────────────────────────
    try:
        r = client.patch_page(ws_id, {
            "properties": {"Work Type": {"select": {"name": work_type}}}
        })
        if not r.ok:
            return jsonify({"error": f"Failed to update Work Session: {r.status_code} {r.text[:200]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to update Work Session: {e}"}), 500

    # ── Step 2: Find WBS source row via inverted sessions_mappings ────────────
    mappings     = load_sessions_mappings()
    wbs_page_id  = None
    source_db_id = None

    for wbs_id, info in mappings.items():
        if isinstance(info, dict) and info.get("ws_id") == ws_id:
            wbs_page_id  = wbs_id
            source_db_id = info.get("source_db_id", "")
            break

    if not wbs_page_id:
        return jsonify({
            "ok":          True,
            "wbs_updated": False,
            "warning":     "Work Session updated. WBS source row not found in mapping — run a Sync to link it.",
        })

    # ── Step 3: Write to WBS source row ───────────────────────────────────────
    sources         = cfg.get("sources", {})
    work_type_field = sources.get(source_db_id, {}).get("field_map", {}).get("work_type", "")

    if not work_type_field:
        return jsonify({
            "ok":          True,
            "wbs_updated": False,
            "warning":     "Work Session updated. No work_type field mapped for this project's WBS — set it in Sources.",
        })

    try:
        wr = client.patch_page(wbs_page_id, {
            "properties": {work_type_field: {"select": {"name": work_type}}}
        })
        if not wr.ok:
            return jsonify({
                "ok":          True,
                "wbs_updated": False,
                "warning":     f"Work Session updated but WBS update failed: {wr.status_code}",
            })
    except Exception as e:
        return jsonify({
            "ok":          True,
            "wbs_updated": False,
            "warning":     f"Work Session updated but WBS update failed: {e}",
        })

    return jsonify({"ok": True, "wbs_updated": True})
