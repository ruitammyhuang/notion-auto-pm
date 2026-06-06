"""
sync_engine.py
──────────────
All synchronisation logic: Project WBS → Master WBS Tasks → Work Sessions.
Deduplication and focus-cache regeneration also live here.

All public functions accept a NotionClient instance instead of a raw token,
so HTTP implementation details stay out of the sync logic.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import requests

from .config import (
    BASE_DIR,
    MASTER_DB_ID,
    PROJECTS_DB_ID,
    WORK_SESSIONS_DB_ID,
    FOCUS_CACHE_FILE,
    PRIORITY_MAP,
    VALID_PRIORITIES,
    VALID_WORK_TYPES,
    load_mappings,
    save_mappings,
)
from .notion_client import NotionClient, extract, p_title, p_text, p_select, p_date


# ── Internal retry helper ──────────────────────────────────────────────────────
def _with_retry(fn, *args, **kwargs):
    """Call fn(*args, **kwargs), retrying once after 3 s on Timeout."""
    for attempt in range(2):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.Timeout:
            if attempt == 0:
                time.sleep(3)
                continue
            raise


# ── Work-session date check ────────────────────────────────────────────────────
def _ws_has_date(ws_page: dict) -> bool:
    """Return True if any date-type property on this Work Session page is set."""
    for prop_val in ws_page.get("properties", {}).values():
        if prop_val.get("type") == "date" and prop_val.get("date") is not None:
            return True
    return False


def has_logged_hours(client: NotionClient, ws_id: str) -> bool:
    """Return True if a Work Session has any actual time logged.
    Defaults to True on any error to avoid accidentally discarding sessions."""
    try:
        r = client.get_page(ws_id)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        for prop_val in r.json().get("properties", {}).values():
            if prop_val.get("type") == "date" and prop_val.get("date") is not None:
                return True
        return False
    except Exception:
        return True  # fail-safe


# ── Page-metadata helpers (pure) ───────────────────────────────────────────────
def _get_task_master_id(ws_page: dict) -> str | None:
    """Find the master task ID from any relation property that isn't 'Project'."""
    for prop_name, prop_val in ws_page.get("properties", {}).items():
        if (prop_val.get("type") == "relation"
                and prop_name.lower() not in ("project", "projects")):
            rels = prop_val.get("relation", [])
            if rels:
                return rels[0]["id"]
    return None


def _get_page_title(page: dict) -> str:
    """Extract plain-text title from a Notion page object."""
    for prop_val in page.get("properties", {}).values():
        if prop_val.get("type") == "title":
            parts = prop_val.get("title", [])
            return "".join(p.get("plain_text", "") for p in parts).strip()
    return ""


def _get_project_id_from_page(page: dict) -> str | None:
    """Extract the first project relation ID from a Notion page."""
    for prop_name, prop_val in page.get("properties", {}).items():
        if (prop_val.get("type") == "relation"
                and prop_name.lower() in ("project", "projects")):
            rels = prop_val.get("relation", [])
            if rels:
                return rels[0]["id"]
    return None


def _archive_page(client: NotionClient, page_id: str, errors: list) -> bool:
    """Archive a Notion page; append to errors list on failure."""
    try:
        r = client.patch_page(page_id, {"archived": True})
        r.raise_for_status()
        return True
    except Exception as e:
        errors.append(f"Could not archive {page_id[:8]}: {e}")
        return False


