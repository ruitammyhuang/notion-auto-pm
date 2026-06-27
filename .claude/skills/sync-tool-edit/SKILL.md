---
name: sync-tool-edit
description: >
  Load before editing any file in focal/ -- the modular Flask package that
  powers the Focal sync tool. Trigger whenever the user wants to: add a new
  Quick Start field, modify a Flask route, fix a sync bug, change task creation
  logic, or edit any .py file under focal/. This skill encodes the package map
  and key function signatures so you can jump directly to the right file and
  lines without reading the full codebase.
---

# sync-tool-edit

Always read `focal_config.json` first (Tier 1). Then read only the specific
Tier 2 file(s) you are modifying. Do not read the full package speculatively.

## Package map

| File | Lines | What's here |
|------|-------|-------------|
| `focal/config.py` | 167 | Constants (MASTER_DB_ID, WORK_SESSIONS_DB_ID, PROJECTS_DB_ID), WORK_TYPE_OPTIONS, load/save helpers for all JSON files |
| `focal/notion_client.py` | 231 | NotionClient class -- ALL Notion API calls go here. Property builders: p_title, p_text, p_select, p_date, p_relation |
| `focal/sync_engine.py` | 652 | Core sync logic, focus cache, due-date writeback, continuation sessions |
| `focal/tasks.py` | 308 | quick_add_task(), delete_task_cascade(), continuation session naming |
| `focal/app.py` | 43 | Flask app factory, blueprint registration |
| `focal/log_writer.py` | 56 | write_sync_log() |
| `focal/orphan_audit.py` | 637 | Orphaned Work Sessions audit logic |
| `focal/routes/config_routes.py` | 117 | /api/config, /api/discover, /api/schema, /api/test-token, /api/projects |
| `focal/routes/sync_routes.py` | 218 | /api/sync, /api/sync-start, /api/sync-status/<job_id>, /api/sync-cancel/<job_id> |
| `focal/routes/task_routes.py` | 666 | /api/quick-add, /api/wbs-categories, /api/wbs-text-options, /api/project-tasks, /api/log-session, /api/push-to-wbs, /api/delete-task, /api/work-types, /api/set-work-type |
| `focal/routes/dashboard_routes.py` | 338 | /focus, /workload, /design, /api/focus-tasks, /api/workload, /api/writeback-dates |
| `focal/routes/student_routes.py` | 81 | /api/students |
| `focal/routes/orphan_routes.py` | 194 | /api/orphans |
| `focal/templates/index.html` | - | Main hub UI (sync panel + Quick Start tab) |
| `focal/templates/focus.html` | - | Focus Tasks view |
| `focal/templates/workload.html` | - | Workload Dashboard |

## Key function signatures

### quick_add_task() -- focal/tasks.py line 60
```python
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
) -> dict:
```
Creates a page in the Project WBS DB, a Master WBS Tasks entry, and a Work
Session -- all in one call. Returns {wbs_id, master_id, ws_id, name}.

### sync_one_database() -- focal/sync_engine.py line 416
Main sync function. Reads a source WBS DB, upserts to Master WBS Tasks, and
updates focal_sessions_mappings.json. Parameters: (client, db_id, source_cfg, mappings, errors).

### regenerate_focus_cache() -- focal/sync_engine.py line 111
Queries Work Sessions for active tasks and writes focus-task-list-cache.json.
Called at the end of every sync and after task creation.

### /api/quick-add route -- focal/routes/task_routes.py line 108
Parses request body, calls quick_add_task(), returns {success, wbs_id, ws_id, name}.

## Adding a new Quick Start field (5 touchpoints)

### 1. quick_add_task() signature -- focal/tasks.py ~line 60
Add two parameters: myfield="" and myfield_field="".

### 2. quick_add_task() body -- focal/tasks.py ~line 90-150
Write the value to the WBS source page props:
```python
if myfield and myfield_field:
    wbs_props[myfield_field] = p_select(myfield)   # for select fields
    # or p_text(myfield) for text fields
```

### 3. /api/quick-add route -- focal/routes/task_routes.py ~line 108
Extract from request body and forward to quick_add_task():
```python
myfield       = body.get("myfield", "").strip()
myfield_field = body.get("myfield_field", "").strip()
```

### 4. /api/wbs-categories or /api/wbs-text-options -- focal/routes/task_routes.py
If the field needs a dropdown, extend api_wbs_categories() (line 39) for
select fields, or api_wbs_text_options() (line 63) for text fields.

### 5. focal/templates/index.html -- Quick Start tab HTML + JS
Add the UI panel and wire up onQsSourceChange() to show/hide it, and
runQuickAdd() to collect and POST the value.

## Adding a new Flask route

1. Choose the right routes file by concern (see package map above).
2. Add the route function following the blueprint pattern in that file.
3. If creating a new routes file, register the blueprint in focal/app.py.
4. All Notion API calls must go through NotionClient -- never call requests directly.

## Conventions

- Property builders in notion_client.py: p_title(v), p_text(v), p_select(v), p_date(v), p_relation([id]). Use these -- do not build raw dicts inline.
- WORK_TYPE_OPTIONS in config.py is the single source of truth. To add a Work Type, edit that list and run sync_work_type_options.py.
- Never use em dashes in code comments or strings.
- Register new blueprints in focal/app.py -- that is the only place.
- Do not run python3 main.py in the background during edits -- it holds the port and masks import errors.

## Common debug patterns

**Sync bug** -> Read focal/sync_engine.py (sync_one_database at line 416) and the relevant sync log in sync_logs/.

**Task creation bug** -> Read focal/tasks.py (quick_add_task at line 60) and focal/routes/task_routes.py (api_quick_add at line 108).

**Notion API error** -> Read focal/notion_client.py. All API calls funnel here.

**Focus cache stale** -> Check regenerate_focus_cache() in focal/sync_engine.py line 111.

**Dashboard shows wrong data** -> Read focal/routes/dashboard_routes.py.
