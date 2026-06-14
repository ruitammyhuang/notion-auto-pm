#!/usr/bin/env python3
"""
notion_backup.py
────────────────
Snapshot every Notion database in the faculty PM system to a timestamped
JSON file in the backups/ directory.  Keeps the last 30 snapshots.

Databases captured:
  • 📁 Projects DB
  • 📋 Master WBS Tasks
  • ⏱️  Work Sessions
  • All project WBS databases listed in focal_config.json (or notion_wbs_sources.json in CI)

Token resolution (first match wins):
  1. NOTION_TOKEN environment variable  (used by GitHub Actions)
  2. focal_config.json → "token" field  (used locally)

WBS sources resolution:
  1. focal_config.json → "sources" (local — also auto-exports notion_wbs_sources.json)
  2. notion_wbs_sources.json (CI — committed to repo, token-free)

Usage:
  python scripts/notion_backup.py              # normal backup
  python scripts/notion_backup.py --list       # list existing backups
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import requests

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE     = os.path.join(BASE_DIR, "focal_config.json")
SOURCES_FILE    = os.path.join(BASE_DIR, "notion_wbs_sources.json")  # token-free, committed
BACKUP_DIR      = os.path.join(BASE_DIR, "backups")
MAX_BACKUPS     = 30  # keep last N daily snapshots

# ── Notion constants ───────────────────────────────────────────────────────────
NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# ── Hard-coded global DB IDs (from focal/config.py) ───────────────────────────
GLOBAL_DBS = {
    "projects_db":       "01705badbb854f019baf7d0ec68b8c7d",  # 📁 Projects DB
    "master_wbs_tasks":  "2de3b2f3d9b74481bc88511ea94de45e",  # 📋 Master WBS Tasks
    "work_sessions":     "308c193fbba34a1ebe8d817fd72e9d9a",  # ⏱️ Work Sessions
}


# ── Notion API helpers ─────────────────────────────────────────────────────────
def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_all_pages(token: str, db_id: str) -> list[dict]:
    """Return every page in a database (auto-paginates)."""
    pages, cursor = [], None
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
        if r.status_code == 404:
            print(f"  ⚠️  DB {db_id} not found (404) — skipping")
            return []
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        time.sleep(0.1)  # gentle rate limiting
    return pages


def fetch_db_schema(token: str, db_id: str) -> dict | None:
    """Return the database object (title + properties schema)."""
    r = requests.get(
        f"{NOTION_API}/databases/{db_id}",
        headers=_headers(token),
        timeout=20,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


# ── Backup logic ───────────────────────────────────────────────────────────────
def run_backup() -> str:
    """Snapshot all databases and return the path to the saved file."""
    # ── Token resolution ───────────────────────────────────────────────────────
    token = os.environ.get("NOTION_TOKEN", "")

    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        if not token:
            token = cfg.get("token", "")

    if not token:
        sys.exit("❌  No Notion token found. Set NOTION_TOKEN env var or add token to focal_config.json")

    # ── WBS sources resolution ─────────────────────────────────────────────────
    wbs_sources = cfg.get("sources", {})

    if not wbs_sources and os.path.exists(SOURCES_FILE):
        # Running in CI where focal_config.json doesn't exist
        with open(SOURCES_FILE) as f:
            wbs_sources = json.load(f)
        print(f"ℹ️   Loaded {len(wbs_sources)} WBS sources from notion_wbs_sources.json")

    # ── Auto-export token-free sources file (for CI) ───────────────────────────
    if wbs_sources and os.path.exists(CONFIG_FILE):
        with open(SOURCES_FILE, "w") as f:
            json.dump(wbs_sources, f, indent=2, ensure_ascii=False)
        print(f"📝  Exported {len(wbs_sources)} WBS sources → notion_wbs_sources.json (commit this)")

    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot: dict = {
        "backup_version": 2,
        "timestamp": datetime.now().isoformat(),
        "databases": {},
    }

    total_pages = 0

    # ── 1. Global databases ────────────────────────────────────────────────────
    for label, db_id in GLOBAL_DBS.items():
        print(f"📦  Backing up {label} ({db_id}) …", end=" ", flush=True)
        schema = fetch_db_schema(token, db_id)
        pages  = fetch_all_pages(token, db_id)
        snapshot["databases"][db_id] = {
            "label":  label,
            "db_id":  db_id,
            "schema": schema,
            "pages":  pages,
        }
        print(f"{len(pages)} pages")
        total_pages += len(pages)

    # ── 2. Per-project WBS databases ──────────────────────────────────────────
    for db_id, src in wbs_sources.items():
        label = src.get("db_title", db_id)
        print(f"📦  Backing up {label} ({db_id}) …", end=" ", flush=True)
        schema = fetch_db_schema(token, db_id)
        pages  = fetch_all_pages(token, db_id)
        snapshot["databases"][db_id] = {
            "label":      label,
            "db_id":      db_id,
            "source_cfg": src,   # preserve field_map, backlink_field, project_id etc.
            "schema":     schema,
            "pages":      pages,
        }
        print(f"{len(pages)} pages")
        total_pages += len(pages)

    # ── 3. Also snapshot the local mapping files ───────────────────────────────
    local_files = {
        "focal_sessions_mappings.json": os.path.join(BASE_DIR, "focal_sessions_mappings.json"),
        "focal_mappings.json":          os.path.join(BASE_DIR, "focal_mappings.json"),
        "focal_students.json":          os.path.join(BASE_DIR, "focal_students.json"),
    }
    snapshot["local_files"] = {}
    for fname, fpath in local_files.items():
        if os.path.exists(fpath):
            with open(fpath) as f:
                snapshot["local_files"][fname] = json.load(f)

    # ── 4. Save snapshot ───────────────────────────────────────────────────────
    out_path = os.path.join(BACKUP_DIR, f"notion_backup_{timestamp}.json")
    with open(out_path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    size_kb = os.path.getsize(out_path) / 1024
    print(f"\n✅  Saved → {out_path}")
    print(f"   {total_pages} pages across {len(snapshot['databases'])} databases "
          f"({size_kb:.0f} KB)")

    # ── 5. Prune old backups ───────────────────────────────────────────────────
    all_backups = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("notion_backup_") and f.endswith(".json")]
    )
    to_delete = all_backups[:-MAX_BACKUPS]
    for fname in to_delete:
        os.remove(os.path.join(BACKUP_DIR, fname))
        print(f"🗑️   Pruned old backup: {fname}")

    return out_path


def list_backups() -> None:
    if not os.path.exists(BACKUP_DIR):
        print("No backups directory found.")
        return
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("notion_backup_") and f.endswith(".json")]
    )
    if not files:
        print("No backups found.")
        return
    print(f"{'Backup file':<45}  {'Size':>8}")
    print("-" * 56)
    for fname in reversed(files):  # newest first
        fpath = os.path.join(BACKUP_DIR, fname)
        size  = os.path.getsize(fpath) / 1024
        print(f"{fname:<45}  {size:>6.0f} KB")
    print(f"\n{len(files)} backup(s) found (keeping last {MAX_BACKUPS})")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Notion PM system backup tool")
    parser.add_argument("--list", action="store_true", help="List existing backups")
    args = parser.parse_args()

    if args.list:
        list_backups()
    else:
        run_backup()
