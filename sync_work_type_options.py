"""
sync_work_type_options.py
──────────────────────────────────────────────────────────────────────────────
Push Work Type select options to every Notion database that uses the column.

Source of truth: work_types.json (managed by focal/work_type_manager.py).

Whenever you want to add or update a Work Type category:
  1. Use the Work Types tab in the Flask UI (http://localhost:8765)
     -- or POST /api/work-types/create  /update  /deprecate directly.
  2. Run this script to push changes made outside the UI:
       python3 sync_work_type_options.py

The core logic is exposed as push_to_all_dbs() for programmatic use
(called by /api/work-types/* after any change).

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
    payload = {"properties": {col_name: {"select": {"options": options}}}}
    r = requests.patch(f"{NOTION_API}/databases/{db_id}", headers=headers,
                       json=payload, timeout=15)
    if r.status_code == 200:
        return True
    print(f"    HTTP {r.status_code}: {r.text[:200]}")
    return False


def push_to_all_dbs(token: str | None = None, cfg: dict | None = None,
                    verbose: bool = False) -> dict:
    """Push current work_types.json options to Work Sessions DB and all WBS DBs.

    Args:
        token:   Notion API token. If None, reads from focal_config.json.
        cfg:     Parsed focal_config.json dict. If None, loads from disk.
        verbose: Print progress to stdout.

    Returns:
        {"ok_count": int, "fail_count": int, "errors": [str]}
    """
    if cfg is None:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    if token is None:
        token = cfg.get("token", "")

    headers  = _make_headers(token)
    options  = get_work_type_options()
    errors: list[str] = []
    ok_count = fail_count = 0

    if verbose:
        print("Work Type options to push:")
        for opt in options:
            print(f"  {opt['name']}  ({opt['color']})")
        print()
        print(f"Work Sessions DB ({WORK_SESSIONS_DB_ID[:8]}...)")

    ok = _patch_select_options(WORK_SESSIONS_DB_ID, "Work Type", options, headers)
    if verbose:
        print(f"  {'ok' if ok else 'FAIL'} Work Type options updated")
    if not ok:
        errors.append(f"Work Sessions DB ({WORK_SESSIONS_DB_ID[:8]}): failed")

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


def push_to_single_db(db_id: str, col_name: str,
                      token: str | None = None,
                      cfg: dict | None = None,
                      verbose: bool = False) -> dict:
    """Push canonical work type options to a single DB.

    Intended for freshly created DBs whose Work Type column is still empty.
    A direct PATCH (without merge) is safe there and sets the correct colors.
    Returns {"ok": bool}.
    """
    if cfg is None:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    if token is None:
        token = cfg.get("token", "")

    headers = _make_headers(token)
    options = get_work_type_options()

    if verbose:
        print(f"Pushing {len(options)} work type options to DB {db_id[:8]}... col '{col_name}'")

    ok = _patch_select_options(db_id, col_name, options, headers)

    if verbose:
        print("  ok" if ok else "  FAIL")

    return {"ok": ok}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Push work type options to Notion DBs")
    parser.add_argument("--db-id", help="Push to a single DB by UUID (skips all other DBs)")
    parser.add_argument("--col", default="Work Type",
                        help="Column name in that DB (default: Work Type)")
    args = parser.parse_args()

    if args.db_id:
        result = push_to_single_db(args.db_id, args.col, verbose=True)
        print()
        print("Current Work Type options in effect:")
        for opt in get_work_type_options():
            print(f"  {opt['name']}")
    else:
        result = push_to_all_dbs(verbose=True)
        if result["errors"]:
            print("\nErrors:")
            for e in result["errors"]:
                print(f"  - {e}")
        print()
        print("Current Work Type options in effect:")
        for opt in get_work_type_options():
            print(f"  {opt['name']}")
