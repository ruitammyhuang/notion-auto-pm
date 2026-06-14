#!/usr/bin/env python3
"""
fill_missing_task_links.py
──────────────────────────
Finds Work Sessions that have no Task relation and fills it in by matching
the session name to the corresponding Master WBS Task title.

MATCHING RULE
  Session name: "Grade M2 Individual Assignment - 2"
  → Strip trailing " - <number>" suffix → "Grade M2 Individual Assignment"
  → Find the Master WBS Task whose title equals that string (case-insensitive)
  → Set Task relation on the Work Session to that task's page ID

RUN FROM TERMINAL
  cd /Users/rui.huang/Documents/Claude/Projects/Notion_Auto_PM
  python3 fill_missing_task_links.py

OPTIONS
  --dry-run    Print what would be updated without making any changes
"""

import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("❌  Please install requests:  pip3 install requests")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG     = os.path.join(BASE_DIR, "focal_config.json")
NOTION_API = "https://api.notion.com/v1"
NOTION_VER = "2022-06-28"

WORK_SESSIONS_DB = "308c193fbba34a1ebe8d817fd72e9d9a"
MASTER_WBS_DB    = "2de3b2f3d9b74481bc88511ea94de45e"
RATE_SLEEP       = 0.35

DRY_RUN = "--dry-run" in sys.argv


def hdrs(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VER,
        "Content-Type": "application/json",
    }


def query_all(token, db_id, extra_filter=None):
    """Return all pages from a Notion database (handles pagination)."""
    pages, body = [], {"page_size": 100}
    if extra_filter:
        body["filter"] = extra_filter
    url = f"{NOTION_API}/databases/{db_id}/query"
    while True:
        resp = requests.post(url, headers=hdrs(token), json=body)
        if not resp.ok:
            print(f"  ⚠️  Query error {resp.status_code}: {resp.text[:200]}")
            break
        data = resp.json()
        pages.extend(data.get("results", []))
        if data.get("has_more"):
            body["start_cursor"] = data["next_cursor"]
        else:
            break
        time.sleep(RATE_SLEEP)
    return pages


def get_title(page):
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return ""


def base_name(session_name):
    """Strip trailing ' - <number>' suffix to get the task name."""
    return re.sub(r"\s*-\s*\d+\s*$", "", session_name).strip()


def main():
    with open(CONFIG) as f:
        token = json.load(f).get("token", "").strip()
    if not token:
        sys.exit("❌  No token found in focal_config.json")

    if DRY_RUN:
        print("🔍  DRY RUN — no changes will be made\n")

    # ── Step 1: fetch all Master WBS Tasks, build title → page_id index ──────
    print("📋  Loading Master WBS Tasks…")
    master_pages = query_all(token, MASTER_WBS_DB)
    master_index: dict[str, str] = {}   # lowercase title → page_id
    for p in master_pages:
        title = get_title(p).strip()
        if title:
            master_index[title.lower()] = p["id"]
    print(f"     {len(master_index)} tasks indexed\n")

    # ── Step 2: fetch Work Sessions with empty Task relation ──────────────────
    print("⏱️   Loading Work Sessions with no Task link…")
    empty_task_filter = {
        "property": "Task",
        "relation": {"is_empty": True}
    }
    sessions = query_all(token, WORK_SESSIONS_DB, extra_filter=empty_task_filter)
    print(f"     {len(sessions)} sessions found with empty Task\n")

    if not sessions:
        print("✅  Nothing to fix — all Work Sessions already have a Task link.")
        return

    # ── Step 3: match and update ──────────────────────────────────────────────
    updated    = 0
    no_match   = []

    for sess in sessions:
        sess_id   = sess["id"]
        sess_name = get_title(sess).strip()
        task_name = base_name(sess_name)
        task_key  = task_name.lower()

        master_id = master_index.get(task_key)
        if not master_id:
            no_match.append(sess_name)
            continue

        print(f"  {'[DRY RUN] ' if DRY_RUN else ''}✅  \"{sess_name}\"  ->  Task: \"{task_name}\"")

        if not DRY_RUN:
            resp = requests.patch(
                f"{NOTION_API}/pages/{sess_id}",
                headers=hdrs(token),
                json={"properties": {"Task": {"relation": [{"id": master_id}]}}},
            )
            if not resp.ok:
                print(f"     ⚠️  Update failed: {resp.status_code} {resp.text[:120]}")
            else:
                updated += 1
            time.sleep(RATE_SLEEP)
        else:
            updated += 1

    print()
    print("═" * 60)
    if DRY_RUN:
        print(f"🔍  Would update: {updated}")
    else:
        print(f"✅  Updated:      {updated}")
    print(f"⚪  No match:     {len(no_match)}")

    if no_match:
        print("\nSessions with no matching Master WBS Task:")
        for name in no_match:
            print(f"  • {name}")
        print("\nFor these, the Task relation must be set manually in Notion.")

    print("\nDone!")


if __name__ == "__main__":
    main()
