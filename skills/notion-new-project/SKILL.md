---
name: notion-new-project
description: >
  Use this skill whenever creating a new project in the faculty Notion PM system.
  Trigger on any request like: "create a new project", "set up a new WBS for X",
  "add a new course/research project/service project", "I'm starting a new project
  called X", or any request to scaffold a new hub page + WBS database + Projects DB
  entry. This skill encodes the exact 8-step creation sequence with all database IDs
  pre-loaded, so no memory files or config files need to be read first. ALWAYS load
  this skill before creating any new project — it eliminates the re-derivation cost
  and ensures the hub page structure is built correctly every time.
---

# notion-new-project

This skill encodes the complete sequence for creating a new project in the faculty
Notion PM system. All database IDs and structural rules are pre-loaded here — do
not read memory files or `focal_config.json` before starting.

---

## Pre-loaded IDs (do not look these up)

```
📁 Projects DB         page:       01705badbb854f019baf7d0ec68b8c7d
                       collection: 80ee95ce-1458-4353-aeab-48f3c7e49be8

📋 Master WBS Tasks    db:         2de3b2f3d9b74481bc88511ea94de45e
                       collection: 94fa9ee4-ec0e-4cc3-8a8f-238d2b6a835c

⏱️ Work Sessions       page:       308c193fbba34a1ebe8d817fd72e9d9a
                       collection: b3982f2e-a253-4a3b-8201-c1a84e34e70b

🎑 Research Projects   page:       08ca352548c24f25a54cab4c3ec2f4c7
   DB                  collection: 8608ff79-56c0-47d1-bace-c5d43ee80f1f
```

---

## Step 0: Classify the project

Before creating anything, identify:

| Question | Determines |
|----------|-----------|
| Which work area? (Teaching, Research, Program Mgmt, Professional Services, etc.) | Which section to place hub page under |
| Is it a research project? | Whether it also needs a 🎑 Research Projects DB row |
| Does it have a repeating category structure? (e.g., service levels, course modules) | Whether to add a `category` or `level` field to the WBS schema |

If unsure, ask the user. One question is enough.

---

## Step 1: Create the Projects DB entry

**Why first:** The Projects DB entry ID is needed as the `project_id` in the sync config and as the relation target for Work Sessions. Creating it first means you have the ID to use in later steps.

```
Tool: notion-create-pages
Parent: 01705badbb854f019baf7d0ec68b8c7d  (📁 Projects DB)
Properties:
  - Name: [Project Name]
  - Category: [work area — Teaching, Research, Program Management,
               Professional Services, Self-Learning & PD, Admin & Ops,
               Instructional Design & Development]
  - Status: Active  (or leave blank)
```

**Save the returned page ID** — this is `project_entry_id`. You'll need it for the sync config.

---

## Step 2: Find the parent page for placement

The hub page must be nested under the correct section in the Notion sidebar.

