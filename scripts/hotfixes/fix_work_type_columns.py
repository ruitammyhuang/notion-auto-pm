"""
fix_work_type_columns.py
────────────────────────────────────────────────────────────────────────
One-time fix: establish a consistent "Work Type" select column across all
Project WBS databases so the sync tool can propagate Work Type through to
Work Sessions → Workload Dashboard.

Actions:
  ADD "Work Type" column (new select, 4 standard options) to:
    - WBS — Beyond the LXD Label Scoping Review
    - WBS — CS Ed EdD Cohort 2 Year 1 Summer Workshop 2026
    - WBS — Dissertation Mentoring
    - WBS — Professional Services
    - WBS — Program Management
    - WBS — EDG 6648 Summer 2026 Instruction

  RENAME existing column → "Work Type":
    - WBS — EDG 6648 Course Design:  "Task Type"  → "Work Type"
    - WBS — CS+AI Competency Job Posts Analysis:  "Type" → "Work Type"
      (also replaces the nonsense "phase/task" options with the 4 standard ones)

  UPDATE focal_config.json:
    - Adds/fixes  "work_type": "Work Type"  in field_map for all 8 projects above

Run:  python3 fix_work_type_columns.py
"""

import json
import requests
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "focal_config.json"

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

TOKEN = cfg["token"]
HEADERS = {
    "Authorization":  f"Bearer {TOKEN}",
    "Content-Type":   "application/json",
    "Notion-Version": NOTION_VERSION,
}

# Standard Work Type options (must match VALID_WORK_TYPES in focal/config.py)
WORK_TYPE_OPTIONS = [
    {"name": "🔵 Deep Work",       "color": "blue"},
    {"name": "🟡 Meeting & Call",  "color": "yellow"},
    {"name": "🟠 Admin & Ops",     "color": "orange"},
    {"name": "🟢 Communication",   "color": "green"},
]

# ── Project definitions ────────────────────────────────────────────────────────
# action: "add"    → create new "Work Type" column
# action: "rename" → rename existing column; old_col = current column name
#                    replace_options=True also resets the select options list

PROJECTS = [
    # ── ADD new column ─────────────────────────────────────────────────────────
    {
        "db_id":  "48cc032e-c2d9-4fdd-98d6-b3c4a42aba27",
        "title":  "WBS — Beyond the LXD Label Scoping Review",
        "action": "add",
    },
    {
        "db_id":  "79345a33-0f69-4b5c-a4fc-0b94e30854d1",
        "title":  "WBS — CS Ed EdD Cohort 2 Year 1 Summer Workshop 2026",
        "action": "add",
    },
    {
        "db_id":  "f0c29cac-ec31-45fa-9193-85567f4d0d77",
        "title":  "WBS — Dissertation Mentoring",
        "action": "add",
    },
    {
        "db_id":  "8d83590f-0a15-4fb4-9ba5-54568c27555b",
        "title":  "WBS — Professional Services",
        "action": "add",
    },
    {
        "db_id":  "53cdd7a1-5f5d-4531-b153-be639ec435c2",
        "title":  "WBS — Program Management",
        "action": "add",
    },
    {
        "db_id":  "001e7ca9-a7b8-4180-9dd8-0fc29fa00836",
        "title":  "WBS — EDG 6648 Summer 2026 Instruction",
        "action": "add",
    },
    # ── RENAME existing column ─────────────────────────────────────────────────
    {
        "db_id":   "b4f7bdc6-365a-429b-a240-8d958f888ecd",
        "title":   "WBS — EDG 6648 Course Design",
        "action":  "rename",
        "old_col": "Task Type",
        "replace_options": False,   # keep existing options; user will repopulate
    },
    {
        "db_id":   "cd502046-314d-43cb-a2e9-1e4c1f59f2b8",
        "title":   "WBS — CS+AI Competency Job Posts Analysis",
        "action":  "rename",
        "old_col": "Type",
        "replace_options": True,    # "phase/task" options are meaningless — replace
    },
]