# ── Focus-task cache ───────────────────────────────────────────────────────────
def regenerate_focus_cache(client: NotionClient) -> None:
    """Regenerate focus-task-list-cache.json from Master WBS Tasks.
    Includes only tasks with Planned End set. Failures are silent."""
    import json

    try:
        filter_body = {"property": "Planned End", "date": {"is_not_empty": True}}
        pages = client.query_db(MASTER_DB_ID, filter_body)

        # Collect unique project page IDs for batch name lookup
        project_ids: set[str] = set()
        for page in pages:
            for rel in page["properties"].get("Project", {}).get("relation", []):
                project_ids.add(rel["id"])

        # Fetch project names
        project_names: dict[str, str] = {}
        for pid in project_ids:
            try:
                r = client.get_page(pid)
                if r.ok:
                    props = r.json().get("properties", {})
                    project_names[pid] = extract(props.get("Project Name", {})) or ""
            except Exception:
                pass

        # Build task entries
        tasks = []
        for page in pages:
            props    = page.get("properties", {})
            date_raw = extract(props.get("Planned End", {}))
            planned_end = date_raw["start"] if isinstance(date_raw, dict) else None
            if not planned_end:
                continue
            rel_list = props.get("Project", {}).get("relation", [])
            proj_id  = rel_list[0]["id"] if rel_list else ""
            ws_urls  = [
                f"https://app.notion.com/p/{r['id'].replace('-', '')}"
                for r in props.get("Work Sessions", {}).get("relation", [])
            ]
            pid_clean = page["id"].replace("-", "")
            tasks.append({
                "id":            page["id"],
                "url":           f"https://app.notion.com/p/{pid_clean}",
                "name":          extract(props.get("Task Name", {})) or "",
                "planned_end":   planned_end,
                "priority":      extract(props.get("Priority", {})) or "Normal",
                "work_type":     extract(props.get("Work Type", {})) or "",
                "project_name":  project_names.get(proj_id, ""),
                "notes":         extract(props.get("Notes", {})) or "",
                "work_sessions": ws_urls,
            })

        tasks.sort(key=lambda t: t["planned_end"])
        cache = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "task_count":   len(tasks),
            "tasks":        tasks,
        }
        with open(FOCUS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)

    except Exception as e:
        print(f"[focus-cache] regeneration failed: {e}")


# ── Orphaned mapping cleanup ───────────────────────────────────────────────────
def cleanup_orphaned_mappings(
    client: NotionClient,
    all_current_src_ids: set,
    mappings: dict,
    sessions_mappings: dict,
) -> int:
    """Remove mapping entries whose source WBS page no longer exists anywhere.
    Targets legacy entries with db=None that per-database stale detection misses.
    Returns: number of orphaned entries cleaned up."""
    orphaned = [
        sid for sid, v in mappings.items()
        if isinstance(v, dict) and v.get("db") is None
        and sid not in all_current_src_ids
    ]
    cleaned = 0
    for sid in orphaned:
        master_id = mappings[sid]["master_id"]
        try:
            client.patch_page(master_id, {"archived": True})
        except Exception:
            pass
        if sessions_mappings and master_id in sessions_mappings:
            ws_id = sessions_mappings[master_id]
            if not has_logged_hours(client, ws_id):
                try:
                    client.patch_page(ws_id, {"archived": True})
                except Exception:
                    pass
            del sessions_mappings[master_id]
        del mappings[sid]
        cleaned += 1
    return cleaned


