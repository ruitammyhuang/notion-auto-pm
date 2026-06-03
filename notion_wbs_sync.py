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
                   category="", category_field=""):
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
    category          = body.get("category", "").strip()       # selected or new category
    category_field    = body.get("category_field", "").strip() # column name in WBS

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
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('setup')">⚙️ Setup</div>
  <div class="tab" onclick="switchTab('sources')">🗂 Sources</div>
  <div class="tab" onclick="switchTab('sync')">▶️ Sync</div>
  <div class="tab" onclick="switchTab('quick')">⚡ Quick Start</div>
  <div class="tab" onclick="switchTab('help')">❓ Help</div>
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
      <!-- Shown when "➕ Add new category" is selected -->
      <input type="text" id="qs-category-new"
             placeholder="Type new category name…"
             style="display:none;margin-top:6px;font-size:13px;"
             oninput="this.value=this.value">
      <div class="field-hint">Categorise this task for time-tracking analysis. Type a new name to create a category on the fly.</div>
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
    const isChecked = !!(saved.project_id);
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
  {key:"task_name",     label:"Task Name",              req:true},
  {key:"priority",      label:"Priority",               req:false},
  {key:"planned_start", label:"Planned Start",          req:false},
  {key:"planned_end",   label:"Planned End / Due Date", req:false},
  {key:"notes",         label:"Notes",                  req:false},
  {key:"work_type",     label:"Work Type",              req:false},
  {key:"category",      label:"Category / Phase / Group", req:false},
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
  const sources = {};
  for (const db of state.discoveredDbs) {
    const chk = document.getElementById("chk-"+db.id);
    if (!chk || !chk.checked) continue;

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
  const entries = Object.entries(state.savedSources);
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

// ── Quick Start tab ───────────────────────────────────────────────────────────
function refreshQuickTab() {
  const sel = document.getElementById("qs-source");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">— select a project —</option>';
  for (const [dbId, src] of Object.entries(state.savedSources)) {
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

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t,i)=> {
    const names = ["setup","sources","sync","quick","help"];
    t.classList.toggle("active", names[i]===name);
  });
  document.querySelectorAll(".pane").forEach(p => {
    p.classList.toggle("active", p.id === "pane-"+name);
  });
  if (name === "sync")  refreshSyncTab();
  if (name === "quick") refreshQuickTab();
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
