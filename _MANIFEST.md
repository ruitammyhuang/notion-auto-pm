# _MANIFEST.md — Focal: Faculty Project & Workload Hub

> **Context rule:** Only read Tier 1 files directly. For all other files, do not read them into context unless explicitly asked by the user.

---

## Folder Structure

```
Notion_Auto_PM/
│
├── _MANIFEST.md                    ← [TIER 1] This file. Read first.
├── focal_config.json               ← [TIER 1] All source DBs, field maps, project IDs
│
├── main.py                         ← [TIER 2] Entry point — run with: python3 main.py
│
├── focal/                          ← [TIER 2] Main application package
│   ├── app.py                      Flask app factory + blueprint registration
│   ├── config.py                   Constants, file paths, load/save helpers
│   ├── notion_client.py            NotionClient class (API wrapper)
│   ├── sync_engine.py              Sync logic, deduplication, focus cache
│   ├── tasks.py                    quick_add_task()
│   ├── log_writer.py               write_sync_log()
│   ├── routes/                     Flask Blueprints (one file per concern)
│   │   ├── config_routes.py        /api/config, /api/discover, /api/schema, etc.
│   │   ├── sync_routes.py          /api/sync, /api/sync-start, /api/sync-status
│   │   ├── task_routes.py          /api/quick-add, /api/wbs-categories, etc.
│   │   └── dashboard_routes.py     /focus, /workload, /design + their APIs
│   └── templates/                  HTML UI pages
│       ├── index.html              Main hub (sync + quick-add)
│       ├── focus.html              📌 Focus Tasks view
│       ├── workload.html           📊 Workload Dashboard
│       └── design.html             System design document
│
├── focal_mappings.json             ← [TIER 3] Auto-generated (~120 task mapping entries)
├── focal_sessions_mappings.json    ← [TIER 3] Auto-generated (~241 session mapping entries)
├── focus-task-list-cache.json      ← [TIER 3] Cached task list for Focus view
│
└── notion_wbs_sync_v1/             ← [ARCHIVE] Original monolithic script (v1)
```

---

## Tier Definitions

| Tier | Read policy | Files |
|------|-------------|-------|
| **Tier 1** | Always safe to read proactively | `_MANIFEST.md`, `focal_config.json` |
| **Tier 2** | Read only when modifying or debugging | `main.py`, `focal/` package |
| **Tier 3** | Read only when explicitly asked | `focal_mappings.json`, `focal_sessions_mappings.json`, `focus-task-list-cache.json` |
| **Archive** | Do not read | `notion_wbs_sync_v1/` |
| **Ignore** | Never read | `__pycache__/`, `.git/`, `.DS_Store` |

---

## What Each Tier 1 File Contains

**`focal_config.json`** — the single source of truth for the sync system. Each entry defines one source WBS database: its Notion database ID (`sources` key), the project it belongs to (`project_id`), how its columns map to the master schema (`field_map`), and the backlink field name. Edit this to add or reconfigure projects.

---

## System Summary

Focal is a local Python/Flask web app that bridges multiple per-project Notion WBS databases into a unified faculty workload management system. It syncs tasks from project WBS databases into a Master WBS Tasks database, creates linked Work Sessions for time tracking, surfaces focus tasks by urgency, and provides a workload dashboard with hours logged by project and work type. Run with `python3 main.py` — opens at http://localhost:8765.
