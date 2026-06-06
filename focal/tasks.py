"""
tasks.py
────────
quick_add_task: create a task simultaneously in Project WBS,
Master WBS Tasks, and Work Sessions in a single call.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import (
    MASTER_DB_ID,
    WORK_SESSIONS_DB_ID,
    PRIORITY_MAP,
    VALID_PRIORITIES,
    VALID_WORK_TYPES,
    load_mappings,
    save_mappings,
    load_sessions_mappings,
    save_sessions_mappings,
)
from .notion_client import NotionClient, p_title, p_text, p_select, p_date
from .sync_engine import regenerate_focus_cache


def quick_add_task(
    client: NotionClient,
    source_db_id: str,
    project_id: str,
    task_name: str,
    task_name_field: str,
    backlink_field: str,
    session_start: str,
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
) -> dict:
    """
    Create a task simultaneously in three places:
      1. Project WBS database  (source_db_id)
      2. Master WBS Tasks
      3. Work Sessions  (with Session Start pre-filled if provided)

    Returns {"wbs_url", "master_url", "ws_url"} or raises on error.
    """
    mappings          = load_mappings()
    sessions_mappings = load_sessions_mappings()

    # ── 1. Project WBS task ───────────────────────────────────────────────────
    wbs_props: dict = {task_name_field: p_title(task_name)}

    if due_date and planned_end_field:
        wbs_props[planned_end_field] = p_date({"start": due_date})
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

    wbs_page = client.create_page({"database_id": source_db_id}, wbs_props)
    wbs_id   = wbs_page["id"]

    # ── 2. Master WBS entry ───────────────────────────────────────────────────
    master_props: dict = {
        "Task Name": p_title(task_name),
        "Project":   {"relation": [{"id": project_id}]},
    }
    if due_date:
        master_props["Planned End"] = p_date({"start": due_date})
        ps = (datetime.strptime(due_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        master_props["Planned Start"] = p_date({"start": ps})

    norm_priority = PRIORITY_MAP.get((priority or "").lower(), priority or "Normal")
    if norm_priority in VALID_PRIORITIES:
        master_props["Priority"] = {"select": {"name": norm_priority}}
    if work_type and work_type in VALID_WORK_TYPES:
        master_props["Work Type"] = {"select": {"name": work_type}}

    master_page = client.create_page({"database_id": MASTER_DB_ID}, master_props)
    master_id   = master_page["id"]

    # Back-link Project WBS → Master WBS
    client.write_backlink(wbs_id, master_id, backlink_field)

    mappings[wbs_id] = {"master_id": master_id, "db": source_db_id}
    save_mappings(mappings)

    # ── 3. Work Session ───────────────────────────────────────────────────────
    ws_props: dict = {
        "Session Name": p_title(task_name),
        "Task":    {"relation": [{"id": master_id}]},
        "Project": {"relation": [{"id": project_id}]},
    }
    if session_start:
        ws_props["Session Start"] = {"date": {"start": session_start}}

    ws_page = client.create_page({"database_id": WORK_SESSIONS_DB_ID}, ws_props)
    ws_id   = ws_page["id"]

    sessions_mappings[master_id] = ws_id
    save_sessions_mappings(sessions_mappings)

    regenerate_focus_cache(client)

    return {
        "wbs_url":    wbs_page.get("url", ""),
        "master_url": master_page.get("url", ""),
        "ws_url":     ws_page.get("url", ""),
    }
