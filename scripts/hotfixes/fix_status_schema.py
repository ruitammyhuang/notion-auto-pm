"""
fix_status_schema.py
────────────────────────────────────────────────────────────────
Reworks the Status → Auto Status chain to match the actual
multi-session workflow:

  STATUS OPTIONS (Work Sessions):
    "In Progress"    – currently working on this block
    "Session Done"   – this block ended; task continues (new
                       continuation session will be auto-created)
    "Task Completed" – the entire task is done

  IS COMPLETED formula (Work Sessions):
    if(prop("Status") == "Task Completed", 1, 0)
    → 1 only when the user explicitly marks the task fully done

  AUTO STATUS formula (Master WBS Tasks):
    if(Total Sessions == 0,        "Not Started",
    if(Completed Sessions >= 1,    "Completed",
                                   "In Progress"))
    → Completed as soon as any session is marked "Task Completed"

Changes made:
  1. Rename Status option "Completed" → "Task Completed"
     (existing data migrates automatically — same option ID)
  2. Add Status option "Session Done" (blue)
  3. Update Is Completed formula on Work Sessions DB
  4. Update Auto Status formula on Master WBS Tasks DB

Run:  python3 fix_status_schema.py
"""

import json
import requests
from pathlib import Path

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "focal_config.json"

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
WS_DB_ID       = "308c193fbba34a1ebe8d817fd72e9d9a"
MASTER_DB_ID   = "2de3b2f3d9b74481bc88511ea94de45e"

with open(CONFIG_FILE) as f:
    TOKEN = json.load(f)["token"]

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type":  "application/json",
    "Notion-Version": NOTION_VERSION,
}

# ── Step 1: Update Status select options ──────────────────────────────────────
# The existing "Completed" option ID must be preserved so existing page values
# automatically show the new name "Task Completed".
# We fetch the current options first to get their IDs.

print("Fetching current Work Sessions DB schema …")
db_resp = requests.get(f"{NOTION_API}/databases/{WS_DB_ID}", headers=HEADERS)
if db_resp.status_code != 200:
    print(f"✗ Could not fetch DB: {db_resp.status_code}\n{db_resp.text[:300]}")
    raise SystemExit(1)

db_schema = db_resp.json()
status_prop = db_schema["properties"].get("Status", {})
existing_options = status_prop.get("select", {}).get("options", [])

print(f"  Found {len(existing_options)} existing Status options: "
      f"{[o['name'] for o in existing_options]}")

# Build updated options list:
# - Rename "Completed" → "Task Completed" (keep its id)
# - Add "Session Done" if not already present
# - Keep "In Progress" unchanged
updated_options = []
has_session_done   = False
has_task_completed = False

for opt in existing_options:
    if opt["name"] == "Completed":
        updated_options.append({"id": opt["id"], "name": "Task Completed", "color": "green"})
        has_task_completed = True
    elif opt["name"] == "Task Completed":
        updated_options.append(opt)
        has_task_completed = True
    elif opt["name"] == "Session Done":
        updated_options.append(opt)
        has_session_done = True
    else:
        updated_options.append(opt)

if not has_session_done:
    updated_options.insert(1, {"name": "Session Done", "color": "blue"})

if not has_task_completed:
    updated_options.append({"name": "Task Completed", "color": "green"})

print("\nStep 1: Updating Status options on Work Sessions …")
r1 = requests.patch(
    f"{NOTION_API}/databases/{WS_DB_ID}",
    headers=HEADERS,
    json={
        "properties": {
            "Status": {
                "select": {"options": updated_options}
            }
        }
    }
)
if r1.status_code == 200:
    new_names = [o["name"] for o in r1.json()["properties"]["Status"]["select"]["options"]]
    print(f"  ✓ Status options now: {new_names}")
else:
    print(f"  ✗ Failed: HTTP {r1.status_code}\n  {r1.text[:300]}")

# ── Step 2: Update Is Completed formula on Work Sessions ──────────────────────
IS_COMPLETED = 'if(prop("Status") == "Task Completed", 1, 0)'

print("\nStep 2: Updating 'Is Completed' formula on Work Sessions …")
r2 = requests.patch(
    f"{NOTION_API}/databases/{WS_DB_ID}",
    headers=HEADERS,
    json={"properties": {"Is Completed": {"formula": {"expression": IS_COMPLETED}}}}
)
if r2.status_code == 200:
    print(f"  ✓ Is Completed = {IS_COMPLETED}")
else:
    print(f"  ✗ Failed: HTTP {r2.status_code}\n  {r2.text[:300]}")

# ── Step 3: Update Auto Status formula on Master WBS Tasks ────────────────────
AUTO_STATUS = (
    'if(prop("Total Sessions") == 0, "Not Started", '
    'if(prop("Completed Sessions") >= 1, "Completed", "In Progress"))'
)

print("\nStep 3: Updating 'Auto Status' formula on Master WBS Tasks …")
r3 = requests.patch(
    f"{NOTION_API}/databases/{MASTER_DB_ID}",
    headers=HEADERS,
    json={"properties": {"Auto Status": {"formula": {"expression": AUTO_STATUS}}}}
)
if r3.status_code == 200:
    print(f"  ✓ Auto Status = {AUTO_STATUS}")
else:
    print(f"  ✗ Failed: HTTP {r3.status_code}\n  {r3.text[:300]}")

# ── Summary ───────────────────────────────────────────────────────────────────
all_ok = all(r.status_code == 200 for r in [r1, r2, r3])
print()
if all_ok:
    print("All 3 fixes applied ✓")
    print()
    print("Workflow going forward:")
    print("  Working on task   → Status = 'In Progress'")
    print("  Block ended, more work to do → Status = 'Session Done'")
    print("    (sync tool will auto-create the continuation session)")
    print("  Task fully done   → Status = 'Task Completed'")
    print("    → Master DB Auto Status flips to 'Completed'")
else:
    print("Some fixes failed — check errors above.")
