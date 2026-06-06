---
name: add-wbs-config-entry
description: >
  Use this skill whenever adding, editing, or validating an entry in
  focal_config.json — the file that tells the Python sync tool which WBS
  databases to sync and how their columns map to the master schema. Trigger on:
  "add this WBS to the sync tool", "connect this database to sync", "configure the
  sync for project X", "the sync tool isn't picking up project X", "update the
  field mapping for X", "I created a new WBS and want to sync it", or any request
  to modify focal_config.json. This skill encodes the exact schema so Claude
  writes a correct entry without reading the full config file first. It also covers
  the most common mistakes (wrong key format, missing backlink_field, mismatched
  field names) so they're caught before the sync tool runs.
---

# add-wbs-config-entry

This skill lets you add or update a source entry in `focal_config.json`
without reading the whole file first. The schema is encoded here.

## File location
`/Users/rui.huang/Documents/Claude/Projects/Notion_Auto_PM/focal_config.json`

---

## Config schema

The file has this top-level structure:

```json
{
  "db_filter": "",
  "token": "<notion integration token — never change this>",
  "sources": {
    "<wbs_database_id>": { ... source entry ... },
    "<wbs_database_id>": { ... source entry ... }
  }
}
```

### Source entry schema

```json
"<wbs_database_id>": {
  "project_id": "<Projects DB entry page ID>",
  "field_map": {
    "task_name":     "<column name in the WBS DB>",
    "priority":      "<column name>",
    "planned_start": "<column name>",
    "planned_end":   "<column name>",
    "notes":         "<column name>",
    "work_type":     "<column name>",
    "category":      "<column name>",
    "level":         "<column name>",
    "org_division":  "<column name>"
  },
  "db_title":               "WBS — <Project Name>",
  "work_type_map":          "",
  "backlink_field":         "Master WBS",
  "auto_calc_planned_start": true,
  "hub_page_url":           ""
}
```

---

## Field reference

### Key (the dict key in `sources`)
Must be the **WBS database ID** — the UUID of the WBS database page itself, not
the hub page, not the Projects DB entry. Format: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.
You can get it from Notion by opening the database → Share → Copy link, then
extracting the 32-char hex ID before the `?v=` parameter.

### Required fields

| Field | What it is | Where to find it |
|-------|-----------|-----------------|
| `project_id` | Projects DB entry page ID | From `notion-new-project` skill quick-reference, or search 📁 Projects DB |
| `field_map.task_name` | Name of the Task title column | Almost always `"Task"` |
| `db_title` | Human-readable name shown in the sync UI | Match the actual DB title in Notion, including emoji if present |
| `backlink_field` | Name of the relation column that links back to Master WBS Tasks | Almost always `"Master WBS"` — must match exactly |

### Optional field_map keys

Only include keys where the WBS actually has that column. Omitting a key means
that field won't sync — that's fine and expected.

| Key | Maps to Master WBS field | Notes |
|-----|--------------------------|-------|
| `task_name` | Task Name (title) | Always include |
| `priority` | Priority (select) | Values normalised: Urgent/High/Normal/Low |
| `planned_start` | Planned Start (date) | If omitted, auto-calculated from `planned_end − 7 days` when `auto_calc_planned_start: true` |
| `planned_end` | Planned End (date) | Usually "Due Date" in source WBS |
| `notes` | Notes (rich text) | Usually "Notes" |
| `work_type` | Work Type (select) | Values: 🔵 Deep Work, 🟡 Meeting & Call, 🟠 Admin & Ops, 🟢 Communication |
| `category` | — (not written to Master WBS) | Used for Quick Start category dropdown |
| `level` | — (not written to Master WBS) | Used for Quick Start service level dropdown (Professional Services) |
| `org_division` | — (not written to Master WBS) | Used for Quick Start org/division combobox |

### Other fields

