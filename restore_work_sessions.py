#!/usr/bin/env python3
"""
restore_work_sessions.py
─────────────────────────
Unarchives the Work Session pages that were archived alongside the
Master WBS Tasks during the bad sync.

focal_sessions_mappings.json maps:  master_id → session_id
We unarchive each session_id whose master_id is now healthy in
focal_mappings.json.

RUN FROM TERMINAL
  cd /Users/rui.huang/Documents/Claude/Projects/Notion_Auto_PM
  python3 restore_work_sessions.py
"""

import json
import os
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("❌  Please install requests:  pip3 install requests")

BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE       = os.path.join(BASE_DIR, "focal_config.json")
MAPPINGS_FILE     = os.path.join(BASE_DIR, "focal_mappings.json")
SESSIONS_MAP_FILE = os.path.join(BASE_DIR, "focal_sessions_mappings.json")
NOTION_API        = "https://api.notion.com/v1"
NOTION_VERSION    = "2022-06-28"
RATE_SLEEP        = 0.35


def hdrs(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def unarchive(token: str, page_id: str) -> bool:
    resp = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=hdrs(token),
        json={"archived": False},
    )
    return resp.ok


def main() -> None:
    with open(CONFIG_FILE) as f:
        token = json.load(f).get("token", "").strip()
    with open(MAPPINGS_FILE) as f:
        mappings: dict = json.load(f)
    with open(SESSIONS_MAP_FILE) as f:
        sessions: dict = json.load(f)

    # All master IDs that are now healthy (restored or were already fine)
    healthy_masters = {
        v["master_id"]
        for v in mappings.values()
        if isinstance(v, dict) and not v.get("deleted")
    }

    # Session IDs to unarchive: those whose master is now healthy
    to_restore = [
        (mid, sid)
        for mid, sid in sessions.items()
        if mid in healthy_masters
    ]

    print(f"Work sessions to unarchive: {len(to_restore)}")
    print()

    restored = 0
    failed   = 0
    for i, (mid, sid) in enumerate(to_restore, 1):
        print(f"  [{i:>3}/{len(to_restore)}] session {sid[:8]}…", end=" ")
        ok = unarchive(token, sid)
        if ok:
            print("✅")
            restored += 1
        else:
            print("⚠️  failed")
            failed += 1
        time.sleep(RATE_SLEEP)

    print()
    print("═" * 50)
    print(f"✅  Restored: {restored}  ⚠️  Failed: {failed}")
    print()
    print("Done! Your logged work hours should now be visible in Notion.")


if __name__ == "__main__":
    main()
