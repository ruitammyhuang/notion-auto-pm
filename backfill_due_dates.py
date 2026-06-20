#!/usr/bin/env python3
"""
backfill_due_dates.py  (one-off)
─────────────────────────────────
Backfill WBS task due dates for all existing records where the linked
Work Session is Completed and has a Session End date.

This is the same logic added to the ongoing sync in focal/sync_engine.py
(sync_due_dates_from_completed_sessions), applied retroactively to every
entry already in focal_sessions_mappings.json.

Run once from the project root:
    python3 backfill_due_dates.py

Safe to re-run: skips any WBS page whose due date already matches
the Session End date.  Reads token from focal_config.json.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR              = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE           = os.path.join(BASE_DIR, "focal_config.json")
SESSIONS_MAPPING_FILE = os.path.join(BASE_DIR, "focal_sessions_mappings.json")

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
WORK_SESSIONS_DB_ID = "308c193fbba34a1ebe8d817fd72e9d9a"

# ── Minimal Notion helpers ─────────────────────────────────────────────────────
import requests

def _headers(token: str) -> dict:
    return {
        "Authorization":  f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type":   "application/json",
    }

def _query_db(token: str, db_id: str) -> list:
    pages = []
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=_headers(token),
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages

def _extract(prop: dict) -> str:
    t = prop.get("type", "")
    if t == "title":
        return "".join(r["plain_text"] for r in prop.get("title", []))
    if t == "rich_text":
        return "".join(r["plain_text"] for r in prop.get("rich_text", []))
    if t == "select":
        s = prop.get("select") or {}
        return s.get("name", "")
    if t == "date":
        d = prop.get("date") or {}
        return d.get("start", "") or ""
    if t == "number":
        return str(prop.get("number", ""))
    return ""

def _patch_page(token: str, page_id: str, properties: dict) -> None:
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=_headers(token),
        json={"properties": properties},
        timeout=30,
    )
    r.raise_for_status()

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Load config
    if not os.path.exists(CONFIG_FILE):
        sys.exit(f"ERROR: {CONFIG_FILE} not found. Run from project root.")
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)

    token = cfg.get("token", "").strip()
    if not token:
        sys.exit("ERROR: No token in focal_config.json.")

    sources_cfg: dict = cfg.get("sources", {})

    # Build source_db_id → planned_end_field lookup
    planned_end_field_for: dict[str, str] = {}
    for db_id, src in sources_cfg.items():
        field = src.get("field_map", {}).get("planned_end", "")
        if field:
            planned_end_field_for[db_id]                  = field
            planned_end_field_for[db_id.replace("-", "")] = field

    # Load sessions mappings
    if not os.path.exists(SESSIONS_MAPPING_FILE):
        sys.exit("ERROR: focal_sessions_mappings.json not found.")
    with open(SESSIONS_MAPPING_FILE) as f:
        sessions_mappings: dict = json.load(f)

    # Build ws_id → wbs_page_id reverse lookup
    ws_id_to_wbs: dict[str, str] = {}
    for wbs_id, info in sessions_mappings.items():
        if isinstance(info, dict) and info.get("ws_id"):
            ws_id_to_wbs[info["ws_id"]] = wbs_id

    print(f"Loaded {len(sessions_mappings)} mapping entries, "
          f"{len(ws_id_to_wbs)} with Work Sessions.")

    # Fetch all Work Sessions
    print("Querying Work Sessions…")
    try:
        all_ws = _query_db(token, WORK_SESSIONS_DB_ID)
    except Exception as e:
        sys.exit(f"ERROR querying Work Sessions: {e}")
    print(f"  Found {len(all_ws)} Work Sessions.")

    updated = skipped = errors = 0
    error_msgs = []
    changed_mappings = False

    for ws in all_ws:
        ws_id = ws["id"]
        if ws_id not in ws_id_to_wbs:
            skipped += 1
            continue

        props  = ws.get("properties", {})
        status = _extract(props.get("Status", {})) or ""

        # Only act on fully Completed sessions — not 'Session Done' or anything else
        if status != "Completed":
            skipped += 1
            continue

        # Extract Session End date
        end_prop    = props.get("Session End", {})
        session_end = ""
        if end_prop.get("type") == "date" and end_prop.get("date"):
            raw = end_prop["date"].get("start", "")
            session_end = raw[:10] if raw else ""

        if not session_end:
            skipped += 1
            continue

        wbs_id = ws_id_to_wbs[ws_id]
        info   = sessions_mappings[wbs_id]
        source_db_id = info.get("source_db_id", "")

        planned_end_field = (
            planned_end_field_for.get(source_db_id)
            or planned_end_field_for.get(source_db_id.replace("-", ""), "")
        )
        if not planned_end_field:
            # Source not in config (e.g. archived project) — skip silently
            skipped += 1
            continue

        current = info.get("planned_end", "")
        task_name = info.get("name", wbs_id[:8])

        if current == session_end:
            skipped += 1
            continue

        try:
            _patch_page(token, wbs_id, {
                planned_end_field: {"date": {"start": session_end}}
            })
            old_label = f" (was {current})" if current else " (was empty)"
            print(f"  UPDATED  {task_name!r}: due date → {session_end}{old_label}")
            info["planned_end"] = session_end
            changed_mappings = True
            updated += 1
        except Exception as e:
            msg = f"  ERROR    {task_name!r} ({wbs_id[:8]}): {e}"
            print(msg)
            error_msgs.append(msg)
            errors += 1

    if changed_mappings:
        with open(SESSIONS_MAPPING_FILE, "w") as f:
            json.dump(sessions_mappings, f, indent=2)
        print(f"\nSaved updated mappings to {SESSIONS_MAPPING_FILE}")

    print(f"\n── Summary ──────────────────────────────────────────")
    print(f"  Updated : {updated}")
    print(f"  Skipped : {skipped}  (not Completed, no Session End, or already current)")
    print(f"  Errors  : {errors}")
    if error_msgs:
        print("\nError details:")
        for m in error_msgs:
            print(m)

if __name__ == "__main__":
    main()
