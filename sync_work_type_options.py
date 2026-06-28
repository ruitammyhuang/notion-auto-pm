"""
sync_work_type_options.py
──────────────────────────────────────────────────────────────────────────────
Push Work Type select options to every Notion database that uses the column.

Source of truth: work_types.json (managed by focal/work_type_manager.py).

Whenever you want to add, rename, or reorder a Work Type category:
  1. Use the /health-check UI or POST /api/work-types/create.
  2. Run:  python3 sync_work_type_options.py

That's it. This script:
  - Reads active work types from work_types.json via work_type_manager
  - Pushes the options to every Project WBS database (from focal_config.json)
    that has "work_type" in its field_map
  - Pushes to the Work Sessions database
  - Reports success/failure per database

The core logic is exposed as push_to_all_dbs() for programmatic use
(called by /api/work-types/create after saving a new type).

Run:  python3 sync_work_type_options.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "focal_config.json"

sys.path.insert(0, str(BASE_DIR))
from focal.config import WORK_SESSIONS_DB_ID
from focal.work_type_manager import get_work_type_options

NOTION_API = "https://api.notion.com/v1"


def _make_headers(token: str) -> dict:
    return {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "Notion-Version": "2022-06-28",
    }


def _patch_select_options(db_id: str, col_name: str, options: list[dict], headers: dict) -> bool:
    """Replace the select options for col_name on a Notion database."""
    payload = {
        "properties": {
            col_name: {
                "select": {"options": options}
            }
        }
    }
    r = requests.patch(f"{NOTION_API}/databases/{db_id}", headers=headers, json=payload)
    if r.status_code == 200:
        return True
    print(f"    HTTP {r.status_code}: {r.text[:200]}")
    return False


def push_to_all_dbs(token: str | None = None, cfg: dict | None = None, verbose: bool = False) -> dict:
    """Push current work_types.json options to Work Sessions DB and all WBS DBs.

    Args:
        token: Notion API token. If None, reads from focal_config.json.
        cfg:   Parsed focal_config.json dict. If None, loads from disk.
        verbose: Print progress to stdout.

    Returns:
        {"ok_count": int, "fail_count": int, "errors": [str]}
    """
    if cfg is None:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    if token is None:
        token = cfg.get("token", "")

    headers = _make_headers(token)
    options  = get_work_type_options()
    errors: list[str] = []
    ok_count = fail_count = 0

    if verbose:
        print("Work Type options to push:")
        for opt in options:
            print(f"  {opt['name']}  ({opt['color']})")
        print()

    # Work Sessions DB
    if verbose:
        print(f"Work Sessions DB ({WORK_SESSIONS_DB_ID[:8]}...)")
    ok = _patch_select_options(WORK_SESSIONS_DB_ID, "Work Type", options, headers)
    if verbose:
        print(f"  {'ok' if ok else 'FAIL'} Work Type options updated")
    if not ok:
        errors.append(f"Work Sessions DB ({WORK_SESSIONS_DB_ID[:8]}): failed")

    # All WBS DBs with work_type mapped
    if verbose:
        print()
        print("Project WBS databases:")

    for db_id, src in cfg.get("sources", {}).items():
        col_name = src.get("field_map", {}).get("work_type", "")
        if not col_name:
            continue

        title = src.get("db_title", db_id[:8])
        if verbose:
            print(f"\n  {title}")
        ok = _patch_select_options(db_id, col_name, options, headers)
        if verbose:
            print(f"  {'ok' if ok else 'FAIL'} '{col_name}' options updated")
        if ok:
            ok_count += 1
        else:
            fail_count += 1
            errors.append(f"{title} ({db_id[:8]}): failed")

    if verbose:
        print()
        print("=" * 60)
        print(f"Done -- {ok_count} WBS databases updated, {fail_count} failed.")

    return {"ok_count": ok_count, "fail_count": fail_count, "errors": errors}


# ── CLI entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    result = push_to_all_dbs(verbose=True)
    if result["errors"]:
        print("\nErrors:")
        for e in result["errors"]:
            print(f"  - {e}")
    print()
    print("Current Work Type options in effect:")
    for opt in get_work_type_options():
        print(f"  {opt['name']}")
    print()
    print("To add a new type: POST /api/work-types/create or use the /health-check UI.")
    print("Then run: python3 sync_work_type_options.py")
