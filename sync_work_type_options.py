"""
sync_work_type_options.py
──────────────────────────────────────────────────────────────────────────────
Push Work Type select options to every Notion database that uses the column.

Single source of truth: WORK_TYPE_OPTIONS in focal/config.py.

Whenever you want to add, rename, or reorder a Work Type category:
  1. Edit WORK_TYPE_OPTIONS in focal/config.py  (name + color).
  2. Run:  python3 sync_work_type_options.py

That's it. This script:
  - Reads WORK_TYPE_OPTIONS from focal/config.py
  - Pushes the options to every Project WBS database (from focal_config.json)
    that has "work_type" in its field_map
  - Pushes to the Work Sessions database
  - Reports success/failure per database

Note on existing page values:
  Notion preserves cell values even when options are removed from the list,
  but those values become "orphaned" (no longer selectable from the picker).
  After a rename run, re-open affected pages in Notion and re-select the new
  matching option.  Values from the old 4-category system (Deep Work, etc.)
  will be orphaned and should be re-classified as you revisit those sessions.

Run:  python3 sync_work_type_options.py
"""

import json
import sys
from pathlib import Path

import requests

# ── Load app config + work type options ───────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "focal_config.json"

sys.path.insert(0, str(BASE_DIR))
from focal.config import WORK_TYPE_OPTIONS, VALID_WORK_TYPES, WORK_SESSIONS_DB_ID

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

TOKEN = cfg["token"]
HEADERS = {
    "Authorization":  f"Bearer {TOKEN}",
    "Content-Type":   "application/json",
    "Notion-Version": "2022-06-28",
}

NOTION_API = "https://api.notion.com/v1"

print("Work Type options to push:")
for opt in WORK_TYPE_OPTIONS:
    print(f"  {opt['name']}  ({opt['color']})")
print()


def patch_select_options(db_id: str, col_name: str) -> bool:
    """Replace the select options for col_name on a Notion database."""
    payload = {
        "properties": {
            col_name: {
                "select": {"options": WORK_TYPE_OPTIONS}
            }
        }
    }
    r = requests.patch(f"{NOTION_API}/databases/{db_id}",
                       headers=HEADERS, json=payload)
    if r.status_code == 200:
        return True
    else:
        print(f"    ✗ HTTP {r.status_code}: {r.text[:200]}")
        return False


# ── 1. Work Sessions database ──────────────────────────────────────────────────
print(f"Work Sessions DB ({WORK_SESSIONS_DB_ID[:8]}…)")
ok = patch_select_options(WORK_SESSIONS_DB_ID, "Work Type")
print(f"  {'✓' if ok else '✗'} Work Type options updated")

# ── 2. Every WBS database that has work_type mapped ───────────────────────────
print()
print("Project WBS databases:")

sources  = cfg.get("sources", {})
ok_count = 0
fail_count = 0

for db_id, src in sources.items():
    col_name = src.get("field_map", {}).get("work_type", "")
    if not col_name:
        continue   # database has no Work Type column — skip

    title = src.get("db_title", db_id[:8])
    print(f"\n  {title}")
    ok = patch_select_options(db_id, col_name)
    print(f"  {'✓' if ok else '✗'} '{col_name}' options updated")
    if ok:
        ok_count += 1
    else:
        fail_count += 1

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print(f"Done — {ok_count} WBS databases updated, {fail_count} failed.")
print()
print("Current Work Type options in effect:")
for opt in WORK_TYPE_OPTIONS:
    print(f"  {opt['name']}")
print()
print("To add a new category in future:")
print("  1. Open focal/config.py")
print("  2. Add an entry to WORK_TYPE_OPTIONS  (name + Notion color)")
print("  3. Run:  python3 sync_work_type_options.py")
