# Cowork vs Claude Code: A Working Guide for the Focal Project

> **Purpose:** A practical decision guide for knowing when to work in Claude Cowork vs Claude Code when developing and operating the Focal faculty PM system.

---

## The Core Decision Rule

Ask one question: **Does this task require running Python and iterating on the result?**

- If yes → **Claude Code**
- If no → **Cowork**

A secondary filter: **Does this task involve reading from or writing to Notion as a user (not a developer)?**

- If yes → **Cowork** (it has the Notion MCP and skills built in)
- If no → probably **Claude Code**

---

## Quick Reference Table

| Task category | Tool | Reason |
|---|---|---|
| Query your Notion tasks / sessions | Cowork | Notion MCP + workload query skill |
| Generate / refresh Focus Task List | Cowork | focus-task-list skill encodes all IDs |
| Create a new project in Notion | Cowork | notion-new-project skill |
| Add a WBS to `focal_config.json` | Cowork | add-wbs-config-entry skill |
| Edit a Flask route or add an endpoint | Claude Code | Needs file editing + server restart to verify |
| Debug a sync error from the logs | Claude Code | Needs to read logs, trace code, test fix |
| Add a new field to Quick Start UI | Claude Code | sync-tool-edit pattern; needs live test |
| Refactor across multiple `focal/` files | Claude Code | Cross-file coherence requires full context |
| Write a shell script or cron job | Claude Code | Needs execution and testing |
| Create a Word doc / slide deck report | Cowork | docx/pptx skills |
| Understand workload this week | Cowork | Notion MCP query |
| Fix a merge conflict in git | Claude Code | Needs git commands + file diffs |

---

## Cowork: What It Does Best

Cowork's strengths are the Notion MCP (live data reads/writes) and the pre-built skills that encode your system's IDs and procedures. Use it for anything that is about **operating** the Focal system, not building it.

### 1. Notion Operations (via MCP)

Cowork has a live connection to your Notion workspace. Use it whenever you need to read or write data rather than edit code.

**Examples from your project:**

- "What tasks are overdue right now?" -- triggers `notion-workload-query` skill, queries Master WBS Tasks
- "Show me what I worked on this week" -- queries Work Sessions DB directly
- "How many hours have I logged on the scoping review this month?" -- workload query by project
- "Create a new project for EDG 6648 Fall 2026" -- triggers `notion-new-project` skill, builds hub page + WBS DB + Projects DB entry in one shot
- "Add the new advising WBS to the sync config" -- triggers `add-wbs-config-entry` skill, writes the correct `focal_config.json` entry

### 2. Focus Task List Operations

The `focus-task-list` skill encodes all your Notion database IDs and the exact update procedure. Cowork can generate or refresh the Focus Tasks page without you touching any code.

**Examples:**

- "What should I work on today?" -- generates a prioritized view from Master WBS Tasks
- "Refresh my focus list" -- updates the 📌 Focus Tasks page on Notion Home
- "What's due this week?" -- queries by urgency bucket

### 3. Document and Report Creation

When you need a deliverable document (not code), Cowork's docx/pptx skills are faster and more natural.

**Examples:**

- "Write a summary of my workload for my annual review as a Word doc"
- "Create a slide deck showing what projects are active this semester"
- "Draft a memo to my department chair about service load"

### 4. Scheduled Tasks and Automation Config

Use Cowork to set up or adjust the automated tasks that run against Notion (focus list refresh, backup reminders, etc.) because these interact with Notion data, not code files.

**Example:**

- "Schedule the focus task list to refresh every Monday morning at 8am"

---

## Claude Code: What It Does Best

Claude Code runs persistently in your terminal. It can start the Flask server, observe the error, edit the file, restart, and verify -- all in one session. Use it for anything that is about **building or fixing** the Focal application.

### 1. Adding or Modifying Flask Routes

Any change to `focal/routes/` requires editing Python, restarting the server, and checking the endpoint response. Claude Code can do all three steps without you.

**Examples from your project:**

