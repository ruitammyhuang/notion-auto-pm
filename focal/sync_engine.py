"""
sync_engine.py  (focal — three-layer relay)
────────────────────────────────────────────
Three-layer sync: Project WBS → Master WBS Tasks (relay) → Work Sessions.

Master WBS Tasks is a thin central relay table.  It exists solely so that
Work Sessions can hold a single "Task" relation pointing to it (Notion
relations target exactly one database, so we can't point directly to the
many individual WBS databases).  Beyond Task Name and Project, no heavy
metadata is stored there.

Each WBS task gets:
  1. One Master WBS Tasks entry (relay).  The WBS row is back-linked to it
     via its "Master WBS" relation field.
  2. One Work Session linked to the Master WBS Tasks entry via "Task".

Task metadata (name, planned_end, priority, work_type) is cached in
sessions_mappings.json so the focus cache doesn't need extra API calls.

Public surface:
  sync_one_database()                     — sync one WBS DB (three-layer)
  regenerate_focus_cache()                — rebuild focus-task-list-cache.json
  has_logged_hours()                      — used by stale-task cleanup
  sync_due_dates_from_completed_sessions() — Completed sessions -> WBS due date
  create_continuations_for_session_done() — Session Done set in Notion -> new WS
  _field_fingerprint()                    — exported for use in tasks.py
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

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
    load_sessions_mappings,
    save_sessions_mappings,
)
from .notion_client import NotionClient, extract, p_title, p_select, p_date


# ── Internal retry helper ──────────────────────────────────────────────────────
def _with_retry(fn, *args, **kwargs):
    for attempt in range(2):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.Timeout:
            if attempt == 0:
                time.sleep(3)
                continue
            raise


# ── Hours-logged check ─────────────────────────────────────────────────────────
def has_logged_hours(client: NotionClient, ws_id: str) -> bool:
    """
    Return True if a Work Session has a Session End recorded.
    Session Start alone (set automatically by Quick Add) is only a
    placeholder.  A Session End means real work happened.
    Defaults to True on any error to avoid accidentally discarding sessions.
    """
    try:
        r = client.get_page(ws_id)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        end_prop = r.json().get("properties", {}).get("Session End", {})
        return end_prop.get("type") == "date" and end_prop.get("date") is not None
    except Exception:
        return True   # fail-safe


# ── Archive helper ─────────────────────────────────────────────────────────────
def _archive_page(client: NotionClient, page_id: str, errors: list) -> bool:
    try:
        r = client.patch_page(page_id, {"archived": True})
        r.raise_for_status()
        return True
    except Exception as e:
        errors.append(f"Archive {page_id[:8]}: {e}")
        return False


# ── Fingerprint (change detection) ────────────────────────────────────────────
def _field_fingerprint(*values) -> str:
    raw = "|".join(str(v or "") for v in values)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ── Title extractor ────────────────────────────────────────────────────────────
def _get_title(props: dict) -> str:
    for v in props.values():
        if v.get("type") == "title":
            return "".join(r["plain_text"] for r in v.get("title", []))
    return ""


# ── Focus cache ────────────────────────────────────────────────────────────────
def regenerate_focus_cache(client: NotionClient) -> None:
    """
    Rebuild focus-task-list-cache.json from the sessions mapping + live
    Work Session statuses.

    Strategy:
      1. Load sessions_mappings (has all task metadata — no per-task API calls).
      2. Batch-query Work Sessions to get current Status for each ws_id.
      3. Emit tasks that are not Completed (including those without a due date).
    """
    try:
        mappings = load_sessions_mappings()
        if not mappings:
            _write_empty_cache()
            return

        # Collect all ws_ids we care about
        ws_id_to_wbs: dict[str, str] = {}
        for wbs_id, info in mappings.items():
            if isinstance(info, dict) and info.get("ws_id"):
                ws_id_to_wbs[info["ws_id"]] = wbs_id

        # One query to get current Status of all Work Sessions
        ws_status: dict[str, str] = {}
        try:
            all_ws = client.query_db(WORK_SESSIONS_DB_ID)
            for ws in all_ws:
                ws_status[ws["id"]] = extract(ws.get("properties", {}).get("Status", {})) or ""
        except Exception as e:
            print(f"[focus-cache] Work Sessions query failed: {e}")
            # Fall back to status stored in local mappings (from last sync)
            for wbs_id, info in mappings.items():
                if isinstance(info, dict) and info.get("ws_id"):
                    ws_status[info["ws_id"]] = info.get("status", "")

        tasks = []
        for wbs_id, info in mappings.items():
            if not isinstance(info, dict):
                continue
            if info.get("deleted"):
                continue
            ws_id       = info.get("ws_id", "")
            planned_end = info.get("planned_end", "")
            if not ws_id:
                continue

            status = ws_status.get(ws_id, info.get("status", ""))
            if status in ("Completed", "Session Done"):
                continue

            wbs_clean = wbs_id.replace("-", "")
            ws_clean  = ws_id.replace("-", "")
            tasks.append({
                "id":           wbs_id,
                "url":          f"https://app.notion.com/p/{wbs_clean}",
                "ws_id":        ws_id,
                "ws_url":       f"https://app.notion.com/p/{ws_clean}",
                "name":         info.get("name", ""),
                "planned_end":  planned_end,
                "priority":     info.get("priority", "Normal") or "Normal",
                "work_type":    info.get("work_type", ""),
                "project_name": info.get("project_name", ""),
                "ws_status":    status,
                "source_db_id": info.get("source_db_id", ""),
            })

        # Tasks with no due date sort to the end
        tasks.sort(key=lambda t: t["planned_end"] or "9999-99-99")
        cache = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "task_count":   len(tasks),
            "tasks":        tasks,
        }
        with open(FOCUS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)

    except Exception as e:
        print(f"[focus-cache] regeneration failed: {e}")


def _write_empty_cache() -> None:
    cache = {"generated_at": datetime.now(timezone.utc).isoformat(),
             "task_count": 0, "tasks": []}
    with open(FOCUS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


# ── Project name lookup (cached per sync run) ─────────────────────────────────
_project_name_cache: dict[str, str] = {}


def _get_project_name(client: NotionClient, project_id: str) -> str:
    if project_id in _project_name_cache:
        return _project_name_cache[project_id]
    try:
        r = client.get_page(project_id)
        if r.ok:
            props = r.json().get("properties", {})
            for v in props.values():
                if v.get("type") == "title":
                    name = extract(v)
                    if name:
                        _project_name_cache[project_id] = name
                        return name
    except Exception:
        pass
    return ""


# ── Reverse sync: Completed sessions → WBS due date ──────────────────────────
def sync_due_dates_from_completed_sessions(
    client: NotionClient,
    sources: list,
) -> dict:
    """
    Reverse-sync pass: for each Work Session with Status == 'Completed' and a
    Session End date, write that Session End date back to the WBS task's due
    date field.

    Only Status == 'Completed' triggers an update.  'Session Done' and every
    other status are explicitly ignored.

    Args:
        client:  authenticated NotionClient
        sources: list of source dicts [{db_id, field_map, ...}] — the same
                 list passed to _run_full_sync().

    Returns: {updated: N, skipped: N, errors: [...]}
    """
    # Build source_db_id → planned_end_field_name lookup (handle dashes either way)
    planned_end_field_for: dict[str, str] = {}
    for src in sources:
        db_id = src.get("db_id", "")
        field = src.get("field_map", {}).get("planned_end", "")
        if db_id and field:
            planned_end_field_for[db_id]                  = field
            planned_end_field_for[db_id.replace("-", "")] = field

    sessions_mappings = load_sessions_mappings()

    # Build reverse lookup: ws_id → wbs_page_id
    ws_id_to_wbs: dict[str, str] = {}
    for wbs_id, info in sessions_mappings.items():
        if isinstance(info, dict) and info.get("ws_id"):
            ws_id_to_wbs[info["ws_id"]] = wbs_id

    if not ws_id_to_wbs:
        return {"updated": 0, "skipped": 0, "errors": []}

    # Fetch only Completed Work Sessions
    try:
        all_ws = client.query_db(
            WORK_SESSIONS_DB_ID,
            filter_body={"property": "Status", "select": {"equals": "Completed"}},
        )
    except Exception as e:
        return {"updated": 0, "skipped": 0, "errors": [f"Work Sessions query failed: {e}"]}

    updated = skipped = 0
    errors: list[str] = []
    changed_mappings = False

    for ws in all_ws:
        ws_id = ws["id"]
        if ws_id not in ws_id_to_wbs:
            skipped += 1
            continue

        props  = ws.get("properties", {})
        status = extract(props.get("Status", {})) or ""

        # Only act on fully Completed sessions
        if status != "Completed":
            skipped += 1
            continue

        # Extract Session End date (date-only, strip time component)
        end_prop   = props.get("Session End", {})
        session_end = ""
        if end_prop.get("type") == "date" and end_prop.get("date"):
            raw = end_prop["date"].get("start", "")
            session_end = raw[:10] if raw else ""

        if not session_end:
            skipped += 1
            continue

        wbs_id = ws_id_to_wbs[ws_id]
        info   = sessions_mappings[wbs_id]
        source_db_id = info.get("source_db_id", "")

        planned_end_field = (
            planned_end_field_for.get(source_db_id)
            or planned_end_field_for.get(source_db_id.replace("-", ""), "")
        )
        if not planned_end_field:
            skipped += 1
            continue

        # Skip if already up to date
        if info.get("planned_end", "") == session_end:
            skipped += 1
            continue

        try:
            client.patch_page(
                wbs_id,
                {"properties": {planned_end_field: p_date({"start": session_end})}},
            ).raise_for_status()
            info["planned_end"] = session_end
            changed_mappings     = True
            updated += 1
        except Exception as e:
            errors.append(
                f"WBS {wbs_id[:8]} ({info.get('name', '?')}): {e}"
            )

    if changed_mappings:
        save_sessions_mappings(sessions_mappings)

    return {"updated": updated, "skipped": skipped, "errors": errors}


# ── Reverse sync: Session Done (set in Notion) → continuation Work Session ───
def create_continuations_for_session_done(client: NotionClient) -> dict:
    """
    Catches Work Sessions whose Status was changed to 'Session Done' directly
    in Notion. The /api/log-session route already creates a continuation
    inline when the Python UI itself sets that status — but Notion has no
    way to call back into the Flask app, so an edit made straight in Notion
    never reached that code. This pass closes the gap by running the same
    check during every full sync.

    Sessions are grouped into chains (same Task relation, or — for
    standalone sessions with no Task — same Project + base name before the
    "-N" suffix). Only the LAST session in a chain is inspected; a
    continuation is created only if that tail's Status == 'Session Done'.

    Idempotent by construction: once a continuation is created its Status is
    'In Progress', so the new tail no longer matches and a re-run is a no-op
    until that new tail is itself marked done.

    Returns: {created: N, skipped: N, errors: [...]}
    """
    try:
        all_ws = client.query_db(WORK_SESSIONS_DB_ID)
    except Exception as e:
        return {"created": 0, "skipped": 0, "errors": [f"Work Sessions query failed: {e}"]}

    chains: dict[tuple, list[dict]] = {}
    for ws in all_ws:
        if ws.get("archived") or ws.get("in_trash"):
            continue
        props = ws.get("properties", {})
        name  = extract(props.get("Session Name", {})) or ""
        if not name:
            continue

        status    = extract(props.get("Status", {})) or ""
        task_rels = props.get("Task", {}).get("relation", [])
        proj_rels = props.get("Project", {}).get("relation", [])
        task_id   = task_rels[0]["id"] if task_rels else None
        proj_id   = proj_rels[0]["id"] if proj_rels else None

        m    = re.search(r'-(\d+)$', name)
        base = name[:m.start()] if m else name
        n    = int(m.group(1)) if m else 1

        key = ("task", task_id) if task_id else ("proj", proj_id, base)
        chains.setdefault(key, []).append({
            "id": ws["id"], "n": n, "status": status, "name": name,
            "base": base, "props": props, "task_id": task_id, "proj_id": proj_id,
        })

    created = skipped = 0
    errors: list[str] = []

    # Maps old ws_id → new active-tail ws_id, collected across all chains.
    # Used at the end to repair sessions_mappings entries that still point at
    # a "Session Done" session when a continuation already exists.
    ws_id_updates: dict[str, str] = {}

    for entries in chains.values():
        entries.sort(key=lambda e: e["n"])
        tail = entries[-1]

        if tail["status"] != "Session Done":
            # Chain already has an active tail.  Record any earlier sessions
            # in the chain so stale mapping entries get repaired below.
            active_tail_id = tail["id"]
            for e in entries[:-1]:
                ws_id_updates[e["id"]] = active_tail_id
            skipped += 1
            continue

        try:
            props     = tail["props"]
            proj_rels = props.get("Project", {}).get("relation", [])
            task_id   = tail["task_id"]
            cont_name = f"{tail['base']}-{tail['n'] + 1}"

            cont_props: dict = {
                "Session Name": p_title(cont_name),
                "Project":      {"relation": proj_rels} if proj_rels else
                                 {"relation": [{"id": tail["proj_id"]}]} if tail["proj_id"]
                                 else {"relation": []},
                "Status":       {"select": {"name": "In Progress"}},
            }
            if task_id:
                cont_props["Task"] = {"relation": [{"id": task_id}]}
            wt = extract(props.get("Work Type", {}))
            if wt:
                cont_props["Work Type"] = {"select": {"name": wt}}

            new_ws = client.create_page({"database_id": WORK_SESSIONS_DB_ID}, cont_props)
            # Point all earlier sessions in this chain at the new continuation.
            new_ws_id = new_ws["id"]
            for e in entries:
                ws_id_updates[e["id"]] = new_ws_id
            created += 1
        except Exception as e:
            errors.append(f"WS {tail['id'][:8]} ({tail['name']}): {e}")

    # Repair any mapping entries that still point at a superseded work session.
    if ws_id_updates:
        mappings = load_sessions_mappings()
        changed  = False
        for info in mappings.values():
            if isinstance(info, dict) and info.get("ws_id") in ws_id_updates:
                info["ws_id"] = ws_id_updates[info["ws_id"]]
                changed = True
        if changed:
            save_sessions_mappings(mappings)

    return {"created": created, "skipped": skipped, "errors": errors}


# ── Core sync ─────────────────────────────────────────────────────────────────
def sync_one_database(
    client: NotionClient,
    source_db_id: str,
    project_page_id: str,
    field_map: dict,
    mappings: dict,
    backlink_field: str = "",
    auto_calc_planned_start: int = 7,
    emit=None,
) -> dict:
    """
    Sync one Project WBS database through three layers:
      WBS task → Master WBS Tasks entry → Work Session

    field_map keys used: task_name, planned_end, planned_start,
                         priority, work_type, category, notes

    backlink_field: name of the relation column on the WBS database that
                    points back to Master WBS Tasks (e.g. "Master WBS").
                    If empty, the backlink step is skipped.

    For each WBS task:
      1. Find or create a Master WBS Tasks relay entry.
      2. Write the backlink from the WBS row → Master WBS Tasks entry.
      3. Find or create a Work Session linked to the Master WBS Tasks entry.
      4. Update if any tracked field changed (fingerprint check).

    Stale tasks (removed from WBS): archive Work Session (if no real hours
    logged) and archive the Master WBS Tasks entry.

    Returns: {created, updated, skipped, deleted, errors, new_tasks,
              skipped_tasks, current_src_ids}
    """
    _project_name_cache.clear()   # reset per sync run

    pages = client.query_db(source_db_id)
    if emit:
        emit({"type": "db_loaded", "task_count": len(pages)})

    created = updated = skipped = deleted = 0
    errors:        list[str]  = []
    new_tasks:     list[dict] = []
    skipped_tasks: list[dict] = []

    current_src_ids = {page["id"] for page in pages}

    # ── Stale task cleanup ─────────────────────────────────────────────────────
    # Safety guard: if the database returned 0 pages, skip stale cleanup.
    # An empty result almost certainly means an API error or misconfiguration,
    # not that all tasks were genuinely deleted.  Archiving everything would
    # be catastrophic data loss.
    if not current_src_ids:
        if emit:
            emit({"type": "warning",
                  "message": f"Skipping stale cleanup for {source_db_id[:8]}: "
                             "database returned 0 pages (possible API error)"})
        # Still run the upsert loop (it will be a no-op with no pages)
        return {
            "created": 0, "updated": 0, "skipped": 0, "deleted": 0,
            "errors": [f"Database {source_db_id[:8]} returned 0 pages — stale cleanup skipped"],
            "new_tasks": [], "skipped_tasks": [],
            "current_src_ids": current_src_ids,
        }

    stale = [
        sid for sid, info in mappings.items()
        if isinstance(info, dict)
        and info.get("source_db_id") == source_db_id
        and sid not in current_src_ids
        and not info.get("deleted")
    ]
    for sid in stale:
        ws_id     = mappings[sid].get("ws_id", "")
        master_id = mappings[sid].get("master_id", "")
        if ws_id:
            if not has_logged_hours(client, ws_id):
                _archive_page(client, ws_id, errors)
            # If hours were logged, keep the Work Session (tombstone only)
        if master_id:
            _archive_page(client, master_id, errors)
        mappings[sid]["deleted"] = True
        deleted += 1
        if emit:
            emit({"type": "task", "task": mappings[sid].get("name", sid[:8]),
                  "action": "deleted"})

    # ── Project name (fetch once per project) ─────────────────────────────────
    project_name = _get_project_name(client, project_page_id)

    # ── Upsert loop ───────────────────────────────────────────────────────────
    for page in pages:
        src_id = page["id"]
        props  = page["properties"]

        def get_field(key: str):
            col = field_map.get(key, "")
            return extract(props[col]) if col and col in props else None

        # Task name
        task_name = get_field("task_name") or _get_title(props)
        if not task_name:
            skipped += 1
            url = page.get("url", "")
            skipped_tasks.append({"page_id": src_id, "url": url,
                                   "reason": "No task name found — check column mapping"})
            if emit:
                emit({"type": "task", "task": "(untitled)", "action": "skipped"})
            continue

        # Tombstone check
        existing = mappings.get(src_id, {})
        if isinstance(existing, dict) and existing.get("deleted"):
            skipped += 1
            if emit:
                emit({"type": "task", "task": task_name, "action": "skipped"})
            continue

        # Priority
        raw_pri  = get_field("priority")
        priority = PRIORITY_MAP.get(str(raw_pri).lower().strip(), raw_pri) if raw_pri else None
        if priority not in VALID_PRIORITIES:
            priority = None

        # Work type
        work_type = get_field("work_type")
        if work_type not in VALID_WORK_TYPES:
            work_type = None

        # Dates
        def _start(v):
            return v["start"] if isinstance(v, dict) else v

        def _end(v):
            return (v.get("end") or v.get("start")) if isinstance(v, dict) else v

        ps_raw = get_field("planned_start")
        pe_raw = get_field("planned_end")
        planned_start = _start(ps_raw) if ps_raw else None
        planned_end   = _end(pe_raw)   if pe_raw else None

        if not planned_start and planned_end and auto_calc_planned_start:
            try:
                pe_dt = datetime.strptime(planned_end[:10], "%Y-%m-%d")
                planned_start = (pe_dt - timedelta(days=int(auto_calc_planned_start))).strftime("%Y-%m-%d")
            except Exception:
                pass

        notes = get_field("notes")

        # Fingerprint covers all fields that should trigger an update
        fp = _field_fingerprint(task_name, priority, work_type, planned_end, planned_start, notes)

        # Pull existing IDs from mappings
        master_id = existing.get("master_id", "") if isinstance(existing, dict) else ""
        ws_id     = existing.get("ws_id",     "") if isinstance(existing, dict) else ""
        old_fp    = existing.get("fp",         "") if isinstance(existing, dict) else ""

        try:
            # ── Phase 1: Master WBS Tasks entry ───────────────────────────────
            master_props: dict = {
                "Task Name": p_title(task_name),
                "Project":   {"relation": [{"id": project_page_id}]},
            }
            if not master_id:
                r = client.create_page({"database_id": MASTER_DB_ID}, master_props)
                master_id = r["id"]
                # Write backlink: WBS row → Master WBS Tasks entry
                if backlink_field:
                    try:
                        client.patch_page(src_id, {"properties": {
                            backlink_field: {"relation": [{"id": master_id}]}
                        }}).raise_for_status()
                    except Exception as e:
                        errors.append(f"Backlink '{task_name}': {e}")
            elif old_fp != fp:
                # Name may have changed — keep Master entry in sync
                client.patch_page(master_id, {"properties": {
                    "Task Name": p_title(task_name)
                }}).raise_for_status()

            # ── Phase 2: Work Session ──────────────────────────────────────────
            ws_props: dict = {
                "Session Name": p_title(task_name),
                "Task":         {"relation": [{"id": master_id}]},
                "Project":      {"relation": [{"id": project_page_id}]},
            }
            if work_type:
                ws_props["Work Type"] = p_select(work_type)

            if not ws_id:
                r = client.create_page({"database_id": WORK_SESSIONS_DB_ID}, ws_props)
                ws_id = r["id"]
                new_tasks.append({
                    "name":   task_name,
                    "ws_url": f"https://app.notion.com/p/{ws_id.replace('-', '')}",
                })
                created += 1
                if emit:
                    emit({"type": "task", "task": task_name, "action": "created"})
            elif old_fp != fp:
                client.patch_page(ws_id, {"properties": ws_props}).raise_for_status()
                updated += 1
                if emit:
                    emit({"type": "task", "task": task_name, "action": "updated"})
            else:
                skipped += 1
                if emit:
                    emit({"type": "task", "task": task_name, "action": "skipped"})
                # Still update master_id in case it was missing before rebuild
                if not existing.get("master_id"):
                    mappings[src_id] = {**existing, "master_id": master_id}
                continue

            # ── Update mappings ────────────────────────────────────────────────
            mappings[src_id] = {
                "master_id":   master_id,
                "ws_id":       ws_id,
                "fp":          fp,
                "name":        task_name,
                "planned_end": planned_end or "",
                "priority":    priority or "",
                "work_type":   work_type or "",
                "project_id":  project_page_id,
                "project_name":project_name,
                "source_db_id":source_db_id,
            }

        except Exception as e:
            errors.append(f"'{task_name}': {e}")
            if emit:
                emit({"type": "task", "task": task_name, "action": "error", "error": str(e)})

    return {
        "created": created, "updated": updated, "skipped": skipped, "deleted": deleted,
        "errors": errors, "new_tasks": new_tasks, "skipped_tasks": skipped_tasks,
        "current_src_ids": current_src_ids,
    }
