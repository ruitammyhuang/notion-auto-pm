#!/usr/bin/env python3
"""
Notion WBS Sync Tool
────────────────────
Syncs project-specific WBS databases into the master WBS Tasks tracking database.
Only syncs the common columns that exist in every project (Task Name, Status,
Priority, Planned Start, Planned End, Notes, Work Type).

Each project WBS can have a completely different schema — you configure the
column mapping once per source database, and the tool remembers it.

Changelog vs. original
──────────────────────
  [FIX]  Project selector now has a "paste page ID" fallback text input.
         Previously, only projects stored in the 📁 Projects database appeared
         in the dropdown. Any Notion page (e.g. a hub sub-page) can now be
         used as the project target by pasting its ID directly.
  [FIX]  Status is now set ONLY on CREATE — never overwritten on UPDATE.
         This protects Auto Status (formula driven by Work Sessions) and
         manual Status edits from being clobbered on every sync run.
  [FIX]  After creating a Master WBS task, the tool writes the Master WBS
         relation back to the source WBS page, keeping both databases
         bi-directionally linked without manual work.
  [NEW]  "Category" is now a mappable field. Pair it with a per-source
         Work Type override map to automatically translate values like
         "Weekly Announcement" → "🟢 Communication".
  [NEW]  Planned Start auto-calculation: if the source has no Planned Start
         but has a Planned End (Due Date), the tool computes
         Planned Start = Planned End − 7 calendar days automatically.

Usage:
    pip install flask requests
    python notion_wbs_sync.py
    → Opens http://localhost:8765 in your browser automatically
"""

import json, os, re, time, threading, webbrowser, uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
import requests

app = Flask(__name__)

# ── Hard-coded master database IDs ────────────────────────────────────────────
MASTER_DB_ID        = "2de3b2f3d9b74481bc88511ea94de45e"   # 📋 Master WBS Tasks
PROJECTS_DB_ID      = "01705badbb854f019baf7d0ec68b8c7d"   # 📁 Projects
WORK_SESSIONS_DB_ID = "308c193fbba34a1ebe8d817fd72e9d9a"   # ⏱️ Work Sessions

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# ── In-memory sync job store (polling-based progress) ────────────────────────
# job_id → {"events": [...], "done": bool, "result": dict|None, "error": str|None}
_sync_jobs = {}

BASE_DIR               = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE            = os.path.join(BASE_DIR, "notion_sync_config.json")
MAPPING_FILE           = os.path.join(BASE_DIR, "notion_sync_mappings.json")
SESSIONS_MAPPING_FILE  = os.path.join(BASE_DIR, "notion_sessions_mappings.json")

# ── Value normalisation maps ──────────────────────────────────────────────────
PRIORITY_MAP = {
    "urgent": "Urgent", "critical": "Urgent", "blocker": "Urgent",
    "high": "High", "important": "High",
    "medium": "Normal", "normal": "Normal", "mid": "Normal",
    "low": "Low", "minor": "Low", "nice to have": "Low",
}
VALID_PRIORITIES = ["Urgent", "High", "Normal", "Low"]
VALID_WORK_TYPES = ["🔵 Deep Work", "🟡 Meeting & Call", "🟠 Admin & Ops", "🟢 Communication"]

# ── Config persistence ────────────────────────────────────────────────────────
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"token": "", "sources": {}}

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def load_mappings():
    """source_page_id → {"master_id": master_page_id, "db": source_db_id or None}
    Automatically migrates old flat format (string values) on first load."""
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE) as f:
            data = json.load(f)
        # Migrate old flat format {src_id: "master_id"} → {src_id: {"master_id": ..., "db": None}}
        migrated = False
        for k, v in list(data.items()):
            if isinstance(v, str):
                data[k] = {"master_id": v, "db": None}
                migrated = True
        if migrated:
            save_mappings(data)
        return data
    return {}

def save_mappings(m):
    with open(MAPPING_FILE, "w") as f:
        json.dump(m, f, indent=2)

def load_sessions_mappings():
    """master_task_page_id → work_session_page_id"""
    if os.path.exists(SESSIONS_MAPPING_FILE):
        with open(SESSIONS_MAPPING_FILE) as f:
            return json.load(f)
    return {}

def save_sessions_mappings(m):
    with open(SESSIONS_MAPPING_FILE, "w") as f:
        json.dump(m, f, indent=2)

# ── Notion API helpers ────────────────────────────────────────────────────────
def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

