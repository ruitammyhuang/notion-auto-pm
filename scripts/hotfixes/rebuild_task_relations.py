"""
rebuild_task_relations.py
─────────────────────────
One-time script: restores the Task relation on existing Work Sessions
and merges the two legacy mapping files into the new unified format.

What it does:
  1. Loads focal_mappings.json     {wbs_id → {master_id, db, task_name, ...}}
  2. Loads focal_sessions_mappings.json  {master_id → ws_id}  (old flat format)
  3. Joins them: wbs_id → {master_id, ws_id, source_db_id, name}
  4. Patches each Work Session via Notion API: sets Task = [master_id]
  5. Saves merged data back to focal_sessions_mappings.json (new enriched format)

Run once after upgrading to the three-layer focal:
    python3 rebuild_task_relations.py

Requires focal_config.json with a valid Notion token.
"""

from __future__ import annotations

import json
import os
import time
import sys

import requests

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_F   = os.path.join(BASE_DIR, "focal_config.json")
MAPPINGS_F = os.path.join(BASE_DIR, "focal_mappings.json")
SESSIONS_F = os.path.join(BASE_DIR, "focal_sessions_mappings.json")

NOTION_API = "https://api.notion.com/v1"
HEADERS_TPL = {
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def patch_page(token: str, page_id: str, props: dict) -> requests.Response:
    headers = {**HEADERS_TPL, "Authorization": f"Bearer {token}"}
    return requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=headers,
        json={"properties": props},
        timeout=15,
    )


def main() -> None:
    # ── Load token ─────────────────────────────────────────────────────────────
    config = load_json(CONFIG_F)
    token  = config.get("token", "").strip()
    if not token:
        print("ERROR: No Notion token found in focal_config.json")
        sys.exit(1)

    # ── Load mapping files ─────────────────────────────────────────────────────
    focal_mappings    = load_json(MAPPINGS_F)   # {wbs_id: {master_id, db, ...}}
    sessions_mappings = load_json(SESSIONS_F)   # {master_id: ws_id}  (flat)

    print(f"focal_mappings entries:    {len(focal_mappings)}")
    print(f"sessions_mappings entries: {len(sessions_mappings)}")

    # ── Build the join ─────────────────────────────────────────────────────────
    # Identify which format sessions_mappings is in
    dict_entries = sum(1 for v in sessions_mappings.values() if isinstance(v, dict))
    flat_entries = sum(1 for v in sessions_mappings.values() if isinstance(v, str))
    print(f"  sessions_mappings: {dict_entries} dict entries, {flat_entries} flat (string) entries")

    # Build master_id → ws_id lookup (works for both old flat and new dict format)
    master_to_ws: dict[str, str] = {}
    for k, v in sessions_mappings.items():
        if isinstance(v, str):
            # Old format: key=master_id, value=ws_id
            master_to_ws[k] = v
        elif isinstance(v, dict) and v.get("master_id") and v.get("ws_id"):
            # New format: key=wbs_id, value={master_id, ws_id, ...}
            master_to_ws[v["master_id"]] = v["ws_id"]

    print(f"  master→ws pairs resolved: {len(master_to_ws)}")

    # ── Merge into new format ──────────────────────────────────────────────────
    new_mappings: dict[str, dict] = {}

    # Start from focal_mappings (wbs_id → master_id)
    for wbs_id, info in focal_mappings.items():
        if not isinstance(info, dict):
            continue
        master_id = info.get("master_id", "")
        if not master_id:
            continue
        ws_id = master_to_ws.get(master_id, "")
        new_mappings[wbs_id] = {
            "master_id":   master_id,
            "ws_id":       ws_id,
            "fp":          info.get("fp", ""),
            "name":        info.get("task_name", ""),
            "planned_end": "",   # not stored in old format; sync will repopulate
            "priority":    "",
            "work_type":   "",
            "project_id":  "",
            "project_name": "",
            "source_db_id": info.get("db", ""),
        }

    print(f"\nMerged entries: {len(new_mappings)}")
    linked   = sum(1 for v in new_mappings.values() if v.get("ws_id"))
    unlinked = len(new_mappings) - linked
    print(f"  with ws_id: {linked},  without ws_id: {unlinked}")

    # ── Patch Work Sessions to add Task relation ───────────────────────────────
    print(f"\nPatching {linked} Work Sessions to set Task relation...")
    patched = skipped = errors = 0

    for wbs_id, info in new_mappings.items():
        master_id = info.get("master_id", "")
        ws_id     = info.get("ws_id", "")
        if not master_id or not ws_id:
            skipped += 1
            continue

        for attempt in range(2):
            try:
                r = patch_page(token, ws_id, {
                    "Task": {"relation": [{"id": master_id}]}
                })
                if r.status_code == 200:
                    patched += 1
                    break
                elif r.status_code == 404:
                    # Work Session was deleted in Notion
                    info["deleted"] = True
                    skipped += 1
                    break
                elif r.status_code == 429 or attempt == 0:
                    # Rate limited or first-attempt failure — wait and retry
                    retry_after = int(r.headers.get("Retry-After", 1))
                    time.sleep(retry_after + 0.5)
                    continue
                else:
                    print(f"  WARN {ws_id[:8]}: HTTP {r.status_code} — {r.text[:120]}")
                    errors += 1
                    break
            except requests.exceptions.Timeout:
                if attempt == 0:
                    time.sleep(3)
                    continue
                print(f"  WARN {ws_id[:8]}: timeout")
                errors += 1
                break

        # Throttle to stay under Notion's ~3 req/s limit
        time.sleep(0.35)

        if (patched + skipped + errors) % 20 == 0:
            print(f"  progress: {patched} patched, {skipped} skipped, {errors} errors")

    print(f"\nDone: {patched} patched, {skipped} skipped, {errors} errors")

    # ── Save merged sessions_mappings ──────────────────────────────────────────
    save_json(SESSIONS_F, new_mappings)
    print(f"Saved merged mappings → {SESSIONS_F}")
    print("\nNext: run a full sync to repopulate planned_end, priority, work_type per task.")


if __name__ == "__main__":
    main()
