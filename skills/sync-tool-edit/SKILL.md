---
name: sync-tool-edit
description: >
  Use this skill whenever editing, extending, or debugging the Focal package —
  the modular Flask/Python sync tool in the Notion_Auto_PM project. Trigger
  whenever the user wants to: add a new quick-start field, modify the Quick Start
  UI, change API behavior, add a Flask route, fix a bug in sync logic, or change
  how tasks/sessions are created. This skill encodes the package's file map and
  the exact end-to-end pattern for adding new fields, so Claude can jump directly
  to the right file and lines without reading or grepping everything. ALWAYS load
  this skill before touching any file in the focal/ package — it prevents context
  window overflow.
---

# sync-tool-edit

This skill gives you a precise map of the `focal/` package so you can jump
directly to the right file instead of reading everything. Read only the specific
files and line ranges you need.

## Package root
`/Users/rui.huang/Documents/Claude/Projects/Notion_Auto_PM/`
(bash path: `/sessions/confident-nice-thompson/mnt/Notion_Auto_PM/`)

Run with: `python3 main.py` → opens at http://localhost:8765

---

## File map

| File | Lines | What's here |
|------|-------|-------------|
| `main.py` | 17 | Top-level entry point — imports and calls `focal.__main__.main()` |
| `focal/__main__.py` | 30 | `main()` — starts Flask + opens browser via `threading.Thread` |
| `focal/app.py` | ~60 | `create_app()` — Flask app factory, blueprint registration, `/` route |
| `focal/config.py` | ~80 | Constants (`MASTER_DB_ID`, `PROJECTS_DB_ID`, `WORK_SESSIONS_DB_ID`, `PRIORITY_MAP`, `VALID_PRIORITIES`, `VALID_WORK_TYPES`), file paths (`focal_config.json`, `focal_mappings.json`, `focal_sessions_mappings.json`), load/save helpers |
| `focal/notion_client.py` | 220 | `NotionClient` class + module-level property builders (`extract`, `p_title`, `p_text`, `p_select`, `p_date`) |
| `focal/sync_engine.py` | 623 | All sync logic — see section map below |
| `focal/tasks.py` | 126 | `quick_add_task(client, ...)` — creates task in 3 databases simultaneously |
| `focal/log_writer.py` | ~40 | `write_sync_log(total)` — writes error log to timestamped `.txt` file |
| `focal/routes/config_routes.py` | ~120 | Blueprint `"config"`: `/api/config`, `/api/test-token`, `/api/discover`, `/api/schema`, `/api/projects` |
| `focal/routes/sync_routes.py` | 239 | Blueprint `"sync"`: `/api/sync`, `/api/sync-start`, `/api/sync-status/<job_id>`, `/api/sync-work-sessions` |
| `focal/routes/task_routes.py` | 214 | Blueprint `"tasks"`: `/api/wbs-categories`, `/api/wbs-text-options`, `/api/quick-add`, `/api/deduplicate`, `/api/add-project-page-link` |
| `focal/routes/dashboard_routes.py` | ~120 | Blueprint `"dashboard"`: `/focus`, `/workload`, `/design` + their POST APIs |
| `focal/templates/index.html` | 1601 | Main UI — see section map below |
| `focal/templates/focus.html` | — | 📌 Focus Tasks view |
| `focal/templates/workload.html` | — | 📊 Workload Dashboard |
| `focal/templates/design.html` | — | System design document |

---

## sync_engine.py section map

| Lines | What's here |
|-------|-------------|
| 1–33 | Imports |
| 34–46 | `_with_retry(fn, *args, **kwargs)` — retries once on Timeout |
| 47–70 | `_ws_has_date()`, `has_logged_hours()` |
| 72–114 | `_get_task_master_id()`, `_get_page_title()`, `_get_project_id_from_page()`, `_archive_page()` |
| 115–181 | `regenerate_focus_cache(client)` — writes focus-task-list-cache.json |
| 182–216 | `cleanup_orphaned_mappings(client, ...)` |
| 217–299 | `sync_work_sessions_for_project(client, project_page_id, sessions_mappings)` |
| **300–512** | **`sync_one_database(client, source_db_id, project_page_id, field_map, ...)` — core sync** |
| 513–622 | `deduplicate_work_sessions_global(client)` |

---

## templates/index.html section map

| Lines | What's here |
|-------|-------------|
| 1–275 | `<head>`, CSS styles |
| 276–290 | Tab bar (`switchTab()` targets: `setup`, `sources`, `sync`, `quick`, `help`) |
| 291–465 | Setup, Sources, Sync, Help tab panels |
| **469–560** | **Quick Start tab HTML** — all `<div class="field-group">` panels |
| 469 | `#qs-source` project dropdown → calls `onQsSourceChange()` |
| 476 | `#qs-category-group` — hidden by default, shown by `onQsSourceChange()` |
| 489 | `#qs-level-group` — hidden by default |
| 501 | `#qs-orgdiv-group` — hidden by default |
| 531 | `#qs-work-type` — always visible select |
| 556 | "⚡ Add Task & Start Session" button → `runQuickAdd()` |
| 560–959 | Setup/Sources/Sync tab JS handlers |
| **960–977** | **`MASTER_FIELDS`** — JS array defining all mappable field keys for Sources tab |
| 978–1397 | Sources tab render logic, sync handlers |
| **1398–1415** | **`refreshQuickTab()`** — repopulates project dropdown |
| **1416–1489** | **`onQsSourceChange()`** — shows/hides QS panels, fetches category/level/orgdiv options |
| **1490–1560** | **`runQuickAdd()`** — collects form values, POSTs to `/api/quick-add` |
| 1580–1601 | `switchTab()` and closing tags |

---

## Key data structures

