---
name: add-wbs-config-entry
description: >
  Load before adding, editing, or validating an entry in focal_config.json --
  the file that tells the sync tool which WBS databases to sync and how their
  columns map to the master schema. Trigger on: "add this WBS to the sync tool",
  "connect this database to sync", "configure the sync for project X", "the sync
  tool is not picking up project X", "update the field mapping for X", "I created
  a new WBS and want to sync it", or any request to modify focal_config.json.
---

# add-wbs-config-entry

This skill lets you add or update a source entry in `focal_config.json`
without reading the whole file first. The schema is encoded here.

## File location
`Notion_Auto_PM/focal_config.json`

IMPORTANT: The file contains a `token` field (Notion integration token).
Never log, echo, or include this value in any output. Edit only the `sources`
object. Do not change `token` or `db_filter`.

## Config schema

```json
{
  "db_filter": "",
  "token": "<do not touch>",
  "sources": {
    "<wbs_database_id>": {
      "auto_calc_planned_start": 7,
      "backlink_field": "Master WBS",
      "db_title": "WBS -- Project Name",
      "field_map": {
        "task_name":     "<column name in WBS DB>",
        "planned_end":   "<column name>",
        "planned_start": "<column name>",
        "priority":      "<column name>",
        "work_type":     "<column name>",
        "notes":         "<column name>",
        "category":      "<column name>"
      },
      "hub_page_url": "",
      "project_id": "<projects_db_entry_page_id>"
    }
  }
}
```

## Field reference

### Key (the dict key in `sources`)
Must be the WBS database ID -- the UUID of the WBS database itself, not the
hub page or Projects DB entry. Get it from Notion: open the database, share,
copy link, extract the 32-char hex ID before `?v=`.
Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

### Required fields

| Field | Description |
|-------|-------------|
| `task_name` in field_map | Column name of the Task title. Almost always "Task". |
| `planned_end` in field_map | Due date column. Usually "Due Date". |
| `project_id` | Projects DB entry page ID for this project. |
| `backlink_field` | Relation column linking WBS row back to Master WBS Tasks. Default: "Master WBS". |
| `db_title` | Human-readable name shown in the sync UI. Match the actual Notion DB title. |

### Optional field_map keys
Only include keys where the WBS DB actually has that column.
Omitting a key means that field will not sync -- that is expected.

| Key | What it maps | Notes |
|-----|-------------|-------|
| `planned_start` | Planned Start (date) | If omitted, auto-calculated as planned_end minus 7 days |
| `priority` | Priority (select) | Values normalised to: Urgent / High / Normal / Low |
| `work_type` | Work Type (select) | Must match WORK_TYPE_OPTIONS names in focal/config.py |
| `notes` | Notes (rich text) | |
| `category` | Category (select) | Used for Quick Start dropdown; not written to Master WBS |

### Special fields (Dissertation Mentoring WBS only)
The `f0c29cac` entry has extra field_map keys: `student_name`, `chair`,
`current_phase`, `degree`, `my_role`, `program`. These are dissertation-specific
and not part of the standard schema.

### `auto_calc_planned_start`
Integer (days). When `planned_start` is not in the field_map, sets
Planned Start = Planned End minus this many days. Default: 7.

### `hub_page_url`
URL of the project hub page. Optional, display only. Leave "" if unknown.

## Existing entries (11 sources as of June 2026)

| WBS DB ID | Project |
|-----------|---------|
| `001e7ca9-a7b8-4180-9dd8-0fc29fa00836` | EDG 6648 Summer 2026 Instruction |
| `48cc032e-c2d9-4fdd-98d6-b3c4a42aba27` | Beyond the LXD Label Scoping Review |
| `53cdd7a1-5f5d-4531-b153-be639ec435c2` | Program Management |
| `54631775-3dac-47db-8b5f-1f7b9aa57073` | EME 6209 Fall 2026 Instruction |
| `720d2e4d-ef4f-47c6-a095-8751449238ae` | EME 6156 Fall 2026 Instruction |
| `79345a33-0f69-4b5c-a4fc-0b94e30854d1` | CS Ed EdD Cohort 2 Year 1 Summer Workshop 2026 |
| `8ca11592-2c3e-4779-a343-37f1756412ec` | AI & PjBL Scoping Review |
| `8d83590f-0a15-4fb4-9ba5-54568c27555b` | Professional Services |
| `b4f7bdc6-365a-429b-a240-8d958f888ecd` | EDG 6648 Course Design |
| `cd502046-314d-43cb-a2e9-1e4c1f59f2b8` | CS+AI Competency Job Posts Analysis |
| `f0c29cac-ec31-45fa-9193-85567f4d0d77` | Dissertation Mentoring (special fields) |

## How to add a new entry

**Step 1 -- Gather two IDs:**
- WBS database ID (the key): from the WBS database URL in Notion
- `project_id`: from the Projects DB entry in Notion (search 📁 Projects)

**Step 2 -- Confirm exact column names in the WBS:**
Column names are case-sensitive. Common variations:
- Task title: "Task" (most), "Name" (Notion default), "Task Name"
- Due date: "Due Date" (most), "Deadline", "End Date"
- Planned start: "Planned Start" (most), "Start Date", "Start"
- Priority: "Priority" (universal)
- Work type: "Work Type" (universal in this system)
- Notes: "Notes" (most), "Description"
- Category: "Category" (most)

**Step 3 -- Write the entry.**
Read only the `sources` object from focal_config.json, add the new entry,
write it back. Do not touch `token` or `db_filter`.

**Step 4 -- Validate before saving:**
- [ ] Key is a valid UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx format)
- [ ] `project_id` is a known Projects DB page ID
- [ ] `field_map.task_name` is present (required)
- [ ] `field_map.planned_end` is present (required)
- [ ] `backlink_field` is "Master WBS" (unless user specifies otherwise)
- [ ] No field_map key maps to an empty string -- omit unmapped fields entirely

## Common mistakes

**Wrong key** -- must be the WBS *database* ID, not the hub page ID or
Projects DB entry ID. These are three different Notion pages.

**Column name mismatch** -- if the sync tool cannot find a column, it silently
skips it. Confirm the exact column name from the WBS schema.

**Missing backlink_field** -- if wrong or absent, the back-relation from
Project WBS to Master WBS Tasks breaks. Default is "Master WBS".

**Duplicate entry** -- check the existing entries table above before adding.
