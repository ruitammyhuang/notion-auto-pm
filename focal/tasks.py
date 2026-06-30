"""
tasks.py  (focal — two-layer)
──────────────────────────────
quick_add_task: create a task in Project WBS + Work Sessions in one call.

Change from v2: no Master WBS entry, no backlink relation, no focal_mappings.
Sessions mapping: wbs_page_id → {ws_id, fp, name, planned_end, ...}.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .config import (
    MASTER_DB_ID,
    WORK_SESSIONS_DB_ID,
    PRIORITY_MAP,
    VALID_PRIORITIES,
    VALID_WORK_TYPES,
    load_sessions_mappings,
    save_sessions_mappings,
)
from .notion_client import NotionClient, p_title, p_text, p_select, p_date
from .sync_engine import regenerate_focus_cache, _field_fingerprint


def _next_continuation_name(client: NotionClient, base_name: str, task_id: str) -> str:
    """
    Returns the next continuation session name (base-N format).
    Strips any existing '-N' suffix, queries all Work Sessions for this Task,
    finds the highest N already used, and returns base-(N+1).
    e.g. if 'Draft paper' and 'Draft paper-2' exist -> returns 'Draft paper-3'
    """
    base = re.sub(r'-\d+$', '', base_name)
    filter_body = {"property": "Task", "relation": {"contains": task_id}}
    try:
        pages = client.query_db(WORK_SESSIONS_DB_ID, filter_body)
    except Exception:
        pages = []

    max_n = 1
    for page in pages:
        name = ""
        props = page.get("properties", {})
        sess_name_prop = props.get("Session Name", {})
        titles = sess_name_prop.get("title", [])
        if titles:
            name = titles[0].get("plain_text", "")
        if name == base:
            max_n = max(max_n, 1)
        elif name.startswith(base + "-"):
            suffix = name[len(base) + 1:]
            if suffix.isdigit():
                max_n = max(max_n, int(suffix))

    return f"{base}-{max_n + 1}"


def quick_add_task(
    client: NotionClient,
    source_db_id: str,
    project_id: str,
    task_name: str,
    task_name_field: str,
    session_start: str,
    session_end: str = "",
    session_comment: str = "",
    session_status: str = "",
    due_date: str = "",
    priority: str = "Normal",
    work_type: str = "",
    planned_end_field: str = "",
    priority_field: str = "",
    work_type_field: str = "",
    category: str = "",
    category_field: str = "",
    level: str = "",
    level_field: str = "",
    org_division: str = "",
    org_division_field: str = "",
    student_name: str = "",
    student_name_field: str = "",
    current_phase: str = "",
    current_phase_field: str = "",
    degree: str = "",
    degree_field: str = "",
    my_role: str = "",
    my_role_field: str = "",
    chair: str = "",
    chair_field: str = "",
    program: str = "",
    program_field: str = "",
    project_name: str = "",
    backlink_field: str = "",
    auto_calc_planned_start: int = 0,
    planned_start_field: str = "",
) -> dict:
    """
    Create a task in three places:
      1. Project WBS database  (source_db_id)
      2. Work Sessions         (with Session Start pre-filled if provided)

    Returns {wbs_url, ws_url}.
    """
    sessions_mappings = load_sessions_mappings()

    # ── 1. Project WBS task ───────────────────────────────────────────────────
    wbs_props: dict = {task_name_field: p_title(task_name)}

    if due_date and planned_end_field:
        wbs_props[planned_end_field] = p_date({"start": due_date})
    if due_date and auto_calc_planned_start and planned_start_field:
        try:
            ps = (datetime.strptime(due_date[:10], "%Y-%m-%d")
                  - timedelta(days=int(auto_calc_planned_start))).strftime("%Y-%m-%d")
            wbs_props[planned_start_field] = p_date({"start": ps})
        except Exception:
            pass
    if priority and priority_field:
        norm = PRIORITY_MAP.get(priority.lower(), priority)
        if norm in VALID_PRIORITIES:
            wbs_props[priority_field] = {"select": {"name": norm}}
    if work_type and work_type_field and work_type in VALID_WORK_TYPES:
        wbs_props[work_type_field] = {"select": {"name": work_type}}
    if category and category_field:
        wbs_props[category_field] = {"select": {"name": category}}
    if level and level_field:
        wbs_props[level_field] = {"select": {"name": level}}
    if org_division and org_division_field:
        wbs_props[org_division_field] = p_text(org_division)
    if student_name and student_name_field:
        wbs_props[student_name_field] = p_text(student_name)
    if current_phase and current_phase_field:
        wbs_props[current_phase_field] = {"select": {"name": current_phase}}
    if degree and degree_field:
        wbs_props[degree_field] = {"select": {"name": degree}}
    if my_role and my_role_field:
        wbs_props[my_role_field] = {"select": {"name": my_role}}
    if chair and chair_field:
        wbs_props[chair_field] = p_text(chair)
    if program and program_field:
        wbs_props[program_field] = {"select": {"name": program}}

    wbs_page = client.create_page({"database_id": source_db_id}, wbs_props)
    wbs_id   = wbs_page["id"]

    norm_priority = PRIORITY_MAP.get((priority or "").lower(), priority or "Normal")

    # ── 2. Master WBS Tasks relay entry ──────────────────────────────────────
    master_props: dict = {
        "Task Name": p_title(task_name),
        "Project":   {"relation": [{"id": project_id}]},
    }
    master_page = client.create_page({"database_id": MASTER_DB_ID}, master_props)
    master_id   = master_page["id"]

    # Write backlink: WBS row → Master WBS Tasks entry
    if backlink_field:
        try:
            client.patch_page(wbs_id, {"properties": {
                backlink_field: {"relation": [{"id": master_id}]}
            }}).raise_for_status()
        except Exception:
            pass  # backlink is navigation sugar; don't fail the whole add

    # ── 3. Work Session linked to Master WBS Tasks ────────────────────────────
    # Work Sessions schema: Session Name, Task (→ Master WBS Tasks),
    # Project, Work Type, Session Start, Session End, Status, Notes.
    ws_props: dict = {
        "Session Name": p_title(task_name),
        "Task":         {"relation": [{"id": master_id}]},
        "Project":      {"relation": [{"id": project_id}]},
    }
    if work_type and work_type in VALID_WORK_TYPES:
        ws_props["Work Type"] = p_select(work_type)
    if session_start:
        ws_props["Session Start"] = {"date": {"start": session_start}}
    if session_end:
        ws_props["Session End"] = {"date": {"start": session_end}}
    if session_comment:
        ws_props["Notes"] = p_text(session_comment)
    if session_status:
        ws_props["Status"] = {"select": {"name": session_status}}

    ws_page = client.create_page({"database_id": WORK_SESSIONS_DB_ID}, ws_props)
    ws_id   = ws_page["id"]

    # ── 3b. Session Done: auto-create a continuation Work Session ─────────────
    continuation_ws_url  = None
    continuation_ws_name = None
    if session_status == "Session Done":
        cont_name = _next_continuation_name(client, task_name, master_id)
        cont_props: dict = {
            "Session Name": p_title(cont_name),
            "Task":         {"relation": [{"id": master_id}]},
            "Project":      {"relation": [{"id": project_id}]},
            "Status":       {"select": {"name": "In Progress"}},
        }
        if work_type and work_type in VALID_WORK_TYPES:
            cont_props["Work Type"] = p_select(work_type)
        cont_page            = client.create_page({"database_id": WORK_SESSIONS_DB_ID}, cont_props)
        continuation_ws_url  = f"https://app.notion.com/p/{cont_page['id'].replace('-', '')}"
        continuation_ws_name = cont_name

    # ── 4. Update sessions mapping ────────────────────────────────────────────
    fp = _field_fingerprint(task_name, norm_priority if norm_priority in VALID_PRIORITIES else None,
                            work_type or None, due_date or None, None, None)
    sessions_mappings[wbs_id] = {
        "master_id":   master_id,
        "ws_id":       ws_id,
        "fp":          fp,
        "name":        task_name,
        "planned_end": due_date or "",
        "priority":    norm_priority if norm_priority in VALID_PRIORITIES else "",
        "work_type":   work_type or "",
        "project_id":  project_id,
        "project_name":project_name,
        "source_db_id":source_db_id,
    }
    save_sessions_mappings(sessions_mappings)

    try:
        regenerate_focus_cache(client)
    except Exception as e:
        print(f"[tasks] focus-cache rebuild after task creation failed: {e}")

    result = {
        "wbs_url":    wbs_page.get("url", ""),
        "master_url": master_page.get("url", ""),
        "ws_url":     ws_page.get("url", ""),
    }
    if continuation_ws_url:
        result["continuation_ws_url"]  = continuation_ws_url
        result["continuation_ws_name"] = continuation_ws_name
    return result


def delete_task_cascade(client: NotionClient, ws_id: str) -> dict:
    """
    Archive all three records that were created together for one task:
      1. Work Session          (ws_id)
      2. Master WBS Tasks entry (master_id, looked up from sessions_mappings)
      3. Project WBS source row (wbs_page_id, key in sessions_mappings)

    Each archive step is attempted independently — a failure on one does
    not prevent the others from running.  The sessions_mappings entry is
    marked deleted and the focus cache is regenerated.

    Args:
        client: authenticated NotionClient
        ws_id:  Work Session page ID (with or without dashes)

    Returns:
        {
            "archived":  list of labels describing what was successfully archived,
            "skipped":   list of labels skipped (not found or already archived),
            "warnings":  list of error strings for steps that failed,
        }
    """
    # Normalise ws_id to dashed form
    hex_only = ws_id.replace("-", "")
    if len(hex_only) >= 32:
        h = hex_only[-32:]
        ws_id = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    sessions_mappings = load_sessions_mappings()

    # Find the mapping entry for this Work Session
    wbs_page_id  = None
    master_id    = None
    task_name    = None
    for wbs_id, info in sessions_mappings.items():
        if isinstance(info, dict) and info.get("ws_id") == ws_id:
            wbs_page_id = wbs_id
            master_id   = info.get("master_id", "")
            task_name   = info.get("name", "")
            break

    archived: list[str] = []
    skipped:  list[str] = []
    warnings: list[str] = []

    def _archive(page_id: str, label: str) -> None:
        if not page_id:
            skipped.append(f"{label} (no ID)")
            return
        try:
            r = client.patch_page(page_id, {"archived": True})
            if r.status_code == 404:
                skipped.append(f"{label} (already deleted or not found)")
            else:
                r.raise_for_status()
                archived.append(label)
        except Exception as e:
            warnings.append(f"{label}: {e}")

    # Archive all three records
    _archive(ws_id,       "Work Session")
    _archive(master_id,   "Master WBS Tasks entry")
    _archive(wbs_page_id, "Project WBS row")

    # Remove from sessions_mappings
    if wbs_page_id and wbs_page_id in sessions_mappings:
        sessions_mappings[wbs_page_id]["deleted"] = True
        save_sessions_mappings(sessions_mappings)

    # Rebuild focus cache so deleted task disappears immediately
    try:
        regenerate_focus_cache(client)
    except Exception as e:
        warnings.append(f"focus-cache rebuild: {e}")

    return {
        "task_name": task_name or ws_id[:8],
        "archived":  archived,
        "skipped":   skipped,
        "warnings":  warnings,
    }