### `NotionClient` (focal/notion_client.py)
All sync functions accept `client: NotionClient` as their first argument — never a raw token string.
```python
client = NotionClient(token)
client.query_db(db_id, filter_body)
client.create_page(parent, properties)    # raises on error
client.patch_page(page_id, data)          # returns raw Response (check .status_code)
client.get_block_children(block_id)
client.append_block_children(block_id, children)
client.write_backlink(source_page_id, master_page_id, backlink_field)
```

### `MASTER_FIELDS` (index.html line 960)
```js
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
```

### `quick_add_task()` signature (tasks.py line 27)
```python
def quick_add_task(
    client: NotionClient,
    source_db_id: str, project_id: str, task_name: str,
    task_name_field: str, backlink_field: str, session_start: str,
    due_date: str = "", priority: str = "Normal", work_type: str = "",
    planned_end_field: str = "", priority_field: str = "", work_type_field: str = "",
    category: str = "", category_field: str = "",
    level: str = "", level_field: str = "",
    org_division: str = "", org_division_field: str = "",
) -> dict:  # returns {"wbs_url", "master_url", "ws_url"}
```

### Hard-coded master DB IDs (config.py)
```python
MASTER_DB_ID        = "2de3b2f3d9b74481bc88511ea94de45e"   # 📋 Master WBS Tasks
PROJECTS_DB_ID      = "01705badbb854f019baf7d0ec68b8c7d"   # 📁 Projects
WORK_SESSIONS_DB_ID = "308c193fbba34a1ebe8d817fd72e9d9a"   # ⏱️ Work Sessions
```

### Property payload builders (notion_client.py, module-level)
```python
p_title(v)   # → {"title": [{"text": {"content": v}}]}
p_text(v)    # → {"rich_text": [{"text": {"content": v}}]}
p_select(v)  # → {"select": {"name": v}}
p_date(v)    # → {"date": {"start": v}}
extract(prop) # → typed value from a Notion property dict
```

---

## Adding a new field end-to-end (7 touchpoints)

When adding a new mappable Quick Start field, touch these 7 places in order.
Read only these specific files/ranges — do not read whole files.

### 1. `MASTER_FIELDS` (index.html ~line 960) — makes field appear in Sources mapping tab
```js
{key:"myfield", label:"My Field Label", req:false},
```

### 2. Backend API for options (task_routes.py)
- **Select field**: extend `/api/wbs-categories` (lines 26–51) or add a parallel route
- **Text field with history**: extend `/api/wbs-text-options` (lines 52–76)

### 3. `quick_add_task()` signature (tasks.py line 27) — add two parameters
```python
myfield: str = "", myfield_field: str = "",
```

### 4. `quick_add_task()` body (tasks.py ~line 60–90) — write value to WBS source
```python
if myfield and myfield_field:
    wbs_props[myfield_field] = p_select(myfield)   # or p_text(myfield)
```
Add to `master_props` only if Master WBS Tasks has this column.

### 5. `/api/quick-add` route (task_routes.py lines 77–131) — extract from body and forward
```python
myfield       = body.get("myfield", "").strip()
myfield_field = body.get("myfield_field", "").strip()
# add to quick_add_task() call at the bottom
```

### 6. Quick Start HTML (index.html ~line 476–555) — add the UI panel
Follow the `qs-level-group` (select + "Add new") or `qs-orgdiv-group` (text combobox) pattern:
```html
<div class="field-group" id="qs-myfield-group" style="display:none;">
  <label>My Field Label <span style="font-weight:400;color:#888;">(optional)</span></label>
  <select id="qs-myfield">
    <option value="">— loading… —</option>
  </select>
</div>
```

### 7a. `onQsSourceChange()` (index.html ~line 1416) — show/hide panel, fetch options
```js
const myfieldField = src?.field_map?.myfield || "";
const myfieldGroup = document.getElementById("qs-myfield-group");
if (!myfieldField) {
  myfieldGroup.style.display = "none";
} else {
  myfieldGroup.style.display = "block";
  // fetch options from /api/wbs-categories or /api/wbs-text-options
}
```

### 7b. `runQuickAdd()` (index.html ~line 1490) — collect value and add to POST payload
```js
const myfield = document.getElementById("qs-myfield").value || "";
// add to the api() payload:
myfield:       myfield,
myfield_field: src.field_map?.myfield || "",
```

---

## Common edit patterns

**Fix sync bug** → Read `focal/sync_engine.py` lines 300–512 (`sync_one_database`).

**Change what Quick Add writes** → Read `focal/tasks.py` lines 27–126.

**Add a Flask route** → Add to the appropriate Blueprint file. Follow the existing pattern in `task_routes.py` or `sync_routes.py`.

**Change Quick Start form layout** → Read `index.html` lines 469–560 (HTML) and 1416–1560 (JS handlers).

**Add a new work type** → Update `VALID_WORK_TYPES` in `focal/config.py` AND `#qs-work-type` select in `index.html` (~line 531).

**Change sync polling behavior** → Read `focal/routes/sync_routes.py` — `_run_full_sync()` is the shared logic used by both blocking and background sync routes.

---

## Edit hygiene

- All sync functions take `client: NotionClient` as first arg — never pass a raw token string to them.
- Use `p_title`, `p_text`, `p_select`, `p_date` from `notion_client.py` — don't build raw Notion property dicts inline.
- `patch_page()` returns a raw `Response` object — check `.status_code` before raising. `create_page()` raises immediately.
- Config key names (`field_map` keys in `focal_config.json`) must match exactly: the `MASTER_FIELDS` `key`, the `focal_config.json` field_map dict key, and the `quick_add_task()` parameter name.
- Templates are Jinja2 files in `focal/templates/` — use `render_template("index.html")`, not `render_template_string()`.