# ── Work Sessions sync ─────────────────────────────────────────────────────────
def sync_work_sessions_for_project(
    client: NotionClient,
    project_page_id: str,
    sessions_mappings: dict,
) -> dict:
    """
    Idempotent Work Sessions sync for one project.

    1. Query existing Work Sessions from Notion → build master_id → ws_id lookup.
    2. Query Master WBS Tasks for this project.
    3. For each master task with no session in Notion → create one.
    """
    # Step 1: ground-truth from Notion
    existing_ws = client.query_db(WORK_SESSIONS_DB_ID, filter_body={
        "property": "Project",
        "relation": {"contains": project_page_id},
    })

    notion_state: dict[str, str] = {}
    for ws in existing_ws:
        task_rel = ws.get("properties", {}).get("Task", {}).get("relation", [])
        if not task_rel:
            continue
        mid = task_rel[0]["id"]
        has_hours = _ws_has_date(ws)
        if mid not in notion_state or has_hours:
            notion_state[mid] = ws["id"]

    # Reconcile: fill sessions_mappings gaps from Notion state
    for mid, ws_id in notion_state.items():
        if mid not in sessions_mappings:
            sessions_mappings[mid] = ws_id

    # Step 2: Master WBS Tasks for this project
    master_pages = client.query_db(MASTER_DB_ID, filter_body={
        "property": "Project",
        "relation": {"contains": project_page_id},
    })

    created = skipped = 0
    errors: list[str] = []

    for master_page in master_pages:
        master_id = master_page["id"]

        if master_id in notion_state or master_id in sessions_mappings:
            skipped += 1
            continue

        task_name = ""
        for v in master_page["properties"].values():
            if v.get("type") == "title":
                task_name = "".join(r["plain_text"] for r in v.get("title", []))
                break

        ws_props = {
            "Session Name": p_title(task_name or "Work Session"),
            "Task":    {"relation": [{"id": master_id}]},
            "Project": {"relation": [{"id": project_page_id}]},
        }

        try:
            ws_page = client.create_page({"database_id": WORK_SESSIONS_DB_ID}, ws_props)
            sessions_mappings[master_id] = ws_page["id"]
            created += 1
        except requests.HTTPError as e:
            if (e.response is not None and e.response.status_code == 404):
                return {
                    "created": created, "skipped": skipped,
                    "errors": [
                        "Work Sessions database not accessible (404). "
                        "In Notion open ⏱️ Work Sessions → click ··· → Connections "
                        "→ add your integration, then sync again."
                    ],
                }
            errors.append(f"'{task_name or master_id[:8]}': {e}")
        except Exception as e:
            errors.append(f"'{task_name or master_id[:8]}': {e}")

    return {"created": created, "skipped": skipped, "errors": errors}


