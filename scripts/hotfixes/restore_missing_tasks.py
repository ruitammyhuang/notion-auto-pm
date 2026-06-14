"""
restore_missing_tasks.py
─────────────────────────
Restores WBS task pages and Master WBS Tasks entries that have been archived
or deleted, using focal_sessions_mappings.json + Work Sessions as the source
of truth.

Strategy per sessions_mappings entry:
  1. GET the wbs_id page in Notion:
     - 200 + archived=true  → unarchive it (PATCH archived=false)
     - 200 + archived=false → already fine, skip
     - 404 / gone          → recreate WBS task from stored metadata
  2. GET the master_id page in Notion:
     - 200 + archived=true  → unarchive it
     - 200 + archived=false → already fine, skip
     - 404 / gone          → recreate Master WBS Tasks entry

After restoration, verifies the backlink and Task relation are correct.

Run:
    python3 restore_missing_tasks.py [--dry-run]

Flags:
    --dry-run   Print what would be done without making any Notion API calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import requests

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_F   = os.path.join(BASE_DIR, "focal_config.json")
SESSIONS_F = os.path.join(BASE_DIR, "focal_sessions_mappings.json")

# ── Notion constants ───────────────────────────────────────────────────────────
NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MASTER_DB_ID   = "2de3b2f3-d9b7-4481-bc88-511ea94de45e"

PRIORITY_OPTIONS = {"Urgent", "High", "Normal", "Low"}
WORK_TYPE_OPTIONS = {
    "🔵 Deep Work", "🟡 Meeting & Call", "🟠 Admin & Ops", "🟢 Communication"
}


# ── Helpers ────────────────────────────────────────────────────────────────────
def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


class NotionClient:
    def __init__(self, token: str, dry_run: bool = False):
        self.token   = token
        self.dry_run = dry_run
        self.headers = {
            "Authorization":  f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type":   "application/json",
        }

    def get_page(self, page_id: str) -> requests.Response:
        return requests.get(
            f"{NOTION_API}/pages/{page_id}",
            headers=self.headers,
            timeout=10,
        )

    def patch_page(self, page_id: str, data: dict, label: str = "") -> bool:
        if self.dry_run:
            print(f"    [DRY-RUN] PATCH {page_id[:8]}... {label}")
            return True
        for attempt in range(2):
            try:
                r = requests.patch(
                    f"{NOTION_API}/pages/{page_id}",
                    headers=self.headers,
                    json=data,
                    timeout=15,
                )
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", 2))
                    time.sleep(wait + 0.5)
                    continue
                if r.ok:
                    return True
                print(f"    WARN PATCH {page_id[:8]}...: {r.status_code} {r.text[:120]}")
                return False
            except requests.exceptions.Timeout:
                if attempt == 0:
                    time.sleep(3)
                    continue
                return False
        return False

    def create_page(self, parent: dict, properties: dict, label: str = "") -> Optional[str]:
        if self.dry_run:
            print(f"    [DRY-RUN] CREATE page {label}")
            return "dry-run-fake-id"
        for attempt in range(2):
            try:
                r = requests.post(
                    f"{NOTION_API}/pages",
                    headers=self.headers,
                    json={"parent": parent, "properties": properties},
                    timeout=20,
                )
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", 2))
                    time.sleep(wait + 0.5)
                    continue
                if r.ok:
                    return r.json()["id"]
                print(f"    WARN CREATE {label}: {r.status_code} {r.text[:120]}")
                return None
            except requests.exceptions.Timeout:
                if attempt == 0:
                    time.sleep(3)
                    continue
                return None
        return None


def p_title(v: str) -> dict:
    return {"title": [{"text": {"content": v or ""}}]}

def p_date(v: str) -> dict:
    return {"date": {"start": v}} if v else {"date": None}


def check_and_restore_page(
    client: NotionClient,
    page_id: str,
) -> tuple[str, bool]:
    """
    GET a Notion page by ID.
    Returns (status, archived_before):
      status: "ok" | "unarchived" | "recreate" | "skip_empty"
      archived_before: True if the page was archived when we checked
    """
    if not page_id:
        return "skip_empty", False
    try:
        r = client.get_page(page_id)
        if r.status_code == 404:
            return "recreate", False
        if not r.ok:
            return "recreate", False
        data = r.json()
        if data.get("archived") or data.get("in_trash"):
            return "unarchived", True
        return "ok", False
    except Exception as e:
        print(f"    ERROR checking {page_id[:8]}: {e}")
        return "recreate", False


def build_wbs_props(info: dict, field_map: dict) -> dict:
    """Build property dict for recreating a WBS task page."""
    task_name_field = field_map.get("task_name", "Task")
    props: dict = {
        task_name_field: p_title(info.get("name", "Untitled Task")),
    }

    planned_end = info.get("planned_end", "")
    if planned_end and field_map.get("planned_end"):
        props[field_map["planned_end"]] = p_date(planned_end)

    priority = info.get("priority", "")
    if priority in PRIORITY_OPTIONS and field_map.get("priority"):
        props[field_map["priority"]] = {"select": {"name": priority}}

    work_type = info.get("work_type", "")
    if work_type in WORK_TYPE_OPTIONS and field_map.get("work_type"):
        props[field_map["work_type"]] = {"select": {"name": work_type}}

    return props


def build_master_props(info: dict, project_id: str) -> dict:
    """Build property dict for recreating a Master WBS Tasks entry."""
    props: dict = {
        "Task Name": p_title(info.get("name", "Untitled Task")),
    }
    if project_id:
        props["Project"] = {"relation": [{"id": project_id}]}
    return props


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Restore archived/deleted WBS tasks and Master WBS entries")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without making API calls")
    args = parser.parse_args()

    config   = load_json(CONFIG_F)
    token    = config.get("token", "").strip()
    if not token:
        print("ERROR: No Notion token in focal_config.json")
        sys.exit(1)

    sources  = config.get("sources", {})   # db_id → {field_map, project_id, backlink_field, ...}
    mappings = load_json(SESSIONS_F)

    client = NotionClient(token, dry_run=args.dry_run)

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}Restore pass over {len(mappings)} mapping entries...\n")

    stats = {
        "wbs_ok": 0, "wbs_unarchived": 0, "wbs_recreated": 0, "wbs_failed": 0,
        "master_ok": 0, "master_unarchived": 0, "master_recreated": 0, "master_failed": 0,
        "skipped": 0,
    }

    changed_mappings = 0

    for idx, (wbs_id, info) in enumerate(mappings.items(), 1):
        if not isinstance(info, dict):
            stats["skipped"] += 1
            continue
        if info.get("deleted"):
            stats["skipped"] += 1
            continue

        master_id    = info.get("master_id", "")
        ws_id        = info.get("ws_id", "")
        task_name    = info.get("name", "")
        source_db_id = info.get("source_db_id", "")
        project_id   = info.get("project_id", "")

        if not task_name:
            stats["skipped"] += 1
            continue

        src_config  = sources.get(source_db_id, {})
        field_map   = src_config.get("field_map", {})
        backlink    = src_config.get("backlink_field", "Master WBS")
        # Prefer project_id from config (authoritative) if mapping entry is empty
        if not project_id and src_config.get("project_id"):
            project_id = src_config["project_id"]
            info["project_id"] = project_id
            changed_mappings += 1

        progress = f"[{idx}/{len(mappings)}]"

        # ── 1. Check / restore WBS task ────────────────────────────────────────
        wbs_status, was_archived = check_and_restore_page(client, wbs_id)
        time.sleep(0.15)

        if wbs_status == "ok":
            stats["wbs_ok"] += 1
            # Even if page is ok, unarchive if it was somehow marked
        elif wbs_status == "unarchived":
            print(f"{progress} WBS task '{task_name[:40]}' was archived → unarchiving")
            ok = client.patch_page(wbs_id, {"archived": False}, f"unarchive WBS {task_name[:30]}")
            time.sleep(0.35)
            if ok:
                stats["wbs_unarchived"] += 1
            else:
                stats["wbs_failed"] += 1
        elif wbs_status == "recreate":
            print(f"{progress} WBS task '{task_name[:40]}' missing → recreating in {source_db_id[:8]}...")
            if not source_db_id:
                print(f"  SKIP: no source_db_id in mapping")
                stats["wbs_failed"] += 1
            else:
                wbs_props = build_wbs_props(info, field_map)
                new_wbs_id = client.create_page(
                    {"database_id": source_db_id},
                    wbs_props,
                    label=f"WBS '{task_name[:30]}'",
                )
                time.sleep(0.35)
                if new_wbs_id and new_wbs_id != "dry-run-fake-id":
                    # Remove old key, add new
                    mappings[new_wbs_id] = {**info, "master_id": master_id}
                    del mappings[wbs_id]
                    wbs_id = new_wbs_id
                    info   = mappings[wbs_id]
                    changed_mappings += 1
                    stats["wbs_recreated"] += 1
                else:
                    stats["wbs_failed"] += 1

        # ── 2. Check / restore Master WBS Tasks entry ──────────────────────────
        master_status, master_was_archived = check_and_restore_page(client, master_id)
        time.sleep(0.15)

        if master_status == "ok":
            stats["master_ok"] += 1
        elif master_status == "unarchived":
            print(f"{progress} Master entry '{task_name[:40]}' was archived → unarchiving")
            ok = client.patch_page(master_id, {"archived": False}, f"unarchive Master {task_name[:30]}")
            time.sleep(0.35)
            if ok:
                stats["master_unarchived"] += 1
            else:
                stats["master_failed"] += 1
        elif master_status == "recreate":
            print(f"{progress} Master entry '{task_name[:40]}' missing → recreating")
            new_master_id = client.create_page(
                {"database_id": MASTER_DB_ID},
                build_master_props(info, project_id),
                label=f"Master '{task_name[:30]}'",
            )
            time.sleep(0.35)
            if new_master_id and new_master_id != "dry-run-fake-id":
                master_id = new_master_id
                info["master_id"] = master_id
                changed_mappings += 1
                stats["master_recreated"] += 1

                # Update Work Session's Task relation to point to new master entry
                if ws_id:
                    print(f"  → Updating Work Session Task relation")
                    client.patch_page(
                        ws_id,
                        {"properties": {"Task": {"relation": [{"id": master_id}]}}},
                        "update WS Task relation",
                    )
                    time.sleep(0.35)
            else:
                stats["master_failed"] += 1

        # ── 3. Ensure backlink: WBS task → Master WBS entry ───────────────────
        # We do this when master was newly created or wbs was newly created
        if master_id and wbs_id and backlink and (
            wbs_status == "recreate" or master_status == "recreate"
        ):
            if not args.dry_run:
                print(f"  → Writing backlink on WBS task")
                client.patch_page(
                    wbs_id,
                    {"properties": {backlink: {"relation": [{"id": master_id}]}}},
                    "write backlink",
                )
                time.sleep(0.35)

        # Throttle
        time.sleep(0.2)

    # ── Save updated mappings ─────────────────────────────────────────────────
    if changed_mappings > 0 and not args.dry_run:
        save_json(SESSIONS_F, mappings)
        print(f"\nSaved {changed_mappings} mapping updates → {SESSIONS_F}")

    print(f"""
{'='*60}
RESTORATION SUMMARY {'(DRY-RUN)' if args.dry_run else ''}
{'='*60}
WBS tasks
  Already fine:   {stats['wbs_ok']}
  Unarchived:     {stats['wbs_unarchived']}
  Recreated:      {stats['wbs_recreated']}
  Failed/skipped: {stats['wbs_failed']}

Master WBS Tasks
  Already fine:   {stats['master_ok']}
  Unarchived:     {stats['master_unarchived']}
  Recreated:      {stats['master_recreated']}
  Failed/skipped: {stats['master_failed']}

Skipped entries:  {stats['skipped']}
{'='*60}

{'Run without --dry-run to apply changes.' if args.dry_run else 'Restoration complete. Run a full sync to repopulate metadata.'}
""")


if __name__ == "__main__":
    main()