def add_work_type_column(db_id: str, title: str) -> bool:
    """Create a new 'Work Type' select column in the database."""
    payload = {
        "properties": {
            "Work Type": {
                "select": {"options": WORK_TYPE_OPTIONS}
            }
        }
    }
    r = requests.patch(f"{NOTION_API}/databases/{db_id}",
                       headers=HEADERS, json=payload)
    if r.status_code == 200:
        print(f"  ✓ Added 'Work Type' column")
        return True
    else:
        print(f"  ✗ Failed HTTP {r.status_code}: {r.text[:200]}")
        return False


def rename_work_type_column(db_id: str, title: str,
                             old_col: str, replace_options: bool) -> bool:
    """Rename an existing column to 'Work Type', optionally replacing its options."""
    prop_update: dict = {"name": "Work Type"}
    if replace_options:
        prop_update["select"] = {"options": WORK_TYPE_OPTIONS}

    payload = {"properties": {old_col: prop_update}}
    r = requests.patch(f"{NOTION_API}/databases/{db_id}",
                       headers=HEADERS, json=payload)
    if r.status_code == 200:
        suffix = " + replaced options" if replace_options else " (options kept)"
        print(f"  ✓ Renamed '{old_col}' → 'Work Type'{suffix}")
        return True
    else:
        print(f"  ✗ Failed HTTP {r.status_code}: {r.text[:200]}")
        return False


def update_config(db_id: str, old_key: str | None = None) -> None:
    """
    Ensure focal_config.json has  "work_type": "Work Type"
    for this db_id.  Removes old_key if it differs (e.g. "Task Type", "Type").
    """
    src = cfg["sources"].get(db_id)
    if not src:
        print(f"  ⚠  {db_id} not found in focal_config.json — skipping config update")
        return

    fm = src.setdefault("field_map", {})

    # Remove stale key if the column was renamed
    if old_key and old_key != "Work Type":
        for k, v in list(fm.items()):
            if k == "work_type" and v == old_key:
                del fm[k]
                break

    fm["work_type"] = "Work Type"
    print(f"  ✓ focal_config.json: field_map['work_type'] = 'Work Type'")


# ── Main ───────────────────────────────────────────────────────────────────────
print("=" * 60)
print("Fix Work Type columns across all WBS databases")
print("=" * 60)

all_ok = True

for proj in PROJECTS:
    db_id  = proj["db_id"]
    title  = proj["title"]
    action = proj["action"]

    print(f"\n{title}")

    if action == "add":
        ok = add_work_type_column(db_id, title)
        if ok:
            update_config(db_id)
    elif action == "rename":
        old_col  = proj["old_col"]
        replace  = proj.get("replace_options", False)
        ok = rename_work_type_column(db_id, title, old_col, replace)
        if ok:
            update_config(db_id, old_key=old_col)
    else:
        print(f"  ✗ Unknown action '{action}'")
        ok = False

    if not ok:
        all_ok = False

# ── Save updated focal_config.json ────────────────────────────────────────────
print("\n" + "=" * 60)
print("Saving focal_config.json …")
with open(CONFIG_FILE, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print("  ✓ focal_config.json saved")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
if all_ok:
    print("All done ✓")
    print()
    print("Next steps:")
    print("  1. Open EDG 6648 Course Design WBS in Notion and fill in Work Type")
    print("     values for each task (column was renamed from 'Task Type').")
    print("  2. Open CS+AI Competency Job Posts WBS in Notion and fill in Work Type")
    print("     values (old 'phase/task' options have been replaced).")
    print("  3. Run a full sync from the Sync tab to push Work Type from all")
    print("     WBS databases into their linked Work Sessions.")
    print("  4. Reload the Workload Dashboard — 'Unclassified' will shrink to")
    print("     only sessions where you haven't set a Work Type yet.")
else:
    print("Some steps failed — check errors above and re-run.")