# ── Core sync ──────────────────────────────────────────────────────────────────
def sync_one_database(
    client: NotionClient,
    source_db_id: str,
    project_page_id: str,
    field_map: dict,
    mappings: dict,
    backlink_field: str = "Master WBS",
    work_type_map: dict | None = None,
    auto_calc_planned_start: bool = True,
    sessions_mappings: dict | None = None,
    emit=None,
) -> dict:
    """
    Pull all pages from source_db_id, upsert them into Master WBS Tasks.

    field_map keys: task_name, status, priority, notes,
                    planned_start, planned_end, work_type, category

    Status is written ONLY on CREATE — never overwritten on UPDATE.

    Delete behaviour: tasks previously synced from this source_db_id that no
    longer appear are archived in Master WBS. Their Work Session is archived only
    if no hours have been logged.
    """
    pages = client.query_db(source_db_id)
    if emit:
        emit({"type": "db_loaded", "task_count": len(pages)})

    created = updated = skipped = deleted = 0
    errors:        list[str]  = []
    new_tasks:     list[dict] = []
    skipped_tasks: list[dict] = []

    # Stale-task detection (tasks deleted in the source WBS)
    current_src_ids = {page["id"] for page in pages}
    stale_src_ids = [
        sid for sid, v in mappings.items()
        if isinstance(v, dict) and v.get("db") == source_db_id
        and sid not in current_src_ids
    ]
    for sid in stale_src_ids:
        master_id = mappings[sid]["master_id"]
        try:
            client.patch_page(master_id, {"archived": True})
        except Exception:
            pass
        if sessions_mappings and master_id in sessions_mappings:
            ws_id = sessions_mappings[master_id]
            if not has_logged_hours(client, ws_id):
                try:
                    client.patch_page(ws_id, {"archived": True})
                except Exception:
                    pass
            del sessions_mappings[master_id]
        del mappings[sid]
        deleted += 1

    for page in pages:
        src_id = page["id"]
        props  = page["properties"]

        def get_field(key: str):
            col = field_map.get(key, "")
            if col and col in props:
                return extract(props[col])
            return None

        # Task name — fall back to the title-type property if not mapped
        task_name = get_field("task_name")
        if not task_name:
            for v in props.values():
                if v.get("type") == "title":
                    task_name = extract(v)
                    break
        if not task_name:
            skipped_tasks.append({
                "page_id": src_id,
                "url": page.get("url", ""),
                "reason": "No task name / title property found — check column mapping",
            })
            skipped += 1
            if emit:
                emit({"type": "task", "task": "(untitled)", "action": "skipped"})
            continue

        # Priority normalisation
        raw_pri  = get_field("priority")
        priority = PRIORITY_MAP.get(str(raw_pri).lower().strip(), raw_pri) if raw_pri else None
        if priority not in VALID_PRIORITIES:
            priority = None

        # Category
        category_val = get_field("category")

        # Work Type — direct field first, then category override map
        work_type = get_field("work_type")
        if work_type not in VALID_WORK_TYPES:
            work_type = None
        if not work_type and work_type_map and category_val:
            mapped = work_type_map.get(category_val)
            if mapped in VALID_WORK_TYPES:
                work_type = mapped

        # Dates
        def date_start(val):
            return val["start"] if isinstance(val, dict) else val

        def date_end(val):
            if isinstance(val, dict):
                return val.get("end") or val.get("start")
            return val

        ps_raw = get_field("planned_start")
        pe_raw = get_field("planned_end")
        planned_start = date_start(ps_raw) if ps_raw else None
        planned_end   = date_end(pe_raw)   if pe_raw else None

        if not planned_start and planned_end and auto_calc_planned_start:
            try:
                pe_dt = datetime.strptime(planned_end[:10], "%Y-%m-%d")
                planned_start = (pe_dt - timedelta(days=7)).strftime("%Y-%m-%d")
            except Exception:
                pass

        notes = get_field("notes")

        master_props = {
            "Task Name": p_title(task_name),
            "Project":   {"relation": [{"id": project_page_id}]},
        }
        if priority:     master_props["Priority"]  = p_select(priority)
        if work_type:    master_props["Work Type"] = p_select(work_type)
        if category_val: master_props["Category"]  = p_text(category_val)
        if notes:        master_props["Notes"]     = p_text(notes)
        master_props["Planned Start"] = p_date({"start": planned_start} if planned_start else None)
        master_props["Planned End"]   = p_date({"start": planned_end}   if planned_end   else None)

        try:
            if src_id in mappings:
                master_id = mappings[src_id]["master_id"]
                mappings[src_id]["db"] = source_db_id  # tag legacy entries

                r = _with_retry(
                    client.patch_page,
                    master_id,
                    {"properties": master_props},
                )
                if r.status_code == 404:
                    # Master page deleted out-of-band — recreate it
                    del mappings[src_id]
                    new_page = _with_retry(
                        client.create_page,
                        {"database_id": MASTER_DB_ID},
                        master_props,
                    )
                    new_id = new_page["id"]
                    mappings[src_id] = {"master_id": new_id, "db": source_db_id}
                    client.write_backlink(src_id, new_id, backlink_field)
                    new_tasks.append({"master_id": new_id,
                                      "project_id": project_page_id,
                                      "task_name": task_name})
                    created += 1
                    if emit:
                        emit({"type": "task", "task": task_name, "action": "created"})
                else:
                    r.raise_for_status()
                    client.write_backlink(src_id, master_id, backlink_field)
                    # Propagate rename to Work Session's Session Name
                    if sessions_mappings and master_id in sessions_mappings:
                        ws_id = sessions_mappings[master_id]
                        try:
                            client.patch_page(
                                ws_id,
                                {"properties": {"Session Name": p_title(task_name)}},
                            )
                        except Exception:
                            pass
                    updated += 1
                    if emit:
                        emit({"type": "task", "task": task_name, "action": "updated"})
            else:
                new_page = _with_retry(
                    client.create_page,
                    {"database_id": MASTER_DB_ID},
                    master_props,
                )
                new_id = new_page["id"]
                mappings[src_id] = {"master_id": new_id, "db": source_db_id}
                client.write_backlink(src_id, new_id, backlink_field)
                new_tasks.append({"master_id": new_id,
                                  "project_id": project_page_id,
                                  "task_name": task_name})
                created += 1
                if emit:
                    emit({"type": "task", "task": task_name, "action": "created"})

        except Exception as e:
            errors.append(f"'{task_name}': {e}")
            if emit:
                emit({"type": "task", "task": task_name,
                      "action": "error", "detail": str(e)})

    regenerate_focus_cache(client)
    return {
        "created": created, "updated": updated,
        "skipped": skipped, "deleted": deleted,
        "errors": errors, "new_tasks": new_tasks,
        "skipped_tasks": skipped_tasks,
        "current_src_ids": current_src_ids,
    }