**Known section parent pages** (use `notion-search` if the one you need isn't listed):

| Work area | Where hub page goes |
|-----------|-------------------|
| Research | Under the corresponding 🎑 Research Projects DB row (search by project name) |
| Teaching & Mentoring | Under the Teaching & Mentoring section hub |
| Instructional Design | Under the Instructional Design & Development section hub |
| Program Management | Under the Program Management hub (`36854686ae15814a91d8c1a48cb2e29d`) |
| Professional Services | Under the Professional Services hub |

For research projects: first create or locate the 🎑 Research Projects DB row, then use that row's page ID as the parent.

---

## Step 3: Create the hub page

```
Tool: notion-create-pages
Parent: [section parent page ID from Step 2]
Title: [Project Name]
Icon: 📋 (or appropriate emoji)
```

**Save the returned page ID** — this is `hub_page_id`.

Add an initial content block with a brief project description if the user provided one.

---

## Step 4: Create the WBS database on the hub page

```
Tool: notion-create-database
Parent: hub_page_id
Title: WBS — [Project Name]
```

**Standard WBS schema** — include these properties on every WBS:

| Property | Type | Notes |
|----------|------|-------|
| Task | title | Required |
| Status | select | Options: Not Started, In Progress, Completed, Blocked, On Hold |
| Auto Status | formula or rollup | Set up manually; auto-driven by Work Sessions |
| Priority | select | Options: Urgent, High, Normal, Low |
| Planned Start | date | |
| Due Date | date | |
| Work Type | select | Options: 🔵 Deep Work, 🟡 Meeting & Call, 🟠 Admin & Ops, 🟢 Communication |
| Notes | rich_text | |
| Master WBS | relation | Relates to Master WBS Tasks DB (`2de3b2f3d9b74481bc88511ea94de45e`) |

**Project-specific additions:**
- Teaching/course projects: add `Module` (select) for grouping by module
- Program Management: add `Category` (select) for reactive task types
- Professional Services: add `Level` (select) and `Organization / Division` (rich_text)
- Research: add `Phase` (select: Literature Review, Data Collection, Analysis, Writing, etc.)

**Save the returned database ID** — this is `wbs_db_id` and `wbs_collection_id`.

---

## Step 5: Create the default WBS view

```
Tool: notion-create-view
Parent: hub_page_id
Data source: wbs_db_id
View type: table
Name: All Tasks
```

Optionally set sort: Priority (ascending), then Due Date (ascending).

---

## Step 6: Update the hub page with structured content

```
Tool: notion-update-page
Page: hub_page_id
```

Add these sections in order:
```
## 📋 WBS
[inline WBS database — already embedded from Step 5]

---

## ⏱️ Work Sessions
[inline linked view — created in Step 7]
```

Also add a header section at the top with:
- Project goal / one-line description (if provided)
- Key dates / deadlines (if known)
- Team members (if applicable)

---

## Step 7: Embed the Work Sessions linked view

```
Tool: notion-create-view
Parent: hub_page_id
Data source: b3982f2e-a253-4a3b-8201-c1a84e34e70b  (⏱️ Work Sessions collection)
View type: table
Name: Work Sessions
```

**Important:** The Project filter (Filter → Project → is → [this project]) cannot be set via API. Tell the user:
> "Please open the Work Sessions table on the hub page in Notion and set the filter: Project = [Project Name]. This takes about 10 seconds and only needs to be done once."

---

## Step 8: Add the project to focal_config.json

Edit `/Users/rui.huang/Documents/Claude/Projects/Notion_Auto_PM/focal_config.json`.

Add a new entry under `"sources"` using the `wbs_db_id` as the key:

```json
"<wbs_db_id>": {
  "project_id": "<project_entry_id>",
  "field_map": {
    "task_name": "Task",
    "priority": "Priority",
    "planned_start": "Planned Start",
    "planned_end": "Due Date",
    "notes": "Notes"
  },
  "db_title": "WBS — <Project Name>",
  "work_type_map": "",
  "backlink_field": "Master WBS",
  "auto_calc_planned_start": true,
  "hub_page_url": ""
}
```

**For projects with extra fields**, extend `field_map`:
- Category/Module/Phase field: add `"category": "<field name>"`
- Service Level field: add `"level": "<field name>"`
- Org/Division text field: add `"org_division": "<field name>"`

---

## Final checklist

Before calling done, confirm:
- [ ] Projects DB entry exists with correct Category tag
- [ ] Hub page is nested under the right section
- [ ] WBS database is on the hub page with `Master WBS` relation property
- [ ] Work Sessions linked table is at the bottom of the hub page
- [ ] `focal_config.json` has the new source entry
- [ ] User has been prompted to set the Work Sessions filter manually

---

## Research projects: dual-record rule

Research projects need **two records** serving different purposes:
1. **📁 Projects DB entry** (Step 1) — for Work Sessions time-logging; every project needs this
2. **🎑 Research Projects DB row** — for research-specific tracking (Stage, Team Members, etc.)

The hub page is nested under the 🎑 Research Projects DB row (not under Projects DB).
Both records must exist — they are not duplicates.

---

## Quick reference: existing project IDs

Use these when you need to reference existing projects in the sync config or Work Sessions:

| Project | Projects DB entry ID |
|---------|---------------------|
| EDG 6648 Summer 2026 Instruction | `36b54686-ae15-80ce-a660-d94c22bdc8ed` |
| EDG 6648 Course Design | `36a54686-ae15-80a0-a85c-f536878e61e9` |
| Beyond the LXD Label Scoping Review | `36e54686-ae15-814f-a1d7-db14785cd5fc` |
| CS Ed EdD Cohort 2 Summer Workshop 2026 | `37454686-ae15-819e-83b7-daaba02d7a51` |
| Program Management | `37454686-ae15-817f-a40d-cf340a0dc0b2` |
