"""
backup_master_deprecated_cols.py
────────────────────────────────────────────────────────────────────────────────
Snapshot the values of the 6 deprecated columns from every page in Master WBS
Tasks before you delete them in Notion.

Deprecated columns captured:
  • Work Type    (select)
  • Priority     (select)
  • Category     (rich_text)
  • Notes        (rich_text)
  • Planned End  (date)
  • Planned Start (date)

Output: master_deprecated_backup_<timestamp>.json

Run BEFORE deleting any column in Notion:
    python3 backup_master_deprecated_cols.py

The companion restore script (restore_master_deprecated_cols.py) can read this
file to re-add columns and re-populate values if anything goes wrong.
"""

import json
import sys
import time
from datetime import datetime
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

DEPRECATED_COLS = ["Work Type", "Priority", "Category", "Notes", "Planned End", "Planned Start"]


def extract(prop: dict) -> str | None:
    """Extract a string value from a Notion property dict."""
    ptype = prop.get("type")
    if ptype == "select":
        sel = prop.get("select")
        return sel["name"] if sel else None
    if ptype == "rich_text":
        parts = prop.get("rich_text", [])
        return "".join(p.get("plain_text", "") for p in parts) or None
    if ptype == "date":
        d = prop.get("date")
        return d["start"] if d else None
    if ptype == "title":
        parts = prop.get("title", [])
        return "".join(p.get("plain_text", "") for p in parts) or None
    return None


# ── Paginate all Master WBS Tasks pages ───────────────────────────────────────
print(f"Fetching all pages from Master WBS Tasks…")

pages = []
cursor = None
page_num = 0

while True:
    body: dict = {"page_size": 100}
    if cursor:
        body["start_cursor"] = cursor

    r = requests.post(
        f"{NOTION_API}/databases/{MASTER_DB_ID}/query",
        headers=HEADERS,
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    batch = data.get("results", [])
    pages.extend(batch)
    page_num += 1
    print(f"  Page {page_num}: fetched {len(batch)} rows (total so far: {len(pages)})")

    if not data.get("has_more"):
        break
    cursor = data.get("next_cursor")
    time.sleep(0.3)

print(f"\nTotal pages fetched: {len(pages)}")

# ── Extract deprecated field values ───────────────────────────────────────────
backup_rows = []
non_empty_by_col: dict[str, int] = {col: 0 for col in DEPRECATED_COLS}

for page in pages:
    page_id = page["id"]
    props   = page.get("properties", {})

    # Task name for readability
    task_name = extract(props.get("Task Name", {})) or ""

    row: dict = {
        "page_id":   page_id,
        "task_name": task_name,
        "archived":  page.get("archived", False),
    }

    for col in DEPRECATED_COLS:
        val = extract(props.get(col, {}))
        row[col] = val
        if val:
            non_empty_by_col[col] += 1

    backup_rows.append(row)

# ── Save backup ────────────────────────────────────────────────────────────────
timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_file = BASE_DIR / f"master_deprecated_backup_{timestamp}.json"

backup_data = {
    "created_at":     datetime.now().isoformat(),
    "master_db_id":   MASTER_DB_ID,
    "total_pages":    len(backup_rows),
    "deprecated_cols": DEPRECATED_COLS,
    "rows":           backup_rows,
}

with open(backup_file, "w") as f:
    json.dump(backup_data, f, indent=2, ensure_ascii=False)

# ── Report ─────────────────────────────────────────────────────────────────────
print(f"\nBackup saved: {backup_file.name}")
print(f"\nNon-empty values captured per column:")
for col, count in non_empty_by_col.items():
    pct = round(100 * count / len(backup_rows)) if backup_rows else 0
    note = ""
    if col in ("Work Type", "Priority", "Planned End"):
        note = "  ← stale v2 data (current values live in WBS source DBs)"
    elif col == "Planned Start":
        note = "  ← stale v2 data"
    print(f"  {col:<16} {count:>4}/{len(backup_rows)} ({pct:>3}%){note}")

print(f"""
✅ Backup complete.

To restore any deleted column, run:
    python3 restore_master_deprecated_cols.py {backup_file.name}
""")