# ── Work Sessions deduplication ───────────────────────────────────────────────
def deduplicate_work_sessions_global(client: NotionClient) -> dict:
    """
    Two-phase deduplication.

    Phase 1 — Deduplicate Master WBS Tasks (same name + project appearing twice).
      Keep the entry in the mappings file (or most-recently-edited), archive rest.
      Re-link Work Sessions from archived duplicates to the survivor.

    Phase 2 — Deduplicate Work Sessions per master task.
      Dated sessions (real work) → keep all, archive empty ones.
      All empty → keep most-recently-edited, archive rest.

    Returns summary dict.
    """
    errors:           list[str]  = []
    updated_mappings: dict       = {}

    # Phase 1: Deduplicate Master WBS Tasks
    all_master = client.query_db(MASTER_DB_ID)
    mappings   = load_mappings()
    tracked_master_ids = {
        v["master_id"] for v in mappings.values() if isinstance(v, dict)
    }

    master_groups: dict[tuple, list] = {}
    for page in all_master:
        name    = _get_page_title(page)
        proj_id = _get_project_id_from_page(page)
        if not name or not proj_id:
            continue
        key = (proj_id, name.lower())
        master_groups.setdefault(key, []).append(page)

    master_dupes_archived = 0
    all_ws   = client.query_db(WORK_SESSIONS_DB_ID)
    ws_by_master: dict[str, list] = {}
    no_task_ids: list[str] = []
    for ws in all_ws:
        mid = _get_task_master_id(ws)
        if mid:
            ws_by_master.setdefault(mid, []).append(ws)
        else:
            no_task_ids.append(ws["id"])

    for key, pages in master_groups.items():
        if len(pages) == 1:
            continue

        def score(p):
            in_mappings = 1 if p["id"] in tracked_master_ids else 0
            has_ws      = 1 if p["id"] in ws_by_master else 0
            return (in_mappings, has_ws, p.get("last_edited_time", ""))

        pages_sorted = sorted(pages, key=score, reverse=True)
        survivor     = pages_sorted[0]
        duplicates   = pages_sorted[1:]

        for dup in duplicates:
            dup_id = dup["id"]
            for ws in ws_by_master.get(dup_id, []):
                try:
                    client.patch_page(
                        ws["id"],
                        {"properties": {"Task": {"relation": [{"id": survivor["id"]}]}}},
                    )
                    ws_by_master.setdefault(survivor["id"], []).append(ws)
                except Exception as e:
                    errors.append(f"Re-link WS {ws['id'][:8]} to survivor: {e}")
            ws_by_master.pop(dup_id, None)
            if _archive_page(client, dup_id, errors):
                master_dupes_archived += 1

    # Phase 2: Deduplicate Work Sessions per master task
    ws_archived = kept = 0

    for master_id, sessions in ws_by_master.items():
        with_date    = [s for s in sessions if _ws_has_date(s)]
        without_date = [s for s in sessions if not _ws_has_date(s)]

        if len(sessions) == 1:
            kept += 1
            updated_mappings[master_id] = sessions[0]["id"]
            continue

        if with_date:
            to_keep    = with_date
            to_archive = without_date
        else:
            by_time    = sorted(sessions,
                                key=lambda s: s.get("last_edited_time", ""),
                                reverse=True)
            to_keep    = by_time[:1]
            to_archive = by_time[1:]

        kept += len(to_keep)
        updated_mappings[master_id] = to_keep[0]["id"]

        for ws in to_archive:
            if not _ws_has_date(ws):
                if _archive_page(client, ws["id"], errors):
                    ws_archived += 1

    return {
        "scanned":               len(all_ws),
        "master_dupes_archived": master_dupes_archived,
        "ws_archived":           ws_archived,
        "kept":                  kept,
        "no_task_sessions":      len(no_task_ids),
        "updated_mappings":      updated_mappings,
        "errors":                errors,
    }
