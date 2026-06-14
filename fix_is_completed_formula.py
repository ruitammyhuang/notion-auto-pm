"""
fix_is_completed_formula.py
────────────────────────────────────────────────────────────────
Fixes two broken formulas in the Auto Status chain.

WORKFLOW:
  - A task may have multiple Work Sessions (one per work block).
  - The user sets Status = "Completed" on a Work Session only
    when the ENTIRE task is done (typically the final session).
  - Session End being set does NOT mean the task is complete —
    it just means that work block ended.

BUG 1 — Work Sessions: `Is Completed` formula
  Was: not empty(prop("Session End"))   ← wrong field, wrong type
  Fix: if(prop("Status") == "Completed", 1, 0)
  Why: Returns a NUMBER (1 or 0) so that the Master DB rollup
       `sum` aggregation works correctly. Booleans sum to 0 in
       Notion even when true.

BUG 2 — Master WBS Tasks: `Auto Status` formula
  Was: if(Completed Sessions == Total Sessions, "Completed", ...)
       ← requires ALL sessions to be "Completed", never right
         when a task has multiple work blocks
  Fix: if(Completed Sessions >= 1, "Completed", ...)
  Why: Task is done when ANY session is marked "Completed"
       (the user marks the final work block as done).

Run:  python3 fix_is_completed_formula.py
"""

import json
import requests
from pathlib import Path

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "focal_config.json"

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

WS_DB_ID     = "308c193fbba34a1ebe8d817fd72e9d9a"   # ⏱️ Work Sessions
MASTER_DB_ID = "2de3b2f3d9b74481bc88511ea94de45e"   # 📋 Master WBS Tasks

with open(CONFIG_FILE) as f:
    cfg = json.load(f)
TOKEN = cfg["token"]

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": NOTION_VERSION,
}

# ── Fix 1: Is Completed formula on Work Sessions ──────────────────────────────
IS_COMPLETED_FORMULA = 'if(prop("Status") == "Completed", 1, 0)'

print("Fix 1: Updating 'Is Completed' formula on Work Sessions …")
r1 = requests.patch(
    f"{NOTION_API}/databases/{WS_DB_ID}",
    headers=HEADERS,
    json={
        "properties": {
            "Is Completed": {
                "formula": {"expression": IS_COMPLETED_FORMULA}
            }
        }
    }
)
if r1.status_code == 200:
    print(f"  ✓ Is Completed = {IS_COMPLETED_FORMULA}")
else:
    print(f"  ✗ Failed: HTTP {r1.status_code}")
    print(f"    {r1.text[:300]}")

# ── Fix 2: Auto Status formula on Master WBS Tasks ────────────────────────────
AUTO_STATUS_FORMULA = (
    'if(prop("Total Sessions") == 0, "Not Started", '
    'if(prop("Completed Sessions") >= 1, "Completed", "In Progress"))'
)

print("\nFix 2: Updating 'Auto Status' formula on Master WBS Tasks …")
r2 = requests.patch(
    f"{NOTION_API}/databases/{MASTER_DB_ID}",
    headers=HEADERS,
    json={
        "properties": {
            "Auto Status": {
                "formula": {"expression": AUTO_STATUS_FORMULA}
            }
        }
    }
)
if r2.status_code == 200:
    print(f"  ✓ Auto Status = {AUTO_STATUS_FORMULA}")
else:
    print(f"  ✗ Failed: HTTP {r2.status_code}")
    print(f"    {r2.text[:300]}")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
if r1.status_code == 200 and r2.status_code == 200:
    print("Both fixes applied. Notion will now recalculate:")
    print("  Work Session 'Status' = 'Completed'  →  Is Completed = 1")
    print("  Master 'Completed Sessions' = sum(Is Completed)")
    print("  Master 'Auto Status' = 'Completed' if any session is done")
    print()
    print("Refresh your Notion views — tasks should update within ~30 seconds.")
else:
    print("One or more fixes failed — check errors above.")
