#!/usr/bin/env python3
"""
backfill_master_wbs_links.py
─────────────────────────────
Reads focal_mappings.json and sets the "Master WBS" relation on every
source WBS task that is missing it. This fixes the silent write_backlink bug
so that Auto Status rollups on WBS databases show the correct status.

Each mapping entry has:
  source_page_id  → the WBS task (e.g. in EME 6209)
  master_id       → the corresponding Master WBS Tasks page
  db              → the WBS database ID (used for filtering)

RUN FROM TERMINAL
  cd /Users/rui.huang/Documents/Claude/Projects/Notion_Auto_PM
  python3 backfill_master_wbs_links.py             # all databases
  python3 backfill_master_wbs_links.py --dry-run   # preview only
  python3 backfill_master_wbs_links.py --db 54631775-3dac-47db-8b5f-1f7b9aa57073
                                                   # single WBS database only
"""

import json
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Please install requests: pip3 install requests")

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG       = os.path.join(BASE_DIR, "focal_config.json")
MAPPINGS     = os.path.join(BASE_DIR, "focal_mappings.json")
NOTION_API   = "https://api.notion.com/v1"
NOTION_VER   = "2022-06-28"
RATE_SLEEP   = 0.35

DRY_RUN  = "--dry-run" in sys.argv
ONLY_DB  = None
for i, arg in enumerate(sys.argv):
    if arg == "--db" and i + 1 < len(sys.argv):
        ONLY_DB = sys.argv[i + 1].replace("-", "")  # normalize dashes


def hdrs(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VER,
        "Content-Type": "application/json",
    }


def get_backlink_field(config_sources, db_id):
    """Look up the backlink field name for a WBS database from focal_config.json."""
    # focal_config keys may be with or without dashes
    for key, src in config_sources.items():
        if key.replace("-", "") == db_id.replace("-", ""):
            return src.get("backlink_field", "Master WBS")
    return "Master WBS"


def main():
    with open(CONFIG) as f:
        cfg = json.load(f)
    token = cfg.get("token", "").strip()
    if not token:
        sys.exit("No token found in focal_config.json")
    sources = cfg.get("sources", {})

    with open(MAPPINGS) as f:
        mappings = json.load(f)

    if DRY_RUN:
        print("DRY RUN — no changes will be made\n")

    # Build list of entries to backfill
    entries = []
    for src_id, val in mappings.items():
        if not isinstance(val, dict):
            continue
        if val.get("deleted"):
            continue
        master_id = val.get("master_id", "")
        if not master_id:
            continue
        db = val.get("db", "")
        if ONLY_DB and db.replace("-", "") != ONLY_DB.replace("-", ""):
            continue
        entries.append({
            "src_id":    src_id,
            "master_id": master_id,
            "db":        db,
            "name":      val.get("task_name") or src_id[:8] + "...",
        })

    # Group by db for reporting
    by_db = {}
    for e in entries:
        by_db.setdefault(e["db"], []).append(e)

    print(f"Found {len(entries)} entries across {len(by_db)} WBS database(s)\n")
    for db, rows in by_db.items():
        db_title = next((s.get("db_title", db) for k, s in sources.items()
                         if k.replace("-", "") == db.replace("-", "")), db)
        print(f"  {db_title}: {len(rows)} tasks")
    print()

    updated  = 0
    failed   = 0
    skipped  = 0

    for e in entries:
        db      = e["db"]
        src_id  = e["src_id"]
        mst_id  = e["master_id"]
        name    = e["name"]
        bf      = get_backlink_field(sources, db)

        label = "[DRY RUN] " if DRY_RUN else ""
        print(f"  {label}Linking \"{name}\" → Master WBS {mst_id[:8]}...")

        if DRY_RUN:
            updated += 1
            continue

        r = requests.patch(
            f"{NOTION_API}/pages/{src_id}",
            headers=hdrs(token),
            json={"properties": {bf: {"relation": [{"id": mst_id}]}}},
        )
        if r.ok:
            updated += 1
        else:
            print(f"     FAILED {r.status_code}: {r.text[:120]}")
            failed += 1
        time.sleep(RATE_SLEEP)

    print()
    print("=" * 60)
    if DRY_RUN:
        print(f"Would update: {updated}")
    else:
        print(f"Updated:  {updated}")
        print(f"Failed:   {failed}")
    print()
    if not DRY_RUN and updated > 0:
        print("Done. Auto Status rollups on WBS databases will now reflect")
        print("the linked Master WBS Task status automatically.")


if __name__ == "__main__":
    main()