- "Add a `/api/session-summary` endpoint to `dashboard_routes.py` that returns hours by project for the current week"
- "The `/api/quick-add` route is returning a 500 when `work_type` is blank -- fix it"
- "Add a new filter parameter to `GET /api/focus-tasks` for filtering by project"

### 2. Sync Engine Changes (`sync_engine.py`, `tasks.py`)

The sync engine is the most complex part of Focal. Changes here require running an actual sync against Notion and checking `sync_logs/` for correctness. Claude Code can run `python3 main.py`, trigger a sync, read the log, and iterate.

**Examples:**

- "The deduplication logic is creating duplicate tasks when the WBS task name has trailing spaces -- fix it"
- "Add a field to the sync output that captures `planned_start` from WBS sources that have it"
- "The orphan audit is flagging sessions incorrectly when a task is in Session Done status -- debug and fix"

### 3. Cross-File Refactors

When a change touches more than two files, Claude Code's full-project indexing prevents the drift that happens in Cowork when only some files are in context.

**Examples:**

- "Rename `focal_config.json` to `focal_sources.json` everywhere it's referenced"
- "Extract the Notion API retry logic from `notion_client.py` into a shared utility"
- "Move the `WORK_SESSIONS_DB_ID` constant out of `config.py` and into `focal_config.json`"

### 4. Debugging Live Errors

When the Flask server throws an error or a sync produces wrong output, you need to trace the stack, find the root cause, and test the fix. Claude Code can do this interactively.

**Examples:**

- "The server crashed with a KeyError in `sync_routes.py` -- here's the traceback: [paste]"
- "After the last change, the workload dashboard shows 0 hours for all projects -- debug it"
- "The backup script is failing silently -- run it with verbose logging and find out why"

### 5. Git Operations

Any git work beyond committing a single-file change belongs in Claude Code.

**Examples:**

- "Resolve a merge conflict in `focal/sync_engine.py`"
- "Create a feature branch for the new student advising tab"
- "Show me what changed between the last two commits and summarize the diff"

### 6. Writing and Testing Scripts

The `scripts/` folder (backup, restore, install plist) should be written and tested in Claude Code so they can be run immediately.

**Examples:**

- "Update `notion_backup.py` to also export Work Sessions"
- "Test the restore script against the latest backup and verify row counts match"

---

## Edge Cases: Tasks That Could Go Either Way

Some tasks sit on the boundary. Here's how to decide:

**"Add a new project to the sync config"**
If you're just adding the entry to `focal_config.json`: **Cowork** (use `add-wbs-config-entry` skill).
If the new project requires a new field type or sync behavior: **Claude Code** (need to edit `sync_engine.py` and test).

**"Update the Focus Task List logic"**
If you want to regenerate it from current Notion data: **Cowork**.
If you want to change *how* it's calculated (edit `sync_engine.py`'s `regenerate_focus_cache`): **Claude Code**.

**"Fix a field mapping"**
If it's a column name mismatch in `focal_config.json`: **Cowork** (edit the JSON, no code change needed).
If the field type is wrong and needs handling in `sync_engine.py`: **Claude Code**.

**"Check why a task didn't sync"**
If you want to read recent sync logs to understand what happened: **Cowork** (read the file, summarize).
If you need to reproduce the bug and fix code: **Claude Code**.

---

## Practical Workflow Pattern

For features that span both tools, this order works well:

1. **Cowork** -- define what you need ("I want a new endpoint that shows advisor meeting hours")
2. **Claude Code** -- build, test, and commit the feature
3. **Cowork** -- validate the result against live Notion data ("query this week's advisor hours and check it matches")

---

## What to Tell Each Tool When You Start

**When opening Cowork:**
> "I'm working on the Focal faculty PM system in Notion_Auto_PM. Read `_MANIFEST.md` first."

**When opening Claude Code:**
> "I'm working on the Focal Flask app. The entry point is `main.py`, the package is `focal/`. Read `_MANIFEST.md` and `focal_config.json` before making changes."

These priming prompts save the tool from re-deriving context it needs.

---

*Last updated: June 2026*