def query_db(token, db_id, filter_body=None):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if filter_body: body["filter"] = filter_body
        if cursor:      body["start_cursor"] = cursor
        r = requests.post(f"{NOTION_API}/databases/{db_id}/query",
                          headers=headers(token), json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"): break
        cursor = data.get("next_cursor")
    return pages

def search_wbs_databases(token):
    """Return only databases whose title starts with 'WBS' (case-insensitive).
    Uses query='WBS' to narrow the Notion search result set, then filters client-side."""
    dbs, cursor = [], None
    while True:
        body = {
            "query": "WBS",
            "filter": {"value": "database", "property": "object"},
            "page_size": 100,
        }
        if cursor: body["start_cursor"] = cursor
        r = requests.post(f"{NOTION_API}/search", headers=headers(token),
                          json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
        dbs.extend(data.get("results", []))
        if not data.get("has_more"): break
        cursor = data.get("next_cursor")
    return dbs

def get_db_schema(token, db_id):
    r = requests.get(f"{NOTION_API}/databases/{db_id}",
                     headers=headers(token), timeout=10)
    r.raise_for_status()
    return r.json()

# ── Property value extraction ─────────────────────────────────────────────────
def extract(prop):
    t = prop.get("type")
    if t == "title":
        return "".join(r["plain_text"] for r in prop.get("title", []))
    if t == "rich_text":
        return "".join(r["plain_text"] for r in prop.get("rich_text", []))
    if t in ("select", "status"):
        s = prop.get(t)
        return s["name"] if s else None
    if t == "multi_select":
        return ", ".join(o["name"] for o in prop.get("multi_select", []))
    if t == "date":
        d = prop.get("date")
        return {"start": d["start"], "end": d.get("end")} if d else None
    if t == "checkbox":
        return prop.get("checkbox")
    return None

# ── Property payload builders ─────────────────────────────────────────────────
def p_title(v):
    return {"title": [{"text": {"content": v or ""}}]}

def p_text(v):
    return {"rich_text": [{"text": {"content": str(v) if v else ""}}]}

def p_select(v):
    return {"select": {"name": v} if v else None}

def p_date(v):
    if not v: return {"date": None}
    if isinstance(v, dict):
        d = {"start": v["start"]}
        if v.get("end"): d["end"] = v["end"]
        return {"date": d}
    return {"date": {"start": str(v)}}

# ── Back-link helper ──────────────────────────────────────────────────────────
def _write_backlink(token, source_page_id, master_page_id, backlink_field):
    """Write the Master WBS relation back to the source WBS page (create AND update).
    Idempotent — safe to call on every sync; Notion ignores no-op relation writes."""
    if not backlink_field:
        return
    try:
        requests.patch(
            f"{NOTION_API}/pages/{source_page_id}",
            headers=headers(token),
            json={"properties": {
                backlink_field: {"relation": [{"id": master_page_id}]}
            }},
            timeout=15,
        )
    except Exception:
        pass  # non-fatal — mapping file still tracks the link

# ── Work Sessions helpers ─────────────────────────────────────────────────────
def has_logged_hours(token, ws_id):
    """Return True if a Work Session page has any actual time logged.
    We check for any date-type property that is non-null — Session Start being
    the canonical indicator. Defaults to True on any error so we never
    accidentally discard a session we can't verify."""
    try:
        r = requests.get(f"{NOTION_API}/pages/{ws_id}",
                         headers=headers(token), timeout=10)
        if r.status_code == 404:
            return False  # already gone, safe to clean up
        r.raise_for_status()
        for prop_val in r.json().get("properties", {}).values():
            if prop_val.get("type") == "date" and prop_val.get("date") is not None:
                return True
        return False
    except Exception:
        return True   # fail-safe: assume hours logged if we can't check


# ── Orphaned mapping cleanup ─────────────────────────────────────────────────
def cleanup_orphaned_mappings(token, all_current_src_ids, mappings, sessions_mappings):
    """
    Remove mapping entries whose source WBS page no longer exists in ANY configured
    database. This handles legacy entries with db=None that the per-database stale
    detector cannot catch (it only matches entries tagged with its own db ID).

    Called from the sync route AFTER all databases have been processed, so
    all_current_src_ids is the union of every database's live page IDs.

    Returns: number of orphaned entries cleaned up.
    """
    orphaned = [
        sid for sid, v in mappings.items()
        if isinstance(v, dict) and v.get("db") is None
        and sid not in all_current_src_ids
    ]
    cleaned = 0
    for sid in orphaned:
        master_id = mappings[sid]["master_id"]
        # Archive Master WBS entry
        try:
            requests.patch(f"{NOTION_API}/pages/{master_id}",
                           headers=headers(token),
                           json={"archived": True}, timeout=15)
        except Exception:
            pass
        # Cascade: archive Work Session only if no hours logged
        if sessions_mappings and master_id in sessions_mappings:
            ws_id = sessions_mappings[master_id]
            if not has_logged_hours(token, ws_id):
                try:
                    requests.patch(f"{NOTION_API}/pages/{ws_id}",
                                   headers=headers(token),
                                   json={"archived": True}, timeout=15)
                except Exception:
                    pass
            del sessions_mappings[master_id]
        del mappings[sid]
        cleaned += 1
    return cleaned


# ── Focus Task Cache ──────────────────────────────────────────────────────────
def regenerate_focus_cache(token):
    """Regenerate focus-task-list-cache.json from Master WBS Tasks.
    Includes only tasks with Planned End set. Called after sync and quick-add
    so the cache stays fresh. Failures are silent — never crashes the caller."""
    import json, os, datetime
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "focus-task-list-cache.json")
    try:
        # 1. Query only tasks that have a Planned End date
        filter_body = {"property": "Planned End", "date": {"is_not_empty": True}}
        pages = query_db(token, MASTER_DB_ID, filter_body)

        # 2. Collect unique project page IDs for batch name lookup
        project_ids = set()
        for page in pages:
            for rel in page["properties"].get("Project", {}).get("relation", []):
                project_ids.add(rel["id"])

        # 3. Fetch project names
        project_names = {}
        for pid in project_ids:
            try:
                r = requests.get(f"{NOTION_API}/pages/{pid}",
                                 headers=headers(token), timeout=10)
                if r.ok:
                    props = r.json().get("properties", {})
                    project_names[pid] = extract(props.get("Project Name", {})) or ""
            except Exception:
                pass

        # 4. Build task entries
        tasks = []
        for page in pages:
            props = page.get("properties", {})
            date_raw = extract(props.get("Planned End", {}))
            planned_end = date_raw["start"] if isinstance(date_raw, dict) else None
            if not planned_end:
                continue
            rel_list = props.get("Project", {}).get("relation", [])
            proj_id  = rel_list[0]["id"] if rel_list else ""
            ws_urls  = [
                f"https://app.notion.com/p/{r['id'].replace('-','')}"
                for r in props.get("Work Sessions", {}).get("relation", [])
            ]
            pid_clean = page["id"].replace("-", "")
            tasks.append({
                "id":           page["id"],
                "url":          f"https://app.notion.com/p/{pid_clean}",
                "name":         extract(props.get("Task Name", {})) or "",
                "planned_end":  planned_end,
                "priority":     extract(props.get("Priority", {})) or "Normal",
                "work_type":    extract(props.get("Work Type", {})) or "",
                "project_name": project_names.get(proj_id, ""),
                "notes":        extract(props.get("Notes", {})) or "",
                "work_sessions": ws_urls,
            })

        tasks.sort(key=lambda t: t["planned_end"])
        cache = {
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "task_count":   len(tasks),
            "tasks":        tasks,
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[focus-cache] regeneration failed: {e}")


# ── Work Sessions sync ────────────────────────────────────────────────────────
def sync_work_sessions_for_project(token, project_page_id, sessions_mappings):
    """
    Idempotent Work Sessions sync.

    Ground-truth check: queries existing Work Sessions from Notion first so the
    local mappings file is never the sole source of "does a session exist?".
    This prevents duplicates even if the mappings file was lost or reset.

    1. Query all existing Work Sessions for this project from Notion.
       Build a master_id → ws_id lookup and merge any gaps into sessions_mappings.
    2. Query Master WBS Tasks for this project.
    3. For each master task with no session in Notion → create one.
    4. Records master_task_id → work_session_id in sessions_mappings.
    """
    # ── Step 1: ground-truth from Notion ─────────────────────────────────────
    existing_ws = query_db(token, WORK_SESSIONS_DB_ID, filter_body={
        "property": "Project",
        "relation": {"contains": project_page_id},
    })

    # Build master_id → best existing ws_id
    # Prefer sessions with logged hours; otherwise take the first found.
    notion_state = {}   # master_id → ws_id
    for ws in existing_ws:
        task_rel = ws.get("properties", {}).get("Task", {}).get("relation", [])
        if not task_rel:
            continue
        mid       = task_rel[0]["id"]
        has_hours = _ws_has_date(ws)
        if mid not in notion_state or has_hours:
            notion_state[mid] = ws["id"]

    # Reconcile: fill in any sessions_mappings gaps from Notion state
    for mid, ws_id in notion_state.items():
        if mid not in sessions_mappings:
            sessions_mappings[mid] = ws_id

    # ── Step 2: Master WBS Tasks for this project ─────────────────────────────
    master_pages = query_db(token, MASTER_DB_ID, filter_body={
        "property": "Project",
        "relation": {"contains": project_page_id},
    })

    created = skipped = 0
    errors  = []

    for master_page in master_pages:
        master_id = master_page["id"]

        # Skip if a session already exists in Notion OR in the mappings file
        if master_id in notion_state or master_id in sessions_mappings:
            skipped += 1
            continue

        # Pull task name from the title-type property
        task_name = ""
        for v in master_page["properties"].values():
            if v.get("type") == "title":
                task_name = "".join(r["plain_text"] for r in v.get("title", []))
                break

        try:
            r = requests.post(
                f"{NOTION_API}/pages",
                headers=headers(token),
                json={
                    "parent":     {"database_id": WORK_SESSIONS_DB_ID},
                    "properties": {
                        "Session Name": p_title(task_name or "Work Session"),
                        "Task":    {"relation": [{"id": master_id}]},
                        "Project": {"relation": [{"id": project_page_id}]},
                    },
                },
                timeout=20,
            )
            if r.status_code == 404:
                # Integration can't access the Work Sessions database — fail fast
                # rather than repeating this error for every task.
                return {
                    "created": created, "skipped": skipped,
                    "errors": [
                        "Work Sessions database not accessible (404). "
                        "In Notion open ⏱️ Work Sessions → click ··· → Connections "
                        "→ add your integration, then sync again."
                    ],
                }
            r.raise_for_status()
            sessions_mappings[master_id] = r.json()["id"]
            created += 1
        except Exception as e:
            errors.append(f"'{task_name or master_id[:8]}': {e}")

    return {"created": created, "skipped": skipped, "errors": errors}


# ── Core sync function ────────────────────────────────────────────────────────
def sync_one_database(token, source_db_id, project_page_id, field_map, mappings,
                      backlink_field="Master WBS",
                      work_type_map=None,
                      auto_calc_planned_start=True,
                      sessions_mappings=None,
                      emit=None):
    """
    Pull all pages from source_db_id, upsert them into the master WBS Tasks DB.

    field_map keys: task_name, status, priority, notes,
                    planned_start, planned_end, work_type, category

    backlink_field: name of the Relation column in the source WBS that points
                    back to Master WBS Tasks (written after CREATE).

    work_type_map: dict mapping source Category values to master Work Type values,
                   e.g. {"Weekly Announcement": "🟢 Communication", "Grading": "🔵 Deep Work"}

    auto_calc_planned_start: when True and planned_start is missing but planned_end
                             is present, sets planned_start = planned_end − 7 days.

    sessions_mappings: the live sessions mappings dict (master_id → ws_id).
                       When provided, deleted tasks cascade-archive their Work Session,
                       and renamed tasks update their Work Session's Session Name.

    Status behaviour
    ────────────────
    Status is written ONLY on CREATE (initial state). On UPDATE it is never touched,
    preserving Auto Status (Work Sessions formula) and any manual edits made in
    Master WBS Tasks.

    Delete behaviour
    ────────────────
    Any task previously synced from this source_db_id that no longer appears in the
    current query is considered deleted. Its Master WBS entry is archived. Its Work
    Session is archived ONLY if no actual hours have been logged (Session Start is
    empty). If work was already logged, the Work Session is preserved as a historical
    record. Both are removed from the mapping files regardless.
    Only entries explicitly tagged with this source_db_id are eligible for deletion —
    legacy entries without a db tag are left untouched to avoid false positives.
    """
    pages = query_db(token, source_db_id)
    if emit:
        emit({"type": "db_loaded", "task_count": len(pages)})
    created = updated = skipped = deleted = 0
    errors        = []
    new_tasks     = []   # [{master_id, project_id, task_name}] — populated on CREATE only
    skipped_tasks = []   # [{page_id, url, reason}] — populated when a page is skipped

    # ── Stale (deleted) task detection ───────────────────────────────────────
    current_src_ids = {page["id"] for page in pages}
    stale_src_ids = [
        sid for sid, v in mappings.items()
        if isinstance(v, dict) and v.get("db") == source_db_id
        and sid not in current_src_ids
    ]
    # NOTE: entries with db=None are handled by cleanup_orphaned_mappings()
    # called from the sync route after ALL databases have been processed.
    for sid in stale_src_ids:
        master_id = mappings[sid]["master_id"]
        # Archive Master WBS entry
        try:
            requests.patch(f"{NOTION_API}/pages/{master_id}",
                           headers=headers(token),
                           json={"archived": True}, timeout=15)
        except Exception:
            pass
        # Cascade: archive Work Session only if no hours have been logged.
        # If the user already logged actual work, preserve the record.
        if sessions_mappings and master_id in sessions_mappings:
            ws_id = sessions_mappings[master_id]
            if not has_logged_hours(token, ws_id):
                try:
                    requests.patch(f"{NOTION_API}/pages/{ws_id}",
                                   headers=headers(token),
                                   json={"archived": True}, timeout=15)
                except Exception:
                    pass
            # Always remove from mapping — the source task no longer exists
            del sessions_mappings[master_id]
        del mappings[sid]
        deleted += 1

    for page in pages:
        src_id = page["id"]
        props  = page["properties"]

        def get_field(key):
            col = field_map.get(key, "")
            if col and col in props:
                return extract(props[col])
            return None

        task_name = get_field("task_name")
        if not task_name:
            # Fallback: every Notion database has exactly one title-type property.
            # Use it automatically so tasks are never skipped due to a missing mapping.
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
            if emit: emit({"type": "task", "task": "(untitled)", "action": "skipped"})
            continue

        # Normalise priority
        raw_pri = get_field("priority")
        priority = PRIORITY_MAP.get(str(raw_pri).lower().strip(), raw_pri) if raw_pri else None
        if priority not in VALID_PRIORITIES: priority = None

        # Category — stored in master as-is for cross-project context
        category_val = get_field("category")

        # Work Type — try direct field first, then category override map
        work_type = get_field("work_type")
        if work_type not in VALID_WORK_TYPES:
            work_type = None
        if not work_type and work_type_map and category_val:
            if category_val in work_type_map:
                mapped = work_type_map[category_val]
                if mapped in VALID_WORK_TYPES:
                    work_type = mapped

        # Dates — handle both plain strings and {start, end} dicts
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

        # Auto-calculate Planned Start = Planned End − 7 days if missing
        if not planned_start and planned_end and auto_calc_planned_start:
            try:
                pe_dt = datetime.strptime(planned_end[:10], "%Y-%m-%d")
                planned_start = (pe_dt - timedelta(days=7)).strftime("%Y-%m-%d")
            except Exception:
                pass

        notes = get_field("notes")

        # ── Properties — same payload on both CREATE and UPDATE ───────────────
        master_props = {
            "Task Name": p_title(task_name),
            "Project":   {"relation": [{"id": project_page_id}]},
        }
        if priority:      master_props["Priority"]  = p_select(priority)
        if work_type:     master_props["Work Type"] = p_select(work_type)
        if category_val:  master_props["Category"]  = p_text(category_val)
        if notes:         master_props["Notes"]     = p_text(notes)
        # Always sync dates — sending None clears a previously-set value so that
        # removing a date in the Project WBS also removes it in Master WBS Tasks.
        master_props["Planned Start"] = p_date({"start": planned_start} if planned_start else None)
        master_props["Planned End"]   = p_date({"start": planned_end}   if planned_end   else None)

        def notion_request(method, url, **kwargs):
            """Make a Notion API request with one automatic retry on timeout."""
            for attempt in range(2):
                try:
                    return method(url, **kwargs)
                except requests.exceptions.Timeout:
                    if attempt == 0:
                        time.sleep(3)
                        continue
                    raise

        try:
            if src_id in mappings:
                master_id = mappings[src_id]["master_id"]
                # Always tag the source db so stale detection works on future syncs.
                # Legacy entries migrated from the old flat format had db=None,
                # which caused the stale detector to silently miss deleted tasks.
                mappings[src_id]["db"] = source_db_id
                r = notion_request(
                    requests.patch,
                    f"{NOTION_API}/pages/{master_id}",
                    headers=headers(token),
                    json={"properties": master_props},
                    timeout=30,
                )
                if r.status_code == 404:
                    # Master page was deleted out-of-band — recreate it
                    del mappings[src_id]
                    r2 = notion_request(
                        requests.post,
                        f"{NOTION_API}/pages",
                        headers=headers(token),
                        json={"parent": {"database_id": MASTER_DB_ID},
                              "properties": master_props},
                        timeout=30,
                    )
                    r2.raise_for_status()
                    new_id = r2.json()["id"]
                    mappings[src_id] = {"master_id": new_id, "db": source_db_id}
                    _write_backlink(token, src_id, new_id, backlink_field)
                    new_tasks.append({"master_id": new_id,
                                      "project_id": project_page_id,
                                      "task_name": task_name})
                    created += 1
                    if emit: emit({"type": "task", "task": task_name, "action": "created"})
                else:
                    r.raise_for_status()
                    # Always ensure back-link is written — older entries predate
                    # this feature and were never back-linked on creation.
                    _write_backlink(token, src_id, master_id, backlink_field)
                    # Propagate rename to Work Session's Session Name
                    if sessions_mappings and master_id in sessions_mappings:
                        ws_id = sessions_mappings[master_id]
                        try:
                            requests.patch(
                                f"{NOTION_API}/pages/{ws_id}",
                                headers=headers(token),
                                json={"properties": {"Session Name": p_title(task_name)}},
                                timeout=15,
                            )
                        except Exception:
                            pass
                    updated += 1
                    if emit: emit({"type": "task", "task": task_name, "action": "updated"})
            else:
                r = notion_request(
                    requests.post,
                    f"{NOTION_API}/pages",
                    headers=headers(token),
                    json={"parent": {"database_id": MASTER_DB_ID},
                          "properties": master_props},
                    timeout=30,
                )
                r.raise_for_status()
                new_id = r.json()["id"]
                mappings[src_id] = {"master_id": new_id, "db": source_db_id}
                _write_backlink(token, src_id, new_id, backlink_field)
                new_tasks.append({"master_id": new_id,
                                  "project_id": project_page_id,
                                  "task_name": task_name})
                created += 1
                if emit: emit({"type": "task", "task": task_name, "action": "created"})
        except Exception as e:
            errors.append(f"'{task_name}': {e}")
            if emit: emit({"type": "task", "task": task_name, "action": "error", "detail": str(e)})

    regenerate_focus_cache(token)
    return {"created": created, "updated": updated,
            "skipped": skipped, "deleted": deleted,
            "errors": errors, "new_tasks": new_tasks,
            "skipped_tasks": skipped_tasks,
            "current_src_ids": current_src_ids}  # used by caller for global orphan cleanup

# ── Work Sessions deduplication ───────────────────────────────────────────────
def _ws_has_date(ws_page):
    """Return True if any date-type property on this Work Session page is set."""
    for prop_val in ws_page.get("properties", {}).values():
        if prop_val.get("type") == "date" and prop_val.get("date") is not None:
            return True
    return False


def _get_task_master_id(ws_page):
    """Find the master task ID from any relation property that isn't 'Project'.
    This is robust to different property names (Task, Task Name, etc.)."""
    for prop_name, prop_val in ws_page.get("properties", {}).items():
        if prop_val.get("type") == "relation" and prop_name.lower() not in ("project", "projects"):
            rels = prop_val.get("relation", [])
            if rels:
                return rels[0]["id"]
    return None


def _get_page_title(page):
    """Extract plain-text title from a Notion page object."""
    for prop_val in page.get("properties", {}).values():
        if prop_val.get("type") == "title":
            parts = prop_val.get("title", [])
            return "".join(p.get("plain_text", "") for p in parts).strip()
    return ""


def _get_project_id_from_page(page):
    """Extract the first project relation ID from a Notion page."""
    for prop_name, prop_val in page.get("properties", {}).items():
        if prop_val.get("type") == "relation" and prop_name.lower() in ("project", "projects"):
            rels = prop_val.get("relation", [])
            if rels:
                return rels[0]["id"]
    return None


def _archive_page(token, page_id, errors):
    """Archive a Notion page; append to errors list on failure."""
    try:
        r = requests.patch(
            f"{NOTION_API}/pages/{page_id}",
            headers=headers(token),
            json={"archived": True},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        errors.append(f"Could not archive {page_id[:8]}: {e}")
        return False


def deduplicate_work_sessions_global(token):
    """
    Two-phase deduplication that handles duplicates caused by duplicate
    Master WBS entries (same task name + project appearing twice).

    Phase 1 — Deduplicate Master WBS Tasks
      Group by (project_id, task_name_lower). For each duplicate group:
        • Keep the entry that appears in the local mappings file (if any),
          otherwise keep the most-recently-edited one.
        • Archive duplicates that have no Work Sessions (safe).
        • If a duplicate has a Work Session, re-link it to the surviving master
          before archiving the duplicate master.

    Phase 2 — Deduplicate Work Sessions
      Group by master task ID. For each group:
        • If any session has logged hours (date set) → keep ALL dated sessions
          (each represents a real work period), archive empty ones.
        • If all sessions are empty → keep most-recently-edited, archive rest.

    Returns: {master_dupes_archived, ws_archived, kept, no_task_sessions,
              updated_mappings, errors}
    """
    errors           = []
    updated_mappings = {}  # master_id → surviving ws_id

    # ── Phase 1: Deduplicate Master WBS Tasks ─────────────────────────────────
    all_master = query_db(token, MASTER_DB_ID)
    mappings   = load_mappings()
    # Build set of master IDs currently tracked in the mappings file
    tracked_master_ids = {v["master_id"] for v in mappings.values() if isinstance(v, dict)}

    # Group by (project_id, task_name_lower)
    master_groups = {}  # (project_id, name_lower) → [page, ...]
    for page in all_master:
        name    = _get_page_title(page)
        proj_id = _get_project_id_from_page(page)
        if not name or not proj_id:
            continue
        key = (proj_id, name.lower())
        master_groups.setdefault(key, []).append(page)

    master_dupes_archived = 0
    # master_id → set of ws_ids that link to it (populated in Phase 2)
    # We'll use this to re-link WS before archiving a duplicate master.

    # Fetch all Work Sessions once (used in both phases)
    all_ws = query_db(token, WORK_SESSIONS_DB_ID)

    # Build index: master_id → [ws_page, ...]
    ws_by_master = {}
    no_task_ids  = []
    for ws in all_ws:
        mid = _get_task_master_id(ws)
        if mid:
            ws_by_master.setdefault(mid, []).append(ws)
        else:
            no_task_ids.append(ws["id"])

    for key, pages in master_groups.items():
        if len(pages) == 1:
            continue  # no duplicate

        # Pick the survivor: prefer the one in the mappings file,
        # then prefer the one with a Work Session, then most recently edited.
        def score(p):
            in_mappings = 1 if p["id"] in tracked_master_ids else 0
            has_ws      = 1 if p["id"] in ws_by_master else 0
            return (in_mappings, has_ws, p.get("last_edited_time", ""))

        pages_sorted = sorted(pages, key=score, reverse=True)
        survivor     = pages_sorted[0]
        duplicates   = pages_sorted[1:]

        for dup in duplicates:
            dup_id = dup["id"]
            # If the duplicate has Work Sessions, re-link them to the survivor
            for ws in ws_by_master.get(dup_id, []):
                try:
                    requests.patch(
                        f"{NOTION_API}/pages/{ws['id']}",
                        headers=headers(token),
                        json={"properties": {"Task": {"relation": [{"id": survivor["id"]}]}}},
                        timeout=15,
                    )
                    # Move the ws to the survivor's bucket for Phase 2
                    ws_by_master.setdefault(survivor["id"], []).append(ws)
                except Exception as e:
                    errors.append(f"Re-link WS {ws['id'][:8]} to survivor: {e}")
            # Remove the duplicate's bucket (all re-linked above)
            ws_by_master.pop(dup_id, None)
            # Archive the duplicate master task
            if _archive_page(token, dup_id, errors):
                master_dupes_archived += 1

    # ── Phase 2: Deduplicate Work Sessions per master task ────────────────────
    ws_archived = 0
    kept        = 0

    for master_id, sessions in ws_by_master.items():
        with_date    = [s for s in sessions if _ws_has_date(s)]
        without_date = [s for s in sessions if not _ws_has_date(s)]

        if len(sessions) == 1:
            kept += 1
            updated_mappings[master_id] = sessions[0]["id"]
            continue

        if with_date:
            # Keep every dated session (real work period), archive empty ones
            to_keep    = with_date
            to_archive = without_date
        else:
            # All empty — keep most-recently-edited, archive rest
            by_time    = sorted(sessions,
                                key=lambda s: s.get("last_edited_time", ""),
                                reverse=True)
            to_keep    = by_time[:1]
            to_archive = by_time[1:]

        kept += len(to_keep)
        updated_mappings[master_id] = to_keep[0]["id"]

        for ws in to_archive:
            if not _ws_has_date(ws):  # safety: never archive logged sessions
                if _archive_page(token, ws["id"], errors):
                    ws_archived += 1

    return {
        "scanned":              len(all_ws),
        "master_dupes_archived": master_dupes_archived,
        "ws_archived":          ws_archived,
        "kept":                 kept,
        "no_task_sessions":     len(no_task_ids),
        "updated_mappings":     updated_mappings,
        "errors":               errors,
    }


# ── Quick-add helper (used by /api/quick-add) ─────────────────────────────────
def quick_add_task(token, source_db_id, project_id, task_name,
                   task_name_field, backlink_field, session_start,
                   due_date="", priority="Normal", work_type="",
                   planned_end_field="", priority_field="", work_type_field="",
                   category="", category_field="",
                   level="", level_field="",
                   org_division="", org_division_field=""):
    """
    Create a task simultaneously in three places:
      1. Project WBS database  (source_db_id)
      2. Master WBS Tasks
      3. Work Sessions  (with Session Start pre-filled if provided)
    Optional: due_date (YYYY-MM-DD), priority, work_type written to both
    Project WBS (using the mapped column names) and Master WBS Tasks.
    Updates both mapping files.
    Returns dict with page URLs or raises on error.
    """
    mappings          = load_mappings()
    sessions_mappings = load_sessions_mappings()

    # 1 ── Project WBS task
    wbs_props = {task_name_field: p_title(task_name)}
    if due_date and planned_end_field:
        wbs_props[planned_end_field] = p_date({"start": due_date})
    if priority and priority_field:
        norm = PRIORITY_MAP.get(priority.lower(), priority)
        if norm in VALID_PRIORITIES:
            wbs_props[priority_field] = {"select": {"name": norm}}
    if work_type and work_type_field and work_type in VALID_WORK_TYPES:
        wbs_props[work_type_field] = {"select": {"name": work_type}}
    # Category — Notion auto-creates the option if it's a new value
    if category and category_field:
        wbs_props[category_field] = {"select": {"name": category}}
    # Service Level — select field, Notion auto-creates new options
    if level and level_field:
        wbs_props[level_field] = {"select": {"name": level}}
    # Organization / Division — plain text field
    if org_division and org_division_field:
        wbs_props[org_division_field] = p_text(org_division)

    r = requests.post(
        f"{NOTION_API}/pages",
        headers=headers(token),
        json={"parent": {"database_id": source_db_id}, "properties": wbs_props},
        timeout=20,
    )
    r.raise_for_status()
    wbs_page = r.json()
    wbs_id   = wbs_page["id"]

    # 2 ── Master WBS entry
    master_props = {
        "Task Name": p_title(task_name),
        "Project":   {"relation": [{"id": project_id}]},
    }
    if due_date:
        master_props["Planned End"]   = p_date({"start": due_date})
        # Auto Planned Start: due_date − 7 days
        ps = (datetime.strptime(due_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        master_props["Planned Start"] = p_date({"start": ps})
    norm_priority = PRIORITY_MAP.get((priority or "").lower(), priority or "Normal")
    if norm_priority in VALID_PRIORITIES:
        master_props["Priority"] = {"select": {"name": norm_priority}}
    if work_type and work_type in VALID_WORK_TYPES:
        master_props["Work Type"] = {"select": {"name": work_type}}

    r2 = requests.post(
        f"{NOTION_API}/pages",
        headers=headers(token),
        json={"parent": {"database_id": MASTER_DB_ID}, "properties": master_props},
        timeout=20,
    )
    r2.raise_for_status()
    master_page = r2.json()
    master_id   = master_page["id"]

    # Back-link Project WBS → Master WBS
    _write_backlink(token, wbs_id, master_id, backlink_field)

    # Save WBS mapping
    mappings[wbs_id] = {"master_id": master_id, "db": source_db_id}
    save_mappings(mappings)

    # 3 ── Work Session
    ws_props = {
        "Session Name": p_title(task_name),
        "Task":    {"relation": [{"id": master_id}]},
        "Project": {"relation": [{"id": project_id}]},
    }
    if session_start:
        ws_props["Session Start"] = {"date": {"start": session_start}}

    r3 = requests.post(
        f"{NOTION_API}/pages",
        headers=headers(token),
        json={"parent": {"database_id": WORK_SESSIONS_DB_ID},
              "properties": ws_props},
        timeout=20,
    )
    r3.raise_for_status()
    ws_page = r3.json()
    ws_id   = ws_page["id"]

    sessions_mappings[master_id] = ws_id
    save_sessions_mappings(sessions_mappings)

    regenerate_focus_cache(token)
    return {
        "wbs_url":    wbs_page.get("url", ""),
        "master_url": master_page.get("url", ""),
        "ws_url":     ws_page.get("url", ""),
    }


# ── Sync log writer ───────────────────────────────────────────────────────────
def write_sync_log(total):
    """Write a timestamped log file when there are errors or skipped records.
    Returns the log file path, or None if the run was clean."""
    errors        = total.get("errors", [])
    skipped_tasks = total.get("skipped_tasks", [])
    if not errors and not skipped_tasks:
        return None

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(BASE_DIR, f"sync_log_{ts}.txt")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Notion WBS Sync Log — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 64 + "\n\n")

        f.write("SUMMARY\n")
        f.write(f"  Master WBS  — Created: {total.get('created',0)}, "
                f"Updated: {total.get('updated',0)}, "
                f"Skipped: {total.get('skipped',0)}, "
                f"Deleted: {total.get('deleted',0)}\n")
        f.write(f"  Work Sessions — Created: {total.get('ws_created',0)}, "
                f"Already existed: {total.get('ws_skipped',0)}\n")
        f.write(f"  Errors: {len(errors)}   Skipped records: {len(skipped_tasks)}\n\n")

        if errors:
            f.write("ERRORS\n")
            f.write("-" * 64 + "\n")
            for i, e in enumerate(errors, 1):
                f.write(f"  {i:3}. {e}\n")
            f.write("\n")

        if skipped_tasks:
            f.write("SKIPPED RECORDS (not synced — check column mapping or add a title)\n")
            f.write("-" * 64 + "\n")
            for i, t in enumerate(skipped_tasks, 1):
                src    = f"[{t['source']}] " if t.get("source") else ""
                target = t.get("url") or t.get("page_id", "unknown")
                reason = t.get("reason", "")
                f.write(f"  {i:3}. {src}{target}\n       → {reason}\n")

    return log_path


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML_PAGE)

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
def api_save_config():
    save_config(request.json)
    return jsonify({"ok": True})

@app.route("/api/discover", methods=["POST"])
def api_discover():
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token provided"}), 400
    try:
        all_dbs = search_wbs_databases(token)
        result = []
        skip = {MASTER_DB_ID.replace("-",""), PROJECTS_DB_ID.replace("-","")}
        for db in all_dbs:
            db_id = db["id"].replace("-","")
            if db_id in skip:
                continue
            title = "".join(t["plain_text"] for t in db.get("title", []))
            # Enforce the naming rule: "WBS" must be the first alphabetic content.
            # Strips leading emoji/symbols so both "WBS - X" and "📋 WBS — X" match.
            if title and re.match(r'^[^a-zA-Z]*wbs', title.strip(), re.IGNORECASE):
                result.append({"id": db["id"], "title": title,
                                "url": db.get("url","")})
        result.sort(key=lambda x: x["title"])
        return jsonify({"databases": result})
    except requests.HTTPError as e:
        return jsonify({"error": f"Notion API error: {e.response.status_code} — {e.response.text}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/schema", methods=["POST"])
def api_schema():
    token = request.json.get("token","").strip()
    db_id = request.json.get("db_id","").strip()
    try:
        schema = get_db_schema(token, db_id)
        cols = [{"name": k, "type": v["type"]}
                for k, v in schema.get("properties", {}).items()]
        cols.sort(key=lambda x: x["name"])
        return jsonify({"columns": cols})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/test-token", methods=["POST"])
def api_test_token():
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token provided"}), 400
    try:
        r = requests.get(f"{NOTION_API}/users/me",
                         headers=headers(token), timeout=10)
        if r.status_code == 401:
            return jsonify({"error": "Invalid token — check it was copied correctly."}), 401
        r.raise_for_status()
        data = r.json()
        name = data.get("name") or data.get("bot", {}).get("owner", {}).get("user", {}).get("name", "")
        return jsonify({"ok": True, "name": name})
    except requests.HTTPError as e:
        return jsonify({"error": f"Notion API error {e.response.status_code}: {e.response.text}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/projects", methods=["POST"])
def api_projects():
    token = request.json.get("token","").strip()
    try:
        pages = query_db(token, PROJECTS_DB_ID)
        projects = []
        for p in pages:
            name = ""
            for v in p["properties"].values():
                if v["type"] == "title":
                    name = "".join(r["plain_text"] for r in v["title"])
                    break
            if name:
                projects.append({"id": p["id"], "name": name})
        projects.sort(key=lambda x: x["name"])
        return jsonify({"projects": projects})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/sync", methods=["POST"])
def api_sync():
    data     = request.json
    token    = data.get("token","").strip()
    sources  = data.get("sources", [])

    if not token:
        return jsonify({"error": "No token"}), 400

    mappings          = load_mappings()
    sessions_mappings = load_sessions_mappings()
    total             = {"created": 0, "updated": 0, "skipped": 0, "deleted": 0,
                         "ws_created": 0, "ws_skipped": 0,
                         "errors": [], "new_tasks": [], "skipped_tasks": []}
    source_labels     = {s["db_id"]: s.get("db_title","?") for s in sources}

    # ── Phase 1: Project WBS → Master WBS Tasks ───────────────────────────────
    all_current_src_ids = set()   # union of live page IDs across all databases
    for src in sources:
        try:
            wt_map = src.get("work_type_map") or {}
            if isinstance(wt_map, str):
                try:
                    wt_map = json.loads(wt_map)
                except Exception:
                    wt_map = {}

            result = sync_one_database(
                token,
                src["db_id"],
                src["project_id"],
                src["field_map"],
                mappings,
                backlink_field=src.get("backlink_field", "Master WBS"),
                work_type_map=wt_map,
                auto_calc_planned_start=src.get("auto_calc_planned_start", True),
                sessions_mappings=sessions_mappings,
            )
            all_current_src_ids |= result.get("current_src_ids", set())
            total["created"]       += result["created"]
            total["updated"]       += result["updated"]
            total["skipped"]       += result["skipped"]
            total["deleted"]       += result["deleted"]
            total["new_tasks"]     += result["new_tasks"]
            for e in result["errors"]:
                total["errors"].append(f"[{source_labels[src['db_id']]}] {e}")
            for t in result.get("skipped_tasks", []):
                total["skipped_tasks"].append({**t, "source": source_labels[src["db_id"]]})
        except Exception as e:
            total["errors"].append(f"[{source_labels.get(src['db_id'],'?')}] Fatal: {e}")

    # Clean up legacy db=None entries whose source page no longer exists anywhere
    orphans = cleanup_orphaned_mappings(
        token, all_current_src_ids, mappings, sessions_mappings)
    total["deleted"] += orphans

    save_mappings(mappings)

    # ── Phase 2: Master WBS Tasks → Work Sessions (auto-chained) ─────────────
    # Deduplicate by project_id — two sources can share the same project entry,
    # but we only need to run the Work Sessions sync once per project.
    processed_projects = set()
    for src in sources:
        project_id = src.get("project_id", "")
        if not project_id or project_id in processed_projects:
            continue
        processed_projects.add(project_id)
        try:
            ws_result = sync_work_sessions_for_project(token, project_id, sessions_mappings)
            total["ws_created"] += ws_result["created"]
            total["ws_skipped"] += ws_result["skipped"]
            for e in ws_result["errors"]:
                total["errors"].append(f"[{source_labels.get(src['db_id'],'?')} · Sessions] {e}")
        except Exception as e:
            total["errors"].append(f"[{source_labels.get(src['db_id'],'?')} · Sessions] Fatal: {e}")

    save_sessions_mappings(sessions_mappings)

    log_path = write_sync_log(total)
    if log_path:
        total["log_file"] = os.path.basename(log_path)
        total["log_dir"]  = BASE_DIR

    return jsonify(total)


# ── Polling-based progress endpoints ─────────────────────────────────────────
@app.route("/api/sync-start", methods=["POST"])
def api_sync_start():
    """
    Start a sync job in a background thread. Returns a job_id immediately.
    The UI polls /api/sync-status/<job_id> for progress updates.
    """
    data    = request.json or {}
    token   = data.get("token", "").strip()
    sources = data.get("sources", [])

    if not token:
        return jsonify({"error": "No token"}), 400
    if not sources:
        return jsonify({"error": "No sources selected"}), 400

    job_id = uuid.uuid4().hex[:12]
    _sync_jobs[job_id] = {"events": [], "done": False, "result": None, "error": None}

    def emit(event):
        _sync_jobs[job_id]["events"].append(event)

    def run():
        try:
            mappings          = load_mappings()
            sessions_mappings = load_sessions_mappings()
            total = {"created": 0, "updated": 0, "skipped": 0, "deleted": 0,
                     "ws_created": 0, "ws_skipped": 0,
                     "errors": [], "new_tasks": [], "skipped_tasks": []}
            source_labels = {s["db_id"]: s.get("db_title", "?") for s in sources}

            emit({"type": "start", "total_dbs": len(sources)})

            # ── Phase 1: Project WBS → Master WBS Tasks ───────────────────
            all_current_src_ids = set()
            for db_n, src in enumerate(sources, 1):
                db_title = source_labels[src["db_id"]]
                emit({"type": "db_start", "db": db_title,
                      "db_n": db_n, "total_dbs": len(sources)})
                try:
                    wt_map = src.get("work_type_map") or {}
                    if isinstance(wt_map, str):
                        try:    wt_map = json.loads(wt_map)
                        except: wt_map = {}

                    result = sync_one_database(
                        token, src["db_id"], src["project_id"],
                        src["field_map"], mappings,
                        backlink_field=src.get("backlink_field", "Master WBS"),
                        work_type_map=wt_map,
                        auto_calc_planned_start=src.get("auto_calc_planned_start", True),
                        sessions_mappings=sessions_mappings,
                        emit=emit,
                    )
                    all_current_src_ids |= result.get("current_src_ids", set())
                    total["created"]   += result["created"]
                    total["updated"]   += result["updated"]
                    total["skipped"]   += result["skipped"]
                    total["deleted"]   += result["deleted"]
                    total["new_tasks"] += result["new_tasks"]
                    for e in result["errors"]:
                        total["errors"].append(f"[{db_title}] {e}")
                    for t in result.get("skipped_tasks", []):
                        total["skipped_tasks"].append({**t, "source": db_title})
                    emit({"type": "db_done", "db": db_title,
                          "created": result["created"], "updated": result["updated"],
                          "skipped": result["skipped"], "deleted": result["deleted"],
                          "errors": len(result["errors"])})
                except Exception as e:
                    msg = f"[{db_title}] Fatal: {e}"
                    total["errors"].append(msg)
                    emit({"type": "db_done", "db": db_title, "created": 0, "updated": 0,
                          "skipped": 0, "deleted": 0, "errors": 1, "fatal": str(e)})

            # Clean up legacy db=None orphans after all databases are known
            orphans = cleanup_orphaned_mappings(
                token, all_current_src_ids, mappings, sessions_mappings)
            if orphans:
                total["deleted"] += orphans
                emit({"type": "orphans_cleaned", "count": orphans})

            save_mappings(mappings)

            # ── Phase 2: Master WBS Tasks → Work Sessions ─────────────────
            processed_projects = set()
            unique_srcs = [s for s in sources if s.get("project_id")
                           and s["project_id"] not in processed_projects
                           and not processed_projects.add(s["project_id"])]
            emit({"type": "phase2_start", "project_count": len(unique_srcs)})

            for ws_n, src in enumerate(unique_srcs, 1):
                db_title   = source_labels[src["db_id"]]
                project_id = src["project_id"]
                try:
                    ws_result = sync_work_sessions_for_project(
                        token, project_id, sessions_mappings)
                    total["ws_created"] += ws_result["created"]
                    total["ws_skipped"] += ws_result["skipped"]
                    for e in ws_result["errors"]:
                        total["errors"].append(f"[{db_title} · Sessions] {e}")
                    emit({"type": "ws_done", "project": db_title,
                          "n": ws_n, "total": len(unique_srcs),
                          "created": ws_result["created"],
                          "skipped": ws_result["skipped"]})
                except Exception as e:
                    total["errors"].append(f"[{db_title} · Sessions] Fatal: {e}")
                    emit({"type": "ws_done", "project": db_title,
                          "n": ws_n, "total": len(unique_srcs),
                          "created": 0, "skipped": 0, "error": str(e)})

            save_sessions_mappings(sessions_mappings)

            log_path = write_sync_log(total)
            if log_path:
                total["log_file"] = os.path.basename(log_path)
                total["log_dir"]  = BASE_DIR

            emit({"type": "finished"})
            _sync_jobs[job_id]["result"] = total

        except Exception as e:
            _sync_jobs[job_id]["error"] = str(e)
            emit({"type": "error", "message": str(e)})
        finally:
            _sync_jobs[job_id]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/sync-status/<job_id>", methods=["GET"])
def api_sync_status(job_id):
    """
    Poll for progress of a running sync job.
    Pass ?offset=N to receive only events after index N (avoids re-sending old events).
    Returns: {events, done, result, error}
    """
    job = _sync_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    offset = max(0, int(request.args.get("offset", 0)))
    return jsonify({
        "events": job["events"][offset:],
        "done":   job["done"],
        "result": job["result"],   # None until done
        "error":  job["error"],
    })


@app.route("/api/sync-work-sessions", methods=["POST"])
def api_sync_work_sessions():
    """
    Idempotent Work Sessions sync.
    Accepts the same 'sources' list as /api/sync.
    Creates a Work Session (Task + Project only) for every Master WBS Task
    that doesn't already have one. Re-running never creates duplicates.
    """
    body    = request.json
    token   = body.get("token", "").strip()
    sources = body.get("sources", [])
    if not token:
        return jsonify({"error": "No token"}), 400

    sessions_mappings = load_sessions_mappings()
    total = {"created": 0, "skipped": 0, "errors": []}

    for src in sources:
        project_id = src.get("project_id", "")
        db_title   = src.get("db_title", "?")
        if not project_id:
            continue
        try:
            result = sync_work_sessions_for_project(token, project_id, sessions_mappings)
            total["created"] += result["created"]
            total["skipped"] += result["skipped"]
            for e in result["errors"]:
                total["errors"].append(f"[{db_title}] {e}")
        except Exception as e:
            total["errors"].append(f"[{db_title}] Fatal: {e}")

    save_sessions_mappings(sessions_mappings)
    return jsonify(total)

def _extract_page_id(url_or_id):
    """Extract a clean UUID (no hyphens) from a Notion URL or raw ID string."""
    if not url_or_id:
        return None
    # Strip query params and fragments
    raw = url_or_id.split("?")[0].split("#")[0].rstrip("/")
    # Last path segment
    segment = raw.split("/")[-1]
    # Remove title prefix (e.g. "EDG-6648-Course-Design-36a54686ae1580a0a85cf536878e61e9")
    # IDs are 32 hex chars (with or without dashes)
    hex_only = segment.replace("-", "")
    if len(hex_only) >= 32:
        raw_id = hex_only[-32:]
        return f"{raw_id[:8]}-{raw_id[8:12]}-{raw_id[12:16]}-{raw_id[16:20]}-{raw_id[20:]}"
    return segment  # fallback


def add_project_page_link(token, project_entry_id, hub_page_url):
    """Append a '# Project Page' heading + link_to_page block to a Projects DB entry.
    Idempotent: skips if the page already has 'Project Page' in its content."""
    hub_page_id = _extract_page_id(hub_page_url)
    if not hub_page_id:
        raise ValueError(f"Could not parse hub page ID from: {hub_page_url}")

    # Check existing blocks to avoid duplicate headings
    r = requests.get(
        f"{NOTION_API}/blocks/{project_entry_id}/children",
        headers=headers(token), timeout=15,
    )
    r.raise_for_status()
    for block in r.json().get("results", []):
        if block.get("type") == "heading_1":
            texts = block.get("heading_1", {}).get("rich_text", [])
            combined = "".join(t.get("plain_text", "") for t in texts)
            if "project page" in combined.lower():
                return {"status": "already_exists"}

    # Append heading + link_to_page
    body = {
        "children": [
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
        ]
    }
    r2 = requests.patch(
        f"{NOTION_API}/blocks/{project_entry_id}/children",
        headers=headers(token), json=body, timeout=20,
    )
    r2.raise_for_status()
    return {"status": "added", "hub_page_id": hub_page_id}


@app.route("/api/add-project-page-link", methods=["POST"])
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
        result = add_project_page_link(token, project_entry_id, hub_page_url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/deduplicate", methods=["POST"])
def api_deduplicate():
    """Deduplicate Work Sessions globally, then reconcile sessions_mappings."""
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400
    try:
        result = deduplicate_work_sessions_global(token)
        # Reconcile sessions_mappings with surviving sessions
        sessions_mappings = load_sessions_mappings()
        sessions_mappings.update(result["updated_mappings"])
        save_sessions_mappings(sessions_mappings)
        result.pop("updated_mappings")   # don't send large dict to browser
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/wbs-categories", methods=["POST"])
def api_wbs_categories():
    """
    Return the select options for the configured category field of a WBS database.
    Used by Quick Start to populate the Category dropdown dynamically.

    Body: {token, db_id, category_field}
    Returns: {options: [{name, color}, ...]}
    If no category_field is configured, returns {options: []}.
    """
    body           = request.json or {}
    token          = body.get("token", "").strip()
    db_id          = body.get("db_id", "").strip()
    category_field = body.get("category_field", "").strip()

    if not token or not db_id:
        return jsonify({"error": "token and db_id required"}), 400
    if not category_field:
        return jsonify({"options": []})

    try:
        r = requests.get(
            f"{NOTION_API}/databases/{db_id}",
            headers=headers(token), timeout=15,
        )
        r.raise_for_status()
        props = r.json().get("properties", {})
        field = props.get(category_field, {})
        raw_options = field.get("select", {}).get("options", [])
        options = [{"name": o["name"], "color": o.get("color", "default")}
                   for o in raw_options]
        return jsonify({"options": options, "field_type": field.get("type", "")})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/wbs-text-options", methods=["POST"])
def api_wbs_text_options():
    """
    Return unique non-empty values that exist in a text field across all rows
    of a WBS database. Used by Quick Start to show previously used
    Organization/Division values as suggestions.

    Body: {token, db_id, field_name}
    Returns: {options: ["value1", "value2", ...]}
    """
    body       = request.json or {}
    token      = body.get("token", "").strip()
    db_id      = body.get("db_id", "").strip()
    field_name = body.get("field_name", "").strip()

    if not token or not db_id or not field_name:
        return jsonify({"options": []})
    try:
        pages = query_db(token, db_id)
        seen = set()
        for page in pages:
            val = extract(page.get("properties", {}).get(field_name, {}))
            if val and val.strip():
                seen.add(val.strip())
        return jsonify({"options": sorted(seen)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/quick-add", methods=["POST"])
def api_quick_add():
    """Create a task in Project WBS + Master WBS + Work Sessions in one call."""
    body              = request.json
    token             = body.get("token", "").strip()
    source_db_id      = body.get("source_db_id", "").strip()
    project_id        = body.get("project_id", "").strip()
    task_name         = body.get("task_name", "").strip()
    task_name_field   = body.get("task_name_field", "Task")
    backlink_field    = body.get("backlink_field", "Master WBS")
    session_start     = body.get("session_start", "")
    due_date          = body.get("due_date", "")
    priority          = body.get("priority", "Normal")
    work_type         = body.get("work_type", "")
    planned_end_field = body.get("planned_end_field", "")
    priority_field    = body.get("priority_field", "")
    work_type_field   = body.get("work_type_field", "")
    category          = body.get("category", "").strip()
    category_field    = body.get("category_field", "").strip()
    level             = body.get("level", "").strip()
    level_field       = body.get("level_field", "").strip()
    org_division      = body.get("org_division", "").strip()
    org_division_field= body.get("org_division_field", "").strip()

    if not all([token, source_db_id, project_id, task_name]):
        return jsonify({"error": "token, source_db_id, project_id, and task_name are all required"}), 400
    try:
        result = quick_add_task(
            token, source_db_id, project_id, task_name,
            task_name_field, backlink_field, session_start,
            due_date=due_date, priority=priority, work_type=work_type,
            planned_end_field=planned_end_field,
            priority_field=priority_field,
            work_type_field=work_type_field,
            category=category,
            category_field=category_field,
            level=level,
            level_field=level_field,
            org_division=org_division,
            org_division_field=org_division_field,
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


# ── Check existing tasks (dedup helper) ───────────────────────────────────────

@app.route("/api/existing-tasks", methods=["POST"])
def api_existing_tasks():
    """
    Return all non-completed tasks in Master WBS Tasks for a given project,
    along with the count and URLs of their linked Work Sessions.

    Body: {token, project_id}
    Returns: {tasks: [{id, name, url, status, planned_end, work_sessions: [{id, url, name, start, status}]}]}
    """
    body       = request.json or {}
    token      = body.get("token", "").strip()
    project_id = body.get("project_id", "").strip()

    if not token or not project_id:
        return jsonify({"error": "token and project_id are required"}), 400

    try:
        filter_body = {
            "and": [
                {"property": "Project", "relation": {"contains": project_id}},
                {"property": "Status",  "status":   {"does_not_equal": "Completed"}},
            ]
        }
        master_pages = query_db(token, MASTER_DB_ID, filter_body)

        # Fetch all Work Sessions for this project in one query
        ws_filter = {"property": "Project", "relation": {"contains": project_id}}
        ws_pages  = query_db(token, WORK_SESSIONS_DB_ID, ws_filter)

        # Index Work Sessions by master task ID
        ws_by_task = {}
        for ws in ws_pages:
            props    = ws.get("properties", {})
            task_rel = props.get("Task", {}).get("relation", [])
            for rel in task_rel:
                tid = rel["id"].replace("-", "")
                ws_by_task.setdefault(tid, []).append(ws)

        tasks = []
        for page in master_pages:
            props     = page.get("properties", {})
            task_id   = page["id"].replace("-", "")
            task_name = "".join(t.get("plain_text","")
                                for t in props.get("Task Name",{}).get("title", []))
            status    = extract(props.get("Status", {})) or ""
            pe_raw    = props.get("Planned End", {}).get("date") or {}
            planned_end = pe_raw.get("start", "") if isinstance(pe_raw, dict) else ""

            sessions_info = []
            for ws in ws_by_task.get(task_id, []):
                wp = ws.get("properties", {})
                ws_name  = "".join(t.get("plain_text","")
                                   for t in wp.get("Session Name",{}).get("title",[])) or "Session"
                ws_start  = (wp.get("Session Start",{}).get("date") or {}).get("start","")
                ws_status = extract(wp.get("Status", {})) or ""
                sessions_info.append({
                    "id":     ws["id"].replace("-",""),
                    "url":    ws.get("url",""),
                    "name":   ws_name,
                    "start":  ws_start,
                    "status": ws_status,
                })
            sessions_info.sort(key=lambda s: s["start"] or "", reverse=True)

            tasks.append({
                "id":            task_id,
                "url":           page.get("url",""),
                "name":          task_name,
                "status":        status,
                "planned_end":   planned_end,
                "work_sessions": sessions_info,
            })

        tasks.sort(key=lambda t: t["planned_end"] or "9999-99-99")
        return jsonify({"tasks": tasks})
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        msg  = e.response.text[:300] if e.response is not None else str(e)
        return jsonify({"error": f"Notion API {code}: {msg}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/log-session", methods=["POST"])
def api_log_session():
    """
    Create a Work Session linked to an EXISTING Master WBS task (no new task created).

    Body: {token, master_task_id, project_id, session_start,
           session_end (optional), status (optional), work_type (optional)}
    Returns: {ok, ws_url}
    """
    body           = request.json or {}
    token          = body.get("token", "").strip()
    master_task_id = body.get("master_task_id", "").strip()
    project_id     = body.get("project_id", "").strip()
    session_start  = body.get("session_start", "").strip()
    session_end    = body.get("session_end", "").strip()
    status         = body.get("status", "").strip()
    work_type      = body.get("work_type", "").strip()

    if not token or not master_task_id or not project_id:
        return jsonify({"error": "token, master_task_id, and project_id are required"}), 400

    try:
        # Fetch task name to use as session name
        r = requests.get(f"{NOTION_API}/pages/{master_task_id}",
                         headers=headers(token), timeout=15)
        r.raise_for_status()
        page_props = r.json().get("properties", {})
        task_name  = "".join(t.get("plain_text","")
                             for t in page_props.get("Task Name",{}).get("title",[]))

        ws_props = {
            "Session Name": p_title(task_name or "Work Session"),
            "Task":    {"relation": [{"id": master_task_id}]},
            "Project": {"relation": [{"id": project_id}]},
        }
        if session_start:
            date_val = {"start": session_start}
            if session_end:
                date_val["end"] = session_end
            ws_props["Session Start"] = {"date": date_val}
        if status:
            ws_props["Status"] = {"status": {"name": status}}
        if work_type and work_type in VALID_WORK_TYPES:
            ws_props["Work Type"] = {"select": {"name": work_type}}

        r2 = requests.post(
            f"{NOTION_API}/pages",
            headers=headers(token),
            json={"parent": {"database_id": WORK_SESSIONS_DB_ID}, "properties": ws_props},
            timeout=20,
        )
        r2.raise_for_status()
        ws_page = r2.json()

        sessions_mappings = load_sessions_mappings()
        sessions_mappings[master_task_id.replace("-","")] = ws_page["id"]
        save_sessions_mappings(sessions_mappings)

        return jsonify({"ok": True, "ws_url": ws_page.get("url", "")})
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        msg  = e.response.text[:300] if e.response is not None else str(e)
        return jsonify({"error": f"Notion API {code}: {msg}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ── Focus Task List ───────────────────────────────────────────────────────────

@app.route("/focus")
def focus_page():
    return render_template_string(FOCUS_HTML_PAGE)


@app.route("/api/focus-tasks", methods=["POST"])
def api_focus_tasks():
    """
    Read focus-task-list-cache.json, classify tasks into Overdue / Due Today /
    Due This Week buckets, then check each task's Work Sessions for completion.

    Body: {token}  — falls back to saved config token if omitted.
    Returns: {today, week_end, overdue, due_today, this_week, generated_at, cache_task_count}
    """
    import datetime, os, json as _json

    body  = request.json or {}
    token = body.get("token", "").strip()
    if not token:
        cfg   = load_config()
        token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token — save one in the Sync Tool first"}), 400

    # 1. Dates
    today      = datetime.date.today()
    week_end   = today + datetime.timedelta(days=7)
    today_s    = today.isoformat()
    week_end_s = week_end.isoformat()

    # 2. Load cache
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "focus-task-list-cache.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = _json.load(f)
    except Exception as e:
        return jsonify({"error": f"Cannot read cache: {e}"}), 500

    all_tasks    = cache.get("tasks", [])
    generated_at = cache.get("generated_at", "")
    cache_count  = cache.get("task_count", len(all_tasks))

    # 3. Classify into buckets
    buckets = {"overdue": [], "due_today": [], "this_week": []}
    for task in all_tasks:
        pe = task.get("planned_end", "")
        if not pe:
            continue
        if pe < today_s:
            buckets["overdue"].append(task)
        elif pe == today_s:
            buckets["due_today"].append(task)
        elif today_s < pe <= week_end_s:
            buckets["this_week"].append(task)

    # 4. Check completion: exclude tasks where every active Work Session is Completed
    def is_completed(task):
        ws_urls = task.get("work_sessions", [])
        if not ws_urls:
            return False
        all_done = True
        for url in ws_urls:
            page_id = url.rstrip("/").split("/")[-1].replace("-", "")
            if len(page_id) == 32:
                page_id = f"{page_id[:8]}-{page_id[8:12]}-{page_id[12:16]}-{page_id[16:20]}-{page_id[20:]}"
            try:
                r = requests.get(f"{NOTION_API}/pages/{page_id}",
                                 headers=headers(token), timeout=8)
                if not r.ok:
                    all_done = False
                    continue
                data = r.json()
                if data.get("archived") or data.get("in_trash"):
                    continue  # trashed — ignore
                status = extract(data.get("properties", {}).get("Status", {}))
                if status != "Completed":
                    all_done = False
            except Exception:
                all_done = False
        return all_done

    priority_order = {"Urgent": 0, "High": 1, "Normal": 2, "Low": 3}

    def filter_and_sort(tasks, sort_by_date=False):
        result = [t for t in tasks if not is_completed(t)]
        if sort_by_date:
            result.sort(key=lambda t: t.get("planned_end", ""))
        else:
            result.sort(key=lambda t: priority_order.get(t.get("priority", "Normal"), 2))
        return result

    return jsonify({
        "today":            today_s,
        "week_end":         week_end_s,
        "overdue":          filter_and_sort(buckets["overdue"],   sort_by_date=True),
        "due_today":        filter_and_sort(buckets["due_today"], sort_by_date=False),
        "this_week":        filter_and_sort(buckets["this_week"], sort_by_date=True),
        "generated_at":     generated_at,
        "cache_task_count": cache_count,
    })


@app.route("/api/regenerate-focus-cache", methods=["POST"])
def api_regenerate_focus_cache():
    """Force-rebuild focus-task-list-cache.json from Notion."""
    body  = request.json or {}
    token = body.get("token", "").strip()
    if not token:
        cfg   = load_config()
        token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400
    try:
        regenerate_focus_cache(token)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


FOCUS_HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📌 Focus Tasks</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f5f5f4;
    color: #1c1917;
    min-height: 100vh;
  }
  header {
    background: #fff;
    border-bottom: 1px solid #e7e5e4;
    padding: 16px 24px;
    display: flex;
    align-items: center;
    gap: 12px;
    position: sticky;
    top: 0;
    z-index: 10;
  }
  header h1 { font-size: 18px; font-weight: 600; flex: 1; }
  .meta { font-size: 12px; color: #78716c; }
  .btn {
    padding: 7px 14px;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    transition: background .15s;
  }
  .btn-primary   { background: #2563eb; color: #fff; }
  .btn-primary:hover { background: #1d4ed8; }
  .btn-secondary { background: #e7e5e4; color: #1c1917; }
  .btn-secondary:hover { background: #d6d3d1; }
  .btn:disabled  { opacity: .5; cursor: not-allowed; }

  main { max-width: 900px; margin: 0 auto; padding: 24px 20px; }

  .loading, .error-box {
    text-align: center;
    padding: 60px 20px;
    color: #78716c;
    font-size: 15px;
  }
  .error-box { color: #dc2626; }

  .bucket { margin-bottom: 28px; }
  .bucket-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
  }
  .bucket-header h2 { font-size: 15px; font-weight: 600; }
  .count-badge {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 20px;
    background: #e7e5e4;
    color: #57534e;
  }
  .empty-msg { font-size: 14px; color: #78716c; padding: 12px 0; }

  .task-card {
    background: #fff;
    border: 1px solid #e7e5e4;
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 8px;
    display: flex;
    align-items: flex-start;
    gap: 12px;
  }
  .task-card:hover { border-color: #a8a29e; }
  .priority-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 5px;
  }
  .dot-urgent { background: #dc2626; }
  .dot-high   { background: #f97316; }
  .dot-normal { background: #3b82f6; }
  .dot-low    { background: #a8a29e; }

  .task-body { flex: 1; min-width: 0; }
  .task-name {
    font-size: 14px;
    font-weight: 500;
    color: #1c1917;
    text-decoration: none;
  }
  .task-name:hover { text-decoration: underline; }
  .task-meta {
    font-size: 12px;
    color: #78716c;
    margin-top: 3px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .task-notes {
    font-size: 12px;
    color: #57534e;
    margin-top: 5px;
    font-style: italic;
  }
  .tag { padding: 1px 6px; border-radius: 4px; font-size: 11px; font-weight: 500; background: #f5f5f4; color: #57534e; }
  .tag-urgent { background: #fee2e2; color: #dc2626; }
  .tag-high   { background: #ffedd5; color: #c2410c; }

  .summary-bar {
    background: #fff;
    border: 1px solid #e7e5e4;
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 28px;
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
    font-size: 13px;
    color: #57534e;
  }
  .summary-bar strong { color: #1c1917; }

  .all-clear { text-align: center; padding: 60px 20px; font-size: 28px; }
  .all-clear p { font-size: 15px; color: #78716c; margin-top: 8px; }

  .spinner {
    display: inline-block;
    width: 16px; height: 16px;
    border: 2px solid #e7e5e4;
    border-top-color: #2563eb;
    border-radius: 50%;
    animation: spin .7s linear infinite;
    vertical-align: middle;
    margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <h1>📌 Focus Tasks</h1>
  <span class="meta" id="meta-line">—</span>
  <button class="btn btn-secondary" id="btn-rebuild" onclick="rebuildCache()"
          title="Re-query Notion and rebuild the local task cache">🔄 Rebuild cache</button>
  <button class="btn btn-primary"   id="btn-refresh" onclick="loadFocus()">Refresh</button>
  <a href="/" class="btn btn-secondary" style="text-decoration:none;">← Sync Tool</a>
</header>

<main id="main-content">
  <div class="loading"><span class="spinner"></span> Loading focus tasks…</div>
</main>

<script>
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-US", {month:"short", day:"numeric"});
}

function priorityDot(p) {
  const cls = {Urgent:"dot-urgent", High:"dot-high", Normal:"dot-normal", Low:"dot-low"}[p] || "dot-normal";
  return `<div class="priority-dot ${cls}"></div>`;
}

function priorityTag(p) {
  if (p === "Urgent") return `<span class="tag tag-urgent">Urgent</span>`;
  if (p === "High")   return `<span class="tag tag-high">High</span>`;
  return "";
}

function renderTask(t, showDue) {
  const due   = showDue && t.planned_end ? ` · Due ${fmtDate(t.planned_end)}` : "";
  const wt    = t.work_type ? ` · ${t.work_type}` : "";
  const proj  = t.project_name ? `<span>${t.project_name}</span>` : "";
  const ptag  = priorityTag(t.priority);
  const notes = t.notes ? `<div class="task-notes">${t.notes}</div>` : "";
  return `
    <div class="task-card">
      ${priorityDot(t.priority)}
      <div class="task-body">
        <a class="task-name" href="${t.url.replace('https://', 'notion://')}">${t.name}</a>
        <div class="task-meta">${proj}${ptag}<span>${t.priority}${due}${wt}</span></div>
        ${notes}
      </div>
    </div>`;
}

function renderBucket(label, emoji, tasks, showDue, weekEnd) {
  const weekNote = label === "Due This Week" && weekEnd
    ? ` <span style="font-weight:400;color:#78716c;font-size:13px;">through ${fmtDate(weekEnd)}</span>`
    : "";
  const emptyMsg = {
    "Overdue":       "✅ Nothing overdue — great!",
    "Due Today":     "📭 Nothing due today.",
    "Due This Week": "📭 Nothing due in the next 7 days.",
  }[label] || "";
  const body = tasks.length === 0
    ? `<div class="empty-msg">${emptyMsg}</div>`
    : tasks.map(t => renderTask(t, showDue)).join("");
  return `
    <div class="bucket">
      <div class="bucket-header">
        <h2>${emoji} ${label}${weekNote}</h2>
        <span class="count-badge">${tasks.length}</span>
      </div>
      ${body}
    </div>`;
}

function setButtons(loading) {
  document.getElementById("btn-refresh").disabled = loading;
  document.getElementById("btn-rebuild").disabled = loading;
  document.getElementById("btn-refresh").innerHTML = loading
    ? '<span class="spinner"></span>Loading…' : "Refresh";
}

async function loadFocus() {
  setButtons(true);
  const main = document.getElementById("main-content");
  main.innerHTML = '<div class="loading"><span class="spinner"></span> Checking task statuses…</div>';
  try {
    const res  = await fetch("/api/focus-tasks", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({})
    });
    const data = await res.json();
    if (data.error) { showError(data.error); return; }

    const total  = data.overdue.length + data.due_today.length + data.this_week.length;
    const all    = [...data.overdue, ...data.due_today, ...data.this_week];
    const urgent = all.filter(t => t.priority === "Urgent").length;
    const high   = all.filter(t => t.priority === "High").length;

    const genAt = data.generated_at
      ? new Date(data.generated_at).toLocaleString("en-US",
          {month:"short", day:"numeric", hour:"numeric", minute:"2-digit"})
      : "—";
    document.getElementById("meta-line").textContent =
      `Cache: ${genAt} · ${data.cache_task_count} tasks total`;

    if (total === 0) {
      main.innerHTML = `<div class="all-clear">🎉<p>No tasks due or overdue. Enjoy the breathing room!</p></div>`;
      return;
    }

    const summary = `<div class="summary-bar">
      <span>Needing attention: <strong>${total}</strong></span>
      <span>🔴 Overdue: <strong>${data.overdue.length}</strong></span>
      <span>🟡 Due today: <strong>${data.due_today.length}</strong></span>
      <span>🔵 This week: <strong>${data.this_week.length}</strong></span>
      ${urgent ? `<span>🚨 Urgent: <strong>${urgent}</strong></span>` : ""}
      ${high   ? `<span>⚠️ High: <strong>${high}</strong></span>` : ""}
    </div>`;

    main.innerHTML = summary
      + renderBucket("Overdue",       "🔴", data.overdue,   true,  null)
      + renderBucket("Due Today",     "🟡", data.due_today, false, null)
      + renderBucket("Due This Week", "🔵", data.this_week, true,  data.week_end);
  } catch(e) {
    showError(e.message);
  } finally {
    setButtons(false);
  }
}

async function rebuildCache() {
  document.getElementById("btn-rebuild").innerHTML = '<span class="spinner"></span>Rebuilding…';
  setButtons(true);
  try {
    const res  = await fetch("/api/regenerate-focus-cache", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({})
    });
    const data = await res.json();
    if (data.error) { showError(data.error); return; }
    await loadFocus();
  } catch(e) {
    showError(e.message);
  } finally {
    setButtons(false);
  }
}

function showError(msg) {
  document.getElementById("main-content").innerHTML =
    `<div class="error-box">⚠️ ${msg}<br><br>
     Make sure the sync tool has a valid Notion token configured.</div>`;
  setButtons(false);
}

loadFocus();
</script>
</body>
</html>"""


# ── Workload Dashboard ────────────────────────────────────────────────────────

@app.route("/workload")
def workload_page():
    return render_template_string(WORKLOAD_HTML_PAGE)


@app.route("/design")
def design_page():
    return render_template_string(DESIGN_HTML_PAGE)


@app.route("/api/workload", methods=["POST"])
def api_workload():
    """
    Query Work Sessions for a date range and return aggregated workload data.

    Body: {token?, mode, start_date?, end_date?}
    mode: "today" | "this_week" | "last_week" | "this_month" | "custom"
    Returns: {start_date, end_date, total_hours, session_count, project_count,
              by_project, by_work_type, sessions}
    """
    import datetime as _dt

    body  = request.json or {}
    token = body.get("token", "").strip()
    if not token:
        cfg   = load_config()
        token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token — save one in the Sync Tool first"}), 400

    mode   = body.get("mode", "this_week")
    today  = _dt.date.today()

    if mode == "today":
        start = end = today
    elif mode == "this_week":
        start = today - _dt.timedelta(days=today.weekday())   # Monday
        end   = start + _dt.timedelta(days=6)                 # Sunday
    elif mode == "last_week":
        start = today - _dt.timedelta(days=today.weekday() + 7)
        end   = start + _dt.timedelta(days=6)
    elif mode == "this_month":
        start = today.replace(day=1)
        end   = today
    elif mode == "custom":
        try:
            start = _dt.date.fromisoformat(body.get("start_date", ""))
            end   = _dt.date.fromisoformat(body.get("end_date", ""))
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid custom date range"}), 400
    else:
        start = today - _dt.timedelta(days=today.weekday())
        end   = start + _dt.timedelta(days=6)

    start_s = start.isoformat()
    end_s   = end.isoformat()

    # Query Work Sessions in the date window
    filter_body = {"and": [
        {"property": "Session Start", "date": {"on_or_after":  start_s}},
        {"property": "Session Start", "date": {"on_or_before": end_s + "T23:59:59"}},
    ]}
    try:
        raw_sessions = query_db(token, WORK_SESSIONS_DB_ID, filter_body)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Batch-fetch project names for unique project IDs in result
    project_ids = set()
    for s in raw_sessions:
        for rel in s["properties"].get("Project", {}).get("relation", []):
            project_ids.add(rel["id"])

    project_names = {}
    for pid in project_ids:
        try:
            r = requests.get(f"{NOTION_API}/pages/{pid}",
                             headers=headers(token), timeout=8)
            if r.ok:
                props = r.json().get("properties", {})
                for val in props.values():
                    if val.get("type") == "title":
                        name = extract(val)
                        if name:
                            project_names[pid] = name
                            break
        except Exception:
            pass

    # Process sessions
    result_sessions = []
    by_project   = {}
    by_work_type = {}
    total_hours  = 0.0

    for s in raw_sessions:
        props = s["properties"]

        name = extract(props.get("Session Name", {})) or "Work Session"

        start_raw = extract(props.get("Session Start", {}))
        sess_start = start_raw["start"] if isinstance(start_raw, dict) else (start_raw or "")
        end_raw = extract(props.get("Session End", {}))
        sess_end = end_raw["start"] if isinstance(end_raw, dict) else (end_raw or "")

        # Duration is a formula (number type)
        dur_formula = props.get("Duration", {}).get("formula", {})
        duration = dur_formula.get("number") if dur_formula.get("type") == "number" else None

        work_type = extract(props.get("Work Type", {})) or "Unclassified"
        status    = extract(props.get("Status", {})) or "—"

        proj_rels = props.get("Project", {}).get("relation", [])
        proj_id   = proj_rels[0]["id"] if proj_rels else ""
        proj_name = project_names.get(proj_id, "Unknown Project")

        pid_clean = s["id"].replace("-", "")
        result_sessions.append({
            "name":      name,
            "start":     sess_start,
            "end":       sess_end,
            "duration":  duration,
            "work_type": work_type,
            "status":    status,
            "project":   proj_name,
            "url":       f"https://app.notion.com/p/{pid_clean}",
        })

        if duration:
            total_hours += duration
            by_project[proj_name]   = by_project.get(proj_name, 0)   + duration
            by_work_type[work_type] = by_work_type.get(work_type, 0) + duration

    result_sessions.sort(key=lambda s: s["start"], reverse=True)
    by_project_list   = sorted(by_project.items(),   key=lambda x: x[1], reverse=True)
    by_work_type_list = sorted(by_work_type.items(), key=lambda x: x[1], reverse=True)

    return jsonify({
        "start_date":    start_s,
        "end_date":      end_s,
        "total_hours":   round(total_hours, 2),
        "session_count": len(result_sessions),
        "project_count": len(by_project),
        "by_project":    [{"name": k, "hours": round(v, 2)} for k, v in by_project_list],
        "by_work_type":  [{"name": k, "hours": round(v, 2)} for k, v in by_work_type_list],
        "sessions":      result_sessions,
    })


@app.route("/api/writeback-dates", methods=["POST"])
def api_writeback_dates():
    """
    Read Planned Start + Planned End from every Master WBS task that has a
    mapping entry, then write those dates back to the corresponding Project WBS
    row using the field names from notion_sync_config.json.

    Body: {token?}
    Returns: {ok, updated, skipped, errors}
    """
    body  = request.json or {}
    token = body.get("token", "").strip()
    if not token:
        cfg   = load_config()
        token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400

    # Invert mappings: master_task_id → {source_page_id, db_id}
    mappings = load_mappings()   # source_page_id → {master_id, db}
    inverted = {}
    for src_page, info in mappings.items():
        mid   = info.get("master_id", "")
        db_id = info.get("db", "")
        if mid:
            inverted[mid] = {"source_page_id": src_page, "db_id": db_id}

    if not inverted:
        return jsonify({"error": "No task mappings found — run a sync first"}), 400

    # Field names per source DB: db_id → {planned_start, planned_end}
    sources = load_config().get("sources", {})
    db_fields = {
        db_id: {
            "planned_start": src.get("field_map", {}).get("planned_start", ""),
            "planned_end":   src.get("field_map", {}).get("planned_end",   ""),
        }
        for db_id, src in sources.items()
    }

    # Fetch all Master WBS tasks that have at least one date set
    try:
        master_pages = query_db(token, MASTER_DB_ID, {"or": [
            {"property": "Planned End",   "date": {"is_not_empty": True}},
            {"property": "Planned Start", "date": {"is_not_empty": True}},
        ]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    updated, skipped, errors = 0, 0, []
    for page in master_pages:
        mid = page["id"]
        if mid not in inverted:
            skipped += 1
            continue

        src_page_id = inverted[mid]["source_page_id"]
        db_id       = inverted[mid]["db_id"]
        fields      = db_fields.get(db_id, {})

        props = page["properties"]
        ps_raw = extract(props.get("Planned Start", {}))
        pe_raw = extract(props.get("Planned End",   {}))
        planned_start = ps_raw["start"] if isinstance(ps_raw, dict) else ps_raw
        planned_end   = pe_raw["start"] if isinstance(pe_raw, dict) else pe_raw

        patch = {}
        if planned_start and fields.get("planned_start"):
            patch[fields["planned_start"]] = p_date({"start": planned_start})
        if planned_end and fields.get("planned_end"):
            patch[fields["planned_end"]]   = p_date({"start": planned_end})

        if not patch:
            skipped += 1
            continue

        for attempt in range(2):
            try:
                r = requests.patch(f"{NOTION_API}/pages/{src_page_id}",
                                   headers=headers(token),
                                   json={"properties": patch},
                                   timeout=20)
                r.raise_for_status()
                updated += 1
                break
            except requests.exceptions.Timeout:
                if attempt == 0:
                    import time; time.sleep(2)
                    continue
                errors.append(f"Timeout after retry: page {src_page_id}")
            except Exception as e:
                errors.append(str(e))
                break

    return jsonify({"ok": True, "updated": updated,
                    "skipped": skipped, "errors": errors[:5]})


# ── System Design Doc ─────────────────────────────────────────────────────────
DESIGN_HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Faculty PM System — Design Document</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --surface2: #21253a;
    --border: #2e3248; --accent: #6c8ef7; --accent2: #a78bfa;
    --accent3: #34d399; --accent4: #f59e0b; --accent5: #f87171;
    --text: #e2e8f0; --muted: #8892a4;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.6; min-height: 100vh; }
  .wrap { max-width: 900px; margin: 0 auto; padding: 0 20px 60px; }
  .hero { padding: 36px 0 28px; border-bottom: 1px solid var(--border); margin-bottom: 32px; }
  .hero-tag { font-size: 11px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--accent); margin-bottom: 8px; }
  .hero h1 { font-size: 26px; font-weight: 700; line-height: 1.2; }
  .hero p { color: var(--muted); font-size: 13px; margin-top: 6px; }
  .tabs { display: flex; gap: 4px; background: var(--surface); border-radius: 10px; padding: 4px; margin-bottom: 28px; position: sticky; top: 8px; z-index: 10; }
  .tab { flex: 1; text-align: center; padding: 7px 10px; font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 7px; color: var(--muted); transition: all .15s; border: none; background: none; }
  .tab.active { background: var(--surface2); color: var(--text); }
  .tab:hover:not(.active) { color: var(--text); }
  .section { display: none; animation: fadein .2s ease; }
  .section.active { display: block; }
  @keyframes fadein { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px 24px; margin-bottom: 16px; }
  .card-title { font-size: 13px; font-weight: 700; color: var(--accent); margin-bottom: 4px; text-transform: uppercase; letter-spacing: .06em; }
  .card h3 { font-size: 17px; font-weight: 700; margin-bottom: 10px; }
  .card p { font-size: 13px; color: var(--muted); line-height: 1.7; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
  .sec-label { font-size: 11px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); margin: 28px 0 12px; display: flex; align-items: center; gap: 8px; }
  .sec-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }
  .arch { display: flex; flex-direction: column; gap: 0; }
  .arch-layer { display: flex; align-items: stretch; gap: 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; }
  .arch-connector { display: flex; align-items: center; justify-content: center; padding: 6px 0; color: var(--muted); font-size: 11px; gap: 8px; letter-spacing: .05em; }
  .arch-connector::before, .arch-connector::after { content: ''; flex: 1; height: 1px; background: var(--border); }
  .layer-num { width: 28px; min-width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; margin-right: 6px; align-self: flex-start; }
  .layer-body { flex: 1; }
  .layer-name { font-size: 15px; font-weight: 700; margin-bottom: 4px; }
  .layer-desc { font-size: 12px; color: var(--muted); }
  .layer-badge { display: inline-block; font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 100px; margin-top: 8px; }
  .l1 { border-left: 3px solid var(--accent3); } .l2 { border-left: 3px solid var(--accent); } .l3 { border-left: 3px solid var(--accent2); }
  .n1 { background: rgba(52,211,153,.15); color: var(--accent3); } .n2 { background: rgba(108,142,247,.15); color: var(--accent); } .n3 { background: rgba(167,139,250,.15); color: var(--accent2); }
  .badge-n1 { background: rgba(52,211,153,.12); color: var(--accent3); } .badge-n2 { background: rgba(108,142,247,.12); color: var(--accent); } .badge-n3 { background: rgba(167,139,250,.12); color: var(--accent2); }
  .flow { display: flex; flex-direction: column; }
  .flow-step { display: flex; gap: 16px; align-items: flex-start; }
  .flow-line { display: flex; flex-direction: column; align-items: center; }
  .flow-dot { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 700; flex-shrink: 0; }
  .flow-vline { width: 2px; flex: 1; min-height: 24px; background: var(--border); }
  .flow-content { flex: 1; padding-bottom: 24px; padding-top: 4px; }
  .flow-content h4 { font-size: 14px; font-weight: 700; margin-bottom: 4px; }
  .flow-content p { font-size: 12px; color: var(--muted); }
  .flow-content code { font-family: 'SF Mono', monospace; font-size: 11px; background: var(--surface2); padding: 1px 5px; border-radius: 3px; color: var(--accent4); }
  .proj-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .proj-table th { text-align: left; padding: 8px 12px; font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 600; }
  .proj-table td { padding: 10px 12px; border-bottom: 1px solid rgba(46,50,72,.6); vertical-align: top; }
  .proj-table tr:last-child td { border-bottom: none; }
  .proj-table tr:hover td { background: rgba(108,142,247,.04); }
  .cat-pill { display: inline-block; font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 100px; }
  .cat-teach { background: rgba(52,211,153,.12); color: var(--accent3); }
  .cat-research { background: rgba(108,142,247,.12); color: var(--accent); }
  .cat-design { background: rgba(245,158,11,.12); color: var(--accent4); }
  .cat-prog { background: rgba(248,113,113,.12); color: var(--accent5); }
  .cat-svc { background: rgba(167,139,250,.12); color: var(--accent2); }
  .db-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .db-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .db-card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .db-icon { font-size: 18px; }
  .db-name { font-size: 13px; font-weight: 700; }
  .db-id { font-family: 'SF Mono', monospace; font-size: 9px; color: var(--muted); margin-top: 2px; word-break: break-all; }
  .db-field { display: flex; justify-content: space-between; font-size: 11px; padding: 4px 0; border-bottom: 1px solid rgba(46,50,72,.4); }
  .db-field:last-child { border-bottom: none; }
  .db-field-name { color: var(--muted); } .db-field-val { color: var(--text); font-weight: 500; }
  .config-block { background: var(--surface2); border-radius: 8px; padding: 14px 16px; font-family: 'SF Mono', monospace; font-size: 11px; line-height: 1.8; overflow-x: auto; }
  .cfg-key { color: #6c8ef7; } .cfg-val { color: #a78bfa; } .cfg-str { color: #34d399; } .cfg-bool { color: #f59e0b; } .cfg-comment { color: var(--muted); font-style: italic; }
  .rule-list { list-style: none; display: flex; flex-direction: column; gap: 10px; }
  .rule-list li { display: flex; gap: 12px; align-items: flex-start; font-size: 13px; }
  .rule-icon { font-size: 16px; flex-shrink: 0; margin-top: 1px; }
  .rule-text { color: var(--muted); }
  .rule-text strong { color: var(--text); }
  .status-chain { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .sc-box { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; font-size: 12px; }
  .sc-box strong { display: block; font-size: 13px; margin-bottom: 2px; }
  .sc-box span { color: var(--muted); font-size: 11px; }
  .sc-arrow { color: var(--muted); font-size: 18px; }
  .warn { background: rgba(245,158,11,.08); border: 1px solid rgba(245,158,11,.2); border-radius: 8px; padding: 12px 16px; font-size: 12px; color: var(--accent4); display: flex; gap: 10px; align-items: flex-start; }
  .footer { text-align: center; color: var(--muted); font-size: 11px; margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--border); }
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="hero-tag">Foundational Design Document &middot; v1.0 &middot; June 2026</div>
    <h1>&#x1F5C2;&#xFE0F; Faculty PM System</h1>
    <p>Automated project management for a multi-faceted faculty role &mdash; built on Notion + Python/Flask</p>
  </div>

  <div class="tabs" role="tablist">
    <button class="tab active" onclick="show('arch')">Architecture</button>
    <button class="tab" onclick="show('flow')">Data Flow</button>
    <button class="tab" onclick="show('projects')">Projects</button>
    <button class="tab" onclick="show('databases')">Databases</button>
    <button class="tab" onclick="show('config')">Config &amp; Rules</button>
  </div>

  <!-- ARCHITECTURE -->
  <div class="section active" id="arch">
    <div class="sec-label">System Overview</div>
    <div class="card" style="margin-bottom:20px">
      <p><strong style="color:var(--text)">Goal:</strong> Automatically track actual workload across a multi-faceted faculty role (Teaching, Research, Program Management, Service, Design) with zero manual data entry overhead &mdash; using Notion as the front-end and a local Python daemon as the automation layer.</p>
    </div>
    <div class="sec-label">Three-Layer Architecture</div>
    <div class="arch">
      <div class="arch-layer l1">
        <div class="layer-num n1">1</div>
        <div class="layer-body">
          <div class="layer-name">&#x1F4CB; Project WBS Databases <span style="font-weight:400;font-size:12px;color:var(--muted)">(Planning Portal)</span></div>
          <div class="layer-desc">One dedicated Notion database per project. Author and edit tasks here &mdash; add rows, set due dates, adjust priorities, track phases. Currently 8 active WBS databases covering all work areas. Lives on each project hub page.</div>
          <span class="layer-badge badge-n1">User edits here &middot; Source of truth</span>
        </div>
      </div>
      <div class="arch-connector">&darr; Python sync pushes changes</div>
      <div class="arch-layer l2">
        <div class="layer-num n2">2</div>
        <div class="layer-body">
          <div class="layer-name">&#x1F4CB; Master WBS Tasks <span style="font-weight:400;font-size:12px;color:var(--muted)">(Implementation Tracking)</span></div>
          <div class="layer-desc">Single aggregated Notion database consolidating tasks from all project WBSes into one canonical list. The Python sync tool writes here &mdash; never edit directly. Powers cross-project views, focus lists, and status rollups.</div>
          <span class="layer-badge badge-n2">Auto-written by sync tool &middot; Never edit directly</span>
        </div>
      </div>
      <div class="arch-connector">&darr; Sync creates sessions &middot; Status propagates up</div>
      <div class="arch-layer l3">
        <div class="layer-num n3">3</div>
        <div class="layer-body">
          <div class="layer-name">&#x23F1;&#xFE0F; Work Sessions <span style="font-weight:400;font-size:12px;color:var(--muted)">(Time-Logging Layer)</span></div>
          <div class="layer-desc">Time log entries linked to both a task (Master WBS) and a project (Projects DB). Created via Quick Start. The Status field on sessions drives the Auto Status formula in Master WBS, which rolls up to Project WBS via relation.</div>
          <span class="layer-badge badge-n3">Status origin &middot; Drives auto-status chain</span>
        </div>
      </div>
    </div>
    <div class="sec-label" style="margin-top:28px">Status Auto-Propagation Chain</div>
    <div class="card">
      <div class="status-chain">
        <div class="sc-box"><strong>&#x23F1;&#xFE0F; Work Session</strong><span>Status field<br>(set manually)</span></div>
        <div class="sc-arrow">&rarr;</div>
        <div class="sc-box"><strong>&#x1F4CB; Master WBS</strong><span>Auto Status<br>(formula &mdash; reads sessions)</span></div>
        <div class="sc-arrow">&rarr;</div>
        <div class="sc-box"><strong>&#x1F4CB; Project WBS</strong><span>Auto Status<br>(rollup via relation)</span></div>
      </div>
      <p style="margin-top:14px;font-size:12px;">All status propagation is automatic. The only manual input is marking a Work Session as &ldquo;Completed.&rdquo; Note: the <code>Auto Status</code> formula field is not readable via the Notion API (returns opaque URL). For programmatic checks, fetch the Work Sessions relation and inspect their <code>Status</code> fields directly.</p>
    </div>
    <div class="sec-label" style="margin-top:16px">Work Areas Covered</div>
    <div class="grid3">
      <div class="card" style="padding:14px 16px"><div style="font-size:20px;margin-bottom:6px">&#x1F393;</div><div style="font-size:13px;font-weight:700;margin-bottom:4px">Teaching &amp; Mentoring</div><div style="font-size:11px;color:var(--muted)">Course instruction, student advising, workshop facilitation</div></div>
      <div class="card" style="padding:14px 16px"><div style="font-size:20px;margin-bottom:6px">&#x1F52C;</div><div style="font-size:13px;font-weight:700;margin-bottom:4px">Research &amp; Scholarship</div><div style="font-size:11px;color:var(--muted)">Scoping reviews, collaborative papers, team projects</div></div>
      <div class="card" style="padding:14px 16px"><div style="font-size:20px;margin-bottom:6px">&#x1F3A8;</div><div style="font-size:13px;font-weight:700;margin-bottom:4px">Instructional Design</div><div style="font-size:11px;color:var(--muted)">Course design &amp; development projects</div></div>
      <div class="card" style="padding:14px 16px"><div style="font-size:20px;margin-bottom:6px">&#x1F4CA;</div><div style="font-size:13px;font-weight:700;margin-bottom:4px">Program Management</div><div style="font-size:11px;color:var(--muted)">Reactive PM: advising, inquiries, email responses</div></div>
      <div class="card" style="padding:14px 16px"><div style="font-size:20px;margin-bottom:6px">&#x1F91D;</div><div style="font-size:13px;font-weight:700;margin-bottom:4px">Professional Services</div><div style="font-size:11px;color:var(--muted)">Service work, external commitments</div></div>
      <div class="card" style="padding:14px 16px"><div style="font-size:20px;margin-bottom:6px">&#x2699;&#xFE0F;</div><div style="font-size:13px;font-weight:700;margin-bottom:4px">Admin &amp; Ops / PD</div><div style="font-size:11px;color:var(--muted)">Administrative tasks, self-learning</div></div>
    </div>
  </div>

  <!-- DATA FLOW -->
  <div class="section" id="flow">
    <div class="sec-label">Sync Tool</div>
    <div class="card" style="margin-bottom:20px">
      <p>The sync tool (<code>notion_wbs_sync.py</code>) is a local Flask web app at <strong style="color:var(--text)">localhost:8765</strong>. One-directional bridge: reads from Project WBS databases, writes to Master WBS Tasks and Work Sessions. State is persisted in JSON mapping files for idempotent upserts.</p>
    </div>
    <div class="sec-label">Process Flow</div>
    <div class="flow">
      <div class="flow-step"><div class="flow-line"><div class="flow-dot" style="background:rgba(52,211,153,.15);color:var(--accent3)">1</div><div class="flow-vline"></div></div><div class="flow-content"><h4>Edit tasks in Project WBS</h4><p>Add/update rows directly in Notion. Set Task name, Due Date, Priority, Notes, Phase/Category. This is the only manual step.</p></div></div>
      <div class="flow-step"><div class="flow-line"><div class="flow-dot" style="background:rgba(108,142,247,.15);color:var(--accent)">2</div><div class="flow-vline"></div></div><div class="flow-content"><h4>Run sync tool &rarr; Sync tab &rarr; Sync Now</h4><p>Reads all configured WBS databases via Notion API. Maps columns using <code>field_map</code> in config. Normalizes status/priority values.</p></div></div>
      <div class="flow-step"><div class="flow-line"><div class="flow-dot" style="background:rgba(108,142,247,.15);color:var(--accent)">3</div><div class="flow-vline"></div></div><div class="flow-content"><h4>Upsert into Master WBS Tasks</h4><p>Checks <code>notion_sync_mappings.json</code> for existing mapping. New &rarr; CREATE + write backlink. Existing &rarr; UPDATE changed fields only. Status set ONLY on CREATE.</p></div></div>
      <div class="flow-step"><div class="flow-line"><div class="flow-dot" style="background:rgba(167,139,250,.15);color:var(--accent2)">4</div><div class="flow-vline"></div></div><div class="flow-content"><h4>Create Work Session (Quick Start)</h4><p>Via Quick Start tab: creates session linked to Master WBS task + Projects DB entry. Mapping persisted in <code>notion_sessions_mappings.json</code>.</p></div></div>
      <div class="flow-step"><div class="flow-line"><div class="flow-dot" style="background:rgba(245,158,11,.15);color:var(--accent4)">5</div></div><div class="flow-content"><h4>Status propagates automatically</h4><p>Mark Work Session Status = &ldquo;Completed&rdquo; in Notion &rarr; Master WBS Auto Status formula updates &rarr; Project WBS Auto Status rollup reflects completion.</p></div></div>
    </div>
    <div class="sec-label" style="margin-top:4px">Key Sync Behaviors</div>
    <div class="grid2">
      <div class="card"><div class="card-title">Idempotent Upserts</div><p>Mapping files track every synced task/session. Re-running sync updates existing records &mdash; never creates duplicates.</p></div>
      <div class="card"><div class="card-title">One-Directional Only</div><p>Project WBS &rarr; Master WBS. Changes to Master WBS directly are NOT reflected back. Always edit in the source WBS.</p></div>
      <div class="card"><div class="card-title">Status Protection</div><p>Status is written ONLY on CREATE in Master WBS. Never overwritten on UPDATE &mdash; protects the Auto Status formula from being clobbered.</p></div>
      <div class="card"><div class="card-title">Value Normalization</div><p>Status and Priority strings normalized on sync: &ldquo;Todo / Not Started&rdquo; &rarr; Not Started &middot; &ldquo;Done / Finished&rdquo; &rarr; Completed &middot; &ldquo;Urgent / Critical&rdquo; &rarr; Urgent.</p></div>
    </div>
    <div class="sec-label">State Files</div>
    <div class="grid3">
      <div class="card" style="padding:14px 16px"><div style="font-size:11px;font-weight:700;color:var(--accent);margin-bottom:6px">notion_sync_config.json</div><div style="font-size:11px;color:var(--muted)">Token + all source WBS DB IDs, project mappings, field maps. Edit to add/reconfigure projects.</div></div>
      <div class="card" style="padding:14px 16px"><div style="font-size:11px;font-weight:700;color:var(--accent2);margin-bottom:6px">notion_sync_mappings.json</div><div style="font-size:11px;color:var(--muted)">Source task ID &rarr; Master WBS page ID. ~120 entries. Auto-generated.</div></div>
      <div class="card" style="padding:14px 16px"><div style="font-size:11px;font-weight:700;color:var(--accent3);margin-bottom:6px">notion_sessions_mappings.json</div><div style="font-size:11px;color:var(--muted)">Master task &rarr; Work Session page ID. ~241 entries. Auto-generated.</div></div>
    </div>
  </div>

  <!-- PROJECTS -->
  <div class="section" id="projects">
    <div class="sec-label">Active Projects (8 WBS Databases)</div>
    <div class="card" style="padding:0;overflow:hidden">
      <table class="proj-table">
        <thead><tr><th>Project Name</th><th>Category</th><th>WBS DB ID</th><th>Notes</th></tr></thead>
        <tbody>
          <tr><td><strong>EDG 6648 Summer 2026 Instruction</strong></td><td><span class="cat-pill cat-teach">Teaching</span></td><td style="font-family:monospace;font-size:10px;color:var(--muted)">001e7ca9&hellip;</td><td style="font-size:11px;color:var(--muted)">Category field</td></tr>
          <tr><td><strong>EDG 6648 Course Design</strong></td><td><span class="cat-pill cat-design">Design</span></td><td style="font-family:monospace;font-size:10px;color:var(--muted)">b4f7bdc6&hellip;</td><td style="font-size:11px;color:var(--muted)">Task Type field</td></tr>
          <tr><td><strong>Beyond the LXD Label Scoping Review</strong></td><td><span class="cat-pill cat-research">Research</span></td><td style="font-family:monospace;font-size:10px;color:var(--muted)">48cc032e&hellip;</td><td style="font-size:11px;color:var(--muted)">Hub nested under Research Projects DB row</td></tr>
          <tr><td><strong>AI &amp; PjBL Scoping Review</strong></td><td><span class="cat-pill cat-research">Research</span></td><td style="font-family:monospace;font-size:10px;color:var(--muted)">8ca11592&hellip;</td><td style="font-size:11px;color:var(--muted)">Phase field; auto_calc_planned_start</td></tr>
          <tr><td><strong>CS Ed EdD Cohort 2 Summer Workshop 2026</strong></td><td><span class="cat-pill cat-teach">Teaching</span></td><td style="font-family:monospace;font-size:10px;color:var(--muted)">79345a33&hellip;</td><td style="font-size:11px;color:var(--muted)">Student-facing; under Teaching hub</td></tr>
          <tr><td><strong>Program Management</strong></td><td><span class="cat-pill cat-prog">Program Mgmt</span></td><td style="font-family:monospace;font-size:10px;color:var(--muted)">53cdd7a1&hellip;</td><td style="font-size:11px;color:var(--muted)">Reactive tasks; flat hub structure</td></tr>
          <tr><td><strong>Professional Services</strong></td><td><span class="cat-pill cat-svc">Service</span></td><td style="font-family:monospace;font-size:10px;color:var(--muted)">8d83590f&hellip;</td><td style="font-size:11px;color:var(--muted)">Level + Org/Division fields</td></tr>
          <tr><td><strong>CS+AI Competency Job Posts Analysis</strong></td><td><span class="cat-pill cat-research">Research</span></td><td style="font-family:monospace;font-size:10px;color:var(--muted)">cd502046&hellip;</td><td style="font-size:11px;color:var(--muted)">Type field; Start Date field</td></tr>
        </tbody>
      </table>
    </div>
    <div class="sec-label" style="margin-top:28px">Hub Page Standard Structure</div>
    <div class="card">
      <p style="margin-bottom:12px">Every project hub page follows this required structure:</p>
      <div class="config-block">
<span class="cfg-comment"># Project Hub Page Template</span><br><br>
&lt;Project title heading&gt;<br>
&lt;Description / context sections&gt;<br>
---<br>
<span class="cfg-key">## &#x1F4CB; WBS</span><br>
<span class="cfg-comment">  &larr; Embedded WBS database (inline, full-page width)</span><br><br>
---<br>
<span class="cfg-key">## &#x23F1;&#xFE0F; Work Sessions</span><br>
<span class="cfg-comment">  &larr; Inline linked view of Work Sessions DB</span><br>
<span class="cfg-comment">  &larr; Filter: Project = this project (set manually once)</span>
      </div>
    </div>
    <div class="sec-label" style="margin-top:16px">Project Placement Rules</div>
    <ul class="rule-list">
      <li><span class="rule-icon">&#x1F4C1;</span><span class="rule-text"><strong>Always create a &#x1F4C1; Projects DB entry</strong> for every new project &mdash; this is what Work Sessions &ldquo;Project&rdquo; relation links to. Tagged by category.</span></li>
      <li><span class="rule-icon">&#x1F52C;</span><span class="rule-text"><strong>Research projects get two records:</strong> a &#x1F4C1; Projects DB entry (for time logging) AND a &#x1F38D; Research Projects DB entry (for research tracking). Hub page nests under the Research Projects DB row.</span></li>
      <li><span class="rule-icon">&#x1F4CD;</span><span class="rule-text"><strong>Hub page placement is separate from DB entry.</strong> Place hub pages nested under their section&rsquo;s DB row in the sidebar hierarchy.</span></li>
    </ul>
  </div>

  <!-- DATABASES -->
  <div class="section" id="databases">
    <div class="sec-label">Global Tracking Databases</div>
    <div class="db-grid">
      <div class="db-card"><div class="db-card-head"><span class="db-icon">&#x1F4C1;</span><div><div class="db-name">Projects DB</div><div class="db-id">Page: 01705bad&hellip; &middot; Col: 80ee95ce&hellip;</div></div></div><div class="db-field"><span class="db-field-name">Purpose</span><span class="db-field-val">Relation target for Work Sessions</span></div><div class="db-field"><span class="db-field-name">Key field</span><span class="db-field-val">Category (Teaching, Research&hellip;)</span></div><div class="db-field"><span class="db-field-name">Count</span><span class="db-field-val">One entry per active project</span></div></div>
      <div class="db-card"><div class="db-card-head"><span class="db-icon">&#x1F4CB;</span><div><div class="db-name">Master WBS Tasks</div><div class="db-id">DB: 2de3b2f3&hellip; &middot; Col: 94fa9ee4&hellip;</div></div></div><div class="db-field"><span class="db-field-name">Purpose</span><span class="db-field-val">Consolidated task tracking</span></div><div class="db-field"><span class="db-field-name">Key formula</span><span class="db-field-val">Auto Status (reads Work Sessions)</span></div><div class="db-field"><span class="db-field-name">Count</span><span class="db-field-val">~120 task entries</span></div></div>
      <div class="db-card"><div class="db-card-head"><span class="db-icon">&#x23F1;&#xFE0F;</span><div><div class="db-name">Work Sessions</div><div class="db-id">Page: 308c193f&hellip; &middot; Col: b3982f2e&hellip;</div></div></div><div class="db-field"><span class="db-field-name">Purpose</span><span class="db-field-val">Time-logging layer</span></div><div class="db-field"><span class="db-field-name">Relations</span><span class="db-field-val">Project &rarr; &#x1F4C1; Projects DB &middot; Task &rarr; Master WBS</span></div><div class="db-field"><span class="db-field-name">Count</span><span class="db-field-val">~241 session entries</span></div></div>
      <div class="db-card"><div class="db-card-head"><span class="db-icon">&#x1F38D;</span><div><div class="db-name">Research Projects DB</div><div class="db-id">Page: 08ca3525&hellip; &middot; Col: 8608ff79&hellip;</div></div></div><div class="db-field"><span class="db-field-name">Purpose</span><span class="db-field-val">Research-specific tracking</span></div><div class="db-field"><span class="db-field-name">Schema</span><span class="db-field-val">Stages, Status, Team Members, Lead, Due</span></div><div class="db-field"><span class="db-field-name">Location</span><span class="db-field-val">Research &amp; Scholarship hub</span></div></div>
    </div>
    <div class="sec-label" style="margin-top:28px">Known API Limitation</div>
    <div class="warn"><span style="flex-shrink:0;font-size:15px">&#x26A0;&#xFE0F;</span><div><strong>Auto Status formula not readable via API.</strong> The Notion MCP returns formula values as opaque <code style="font-family:monospace;font-size:10px;background:rgba(245,158,11,.1);padding:1px 4px;border-radius:3px;">formulaResult://&hellip;</code> URLs. To check task completion programmatically: fetch the task&rsquo;s linked Work Sessions, then check each session&rsquo;s <code style="font-family:monospace;font-size:10px;background:rgba(245,158,11,.1);padding:1px 4px;border-radius:3px;">Status</code> field. A task is done when all non-deleted sessions are &ldquo;Completed.&rdquo;</div></div>
  </div>

  <!-- CONFIG & RULES -->
  <div class="section" id="config">
    <div class="sec-label">Config Schema &mdash; notion_sync_config.json</div>
    <div class="card">
      <p style="margin-bottom:14px;font-size:12px">One entry per source WBS database. The <code>sources</code> key is a dict keyed by WBS Notion DB ID (UUID with hyphens).</p>
      <div class="config-block">
<span class="cfg-key">"sources"</span>: {<br>
&nbsp;&nbsp;<span class="cfg-str">"&lt;wbs-db-uuid&gt;"</span>: {<br>
&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"db_title"</span>: <span class="cfg-str">"WBS &mdash; Project Name"</span>, <span class="cfg-comment">// display name</span><br>
&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"project_id"</span>: <span class="cfg-str">"&lt;projects-db-entry-uuid&gt;"</span>, <span class="cfg-comment">// &#x1F4C1; Projects DB entry ID</span><br>
&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"backlink_field"</span>: <span class="cfg-str">"Master WBS"</span>, <span class="cfg-comment">// relation column in Project WBS</span><br>
&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"auto_calc_planned_start"</span>: <span class="cfg-bool">true</span>,<br>
&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"field_map"</span>: {<br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"task_name"</span>: <span class="cfg-str">"Task"</span>, <span class="cfg-comment">// REQUIRED</span><br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"priority"</span>: <span class="cfg-str">"Priority"</span>,<br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"planned_end"</span>: <span class="cfg-str">"Due Date"</span>,<br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"notes"</span>: <span class="cfg-str">"Notes"</span>,<br>
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span class="cfg-key">"category"</span>: <span class="cfg-str">"Phase"</span> <span class="cfg-comment">// "Category", "Phase", etc.</span><br>
&nbsp;&nbsp;&nbsp;&nbsp;}<br>
&nbsp;&nbsp;}<br>
}
      </div>
    </div>
    <div class="sec-label" style="margin-top:20px">Design Rules &amp; Invariants</div>
    <ul class="rule-list">
      <li><span class="rule-icon">&#x1F512;</span><span class="rule-text"><strong>Never edit Master WBS Tasks directly.</strong> All changes must originate in a Project WBS and flow through the sync tool.</span></li>
      <li><span class="rule-icon">&#x1F501;</span><span class="rule-text"><strong>Status written only on CREATE.</strong> The sync tool never overwrites Status on UPDATE &mdash; intentional to protect the Auto Status formula chain.</span></li>
      <li><span class="rule-icon">&#x1F5C2;&#xFE0F;</span><span class="rule-text"><strong>Every project needs a &#x1F4C1; Projects DB entry.</strong> This is the anchor for Work Sessions. Without it, sessions cannot be assigned to a project.</span></li>
      <li><span class="rule-icon">&#x1F517;</span><span class="rule-text"><strong>backlink_field must always be &ldquo;Master WBS&rdquo;.</strong> This relation column links Project WBS rows back to Master WBS Tasks entries. Auto-written by the tool on first sync.</span></li>
      <li><span class="rule-icon">&#x1F9E9;</span><span class="rule-text"><strong>Config keys must be UUID-with-hyphens format.</strong> e.g. <code style="font-family:monospace;font-size:10px;background:var(--surface2);padding:1px 4px;border-radius:3px;color:var(--accent4)">001e7ca9-a7b8-4180-&hellip;</code>, not the compact form.</span></li>
      <li><span class="rule-icon">&#x1F6AB;</span><span class="rule-text"><strong>Deleted source tasks are NOT removed from Master WBS.</strong> Preserves historical log integrity.</span></li>
    </ul>
    <div class="sec-label" style="margin-top:24px">Running the Tool</div>
    <div class="card">
      <div class="config-block">
<span class="cfg-comment"># From the Notion_Auto_PM folder:</span><br>
python3 notion_wbs_sync.py<br><br>
<span class="cfg-comment"># Opens browser to:</span><br>
http://localhost:8765<br><br>
<span class="cfg-comment"># One-time install:</span><br>
pip3 install flask requests
      </div>
    </div>
  </div>

  <div class="footer">Faculty PM System Design Doc &middot; v1.0 &middot; June 2026</div>
</div>
<script>
function show(id) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  event.target.classList.add('active');
}
</script>
</body>
</html>"""


WORKLOAD_HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📊 Workload Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f4; color: #1c1917; min-height: 100vh; }

  header { background: #fff; border-bottom: 1px solid #e7e5e4; padding: 14px 24px;
           display: flex; align-items: center; gap: 12px; position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 18px; font-weight: 600; flex: 1; }

  .btn { padding: 7px 14px; border-radius: 6px; border: none; cursor: pointer;
         font-size: 13px; font-weight: 500; transition: background .15s; }
  .btn-primary   { background: #2563eb; color: #fff; }
  .btn-primary:hover   { background: #1d4ed8; }
  .btn-secondary { background: #e7e5e4; color: #1c1917; }
  .btn-secondary:hover { background: #d6d3d1; }
  .btn-range { padding: 5px 12px; border-radius: 6px; border: 1px solid #e7e5e4;
               background: #fff; cursor: pointer; font-size: 13px; color: #57534e;
               transition: all .15s; }
  .btn-range.active, .btn-range:hover { background: #2563eb; color: #fff; border-color: #2563eb; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }

  main { max-width: 1000px; margin: 0 auto; padding: 24px 20px; }

  .toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .toolbar .label { font-size: 13px; color: #78716c; font-weight: 500; }
  .custom-dates { display: none; align-items: center; gap: 6px; }
  .custom-dates input { padding: 5px 8px; border: 1px solid #e7e5e4; border-radius: 6px;
                        font-size: 13px; color: #1c1917; }

  .stats-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 20px; }
  .stat-card { background: #fff; border: 1px solid #e7e5e4; border-radius: 8px;
               padding: 16px 20px; }
  .stat-card .val { font-size: 28px; font-weight: 700; color: #1c1917; line-height: 1; }
  .stat-card .lbl { font-size: 12px; color: #78716c; margin-top: 4px; }

  .charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }
  .chart-card { background: #fff; border: 1px solid #e7e5e4; border-radius: 8px; padding: 16px 20px; }
  .chart-card h3 { font-size: 13px; font-weight: 600; color: #57534e;
                   text-transform: uppercase; letter-spacing: .04em; margin-bottom: 12px; }
  .bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .bar-label { font-size: 12px; color: #1c1917; width: 140px; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; flex-shrink: 0; }
  .bar-track { flex: 1; background: #f5f5f4; border-radius: 4px; height: 10px; overflow: hidden; }
  .bar-fill  { height: 100%; border-radius: 4px; background: #2563eb; transition: width .4s; }
  .bar-fill-wt { background: #7c3aed; }
  .bar-hrs   { font-size: 12px; color: #78716c; width: 42px; text-align: right; flex-shrink: 0; }
  .empty-chart { font-size: 13px; color: #a8a29e; padding: 8px 0; }

  .section-card { background: #fff; border: 1px solid #e7e5e4; border-radius: 8px;
                  padding: 16px 20px; margin-bottom: 16px; }
  .section-card h3 { font-size: 13px; font-weight: 600; color: #57534e;
                     text-transform: uppercase; letter-spacing: .04em; margin-bottom: 14px; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 0 10px 8px 0; color: #78716c; font-weight: 500;
       font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
       border-bottom: 1px solid #e7e5e4; }
  td { padding: 9px 10px 9px 0; border-bottom: 1px solid #f5f5f4; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  .dur { font-weight: 600; color: #1c1917; }
  .wt-badge { display: inline-block; padding: 2px 6px; border-radius: 4px;
              font-size: 11px; font-weight: 500; }
  .status-badge { display: inline-block; padding: 2px 6px; border-radius: 4px;
                  font-size: 11px; font-weight: 500; }
  .status-completed { background: #dcfce7; color: #166534; }
  .status-inprog    { background: #fef9c3; color: #854d0e; }
  .status-other     { background: #f5f5f4; color: #57534e; }
  .sess-name a { color: #1c1917; text-decoration: none; font-weight: 500; }
  .sess-name a:hover { text-decoration: underline; }
  .sess-proj { font-size: 11px; color: #78716c; margin-top: 2px; }

  .p-urgent { background: #fee2e2; color: #dc2626; }
  .p-high   { background: #ffedd5; color: #c2410c; }
  .p-normal { background: #f5f5f4; color: #57534e; }
  .p-low    { background: #f5f5f4; color: #a8a29e; }

  .loading-inline { font-size: 13px; color: #78716c; padding: 12px 0; }
  .error-box { color: #dc2626; font-size: 13px; padding: 12px 0; }
  .all-clear  { text-align: center; padding: 40px 0; font-size: 24px; }
  .all-clear p { font-size: 14px; color: #78716c; margin-top: 6px; }

  .spinner { display: inline-block; width: 14px; height: 14px;
             border: 2px solid #e7e5e4; border-top-color: #2563eb;
             border-radius: 50%; animation: spin .7s linear infinite;
             vertical-align: middle; margin-right: 4px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 600px) {
    .charts-row { grid-template-columns: 1fr; }
    .stats-row  { grid-template-columns: repeat(3, 1fr); }
    .bar-label  { width: 100px; }
  }
</style>
</head>
<body>

<header>
  <h1>📊 Workload Dashboard</h1>
  <a href="/focus"    class="btn btn-secondary" style="text-decoration:none;">📌 Focus</a>
  <a href="/"         class="btn btn-secondary" style="text-decoration:none;">← Sync Tool</a>
</header>

<main>
  <!-- Toolbar -->
  <div class="toolbar">
    <span class="label">Range:</span>
    <button class="btn-range" onclick="setMode('today')">Today</button>
    <button class="btn-range active" id="btn-this_week" onclick="setMode('this_week')">This Week</button>
    <button class="btn-range" onclick="setMode('last_week')">Last Week</button>
    <button class="btn-range" onclick="setMode('this_month')">This Month</button>
    <button class="btn-range" onclick="setMode('custom')">Custom…</button>
    <div class="custom-dates" id="custom-dates">
      <input type="date" id="start-date">
      <span style="color:#78716c">to</span>
      <input type="date" id="end-date">
      <button class="btn btn-primary" onclick="load()">Go</button>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats-row" id="stats-row">
    <div class="stat-card"><div class="val" id="stat-hours">—</div><div class="lbl">Total Hours</div></div>
    <div class="stat-card"><div class="val" id="stat-sessions">—</div><div class="lbl">Sessions</div></div>
    <div class="stat-card"><div class="val" id="stat-projects">—</div><div class="lbl">Projects</div></div>
  </div>

  <!-- Charts -->
  <div class="charts-row">
    <div class="chart-card">
      <h3>By Project</h3>
      <div id="chart-project"><div class="loading-inline"><span class="spinner"></span> Loading…</div></div>
    </div>
    <div class="chart-card">
      <h3>By Work Type</h3>
      <div id="chart-worktype"><div class="loading-inline"><span class="spinner"></span> Loading…</div></div>
    </div>
  </div>

  <!-- Sessions table -->
  <div class="section-card">
    <h3>Sessions</h3>
    <div id="sessions-body"><div class="loading-inline"><span class="spinner"></span> Loading…</div></div>
  </div>

</main>

<script>
let currentMode = "this_week";

function setMode(mode) {
  currentMode = mode;
  document.querySelectorAll(".btn-range").forEach(b => b.classList.remove("active"));
  const ids = {today:"btn-today", this_week:"btn-this_week",
               last_week:"btn-last_week", this_month:"btn-this_month"};
  if (ids[mode]) document.getElementById(ids[mode])?.classList.add("active");
  document.getElementById("custom-dates").style.display = mode === "custom" ? "flex" : "none";
  if (mode !== "custom") load();
}

function buildPayload() {
  const p = {mode: currentMode};
  if (currentMode === "custom") {
    p.start_date = document.getElementById("start-date").value;
    p.end_date   = document.getElementById("end-date").value;
    if (!p.start_date || !p.end_date) return null;
  }
  return p;
}

function fmtHours(h) {
  if (h == null) return "—";
  const hrs = Math.floor(h), mins = Math.round((h - hrs) * 60);
  return hrs > 0 ? (mins > 0 ? `${hrs}h ${mins}m` : `${hrs}h`) : `${mins}m`;
}

function fmtDT(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-US", {month:"short", day:"numeric",
      hour:"numeric", minute:"2-digit", hour12:true});
  } catch { return iso; }
}

function fmtDateOnly(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
    return d.toLocaleDateString("en-US", {month:"short", day:"numeric"});
  } catch { return iso; }
}

function renderBars(items, maxHours, fillClass) {
  if (!items || items.length === 0) return '<div class="empty-chart">No data for this period.</div>';
  return items.map(item => `
    <div class="bar-row">
      <div class="bar-label" title="${item.name}">${item.name}</div>
      <div class="bar-track">
        <div class="bar-fill ${fillClass}" style="width:${maxHours ? (item.hours/maxHours*100).toFixed(1) : 0}%"></div>
      </div>
      <div class="bar-hrs">${fmtHours(item.hours)}</div>
    </div>`).join("");
}

function statusClass(s) {
  if (s === "Completed")  return "status-completed";
  if (s === "In Progress") return "status-inprog";
  return "status-other";
}

function renderSessions(sessions) {
  if (!sessions || sessions.length === 0)
    return '<div class="all-clear">📭<p>No sessions logged for this period.</p></div>';
  const rows = sessions.map(s => `
    <tr>
      <td class="sess-name">
        <a href="${s.url.replace("https://","notion://")}" >${s.name}</a>
        <div class="sess-proj">${s.project}</div>
      </td>
      <td>${fmtDT(s.start)}</td>
      <td>${s.end ? fmtDT(s.end) : '<span style="color:#a8a29e">in progress</span>'}</td>
      <td class="dur">${fmtHours(s.duration)}</td>
      <td>${s.work_type}</td>
      <td><span class="status-badge ${statusClass(s.status)}">${s.status}</span></td>
    </tr>`).join("");
  return `<table>
    <thead><tr>
      <th>Session</th><th>Start</th><th>End</th>
      <th>Duration</th><th>Work Type</th><th>Status</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function load() {
  const payload = buildPayload();
  if (!payload) return;

  document.getElementById("chart-project").innerHTML   = '<div class="loading-inline"><span class="spinner"></span> Loading…</div>';
  document.getElementById("chart-worktype").innerHTML  = '<div class="loading-inline"><span class="spinner"></span> Loading…</div>';
  document.getElementById("sessions-body").innerHTML   = '<div class="loading-inline"><span class="spinner"></span> Loading…</div>';
  document.getElementById("stat-hours").textContent    = "—";
  document.getElementById("stat-sessions").textContent = "—";
  document.getElementById("stat-projects").textContent = "—";

  try {
    const res  = await fetch("/api/workload", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.error) { showErr(data.error); return; }

    // Stats
    document.getElementById("stat-hours").textContent    = fmtHours(data.total_hours);
    document.getElementById("stat-sessions").textContent = data.session_count;
    document.getElementById("stat-projects").textContent = data.project_count;

    // Charts
    const maxP  = data.by_project[0]?.hours  || 1;
    const maxWT = data.by_work_type[0]?.hours || 1;
    document.getElementById("chart-project").innerHTML  = renderBars(data.by_project,   maxP,  "");
    document.getElementById("chart-worktype").innerHTML = renderBars(data.by_work_type, maxWT, "bar-fill-wt");

    // Sessions
    document.getElementById("sessions-body").innerHTML = renderSessions(data.sessions);
  } catch(e) {
    showErr(e.message);
  }
}

function showErr(msg) {
  const html = `<div class="error-box">⚠️ ${msg}</div>`;
  document.getElementById("chart-project").innerHTML  = html;
  document.getElementById("chart-worktype").innerHTML = html;
  document.getElementById("sessions-body").innerHTML  = html;
}

// Add missing id attrs to range buttons on load
document.addEventListener("DOMContentLoaded", () => {
  const labels = ["today","this_week","last_week","this_month"];
  document.querySelectorAll(".btn-range").forEach((b, i) => {
    if (labels[i]) b.id = "btn-" + labels[i];
  });
  load();
});
</script>
</body>
</html>"""


# ── Embedded HTML UI ──────────────────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Notion WBS Sync — Master WBS &amp; Work Sessions</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 14px;
    background: #f5f5f0;
    color: #1a1a1a;
    min-height: 100vh;
  }

  header {
    background: #fff;
    border-bottom: 1px solid #e8e8e0;
    padding: 16px 24px;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  header h1 { font-size: 18px; font-weight: 600; }
  header .sub { font-size: 13px; color: #777; margin-top: 2px; }

  .tabs {
    display: flex;
    gap: 0;
    padding: 0 24px;
    background: #fff;
    border-bottom: 1px solid #e8e8e0;
  }
  .tab {
    padding: 10px 18px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    font-weight: 500;
    color: #666;
    transition: all .15s;
  }
  .tab.active { color: #2383e2; border-bottom-color: #2383e2; }
  .tab:hover:not(.active) { color: #333; }

  .pane { display: none; padding: 24px; max-width: 860px; }
  .pane.active { display: block; }

  .card {
    background: #fff;
    border: 1px solid #e8e8e0;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 16px;
  }
  .card h3 { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
  .card .hint { font-size: 12px; color: #888; margin-bottom: 14px; line-height: 1.5; }

  label { display: block; font-weight: 500; margin-bottom: 6px; font-size: 13px; }

  input[type=text], input[type=password], select, textarea {
    width: 100%;
    padding: 8px 10px;
    border: 1px solid #ddd;
    border-radius: 6px;
    font-size: 13px;
    background: #fafafa;
    transition: border .15s;
  }
  input[type=text]:focus, input[type=password]:focus, select:focus, textarea:focus {
    outline: none;
    border-color: #2383e2;
    background: #fff;
  }
  textarea { font-family: monospace; resize: vertical; min-height: 56px; }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 16px;
    border-radius: 6px;
    border: none;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: opacity .15s, background .15s;
  }
  .btn:hover { opacity: .88; }
  .btn-primary { background: #2383e2; color: #fff; }
  .btn-ghost { background: #f0f0ec; color: #333; }
  .btn-success { background: #38a169; color: #fff; }
  .btn:disabled { opacity: .45; cursor: not-allowed; }

  .row { display: flex; gap: 10px; align-items: center; margin-top: 12px; }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 600;
  }
  .badge-blue { background: #dbeafe; color: #1d4ed8; }
  .badge-green { background: #d1fae5; color: #065f46; }
  .badge-gray { background: #f3f4f6; color: #374151; }

  .source-card {
    background: #fff;
    border: 1px solid #e8e8e0;
    border-radius: 8px;
    margin-bottom: 12px;
    overflow: hidden;
  }
  .source-card-header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 16px;
    cursor: pointer;
    user-select: none;
  }
  .source-card-header:hover { background: #fafaf8; }
  .source-card-header input[type=checkbox] { width: 16px; height: 16px; flex-shrink: 0; }
  .source-card-title { font-weight: 600; flex: 1; }
  .source-card-body {
    display: none;
    padding: 16px;
    border-top: 1px solid #f0f0ec;
    background: #fafaf8;
  }
  .source-card-body.open { display: block; }

  .mapping-grid {
    display: grid;
    grid-template-columns: 180px 1fr;
    gap: 8px 12px;
    align-items: center;
  }
  .mapping-label { font-size: 12px; font-weight: 500; color: #555; }
  .mapping-req { color: #e53e3e; }

  .divider { border: none; border-top: 1px solid #e8e8e0; margin: 14px 0; }

  .field-group { margin-bottom: 14px; }
  .field-group label { margin-bottom: 4px; }
  .field-hint { font-size: 11px; color: #999; margin-top: 4px; line-height: 1.4; }

  .proj-manual {
    margin-top: 8px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .proj-manual label {
    font-size: 11px;
    color: #888;
    white-space: nowrap;
    margin: 0;
    font-weight: 400;
  }

  #result-box {
    background: #fff;
    border: 1px solid #e8e8e0;
    border-radius: 8px;
    padding: 20px;
    display: none;
  }
  #result-box.show { display: block; }
  .result-stat { display: flex; gap: 24px; margin-bottom: 12px; }
  .result-num { font-size: 28px; font-weight: 700; }
  .result-num.green { color: #38a169; }
  .result-num.blue  { color: #2383e2; }
  .result-num.gray  { color: #999; }
  .result-num.red   { color: #e53e3e; }
  .result-label { font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: .5px; }

  .detail-list {
    margin-top: 10px;
    padding: 10px 12px;
    border-radius: 6px;
    font-size: 12px;
    max-height: 180px;
    overflow-y: auto;
  }
  .detail-list li { margin-left: 16px; margin-bottom: 5px; line-height: 1.5; }
  .detail-list strong { display: block; margin-bottom: 6px; font-size: 12px; }

  .error-list {
    background: #fff5f5;
    border: 1px solid #fed7d7;
    color: #742a2a;
  }
  .error-list a { color: #742a2a; }

  .skipped-list {
    background: #fffbeb;
    border: 1px solid #fde68a;
    color: #78350f;
  }
  .skipped-list a { color: #92400e; }

  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid rgba(255,255,255,.4);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin .6s linear infinite;
    flex-shrink: 0;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .notice {
    padding: 10px 14px;
    border-radius: 6px;
    font-size: 13px;
    margin-bottom: 14px;
    line-height: 1.5;
  }
  .notice-info  { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e3a8a; }
  .notice-warn  { background: #fffbeb; border: 1px solid #fde68a; color: #78350f; }

  a { color: #2383e2; text-decoration: none; }
  a:hover { text-decoration: underline; }

  .step {
    display: flex;
    gap: 12px;
    margin-bottom: 14px;
    align-items: flex-start;
  }
  .step-num {
    width: 24px; height: 24px;
    border-radius: 50%;
    background: #2383e2;
    color: #fff;
    font-size: 12px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    margin-top: 1px;
  }
  .step-body { line-height: 1.6; font-size: 13px; }
  .step-body code {
    background: #f0f0ec;
    padding: 1px 5px;
    border-radius: 4px;
    font-size: 12px;
    font-family: monospace;
  }
  hr { border: none; border-top: 1px solid #e8e8e0; margin: 16px 0; }
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    margin-top: 4px;
  }
  .checkbox-row input[type=checkbox] { width: 15px; height: 15px; }
</style>
</head>
<body>

<header>
  <div>
    <h1>📋 Notion WBS Sync</h1>
    <div class="sub">Sync project WBS databases → Master WBS Tasks &amp; Work Sessions</div>
  </div>
  <div style="margin-left:auto;display:flex;gap:8px;">
    <a href="/workload" style="padding:7px 14px;border-radius:6px;background:#7c3aed;color:#fff;text-decoration:none;font-size:13px;font-weight:500;">📊 Workload</a>
    <a href="/focus"    style="padding:7px 14px;border-radius:6px;background:#2563eb;color:#fff;text-decoration:none;font-size:13px;font-weight:500;">📌 Focus Tasks</a>
  </div>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('setup')">⚙️ Setup</div>
  <div class="tab" onclick="switchTab('sources')">🗂 Sources</div>
  <div class="tab" onclick="switchTab('sync')">▶️ Sync</div>
  <div class="tab" onclick="switchTab('quick')">⚡ Quick Start</div>
  <div class="tab" onclick="switchTab('check')">🔍 Check Tasks</div>
  <div class="tab" onclick="switchTab('help')">❓ Help</div>
  <div class="tab" onclick="window.open('/design','_blank')">📐 Design Doc</div>
</div>

<!-- ── SETUP TAB ─────────────────────────────────────────────────────────── -->
<div id="pane-setup" class="pane active">
  <div class="card">
    <h3>Notion Integration Token</h3>
    <div class="hint">
      Your token authenticates this tool with Notion. It stays on your computer — never sent anywhere else.<br>
      <a href="https://www.notion.so/my-integrations" target="_blank">Create an integration →</a>
      then copy the Internal Integration Secret (starts with <code>ntn_</code> or <code>secret_</code>).
    </div>
    <label>Token</label>
    <input type="password" id="token-input" placeholder="ntn_xxxxxxxxxxxxxxxxxxxx" />
    <div class="row">
      <button class="btn btn-primary" onclick="saveToken()">Save &amp; Test Connection</button>
      <span id="token-status" style="font-size:13px;color:#666;"></span>
    </div>
  </div>

  <div class="notice notice-warn">
    <strong>Important:</strong> You must share each source database <em>and</em> the master databases
    (📁 Projects, 📋 Master WBS Tasks) with your integration in Notion.
    Open each database → click <strong>···</strong> → <strong>Connections</strong> → add your integration.
  </div>
</div>

<!-- ── SOURCES TAB ───────────────────────────────────────────────────────── -->
<div id="pane-sources" class="pane">
  <div class="card">
    <h3>Discover &amp; Configure WBS Databases</h3>
    <div class="hint">
      Click Discover to find all your WBS databases. Only databases whose title starts with
      <strong>WBS</strong> are included — name your databases <em>WBS - Project Title</em>
      and they will appear here automatically.
    </div>
    <div class="row" style="margin-top:0;">
      <button class="btn btn-primary" id="discover-btn" onclick="discoverDatabases()">
        Discover WBS databases
      </button>
      <span id="discover-status" style="font-size:13px;color:#666;"></span>
    </div>
  </div>

  <div id="sources-list"></div>

  <div id="save-sources-row" style="display:none;">
    <button class="btn btn-success" onclick="saveSourceConfig()">💾 Save configuration</button>
    <span id="save-status" style="font-size:13px;color:#666;margin-left:10px;"></span>
  </div>
</div>

<!-- ── SYNC TAB ──────────────────────────────────────────────────────────── -->
<div id="pane-sync" class="pane">

  <div class="card" style="border-color:#c7d2fe;background:#eef2ff;">
    <h3 style="color:#3730a3;">↩ Write-back Dates to Project WBS</h3>
    <div class="hint" style="color:#4338ca;">
      Reads the current Planned Start and Due Date from every task in Master WBS Tasks
      and writes them back to the matching row in each Project WBS database.
      Run this after rescheduling tasks in Master WBS so both tables stay in sync.
    </div>
    <div class="row" style="margin-top:0;">
      <button class="btn" style="background:#4f46e5;color:#fff;" id="writeback-btn" onclick="runWriteback()">
        ↩ Write-back Dates
      </button>
      <span id="writeback-status" style="font-size:13px;color:#666;"></span>
    </div>
  </div>

  <div class="card" style="border-color:#fde68a;background:#fffbeb;">
    <h3 style="color:#92400e;">🧹 Deduplicate Work Sessions</h3>
    <div class="hint" style="color:#78350f;">
      Scans the entire Work Sessions database and removes duplicate rows for the same task.
      Sessions with a logged start/end time are always kept. Empty duplicates are archived.
      Run this once to clean up existing duplicates; future syncs won't create new ones.
    </div>
    <div class="row" style="margin-top:0;">
      <button class="btn" style="background:#d97706;color:#fff;" id="dedup-btn" onclick="runDedup()">
        🧹 Remove Duplicates
      </button>
      <span id="dedup-status" style="font-size:13px;color:#666;"></span>
    </div>
  </div>

  <div class="card">
    <h3>Sync to Master WBS Tasks</h3>
    <div class="hint">
      Select which WBS databases to sync, then click Sync Now.
      Tasks already synced are updated in place — no duplicates created.
    </div>
    <div id="sync-source-list" style="margin-bottom:14px;"></div>
    <button class="btn btn-primary" id="sync-btn" onclick="runSync()" style="min-width:130px;">
      ▶ Sync Now
    </button>

    <!-- Live progress panel — shown while sync is running -->
    <div id="sync-progress" style="display:none;margin-top:18px;">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px;">
        <span id="sync-phase-label" style="font-size:13px;font-weight:600;color:#4a5568;"></span>
        <span id="sync-task-counter" style="font-size:12px;color:#a0aec0;font-variant-numeric:tabular-nums;"></span>
      </div>
      <div style="background:#e2e8f0;border-radius:99px;height:7px;overflow:hidden;margin-bottom:8px;">
        <div id="sync-progress-bar" style="background:linear-gradient(90deg,#667eea,#764ba2);height:100%;width:0%;transition:width .3s ease;border-radius:99px;"></div>
      </div>
      <div id="sync-current-task" style="font-size:12px;color:#718096;margin-bottom:10px;min-height:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-style:italic;"></div>
      <div id="sync-log" style="background:#1a202c;border-radius:6px;padding:8px 10px;font-size:11px;font-family:monospace;max-height:180px;overflow-y:auto;color:#e2e8f0;line-height:1.7;"></div>
    </div>
  </div>

  <div id="result-box">
    <!-- Master WBS stats -->
    <div style="font-size:11px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px;">
      📋 Master WBS Tasks
    </div>
    <div class="result-stat">
      <div>
        <div class="result-num green" id="r-created">—</div>
        <div class="result-label">Created</div>
      </div>
      <div>
        <div class="result-num blue" id="r-updated">—</div>
        <div class="result-label">Updated</div>
      </div>
      <div>
        <div class="result-num gray" id="r-skipped">—</div>
        <div class="result-label">Skipped</div>
      </div>
      <div>
        <div class="result-num" style="color:#dd6b20;" id="r-deleted">—</div>
        <div class="result-label">Deleted</div>
      </div>
    </div>

    <!-- Work Sessions stats -->
    <div style="font-size:11px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.6px;margin:16px 0 8px;">
      ⏱️ Work Sessions
    </div>
    <div class="result-stat">
      <div>
        <div class="result-num green" id="r-ws-created">—</div>
        <div class="result-label">Created</div>
      </div>
      <div>
        <div class="result-num gray" id="r-ws-skipped">—</div>
        <div class="result-label">Already existed</div>
      </div>
    </div>

    <!-- Errors -->
    <div style="margin-top:14px;padding-top:14px;border-top:1px solid #f0f0ec;">
      <div class="result-stat" style="margin-bottom:0;">
        <div>
          <div class="result-num red" id="r-errors-count">—</div>
          <div class="result-label">Errors</div>
        </div>
      </div>
    </div>
    <div id="r-errors-list" style="display:none;" class="detail-list error-list"></div>
    <div id="r-skipped-list" style="display:none;" class="detail-list skipped-list"></div>
    <div id="r-log-notice" style="display:none;margin-top:10px;font-size:12px;color:#666;">
      📄 Log saved: <span id="r-log-file" style="font-family:monospace;"></span>
    </div>
  </div>
</div>

<!-- ── QUICK START TAB ──────────────────────────────────────────────────── -->
<div id="pane-quick" class="pane">
  <div class="card">
    <h3>⚡ Quick Start a Task</h3>
    <div class="hint">
      Add a new task to a project and begin a Work Session in one click.
      The task is created in the Project WBS, Master WBS Tasks, and Work Sessions simultaneously.
      If you're already mid-task and just want to log time, open Work Sessions in Notion and add a row manually instead.
    </div>

    <div class="field-group">
      <label>Task Name <span class="mapping-req">*</span></label>
      <input type="text" id="qs-task-name" placeholder="e.g. Review participant data for RQ2">
    </div>

    <div class="field-group">
      <label>Project / WBS Database <span class="mapping-req">*</span></label>
      <select id="qs-source" onchange="onQsSourceChange()">
        <option value="">— select a project —</option>
      </select>
      <div class="field-hint">Only projects configured in the Sources tab appear here.</div>
    </div>

    <!-- Category field — shown only when the selected WBS has a category mapping -->
    <div class="field-group" id="qs-category-group" style="display:none;">
      <label>Category <span style="font-weight:400;color:#888;">(optional)</span></label>
      <select id="qs-category" onchange="onQsCategoryChange()">
        <option value="">— loading… —</option>
      </select>
      <input type="text" id="qs-category-new"
             placeholder="Type new category name…"
             style="display:none;margin-top:6px;font-size:13px;"
             oninput="this.value=this.value">
      <div class="field-hint">Categorise this task for time-tracking analysis. Type a new name to create a category on the fly.</div>
    </div>

    <!-- Service Level — shown when the WBS has a level field mapped (e.g. Professional Services) -->
    <div class="field-group" id="qs-level-group" style="display:none;">
      <label>Service Level <span style="font-weight:400;color:#888;">(optional)</span></label>
      <select id="qs-level" onchange="onQsLevelChange()">
        <option value="">— loading… —</option>
      </select>
      <input type="text" id="qs-level-new"
             placeholder="Type new level name…"
             style="display:none;margin-top:6px;font-size:13px;">
      <div class="field-hint">Select the service tier for this task.</div>
    </div>

    <!-- Organization / Division — shown when the WBS has an org_division field mapped -->
    <div class="field-group" id="qs-orgdiv-group" style="display:none;">
      <label>Organization / Division <span style="font-weight:400;color:#888;">(optional)</span></label>
      <select id="qs-orgdiv" onchange="onQsOrgDivChange()">
        <option value="">— loading… —</option>
      </select>
      <input type="text" id="qs-orgdiv-new"
             placeholder="Type organization or division name…"
             style="display:none;margin-top:6px;font-size:13px;">
      <div class="field-hint">Select an existing org/division or type a new one.</div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
      <div class="field-group" style="margin-bottom:0;">
        <label>Due Date <span style="font-weight:400;color:#888;">(optional)</span></label>
        <input type="date" id="qs-due-date" style="font-size:13px;">
        <div class="field-hint">Synced to Due Date in the Project WBS and Master WBS.</div>
      </div>
      <div class="field-group" style="margin-bottom:0;">
        <label>Priority <span style="font-weight:400;color:#888;">(optional)</span></label>
        <select id="qs-priority">
          <option value="Normal" selected>Normal (default)</option>
          <option value="Urgent">🔴 Urgent</option>
          <option value="High">🟠 High</option>
          <option value="Low">🔵 Low</option>
        </select>
      </div>
    </div>

    <div class="field-group" style="margin-top:14px;">
      <label>Work Type <span style="font-weight:400;color:#888;">(optional)</span></label>
      <select id="qs-work-type">
        <option value="">— leave blank —</option>
        <option value="🔵 Deep Work">🔵 Deep Work</option>
        <option value="🟡 Meeting &amp; Call">🟡 Meeting &amp; Call</option>
        <option value="🟠 Admin &amp; Ops">🟠 Admin &amp; Ops</option>
        <option value="🟢 Communication">🟢 Communication</option>
      </select>
      <div class="field-hint">Sets Work Type on the Master WBS task so time-tracking by type is pre-filled.</div>
    </div>

    <div class="field-group">
      <label>Due Date <span style="font-weight:400;color:#888;">(optional)</span></label>
      <input type="date" id="qs-due-date" style="font-size:13px;">
      <div class="field-hint">Written to both the Project WBS and Master WBS Tasks.</div>
    </div>

    <div class="field-group">
      <label>Session Start <span style="font-weight:400;color:#888;">(leave blank to fill in Notion later)</span></label>
      <input type="datetime-local" id="qs-start" style="font-size:13px;">
      <div class="row" style="margin-top:6px;">
        <button class="btn btn-ghost" onclick="setQsNow()" style="font-size:12px;padding:5px 10px;">Use Now</button>
      </div>
    </div>

    <div class="row" style="margin-top:4px;">
      <button class="btn btn-primary" id="qs-btn" onclick="runQuickAdd()">⚡ Add Task &amp; Start Session</button>
    </div>
  </div>

  <div id="qs-result" style="display:none;" class="card">
    <div id="qs-result-ok" style="display:none;">
      <p style="font-size:14px;font-weight:600;color:#38a169;margin-bottom:12px;">✓ Task created in all three places</p>
      <div style="display:flex;flex-direction:column;gap:8px;font-size:13px;">
        <a id="qs-link-wbs"    href="#" target="_blank">📋 Open in Project WBS →</a>
        <a id="qs-link-master" href="#" target="_blank">📋 Open in Master WBS Tasks →</a>
        <a id="qs-link-ws"     href="#" target="_blank">⏱️ Open Work Session →</a>
      </div>
      <p style="font-size:12px;color:#888;margin-top:10px;">
        Remember to set the Session End time in Notion when you finish.
      </p>
    </div>
    <div id="qs-result-err" style="display:none;font-size:13px;color:#e53e3e;"></div>
  </div>
</div>

<!-- ── CHECK TASKS TAB ───────────────────────────────────────────────────── -->
<div id="pane-check" class="pane">
  <div class="card">
    <h3>🔍 Check Existing Tasks</h3>
    <div class="hint">
      Query all active (non-completed) tasks for a project before creating a new one.
      If you find a matching task, log a work session directly from here.
    </div>

    <div class="field-group">
      <label>Project / WBS Database <span class="mapping-req">*</span></label>
      <select id="chk-source" onchange="onChkSourceChange()">
        <option value="">— select a project —</option>
      </select>
    </div>

    <div class="row" style="margin-top:4px;">
      <button class="btn btn-primary" id="chk-btn" onclick="runCheckTasks()">🔍 Query Active Tasks</button>
    </div>
  </div>

  <!-- Task results list -->
  <div id="chk-results" style="display:none;">
    <div id="chk-results-err" style="display:none;" class="notice notice-err" style="margin-bottom:12px;"></div>
    <div id="chk-task-list"></div>
  </div>

  <!-- Inline log-session form (shown when user clicks "Log Session" on a task) -->
  <div id="chk-log-form" style="display:none;" class="card">
    <h3 style="margin-bottom:4px;">⏱️ Log Session for: <span id="chk-log-task-name" style="color:var(--accent);font-weight:600;"></span></h3>
    <p style="font-size:12px;color:#888;margin-bottom:14px;">This creates a new Work Session linked to the existing task — no duplicate task is created.</p>

    <div class="field-group">
      <label>Session Start <span class="mapping-req">*</span></label>
      <input type="datetime-local" id="chk-start" style="font-size:13px;">
      <div class="row" style="margin-top:6px;">
        <button class="btn btn-ghost" onclick="setChkNow('chk-start')" style="font-size:12px;padding:5px 10px;">Use Now</button>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
      <div class="field-group" style="margin-bottom:0;">
        <label>Session End <span style="font-weight:400;color:#888;">(optional)</span></label>
        <input type="datetime-local" id="chk-end" style="font-size:13px;">
        <div class="row" style="margin-top:6px;">
          <button class="btn btn-ghost" onclick="setChkNow('chk-end')" style="font-size:12px;padding:5px 10px;">Use Now</button>
        </div>
      </div>
      <div class="field-group" style="margin-bottom:0;">
        <label>Status <span style="font-weight:400;color:#888;">(optional)</span></label>
        <select id="chk-status">
          <option value="">— leave blank —</option>
          <option value="In Progress">In Progress</option>
          <option value="Completed">Completed</option>
          <option value="On Hold">On Hold</option>
        </select>
      </div>
    </div>

    <div class="field-group" style="margin-top:14px;">
      <label>Work Type <span style="font-weight:400;color:#888;">(optional)</span></label>
      <select id="chk-work-type">
        <option value="">— leave blank —</option>
        <option value="🔵 Deep Work">🔵 Deep Work</option>
        <option value="🟡 Meeting &amp; Call">🟡 Meeting &amp; Call</option>
        <option value="🟠 Admin &amp; Ops">🟠 Admin &amp; Ops</option>
        <option value="🟢 Communication">🟢 Communication</option>
      </select>
    </div>

    <div class="row" style="margin-top:4px;gap:8px;">
      <button class="btn btn-primary" id="chk-log-btn" onclick="runLogSession()">⏱️ Log Session</button>
      <button class="btn btn-ghost" onclick="cancelLogSession()">Cancel</button>
    </div>

    <div id="chk-log-result" style="display:none;margin-top:12px;font-size:13px;"></div>
  </div>
</div>

<!-- ── HELP TAB ──────────────────────────────────────────────────────────── -->
<div id="pane-help" class="pane">
  <div class="card">
    <h3>How to set this up (first time)</h3>
    <br>
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-body">
        Go to <a href="https://www.notion.so/my-integrations" target="_blank">notion.so/my-integrations</a>
        and click <strong>+ New integration</strong>. Give it a name (e.g. "WBS Sync"),
        set the workspace, and click Submit. Copy the <strong>Internal Integration Secret</strong>.
      </div>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-body">
        Paste the token into the <strong>Setup</strong> tab and click Save &amp; Test.
      </div>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-body">
        Name every project WBS database starting with <strong>WBS</strong> —
        e.g. <code>WBS - EDG 6648 Instruction</code>.
        In Notion, open each WBS database → click <strong>···</strong> → <strong>Connections</strong>
        → add your integration. Do the same for the <strong>📁 Projects</strong> and
        <strong>📋 Master WBS Tasks</strong> databases.
      </div>
    </div>
    <div class="step">
      <div class="step-num">4</div>
      <div class="step-body">
        Go to the <strong>Sources</strong> tab → click <strong>Discover WBS databases</strong>.
        Only databases starting with "WBS" appear — no filtering needed.
        Map each database's columns, choose its project (dropdown or paste page ID), and click
        <strong>Save configuration</strong>.
      </div>
    </div>
    <div class="step">
      <div class="step-num">5</div>
      <div class="step-body">
        Go to the <strong>Sync</strong> tab → click <strong>Sync Now</strong>.
        Run again anytime you update your project WBS databases.
      </div>
    </div>

    <hr>
    <h3>Why does my project not appear in the dropdown?</h3>
    <br>
    <p style="font-size:13px;line-height:1.7;color:#444;">
      The dropdown only shows pages that are rows in the <strong>📁 Projects</strong> database.
      If your project lives as a standalone page (e.g. inside a Teaching &amp; Mentoring hub),
      it won't appear there.<br><br>
      <strong>Fix:</strong> use the <em>"Or paste page ID"</em> field below the dropdown.
      Open the project page in Notion, click <strong>···</strong> → <strong>Copy link</strong>,
      then extract the 32-character ID from the URL (the last segment before any <code>?</code>).
      Paste it into the manual ID field — the dropdown selection is ignored when a manual ID is present.
    </p>

    <hr>
    <h3>What gets synced</h3>
    <br>
    <p style="font-size:13px;line-height:1.7;color:#444;">
      A single <strong>Sync Now</strong> run does the full three-layer sync automatically:<br><br>
      <strong>Phase 1 — Project WBS → Master WBS Tasks.</strong>
      Syncs Task Name, Priority, Planned Start, Planned End, Notes, and Work Type.
      Status is set once on first create and never overwritten afterward, preserving
      Auto Status and any manual edits.<br><br>
      <strong>Phase 2 — Master WBS Tasks → Work Sessions.</strong>
      Creates one Work Session row per Master WBS task (Project + Task relation only).
      Tasks that already have a session are skipped — re-running is always safe.<br><br>
      Each task is tracked by its Notion page ID, so running sync again will <em>update</em>
      existing rows rather than create duplicates.
    </p>

    <hr>
    <h3>Back-link / Master WBS relation</h3>
    <br>
    <p style="font-size:13px;line-height:1.7;color:#444;">
      After creating a new Master WBS entry, the tool automatically writes the page ID
      back into the source WBS row's <strong>Master WBS</strong> relation field (configurable
      per source). This keeps the two databases bi-directionally linked and makes the
      <em>Auto Status</em> rollup populate immediately without any manual linking.
    </p>

    <hr>
    <h3>Work Type override map</h3>
    <br>
    <p style="font-size:13px;line-height:1.7;color:#444;">
      If your source WBS has a Category column instead of a Work Type column, paste a
      JSON map in the <em>Work Type override</em> field to translate automatically:<br>
      <code>{"Weekly Announcement": "🟢 Communication", "Grading": "🔵 Deep Work"}</code><br><br>
      Valid master Work Type values: <strong>🔵 Deep Work</strong>, <strong>🟡 Meeting &amp; Call</strong>,
      <strong>🟠 Admin &amp; Ops</strong>, <strong>🟢 Communication</strong>.
    </p>

    <hr>
    <h3>Auto Planned Start</h3>
    <br>
    <p style="font-size:13px;line-height:1.7;color:#444;">
      When enabled (default), if a task has no Planned Start but has a Due Date (Planned End),
      Planned Start is automatically set to <strong>Due Date − 7 calendar days</strong>.
      Uncheck per source if your WBS already has explicit start dates for every task.
    </p>

    <hr>
    <h3>Dynamic task deletion &amp; renaming</h3>
    <br>
    <p style="font-size:13px;line-height:1.7;color:#444;">
      When you delete or rename tasks in a project WBS, those changes flow through automatically on the next sync run:<br><br>
      <strong>Deleted task:</strong> The corresponding Master WBS entry is archived (moved to Notion Trash),
      and its linked Work Session is archived as well. Both are removed from the mapping files so they won't
      reappear on future syncs.<br><br>
      <strong>Renamed task:</strong> The Master WBS entry's Task Name is updated to match, and the linked
      Work Session's Session Name is updated too — keeping all three layers consistent with no manual work.<br><br>
      <em>Note:</em> Only tasks that were synced with the current version of the tool (v2 mappings format)
      participate in deletion detection. Legacy entries synced before this feature was added are left untouched
      until their next sync run, which will tag them automatically.
    </p>

    <hr>
    <h3>How status flows</h3>
    <br>
    <p style="font-size:13px;line-height:1.7;color:#444;">
      There is no manual Status column anywhere. Status is entirely automatic:<br><br>
      <strong>Work Session</strong> (In Progress / Completed) →
      <strong>Master WBS Auto Status</strong> (formula, updates instantly) →
      <strong>Project WBS Auto Status</strong> (rollup via Master WBS relation, updates instantly).<br><br>
      To start a task: create a Work Session linked to the Master WBS entry.
      To complete it: mark the Work Session Completed. Both the master list and project WBS reflect the change immediately.
    </p>
  </div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let state = {
  token: "",
  discoveredDbs: [],
  projects: [],            // [{id, name}] — from 📁 Projects database
  savedSources: {},        // db_id → {project_id, field_map, db_title,
                           //          work_type_map, backlink_field, auto_calc_planned_start}
};

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  const cfg = await api("GET", "/api/config");
  if (cfg.token) {
    state.token = cfg.token;
    document.getElementById("token-input").value = cfg.token;
    setStatus("token-status", "✓ Token loaded", "green");
  }
  if (cfg.sources) {
    state.savedSources = cfg.sources;
  }
  refreshSyncTab();
}

// ── API helper ────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {"Content-Type":"application/json"} };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

function setStatus(id, msg, color="gray") {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  el.style.color = color === "green" ? "#38a169"
                 : color === "red"   ? "#e53e3e"
                 : "#666";
}

// ── Setup tab ─────────────────────────────────────────────────────────────────
async function saveToken() {
  const token = document.getElementById("token-input").value.trim();
  if (!token) { setStatus("token-status","Enter a token first","red"); return; }
  setStatus("token-status", "Testing…");

  const test = await api("POST", "/api/test-token", {token});
  if (test.error) {
    setStatus("token-status", "✗ " + test.error, "red");
    return;
  }
  state.token = token;

  const cfg = await api("GET", "/api/config");
  cfg.token = token;
  await api("POST", "/api/config", cfg);

  const pr = await api("POST", "/api/projects", {token});
  if (pr.error) {
    setStatus("token-status",
      `✓ Token valid${test.name ? " (" + test.name + ")" : ""} — ` +
      `Projects database not accessible yet (share it with the integration in Notion)`, "green");
  } else {
    state.projects = pr.projects;
    setStatus("token-status",
      `✓ Connected${test.name ? " (" + test.name + ")" : ""} — ${pr.projects.length} project(s) in dropdown`, "green");
  }
}

// ── Sources tab ───────────────────────────────────────────────────────────────
async function discoverDatabases() {
  if (!state.token) { alert("Save your token first in the Setup tab."); return; }
  const btn = document.getElementById("discover-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="border-top-color:#fff;border-color:rgba(0,0,0,.2)"></span> Discovering…';
  setStatus("discover-status","");

  // Try to load projects — non-fatal if unavailable
  if (!state.projects.length) {
    const pr = await api("POST","/api/projects",{token:state.token});
    if (!pr.error) state.projects = pr.projects;
  }

  const res = await api("POST","/api/discover",{token:state.token});
  btn.disabled = false;
  btn.textContent = "Discover databases";

  if (res.error) { setStatus("discover-status","Error: "+res.error,"red"); return; }
  state.discoveredDbs = res.databases;
  setStatus("discover-status", `Found ${res.databases.length} WBS database(s)`);
  renderSourceList();
  document.getElementById("save-sources-row").style.display = "block";
}

function renderSourceList() {
  const container = document.getElementById("sources-list");
  container.innerHTML = "";

  if (!state.discoveredDbs.length) {
    container.innerHTML = '<p style="color:#888;font-size:13px;">No WBS databases found. Make sure your databases are named starting with "WBS" and are shared with the integration in Notion.</p>';
    return;
  }

  for (const db of state.discoveredDbs) {
    const saved = state.savedSources[db.id] || {};
    const isChecked = !!(saved.project_id && !saved.disabled);
    const card = document.createElement("div");
    card.className = "source-card";
    card.id = "sc-" + db.id;

    // Determine whether saved project_id came from the dropdown or was manual
    const inDropdown = state.projects.some(p => p.id === saved.project_id);
    const manualId   = (!inDropdown && saved.project_id) ? saved.project_id : "";
    const dropdownId = inDropdown ? saved.project_id : "";

    card.innerHTML = `
      <div class="source-card-header" onclick="toggleCard('${db.id}')">
        <input type="checkbox" id="chk-${db.id}" ${isChecked?"checked":""} onclick="event.stopPropagation();toggleCheck('${db.id}')">
        <div class="source-card-title">${escHtml(db.title)}</div>
        <span class="badge ${isChecked?"badge-green":"badge-gray"}" id="badge-${db.id}">
          ${isChecked?"configured":"not configured"}
        </span>
      </div>
      <div class="source-card-body ${isChecked?"open":""}" id="body-${db.id}">

        <!-- Project selection -->
        <div class="field-group">
          <label>Maps to Project <span class="mapping-req">*</span></label>
          <select id="proj-${db.id}" onchange="updateBadge('${db.id}')">
            <option value="">— choose from Projects database —</option>
            ${state.projects.map(p=>`<option value="${p.id}" ${dropdownId===p.id?"selected":""}>${escHtml(p.name)}</option>`).join("")}
          </select>
          <div class="proj-manual">
            <label for="proj-manual-${db.id}">Or paste page ID:</label>
            <input type="text" id="proj-manual-${db.id}"
              placeholder="36b54686ae1581a2953cffc2c88f8bd2"
              value="${escHtml(manualId)}"
              oninput="updateBadge('${db.id}')"
              style="font-family:monospace;font-size:12px;">
          </div>
          <div class="field-hint">
            Manual ID takes priority over the dropdown. Copy from the Notion page URL
            (last segment, remove hyphens if needed — both formats accepted).
          </div>
        </div>

        <hr class="divider">

        <!-- Column mapping -->
        <div class="mapping-grid" id="mapping-${db.id}">
          <div style="grid-column:1/-1;font-size:12px;font-weight:600;color:#888;margin-bottom:4px;">
            Column mapping — which column in <em>${escHtml(db.title)}</em> contains each field?
          </div>
        </div>
        <div class="row" style="margin-top:12px;">
          <button class="btn btn-ghost" onclick="loadSchema('${db.id}')">Load columns</button>
          <span id="schema-status-${db.id}" style="font-size:12px;color:#888;"></span>
        </div>

        <hr class="divider">

        <!-- Advanced options -->
        <div style="font-size:12px;font-weight:600;color:#888;margin-bottom:10px;">Advanced options</div>

        <div class="field-group">
          <label>Work Type override map (JSON)</label>
          <textarea id="wtmap-${db.id}" placeholder='{"Weekly Announcement": "🟢 Communication", "Grading": "🔵 Deep Work"}'>${escHtml(saved.work_type_map||"")}</textarea>
          <div class="field-hint">Maps Category column values → Master Work Type. Leave blank if you mapped Work Type directly above.</div>
        </div>

        <div class="field-group">
          <label>Back-link field name</label>
          <input type="text" id="backlink-${db.id}"
            value="${escHtml(saved.backlink_field !== undefined ? saved.backlink_field : "Master WBS")}"
            placeholder="Master WBS">
          <div class="field-hint">Name of the Relation column in this WBS that points back to Master WBS Tasks. Leave blank to skip.</div>
        </div>

        <div class="checkbox-row">
          <input type="checkbox" id="autops-${db.id}" ${saved.auto_calc_planned_start===false?"":"checked"}>
          <label for="autops-${db.id}" style="font-weight:400;margin:0;">
            Auto Planned Start (Due Date − 7 days when no Planned Start mapped)
          </label>
        </div>

        <div class="field-group" style="margin-top:12px;">
          <label>Hub Page URL or ID</label>
          <input type="text" id="hubpage-${db.id}"
            value="${escHtml(saved.hub_page_url||"")}"
            placeholder="https://www.notion.so/... or page ID"
            style="font-family:monospace;font-size:12px;">
          <div class="field-hint">
            Optional. When set, a "Project Page" link to this hub page is added to the
            📁 Projects entry automatically when you save. Leave blank to skip.
          </div>
        </div>

      </div>
    `;
    container.appendChild(card);

    if (saved.field_map) {
      renderMapping(db.id, null, saved.field_map);
    }
  }
}

function resolveProjectId(dbId) {
  const manual = (document.getElementById("proj-manual-"+dbId)||{}).value || "";
  if (manual.trim()) {
    // Accept both hyphenated and raw IDs
    return manual.trim().replace(/-/g,"")
      .replace(/^(.{8})(.{4})(.{4})(.{4})(.{12})$/,"$1-$2-$3-$4-$5");
  }
  return (document.getElementById("proj-"+dbId)||{}).value || "";
}

function toggleCard(dbId) {
  const body = document.getElementById("body-"+dbId);
  body.classList.toggle("open");
}

function toggleCheck(dbId) {
  const chk = document.getElementById("chk-"+dbId);
  const body = document.getElementById("body-"+dbId);
  if (chk.checked) body.classList.add("open");
  updateBadge(dbId);
}

function updateBadge(dbId) {
  const chk   = document.getElementById("chk-"+dbId);
  const badge  = document.getElementById("badge-"+dbId);
  const projId = resolveProjectId(dbId);
  const ok = chk && chk.checked && !!projId;
  badge.className = "badge " + (ok ? "badge-green" : "badge-gray");
  badge.textContent = ok ? "configured" : "not configured";
}

async function loadSchema(dbId) {
  const statusEl = document.getElementById("schema-status-"+dbId);
  statusEl.textContent = "Loading…";
  const res = await api("POST","/api/schema",{token:state.token, db_id:dbId});
  if (res.error) { statusEl.textContent = "Error: "+res.error; return; }
  statusEl.textContent = `${res.columns.length} columns found`;

  const saved = state.savedSources[dbId] || {};
  renderMapping(dbId, res.columns, saved.field_map || {});
}

const MASTER_FIELDS = [
  {key:"task_name",     label:"Task Name",                req:true},
  {key:"priority",      label:"Priority",                 req:false},
  {key:"planned_start", label:"Planned Start",            req:false},
  {key:"planned_end",   label:"Planned End / Due Date",   req:false},
  {key:"notes",         label:"Notes",                    req:false},
  {key:"work_type",     label:"Work Type",                req:false},
  {key:"category",      label:"Category / Phase / Group", req:false},
  {key:"level",         label:"Service Level",            req:false},
  {key:"org_division",  label:"Organization / Division",  req:false},
];

function renderMapping(dbId, columns, savedMap) {
  const container = document.getElementById("mapping-"+dbId);
  const header = container.querySelector("div");
  container.innerHTML = "";
  container.appendChild(header);

  for (const field of MASTER_FIELDS) {
    const lbl = document.createElement("div");
    lbl.className = "mapping-label";
    lbl.innerHTML = field.label + (field.req ? ' <span class="mapping-req">*</span>' : '');

    const sel = document.createElement("select");
    sel.id = `map-${dbId}-${field.key}`;
    sel.innerHTML = `<option value="">— skip —</option>`;

    if (columns) {
      for (const col of columns) {
        const selected = savedMap[field.key] === col.name;
        sel.innerHTML += `<option value="${escHtml(col.name)}" ${selected?"selected":""}>${escHtml(col.name)} <span style="color:#aaa">(${col.type})</span></option>`;
      }
      if (!savedMap[field.key]) {
        const keywords = {
          task_name:     ["task","name","title","item"],
          status:        ["status","state"],
          priority:      ["priority","prio"],
          planned_start: ["planned start","start date","start"],
          planned_end:   ["planned end","due date","due","end date","end","deadline"],
          notes:         ["notes","note","description","desc","comment"],
          work_type:     ["work type","type","kind"],
          category:      ["category","cat"],
        };
        const kws = keywords[field.key] || [];
        for (const col of columns) {
          const name = col.name.toLowerCase();
          if (kws.some(k => name.includes(k))) {
            sel.value = col.name;
            break;
          }
        }
      }
    } else if (savedMap[field.key]) {
      sel.innerHTML += `<option value="${escHtml(savedMap[field.key])}" selected>${escHtml(savedMap[field.key])}</option>`;
    }

    container.appendChild(lbl);
    container.appendChild(sel);
  }
}

async function saveSourceConfig() {
  // Start with all existing configs so unchecked sources are never lost
  const sources = Object.assign({}, state.savedSources || {});

  for (const db of state.discoveredDbs) {
    const chk = document.getElementById("chk-"+db.id);
    const checked = chk && chk.checked;

    if (!checked) {
      // Preserve any existing config but mark as disabled so it won't sync
      if (sources[db.id]) {
        sources[db.id] = Object.assign({}, sources[db.id], {disabled: true});
      }
      continue;
    }

    const projId = resolveProjectId(db.id);
    if (!projId) {
      alert(`Please select or paste a project ID for "${db.title}".`);
      return;
    }

    const field_map = {};
    for (const field of MASTER_FIELDS) {
      const sel = document.getElementById(`map-${db.id}-${field.key}`);
      if (sel && sel.value) field_map[field.key] = sel.value;
    }
    if (!field_map.task_name) {
      alert(`Please map the Task Name column for "${db.title}".`);
      return;
    }

    const wtRaw   = (document.getElementById("wtmap-"+db.id)||{}).value || "";
    const blField = (document.getElementById("backlink-"+db.id)||{}).value || "Master WBS";
    const autops  = (document.getElementById("autops-"+db.id)||{}).checked !== false;
    const hubUrl  = ((document.getElementById("hubpage-"+db.id)||{}).value || "").trim();

    sources[db.id] = {
      project_id: projId,
      field_map,
      db_title: db.title,
      work_type_map: wtRaw,
      backlink_field: blField,
      auto_calc_planned_start: autops,
      hub_page_url: hubUrl,
      disabled: false,
    };
  }

  // For any source that now has a hub_page_url that wasn't set before, wire up the link
  const prevSources = state.savedSources || {};
  for (const [dbId, src] of Object.entries(sources)) {
    const prev = prevSources[dbId] || {};
    if (src.hub_page_url && src.hub_page_url !== prev.hub_page_url && src.project_id) {
      const linkRes = await api("POST", "/api/add-project-page-link", {
        token: state.token,
        project_entry_id: src.project_id,
        hub_page_url: src.hub_page_url,
      });
      if (linkRes.error) {
        setStatus("save-status", `⚠️ Saved, but project link failed: ${linkRes.error}`, "orange");
      }
    }
  }

  state.savedSources = sources;
  const cfg = await api("GET","/api/config");
  cfg.sources = sources;
  await api("POST","/api/config",cfg);
  setStatus("save-status","✓ Saved","green");
  setTimeout(()=>setStatus("save-status",""),2500);
  refreshSyncTab();
  refreshQuickTab();
}

// ── Sync tab ──────────────────────────────────────────────────────────────────
function refreshSyncTab() {
  const container = document.getElementById("sync-source-list");
  const entries = Object.entries(state.savedSources).filter(([,src]) => !src.disabled);
  if (!entries.length) {
    container.innerHTML = '<p style="color:#888;font-size:13px;">No sources configured yet. Go to the Sources tab to set them up.</p>';
    return;
  }
  container.innerHTML = entries.map(([dbId, src]) => `
    <label style="display:flex;align-items:center;gap:10px;padding:8px 0;cursor:pointer;font-weight:400;">
      <input type="checkbox" id="sync-chk-${dbId}" checked style="width:16px;height:16px;">
      <span>${escHtml(src.db_title || dbId)}</span>
      <span class="badge badge-blue" style="margin-left:auto;">${escHtml(getProjectLabel(src.project_id))}</span>
    </label>
  `).join("");
}

function getProjectLabel(projectId) {
  if (!projectId) return "—";
  const p = state.projects.find(p => p.id === projectId);
  return p ? p.name : projectId.replace(/-/g,"").slice(0,8) + "…";
}

function buildSourcesList() {
  const sources = [];
  for (const [dbId, src] of Object.entries(state.savedSources)) {
    if (src.disabled) continue;
    const chk = document.getElementById("sync-chk-"+dbId);
    if (chk && chk.checked) {
      sources.push({
        db_id: dbId,
        project_id: src.project_id,
        field_map: src.field_map,
        db_title: src.db_title || dbId,
        work_type_map: src.work_type_map || "",
        backlink_field: src.backlink_field || "Master WBS",
        auto_calc_planned_start: src.auto_calc_planned_start !== false,
      });
    }
  }
  return sources;
}

// ── Sync progress helpers ─────────────────────────────────────────────────────
let _syncState = {};

function _syncLog(text, color) {
  const log = document.getElementById("sync-log");
  const line = document.createElement("div");
  line.style.cssText = `padding:1px 0;border-bottom:1px solid #2d3748;color:${color||"#e2e8f0"};`;
  line.textContent   = text;
  log.appendChild(line);
  while (log.children.length > 300) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

function _syncHandleEvent(evt) {
  const phaseEl   = document.getElementById("sync-phase-label");
  const counterEl = document.getElementById("sync-task-counter");
  const curEl     = document.getElementById("sync-current-task");
  const bar       = document.getElementById("sync-progress-bar");
  const pct = (n, t) => t > 0 ? Math.round(n / t * 100) : 0;

  switch (evt.type) {
    case "start":
      _syncState = {totalDbs: evt.total_dbs, taskTotal: 0, taskDone: 0};
      phaseEl.textContent = `Phase 1 of 2 — Master WBS Tasks`;
      _syncLog(`▶ Syncing ${evt.total_dbs} database(s)…`, "#90cdf4");
      break;

    case "db_start":
      _syncState.taskTotal = 0; _syncState.taskDone = 0;
      phaseEl.textContent  = `Phase 1 of 2 — ${evt.db}  (${evt.db_n}/${evt.total_dbs})`;
      counterEl.textContent = "Loading tasks…";
      bar.style.width = "0%";
      curEl.textContent = "";
      _syncLog(`📂 ${evt.db}`, "#fbd38d");
      break;

    case "db_loaded":
      _syncState.taskTotal  = evt.task_count;
      counterEl.textContent = `0 / ${evt.task_count}`;
      break;

    case "task":
      _syncState.taskDone++;
      bar.style.width       = pct(_syncState.taskDone, _syncState.taskTotal) + "%";
      counterEl.textContent = `${_syncState.taskDone} / ${_syncState.taskTotal || "?"}`;
      curEl.textContent     = evt.task;
      if (evt.action === "created") _syncLog(`  ✅ ${evt.task}`, "#68d391");
      else if (evt.action === "error") _syncLog(`  ❌ ${evt.task}: ${evt.detail||""}`, "#fc8181");
      else if (evt.action === "deleted") _syncLog(`  🗑 ${evt.task}`, "#f6ad55");
      break;

    case "db_done": {
      bar.style.width = "100%";
      const parts = [`✓ ${evt.created} created, ${evt.updated} updated`];
      if (evt.skipped) parts.push(`${evt.skipped} skipped`);
      if (evt.deleted) parts.push(`${evt.deleted} deleted`);
      if (evt.errors)  parts.push(`${evt.errors} error(s)`);
      _syncLog(parts.join(" · "), "#9ae6b4");
      break;
    }
    case "phase2_start":
      phaseEl.textContent   = `Phase 2 of 2 — Work Sessions`;
      bar.style.width       = "0%";
      counterEl.textContent = `0 / ${evt.project_count} project(s)`;
      curEl.textContent     = "";
      _syncLog(`⏱️ Syncing Work Sessions (${evt.project_count} project(s))`, "#90cdf4");
      break;

    case "ws_done":
      bar.style.width       = pct(evt.n, evt.total) + "%";
      counterEl.textContent = `${evt.n} / ${evt.total} project(s)`;
      curEl.textContent     = evt.project || "";
      _syncLog(
        `  ⏱️ ${evt.project}: ${evt.created} created, ${evt.skipped} existed` +
        (evt.error ? ` ❌ ${evt.error}` : ""),
        evt.error ? "#fc8181" : "#e2e8f0"
      );
      break;

    case "finished":
      bar.style.width       = "100%";
      counterEl.textContent = "";
      curEl.textContent     = "";
      phaseEl.textContent   = "✓ Sync complete";
      _syncLog("✅ Done", "#68d391");
      break;

    case "error":
      phaseEl.textContent = "⚠ Error";
      _syncLog(`❌ ${evt.message}`, "#fc8181");
      break;
  }
}

async function runSync() {
  if (!state.token) { alert("Set your token in the Setup tab first."); return; }
  const sources = buildSourcesList();
  if (!sources.length) { alert("Select at least one source database to sync."); return; }

  const btn = document.getElementById("sync-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Syncing…';
  document.getElementById("result-box").classList.remove("show");

  // Reset and show progress panel
  const prog = document.getElementById("sync-progress");
  prog.style.display = "block";
  document.getElementById("sync-log").innerHTML = "";
  document.getElementById("sync-progress-bar").style.width = "0%";
  document.getElementById("sync-phase-label").textContent = "Starting…";
  document.getElementById("sync-task-counter").textContent = "";
  document.getElementById("sync-current-task").textContent = "";

  // Start job
  let jobId;
  try {
    const startRes = await api("POST", "/api/sync-start", {token: state.token, sources});
    if (startRes.error) {
      document.getElementById("sync-phase-label").textContent = "✗ " + startRes.error;
      btn.disabled = false; btn.innerHTML = "▶ Sync Now";
      return;
    }
    jobId = startRes.job_id;
  } catch(e) {
    document.getElementById("sync-phase-label").textContent = "✗ Could not start sync";
    btn.disabled = false; btn.innerHTML = "▶ Sync Now";
    return;
  }

  // Poll for progress
  let offset = 0;
  let finalResult = null;
  while (true) {
    await new Promise(r => setTimeout(r, 500));
    let status;
    try {
      const r = await fetch(`/api/sync-status/${jobId}?offset=${offset}`);
      status = await r.json();
    } catch(e) {
      _syncLog(`❌ Poll error: ${e.message}`, "#fc8181");
      break;
    }
    for (const evt of (status.events || [])) _syncHandleEvent(evt);
    offset += (status.events || []).length;
    if (status.done) { finalResult = status.result; break; }
  }

  btn.disabled = false;
  btn.innerHTML = "▶ Sync Now";

  if (!finalResult) return;
  const res = finalResult;

  btn.disabled = false;
  btn.innerHTML = "▶ Sync Now";

  document.getElementById("r-created").textContent      = res.created    ?? "—";
  document.getElementById("r-updated").textContent      = res.updated    ?? "—";
  document.getElementById("r-skipped").textContent      = res.skipped    ?? "—";
  document.getElementById("r-deleted").textContent      = res.deleted    ?? "—";
  document.getElementById("r-ws-created").textContent   = res.ws_created ?? "—";
  document.getElementById("r-ws-skipped").textContent   = res.ws_skipped ?? "—";
  document.getElementById("r-errors-count").textContent = res.errors?.length ?? "—";

  const errList = document.getElementById("r-errors-list");
  if (res.errors && res.errors.length) {
    errList.style.display = "block";
    errList.innerHTML =
      `<strong>⛔ Errors (${res.errors.length}):</strong><ul>` +
      res.errors.map(e => `<li>${escHtml(e)}</li>`).join("") +
      "</ul>";
  } else {
    errList.style.display = "none";
  }

  const skipList = document.getElementById("r-skipped-list");
  if (res.skipped_tasks && res.skipped_tasks.length) {
    skipList.style.display = "block";
    skipList.innerHTML =
      `<strong>⚠️ Skipped (${res.skipped_tasks.length}) — these records were not synced:</strong><ul>` +
      res.skipped_tasks.map(t => {
        const src   = t.source ? `[${escHtml(t.source)}] ` : "";
        const label = t.url
          ? `<a href="${escHtml(t.url)}" target="_blank">Open in Notion ↗</a>`
          : escHtml(t.page_id);
        return `<li>${src}${label} — ${escHtml(t.reason)}</li>`;
      }).join("") +
      "</ul>";
  } else {
    skipList.style.display = "none";
  }

  const logNotice = document.getElementById("r-log-notice");
  if (res.log_file) {
    document.getElementById("r-log-file").textContent = res.log_file;
    logNotice.style.display = "block";
    logNotice.title = res.log_dir || "";
  } else {
    logNotice.style.display = "none";
  }

  document.getElementById("result-box").classList.add("show");
}


// ── Deduplication ────────────────────────────────────────────────────────────
async function runDedup() {
  if (!state.token) { alert("Set your token in the Setup tab first."); return; }
  const btn = document.getElementById("dedup-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="border-top-color:#fff;border-color:rgba(255,255,255,.3)"></span> Scanning…';
  setStatus("dedup-status", "");

  const res = await api("POST", "/api/deduplicate", {token: state.token});

  btn.disabled = false;
  btn.innerHTML = "🧹 Remove Duplicates";

  if (res.error) {
    setStatus("dedup-status", "✗ " + res.error, "red");
    return;
  }

  const totalArchived = (res.master_dupes_archived || 0) + (res.ws_archived || 0);
  let msg;
  if (totalArchived === 0) {
    msg = `✓ No duplicates found — ${res.kept} session(s) all clean`;
  } else {
    const parts = [];
    if (res.master_dupes_archived > 0)
      parts.push(`${res.master_dupes_archived} duplicate task(s) in Master WBS`);
    if (res.ws_archived > 0)
      parts.push(`${res.ws_archived} duplicate Work Session(s)`);
    msg = `✓ Archived: ${parts.join(" · ")} · ${res.kept} session(s) kept`;
  }

  if (res.no_task_sessions > 0) {
    msg += ` · ⚠️ ${res.no_task_sessions} unlinked session(s) skipped`;
  }
  if (res.errors && res.errors.length) {
    msg += ` · ❌ ${res.errors.length} error(s): ${res.errors.slice(0,3).join("; ")}`;
    setStatus("dedup-status", msg, "red");
  } else {
    setStatus("dedup-status", msg, totalArchived > 0 ? "green" : res.no_task_sessions > 0 ? "orange" : "green");
  }
}

async function runWriteback() {
  if (!state.token) { alert("Set your token in the Setup tab first."); return; }
  const btn = document.getElementById("writeback-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="border-top-color:#fff;border-color:rgba(255,255,255,.3)"></span> Writing back…';
  setStatus("writeback-status", "");

  const res = await api("POST", "/api/writeback-dates", {token: state.token});

  btn.disabled = false;
  btn.innerHTML = "↩ Write-back Dates";

  if (res.error) {
    setStatus("writeback-status", "✗ " + res.error, "red");
    return;
  }
  let msg = `✓ ${res.updated} task(s) updated`;
  if (res.skipped) msg += ` · ${res.skipped} skipped (no mapping or field)`;
  if (res.errors && res.errors.length)
    msg += ` · ❌ ${res.errors.length} error(s): ${res.errors.slice(0,2).join("; ")}`;
  setStatus("writeback-status", msg, res.errors?.length ? "orange" : "green");
}

// ── Quick Start tab ───────────────────────────────────────────────────────────
function refreshQuickTab() {
  const sel = document.getElementById("qs-source");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">— select a project —</option>';
  for (const [dbId, src] of Object.entries(state.savedSources)) {
    if (src.disabled) continue;
    const label = src.db_title || dbId;
    const opt   = document.createElement("option");
    opt.value       = dbId;
    opt.textContent = label;
    if (dbId === current) opt.selected = true;
    sel.appendChild(opt);
  }
  // Reset category panel whenever sources change
  document.getElementById("qs-category-group").style.display = "none";
}

// Called when the WBS dropdown changes — load category options for that database
async function onQsSourceChange() {
  const dbId = document.getElementById("qs-source").value;
  const catGroup = document.getElementById("qs-category-group");
  const catSel   = document.getElementById("qs-category");
  const catNew   = document.getElementById("qs-category-new");

  catNew.style.display = "none";
  catNew.value = "";

  if (!dbId || !state.token) {
    catGroup.style.display = "none";
    return;
  }

  const src = state.savedSources[dbId];
  const categoryField = src?.field_map?.category || "";
  if (!categoryField) {
    catGroup.style.display = "none";
    return;
  }

  // Show the panel immediately with a loading state
  catGroup.style.display = "block";
  catSel.innerHTML = '<option value="">Loading categories…</option>';
  catSel.disabled = true;

  const res = await api("POST", "/api/wbs-categories", {
    token: state.token,
    db_id: dbId,
    category_field: categoryField,
  });

  catSel.disabled = false;
  if (res.error || !res.options) {
    catSel.innerHTML = '<option value="">— could not load —</option>';
    return;
  }

  catSel.innerHTML = '<option value="">— no category —</option>';
  for (const opt of res.options) {
    const el = document.createElement("option");
    el.value       = opt.name;
    el.textContent = opt.name;
    catSel.appendChild(el);
  }
  // Always add the "new category" option at the end
  const newOpt = document.createElement("option");
  newOpt.value       = "__new__";
  newOpt.textContent = "➕ Add new category…";
  catSel.appendChild(newOpt);
}

// Show/hide the free-text input when "Add new category" is chosen
function onQsCategoryChange() {
  const val    = document.getElementById("qs-category").value;
  const catNew = document.getElementById("qs-category-new");
  if (val === "__new__") {
    catNew.style.display = "block";
    catNew.focus();
  } else {
    catNew.style.display = "none";
    catNew.value = "";
  }
}

function setQsNow() {
  const now = new Date();
  // datetime-local format: YYYY-MM-DDTHH:MM
  const pad = n => String(n).padStart(2,"0");
  const val = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}` +
              `T${pad(now.getHours())}:${pad(now.getMinutes())}`;
  document.getElementById("qs-start").value = val;
}

async function runQuickAdd() {
  const taskName  = (document.getElementById("qs-task-name").value || "").trim();
  const dbId      = document.getElementById("qs-source").value;
  const startVal  = document.getElementById("qs-start").value;
  const dueDate   = document.getElementById("qs-due-date").value || "";
  const priority  = document.getElementById("qs-priority").value || "Normal";
  const workType  = document.getElementById("qs-work-type").value || "";

  // Resolve category: use free-text input if "Add new" was chosen
  const catSel  = document.getElementById("qs-category");
  const catNew  = document.getElementById("qs-category-new");
  let   category = "";
  if (catSel && catSel.value === "__new__") {
    category = (catNew.value || "").trim();
    if (!category) { alert("Please enter a name for the new category."); return; }
  } else if (catSel) {
    category = catSel.value || "";
  }

  if (!taskName) { alert("Please enter a task name."); return; }
  if (!dbId)     { alert("Please select a project."); return; }

  const src = state.savedSources[dbId];
  if (!src) { alert("Source not found — go to Sources tab and save configuration."); return; }

  const btn = document.getElementById("qs-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Creating…';

  document.getElementById("qs-result").style.display = "none";
  document.getElementById("qs-result-ok").style.display  = "none";
  document.getElementById("qs-result-err").style.display = "none";

  // Convert datetime-local value to ISO-8601 with local timezone offset.
  // IMPORTANT: use local time components (getHours etc.), NOT toISOString()
  // which returns UTC — that caused times to appear shifted by the UTC offset.
  let sessionStart = "";
  if (startVal) {
    const d    = new Date(startVal);
    const pad  = n => String(n).padStart(2, "0");
    const off  = -d.getTimezoneOffset();          // minutes ahead of UTC
    const sign = off >= 0 ? "+" : "-";
    const hh   = pad(Math.floor(Math.abs(off) / 60));
    const mm   = pad(Math.abs(off) % 60);
    const local = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}` +
                  `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    sessionStart = local + `${sign}${hh}:${mm}`;
  }

  const res = await api("POST", "/api/quick-add", {
    token:           state.token,
    source_db_id:    dbId,
    project_id:      src.project_id,
    task_name:       taskName,
    task_name_field: src.field_map?.task_name || "Task",
    backlink_field:  src.backlink_field || "Master WBS",
    session_start:   sessionStart,
    due_date:        dueDate,
    priority:        priority,
    work_type:       workType,
    planned_end_field: src.field_map?.planned_end || "",
    priority_field:    src.field_map?.priority    || "",
    work_type_field:   src.field_map?.work_type   || "",
    category:          category,
    category_field:    src.field_map?.category    || "",
  });

  btn.disabled = false;
  btn.innerHTML = "⚡ Add Task &amp; Start Session";
  document.getElementById("qs-result").style.display = "block";

  if (res.error) {
    document.getElementById("qs-result-err").style.display  = "block";
    document.getElementById("qs-result-err").textContent    = "✗ " + res.error;
  } else {
    document.getElementById("qs-result-ok").style.display   = "block";
    document.getElementById("qs-link-wbs").href    = res.wbs_url    || "#";
    document.getElementById("qs-link-master").href = res.master_url || "#";
    document.getElementById("qs-link-ws").href     = res.ws_url     || "#";
    document.getElementById("qs-task-name").value  = "";  // clear for next entry
    // Reset category to "no category" (keep dropdown open for next quick entry)
    if (document.getElementById("qs-category").value === "__new__") {
      document.getElementById("qs-category").value = "";
      document.getElementById("qs-category-new").style.display = "none";
      document.getElementById("qs-category-new").value = "";
    }
  }
}

// ── Check Tasks tab ───────────────────────────────────────────────────────────
let _chkActiveTaskId   = null;
let _chkActiveTaskName = null;
let _chkActiveProjectId = null;

function refreshCheckTab() {
  const sel = document.getElementById("chk-source");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">— select a project —</option>';
  for (const [dbId, src] of Object.entries(state.savedSources)) {
    if (src.disabled) continue;
    const opt = document.createElement("option");
    opt.value       = dbId;
    opt.textContent = src.db_title || dbId;
    if (dbId === current) opt.selected = true;
    sel.appendChild(opt);
  }
}

function onChkSourceChange() {
  // Reset results when project changes
  document.getElementById("chk-results").style.display = "none";
  document.getElementById("chk-log-form").style.display = "none";
}

async function runCheckTasks() {
  const dbId = document.getElementById("chk-source").value;
  if (!dbId) { alert("Please select a project."); return; }
  if (!state.token) { alert("No token — save one in the Setup tab."); return; }

  const src = state.savedSources[dbId];
  if (!src || !src.project_id) {
    alert("Project ID not found — go to Sources tab and save configuration.");
    return;
  }

  const btn = document.getElementById("chk-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Querying…';
  document.getElementById("chk-results").style.display = "none";
  document.getElementById("chk-log-form").style.display = "none";

  const res = await api("POST", "/api/existing-tasks", {
    token:      state.token,
    project_id: src.project_id,
  });

  btn.disabled = false;
  btn.innerHTML = "🔍 Query Active Tasks";
  document.getElementById("chk-results").style.display = "block";

  const errEl  = document.getElementById("chk-results-err");
  const listEl = document.getElementById("chk-task-list");

  if (res.error) {
    errEl.textContent = "✗ " + res.error;
    errEl.style.display = "block";
    listEl.innerHTML = "";
    return;
  }
  errEl.style.display = "none";

  const tasks = res.tasks || [];
  if (tasks.length === 0) {
    listEl.innerHTML = '<div class="card" style="color:#666;font-size:13px;">No active tasks found for this project. Safe to create a new one.</div>';
    return;
  }

  const rows = tasks.map(t => {
    const dueLabel = t.planned_end
      ? `<span style="font-size:11px;color:#888;">Due: ${t.planned_end}</span>`
      : "";
    const statusBadge = t.status
      ? `<span style="font-size:11px;padding:2px 7px;border-radius:10px;background:#e2e8f0;color:#4a5568;">${escHtml(t.status)}</span>`
      : "";
    const sessionLines = t.work_sessions.length === 0
      ? '<span style="font-size:11px;color:#a0aec0;">No sessions yet</span>'
      : t.work_sessions.slice(0, 3).map(s => {
          const startFmt = s.start ? s.start.replace("T"," ").slice(0,16) : "no date";
          const sStatus  = s.status ? ` · ${escHtml(s.status)}` : "";
          return `<a href="${escHtml(s.url)}" target="_blank" style="font-size:11px;color:var(--accent);display:block;">
            ⏱️ ${escHtml(s.name)} — ${startFmt}${sStatus}
          </a>`;
        }).join("") +
        (t.work_sessions.length > 3
          ? `<span style="font-size:11px;color:#a0aec0;">+ ${t.work_sessions.length - 3} more</span>`
          : "");

    return `<div class="card" style="margin-bottom:10px;padding:14px 16px;">
      <div style="display:flex;align-items:flex-start;gap:10px;justify-content:space-between;">
        <div style="flex:1;min-width:0;">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px;">
            <a href="${escHtml(t.url)}" target="_blank"
               style="font-size:14px;font-weight:600;color:var(--text);text-decoration:none;word-break:break-word;">
              ${escHtml(t.name)}
            </a>
            ${statusBadge}
            ${dueLabel}
          </div>
          <div style="margin-top:4px;">${sessionLines}</div>
        </div>
        <button class="btn btn-ghost"
                style="font-size:12px;padding:5px 10px;white-space:nowrap;flex-shrink:0;"
                onclick="openLogForm('${escHtml(t.id)}','${escHtml(t.name.replace(/'/g,"\\'"))}','${escHtml(src.project_id)}')">
          ⏱️ Log Session
        </button>
      </div>
    </div>`;
  }).join("");

  listEl.innerHTML = `<p style="font-size:12px;color:#888;margin-bottom:8px;">${tasks.length} active task(s) found — click a task name to open in Notion, or "Log Session" to record work.</p>` + rows;
}

function openLogForm(taskId, taskName, projectId) {
  _chkActiveTaskId    = taskId;
  _chkActiveTaskName  = taskName;
  _chkActiveProjectId = projectId;

  document.getElementById("chk-log-task-name").textContent = taskName;
  document.getElementById("chk-start").value   = "";
  document.getElementById("chk-end").value     = "";
  document.getElementById("chk-status").value  = "";
  document.getElementById("chk-work-type").value = "";
  document.getElementById("chk-log-result").style.display = "none";
  document.getElementById("chk-log-result").textContent   = "";

  const form = document.getElementById("chk-log-form");
  form.style.display = "block";
  form.scrollIntoView({behavior:"smooth", block:"nearest"});
}

function cancelLogSession() {
  document.getElementById("chk-log-form").style.display = "none";
  _chkActiveTaskId = _chkActiveTaskName = _chkActiveProjectId = null;
}

function setChkNow(fieldId) {
  const now = new Date();
  const pad = n => String(n).padStart(2,"0");
  document.getElementById(fieldId).value =
    `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}` +
    `T${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function localIso(val) {
  if (!val) return "";
  const d   = new Date(val);
  const pad = n => String(n).padStart(2,"0");
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const hh   = pad(Math.floor(Math.abs(off)/60));
  const mm   = pad(Math.abs(off)%60);
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}` +
         `T${pad(d.getHours())}:${pad(d.getMinutes())}${sign}${hh}:${mm}`;
}

async function runLogSession() {
  if (!_chkActiveTaskId || !_chkActiveProjectId) return;

  const startVal  = document.getElementById("chk-start").value;
  if (!startVal) { alert("Please enter a Session Start time."); return; }

  const endVal    = document.getElementById("chk-end").value;
  const status    = document.getElementById("chk-status").value;
  const workType  = document.getElementById("chk-work-type").value;

  const btn = document.getElementById("chk-log-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Logging…';

  const resultEl = document.getElementById("chk-log-result");
  resultEl.style.display = "none";

  const res = await api("POST", "/api/log-session", {
    token:          state.token,
    master_task_id: _chkActiveTaskId,
    project_id:     _chkActiveProjectId,
    session_start:  localIso(startVal),
    session_end:    localIso(endVal),
    status:         status,
    work_type:      workType,
  });

  btn.disabled = false;
  btn.innerHTML = "⏱️ Log Session";
  resultEl.style.display = "block";

  if (res.error) {
    resultEl.innerHTML = `<span style="color:#e53e3e;">✗ ${escHtml(res.error)}</span>`;
  } else {
    resultEl.innerHTML = `<span style="color:#38a169;font-weight:600;">✓ Session logged!</span>
      &nbsp;<a href="${escHtml(res.ws_url)}" target="_blank" style="font-size:13px;">Open in Notion →</a>`;
    // Refresh the task list so updated session count shows
    setTimeout(runCheckTasks, 800);
  }
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t,i)=> {
    const names = ["setup","sources","sync","quick","check","help"];
    t.classList.toggle("active", names[i]===name);
  });
  document.querySelectorAll(".pane").forEach(p => {
    p.classList.toggle("active", p.id === "pane-"+name);
  });
  if (name === "sync")  refreshSyncTab();
  if (name === "quick") refreshQuickTab();
  if (name === "check") refreshCheckTab();
}

// ── Utils ─────────────────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str||"")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

init();
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  📋 Notion WBS Sync Tool")
    print("  Opening http://localhost:8765 ...")
    print("  Press Ctrl+C to stop.")
    print("=" * 55)

    def open_browser():
        time.sleep(1.2)
        webbrowser.open("http://localhost:8765")

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