| Field | Default | Notes |
|-------|---------|-------|
| `work_type_map` | `""` | Optional dict mapping source category values → Work Type. E.g. `{"Weekly Announcement": "🟢 Communication"}`. Leave `""` if not needed. |
| `auto_calc_planned_start` | `true` | When true and `planned_start` is missing, sets Planned Start = Planned End − 7 days. |
| `hub_page_url` | `""` | URL of the project hub page. Optional — used for display only in the sync UI. |

---

## Existing entries (current state)

| WBS DB ID | Project | Notes |
|-----------|---------|-------|
| `48cc032e-c2d9-4fdd-98d6-b3c4a42aba27` | Beyond the LXD Label Scoping Review | |
| `53cdd7a1-5f5d-4531-b153-be639ec435c2` | Program Management | has `category` |
| `cd502046-314d-43cb-a2e9-1e4c1f59f2b8` | CS+AI Competency Job Posts Analysis | has `work_type` mapped to "Type" |
| `b4f7bdc6-365a-429b-a240-8d958f888ecd` | EDG 6648 Course Design | has `work_type` mapped to "Task Type" |
| `001e7ca9-a7b8-4180-9dd8-0fc29fa00836` | EDG 6648 Summer 2026 Instruction | has `category` |

---

## How to add a new entry

**Step 1 — Gather the two IDs you need:**
- WBS database ID (the key): get it from the WBS database URL in Notion
- Projects DB entry page ID (`project_id`): from the `notion-new-project` skill
  quick-reference table, or use `notion-search` in 📁 Projects DB

**Step 2 — Find the actual column names in the WBS:**
Use `notion-fetch` on the WBS database (or ask the user) to confirm the exact
column names. Common variations:
- Task title: `"Task"` (most), `"Name"` (default Notion), `"Task Name"`
- Due date: `"Due Date"` (most), `"Deadline"`, `"End Date"`
- Planned start: `"Planned Start"` (most), `"Start Date"`, `"Start"`
- Priority: `"Priority"` (universal)
- Notes: `"Notes"` (most), `"Description"`, `"Comment"`

Column names are case-sensitive and must match exactly.

**Step 3 — Write the entry:**
Read only the `sources` object from the config (not the full file if large),
add the new entry, and write it back. Do not change `token` or `db_filter`.

**Step 4 — Validate before saving:**
Check these before writing:
- [ ] Key is a valid UUID in `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` format
- [ ] `project_id` is a known Projects DB page ID
- [ ] `field_map.task_name` is present (required)
- [ ] `backlink_field` is `"Master WBS"` (unless user says otherwise)
- [ ] No field_map keys have empty string values — omit unmapped fields entirely rather than mapping them to `""`

---

## Common mistakes

**Wrong key format** — the key must be the WBS *database* ID, not the hub page ID
or the Projects DB entry ID. If the user pastes a hub page URL, the database ID
is a different page.

**Mismatch between field_map value and actual column name** — if the sync tool
can't find a column, it silently skips it. Always confirm the exact column name
from the WBS schema, including spaces, slashes, and capitalization.

**Missing backlink_field** — if `backlink_field` is wrong or missing, the sync
tool can't write the back-relation from Project WBS → Master WBS Tasks. This
breaks the bi-directional link. Default is `"Master WBS"`.

**Duplicate entry** — if a WBS DB ID already exists in `sources`, adding it again
creates a duplicate. Check existing entries (table above) before adding.

---

## work_type_map example

If a WBS has a Category column with values like "Weekly Announcement", "1:1
Meeting", "Email Thread" and you want these to auto-map to Work Type on sync:

```json
"work_type_map": {
  "Weekly Announcement": "🟢 Communication",
  "Email Thread": "🟢 Communication",
  "1:1 Meeting": "🟡 Meeting & Call",
  "Deep Work Session": "🔵 Deep Work"
}
```

Values must match one of: `"🔵 Deep Work"`, `"🟡 Meeting & Call"`,
`"🟠 Admin & Ops"`, `"🟢 Communication"`.
