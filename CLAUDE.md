# Focal — Faculty Project & Workload Hub

A local Flask/Python web app that bridges per-project Notion WBS databases into a unified faculty workload system. It syncs tasks into a Master WBS Tasks database, creates linked Work Sessions for time tracking, and provides a workload dashboard.

## Run Commands

```bash
python3 main.py                  # start on http://localhost:8765
python3 main.py --debug          # Flask debug mode (no auto browser open)
python3 main.py --port 9000      # custom port
python3 sync_work_type_options.py  # push Work Type taxonomy to all Notion DBs
```

Dependencies: Python 3.12, Flask 3.x. Install with `pip install -r requirements.txt`.

## Architecture

Three-layer relay: **Project WBS DBs → Master WBS Tasks → Work Sessions**

- Each project has its own Notion WBS database (source of truth for tasks).
- The sync engine reads those WBS DBs and upserts into Master WBS Tasks (relay layer).
- Work Sessions are linked to Master WBS Tasks entries for time tracking.
- This relay design exists because Notion relations can only target one DB.

Key Notion database IDs (hardcoded in `focal/config.py`):
- `PROJECTS_DB_ID` = `01705bad...` — 📁 Projects
- `MASTER_DB_ID` = `2de3b2f3...` — 📋 Master WBS Tasks
- `WORK_SESSIONS_DB_ID` = `308c193f...` — ⏱️ Work Sessions

## File Map

```
main.py                     Entry point
focal/
  app.py                    Flask app factory, blueprint registration
  config.py                 Constants, DB IDs, load/save helpers
  notion_client.py          NotionClient class — all Notion API calls go here
  sync_engine.py            Sync logic, deduplication, focus cache regeneration
  tasks.py                  quick_add_task(), continuation session logic
  log_writer.py             write_sync_log()
  orphan_audit.py           Audit for orphaned Work Sessions
  routes/
    config_routes.py        /api/config, /api/discover, /api/schema
    sync_routes.py          /api/sync, /api/sync-start, /api/sync-status
    task_routes.py          /api/quick-add, /api/wbs-categories
    dashboard_routes.py     /focus, /workload, /design + their APIs
    student_routes.py       /api/students (dissertation advisee tracking)
    orphan_routes.py        /api/orphans
  templates/
    index.html              Main hub (sync + quick-add)
    focus.html              Focus Tasks view
    workload.html           Workload Dashboard
    design.html             System design doc
focal_config.json           SOURCE OF TRUTH — all WBS source DBs, field maps, project IDs
focal_sessions_mappings.json  Auto-generated — wbs_page_id → master/session IDs + metadata
focal_mappings.json           Auto-generated — legacy task mappings (~120 entries)
focus-task-list-cache.json    Cached task list for Focus view
```

## Read Policy (follow this before editing)

| Tier | Files | Policy |
|------|-------|--------|
| Tier 1 | `focal_config.json`, this file | Always read first |
| Tier 2 | `focal/*.py`, `focal/routes/*.py`, `main.py` | Read only the file(s) you are modifying |
| Tier 3 | `focal_sessions_mappings.json`, `focal_mappings.json`, `focus-task-list-cache.json` | Read only if explicitly debugging mapping state |
| Archive | `archive/` | Never read |
| Ignore | `__pycache__/`, `.git/`, `venv/`, `backups/`, `sync_logs/` | Never read |

Do not read the full codebase speculatively. Read Tier 1 first, then only the specific Tier 2 files relevant to the task.

## Conventions

- All Notion API calls go through `NotionClient` in `notion_client.py`. Do not call `requests` directly in routes or sync logic.
- Field mappings between WBS source columns and the master schema are defined in `focal_config.json` under each source's `field_map` key. Do not hardcode field names in sync logic.
- Work Type options are the single source of truth in `WORK_TYPE_OPTIONS` in `config.py`. To add a Work Type, edit that list and run `sync_work_type_options.py`.
- Never use em dashes (—) in any code comments or strings.
- Route blueprints are registered in `focal/app.py`. When adding a new route file, register it there.

## Config File Schema (`focal_config.json`)

```json
{
  "token": "<Notion integration token>",
  "sources": {
    "<notion_db_id>": {
      "db_title": "WBS — Project Name",
      "project_id": "<projects_db_entry_id>",
      "backlink_field": "Master WBS",
      "field_map": {
        "task_name": "Task",
        "planned_end": "Due Date",
        "planned_start": "Planned Start",
        "priority": "Priority",
        "work_type": "Work Type",
        "notes": "Notes",
        "category": "Category"
      },
      "auto_calc_planned_start": 7
    }
  }
}
```

`field_map` keys are canonical schema names; values are the actual Notion column names in that WBS database. Only `task_name` and `planned_end` are required.

## Skills

`.claude/skills/sync-tool-edit/SKILL.md` — load before editing any file in `focal/`
`.claude/skills/add-wbs-config-entry/SKILL.md` — load before editing `focal_config.json`

## What Not to Do

- Do not run `python3 main.py` in the background during edits — it holds a port and masks import errors.
- Do not edit `focal_sessions_mappings.json` or `focal_mappings.json` directly — they are overwritten by sync runs.
- Do not push API calls that modify Notion data without confirming with the user first — the Notion workspace is live.
- Do not read `archive/` — it contains superseded v1/v2 code that will mislead context.
