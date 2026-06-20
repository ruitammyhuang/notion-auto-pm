"""
restore_master_deprecated_cols.py
────────────────────────────────────────────────────────────────────────────────
Restore one or more deleted columns to Master WBS Tasks from a backup file
created by backup_master_deprecated_cols.py.

Usage:
    # Restore ALL 6 deprecated columns:
    python3 restore_master_deprecated_cols.py master_deprecated_backup_<timestamp>.json

    # Restore specific columns only:
    python3 restore_master_deprecated_cols.py master_deprecated_backup_<timestamp>.json \
        --cols "Category" "Notes"

What this script does:
  1. Re-adds the column definition to the Master WBS Tasks database (if absent)
  2. Re-writes the backed-up values to each page that had a non-null value

Note on "stale" columns:
  Work Type, Priority, Planned End, Planned Start values in the backup are from
  the old v2 system. Restoring them to Master WBS Tasks brings back the stale
  data — it does NOT re-sync from WBS source databases. This is intentional:
  the restore is a safety net, not a live sync.
"""

import json
import sys
import time
from pathlib import Path

import requests

# ── Setup ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "focal_config.json"

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

TOKEN = cfg["token"]
HEADERS = {
    "Authorization":  f"Bearer {TOKEN}",
    "Content-Type":   "application/json",
    "Notion-Version": "2022-06-28",
}
NOTION_API   = "https://api.notion.com/v1"
MASTER_DB_ID = "2de3b2f3d9b74481bc88511ea94de45e"

# Column definitions for re-creation
COL_DEFINITIONS: dict[str, dict] = {
    "Work Type":    {"select": {}},
    "Priority":     {"select": {}},
    "Category":     {"rich_text": {}},
    "Notes":        {"rich_text": {}},
    "Planned End":  {"date": {}},
    "Planned Start":{"date": {}},
}

# Notion color for select options (restored without the old options list — Notion
# will auto-create colors as values are written back)
SELECT_OPTION_COLORS: dict[str, str] = {
    # Legacy Work Type values
    "🔵 Deep Work":      "blue",
    "🟡 Meeting & Call": "yellow",
    "🟠 Admin & Ops":    "orange",
    "🟢 Communication":  "green",
    # Legacy Priority values
    "Urgent": "red",
    "High":   "orange",
    "Normal": "blue",
    "Low":    "gray",
}


def build_prop_payload(col: str, value: str) -> dict:
    """Build a Notion property patch payload for the given column and value."""
    if col in ("Work Type", "Priority"):
        return {"select": {"name": value}}
    if col in ("Category", "Notes"):
        return {"rich_text": [{"text": {"content": value}}]}
    if col in ("Planned End", "Planned Start"):
        return {"date": {"start": value}}
    return {}


# ── Parse args ────────────────────────────────────────────────────────────────
args = sys.argv[1:]
if not args:
    print("Usage: python3 restore_master_deprecated_cols.py <backup_file> [--cols col1 col2 ...]")
    sys.exit(1)

backup_path = BASE_DIR / args[0]
if not backup_path.exists():
    print(f"Error: backup file not found: {backup_path}")
    sys.exit(1)

# Parse --cols flag
cols_to_restore = None
if "--cols" in args:
    idx = args.index("--cols")
    cols_to_restore = args[idx + 1:]
    if not cols_to_restore:
        print("Error: --cols requires at least one column name")
        sys.exit(1)

with open(backup_path) as f:
    backup = json.load(f)

available_cols = backup.get("deprecated_cols", [])
rows = backup.get("rows", [])

if cols_to_restore is None:
    cols_to_restore = available_cols

# Validate requested cols
invalid = [c for c in cols_to_restore if c not in available_cols]
if invalid:
    print(f"Error: columns not found in backup: {invalid}")
    print(f"Available: {available_cols}")
    sys.exit(1)

print(f"Backup:  {backup_path.name}")
print(f"Created: {backup.get('created_at', '?')}")
print(f"Rows:    {len(rows)}")
print(f"Columns to restore: {cols_to_restore}")
print()


# ── Step 1: Re-add column definitions to Master WBS Tasks DB ─────────────────
print("Step 1: Re-adding column definitions…")

patch_payload: dict = {"properties": {}}
for col in cols_to_restore:
    col_def = COL_DEFINITIONS.get(col)
    if col_def:
        patch_payload["properties"][col] = col_def
    else:
        print(f"  ⚠️  No column definition for '{col}' — skipping")

if patch_payload["properties"]:
    r = requests.patch(
        f"{NOTION_API}/databases/{MASTER_DB_ID}",
        headers=HEADERS,
        json=patch_payload,
        timeout=15,
    )
    if r.status_code == 200:
        print(f"  ✓ Column definitions added/confirmed: {list(patch_payload['properties'].keys())}")
    else:
        print(f"  ✗ Failed to add columns: HTTP {r.status_code}")
        print(f"    {r.text[:300]}")
        print("  Cannot continue without column definitions.")
        sys.exit(1)
else:
    print("  Nothing to add.")

time.sleep(1)  # Let Notion stabilize the schema

# ── Step 2: Re-write values to pages ─────────────────────────────────────────
print(f"\nStep 2: Re-writing values to {len(rows)} pages…")

restored = {col: 0 for col in cols_to_restore}
skipped  = 0
errors   = 0

for row in rows:
    page_id   = row["page_id"]
    task_name = row.get("task_name", page_id[:8])

    props_to_patch: dict = {}
    for col in cols_to_restore:
        value = row.get(col)
        if value:
            props_to_patch[col] = build_prop_payload(col, value)

    if not props_to_patch:
        skipped += 1
        continue

    for attempt in range(2):
        try:
            r = requests.patch(
                f"{NOTION_API}/pages/{page_id}",
                headers=HEADERS,
                json={"properties": props_to_patch},
                timeout=15,
            )
            if r.status_code == 200:
                for col in props_to_patch:
                    restored[col] += 1
                break
            else:
                if attempt == 0:
                    time.sleep(2)
                    continue
                print(f"  ✗ {task_name[:45]}: HTTP {r.status_code}")
                errors += 1
                break
        except requests.exceptions.Timeout:
            if attempt == 0:
                time.sleep(3)
                continue
            print(f"  ✗ {task_name[:45]}: timeout")
            errors += 1

    time.sleep(0.15)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'─' * 60}")
print("Restore complete")
print(f"  Skipped (no values to restore): {skipped}")
print(f"  Errors:                         {errors}")
print(f"\n  Values restored per column:")
for col, count in restored.items():
    print(f"    {col:<16} {count:>4} pages")

if errors == 0:
    print("\n✅ Restore finished with no errors.")
else:
    print(f"\n⚠️  {errors} page(s) failed — check output above and re-run if needed.")
