#!/usr/bin/env python3
"""
notion_restore.py
─────────────────
Restore Notion databases from a snapshot created by notion_backup.py.

Restore strategy (safe upsert):
  1. For each page in the snapshot, check if the page ID still exists in Notion.
     • If YES → PATCH it back to the snapshot state (fixes corrupted data).
     • If NO  → CREATE a new page, record old_id → new_id in a remap table.
  2. After all pages are upserted, make a second pass to fix relation
     properties using the remap table (so Work Sessions still point to
     the correct Master WBS Tasks and Projects DB entries).

Property types handled in restore:
  title, rich_text, select, status, multi_select, date, checkbox, number, url,
  email, phone_number, relation (remapped), people (skipped — API requires user IDs)

Usage:
  python scripts/notion_restore.py --list
  python scripts/notion_restore.py --backup backups/notion_backup_20260614_060000.json
  python scripts/notion_restore.py --backup backups/notion_backup_20260614_060000.json --dry-run
  python scripts/notion_restore.py --backup backups/notion_backup_20260614_060000.json --db projects_db
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
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "focal_config.json")
BACKUP_DIR  = os.path.join(BASE_DIR, "backups")

# ── Notion constants ───────────────────────────────────────────────────────────
NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# ── Restore order: restore dependencies before dependents ─────────────────────
# projects_db has no inbound relations from our system → restore first
# wbs databases are standalone except for backlinks → restore second
# master_wbs_tasks relates to wbs pages → restore third
# work_sessions relates to projects_db and master_wbs_tasks → restore last
RESTORE_ORDER = ["projects_db", "wbs_sources", "master_wbs_tasks", "work_sessions"]

# ── Property types that can be safely written back via the API ─────────────────
WRITABLE_SCALAR_TYPES = {
    "title", "rich_text", "select", "status", "multi_select",
    "date", "checkbox", "number", "url", "email", "phone_number",
}
# Relation properties need special handling (ID remapping); others skipped.
SKIP_TYPES = {
    "formula", "rollup", "created_time", "created_by",
    "last_edited_time", "last_edited_by", "unique_id", "files",
}


# ── Notion API helpers ─────────────────────────────────────────────────────────
def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def page_exists(token: str, page_id: str) -> bool:
    r = requests.get(
        f"{NOTION_API}/pages/{page_id}",
        headers=_headers(token),
        timeout=15,
    )
    return r.status_code == 200


def get_page(token: str, page_id: str) -> dict | None:
    r = requests.get(
        f"{NOTION_API}/pages/{page_id}",
        headers=_headers(token),
        timeout=15,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def create_page(token: str, parent_db_id: str, properties: dict, dry_run: bool = False) -> str | None:
    """Create a new page in a database and return its new page ID."""
    if dry_run:
        return f"dry-run-{parent_db_id[:8]}"
    r = requests.post(
        f"{NOTION_API}/pages",
        headers=_headers(token),
        json={"parent": {"database_id": parent_db_id}, "properties": properties},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["id"]


def patch_page(token: str, page_id: str, properties: dict, dry_run: bool = False) -> None:
    """Update an existing page's properties."""
    if dry_run:
        return
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=_headers(token),
        json={"properties": properties},
        timeout=20,
    )
    r.raise_for_status()


def unarchive_page(token: str, page_id: str, dry_run: bool = False) -> None:
    if dry_run:
        return
    requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=_headers(token),
        json={"archived": False},
        timeout=15,
    )


# ── Property extraction and rebuilding ────────────────────────────────────────
def extract_writable_properties(props: dict, include_relations: bool = True) -> dict:
    """
    Convert a Notion properties dict (from a backup) into a payload safe to
    send back via the API.  Skips computed/read-only fields.
    """
    result = {}
    for name, prop in props.items():
        ptype = prop.get("type")
        if ptype in SKIP_TYPES:
            continue
        if ptype == "people":
            continue  # user objects require member IDs we can't guarantee
        if ptype == "relation":
            if include_relations:
                # Copy raw relation list; remap IDs later
                result[name] = {"relation": prop.get("relation", [])}
            continue
        if ptype in WRITABLE_SCALAR_TYPES:
            result[name] = {ptype: prop.get(ptype)}
        # Unknown types: skip safely
    return result


def remap_relation_properties(props: dict, id_map: dict[str, str]) -> dict:
    """
    For every relation property, replace any page IDs that were re-created
    (old_id → new_id) using id_map.  Returns only the relation properties.
    """
    result = {}
    for name, prop in props.items():
        if prop.get("type") != "relation":
            continue
        old_ids = [r["id"] for r in prop.get("relation", [])]
        new_ids = [id_map.get(oid, oid) for oid in old_ids]  # fall back to original if not remapped
        if new_ids:
            result[name] = {"relation": [{"id": nid} for nid in new_ids]}
    return result


# ── Core restore logic ─────────────────────────────────────────────────────────
def restore_database(
    token: str,
    db_entry: dict,
    id_map: dict[str, str],
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """
    Upsert all pages from a single database entry in the backup.
    Mutates id_map in-place with any old_id → new_id mappings.
    """
    db_id     = db_entry["db_id"]
    label     = db_entry.get("label", db_id)
    pages     = db_entry.get("pages", [])
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Restoring {label} ({len(pages)} pages) …")

    created = updated = skipped = 0

    for page in pages:
        old_id = page["id"]
        props  = page.get("properties", {})
        archived = page.get("archived", False)

        # Extract scalar properties only (relations handled in second pass)
        writable = extract_writable_properties(props, include_relations=False)

        existing = get_page(token, old_id)
        if existing:
            # Page still exists → patch it
            try:
                if archived:
                    pass  # leave archived state as-is (don't restore deleted rows)
                else:
                    patch_page(token, old_id, writable, dry_run=dry_run)
                    if verbose:
                        title_prop = next(
                            (p for p in props.values() if p.get("type") == "title"), {}
                        )
                        name = "".join(r["plain_text"] for r in title_prop.get("title", []))
                        print(f"  ✏️   Updated: {name[:60] or old_id}")
                updated += 1
            except requests.HTTPError as e:
                print(f"  ⚠️  Failed to update {old_id}: {e}")
                skipped += 1
        else:
            # Page was deleted → create new
            try:
                new_id = create_page(token, db_id, writable, dry_run=dry_run)
                id_map[old_id] = new_id
                if verbose:
                    title_prop = next(
                        (p for p in props.values() if p.get("type") == "title"), {}
                    )
                    name = "".join(r["plain_text"] for r in title_prop.get("title", []))
                    print(f"  ✨  Created: {name[:60] or old_id} → {new_id}")
                created += 1
                time.sleep(0.15)  # avoid hitting rate limits
            except requests.HTTPError as e:
                print(f"  ⚠️  Failed to create {old_id}: {e}")
                skipped += 1

    print(f"   → {updated} updated, {created} created, {skipped} skipped")


def restore_relations_pass(
    token: str,
    db_entry: dict,
    id_map: dict[str, str],
    dry_run: bool = False,
) -> None:
    """Second pass: fix relation properties using the id_map."""
    db_id  = db_entry["db_id"]
    label  = db_entry.get("label", db_id)
    pages  = db_entry.get("pages", [])
    fixed  = 0

    for page in pages:
        old_id   = page["id"]
        props    = page.get("properties", {})
        current_id = id_map.get(old_id, old_id)  # use remapped ID if page was re-created

        rel_props = remap_relation_properties(props, id_map)
        if not rel_props:
            continue

        try:
            patch_page(token, current_id, rel_props, dry_run=dry_run)
            fixed += 1
            time.sleep(0.1)
        except requests.HTTPError as e:
            print(f"  ⚠️  Relation patch failed for {current_id}: {e}")

    if fixed:
        print(f"  🔗  {label}: fixed relations on {fixed} page(s)")


# ── Main restore flow ──────────────────────────────────────────────────────────
def run_restore(
    backup_path: str,
    dry_run: bool = False,
    db_filter: str | None = None,
    verbose: bool = False,
) -> None:
    if not os.path.exists(backup_path):
        sys.exit(f"❌  Backup file not found: {backup_path}")

    with open(backup_path) as f:
        snapshot = json.load(f)

    ts      = snapshot.get("timestamp", "unknown")
    dbs     = snapshot.get("databases", {})
    version = snapshot.get("backup_version", 1)
    print(f"{'[DRY RUN] ' if dry_run else ''}Restoring snapshot from {ts}")
    print(f"  Version: {version}, Databases: {len(dbs)}")

    if not os.path.exists(CONFIG_FILE):
        sys.exit(f"❌  Config not found: {CONFIG_FILE}")
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    token = cfg.get("token", "")
    if not token:
        sys.exit("❌  No Notion token in focal_config.json")

    if not dry_run:
        confirm = input("\n⚠️  This will OVERWRITE live Notion data. Type 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return

    # ── Determine restore order ────────────────────────────────────────────────
    # Build label → db_entry map for ordering
    label_to_entry: dict[str, dict] = {e["label"]: e for e in dbs.values()}
    db_id_to_entry: dict[str, dict] = {e["db_id"]: e for e in dbs.values()}

    # Ordered list: global DBs first, then WBS sources
    ordered_entries: list[dict] = []
    for label, db_id in [
        ("projects_db",       "01705badbb854f019baf7d0ec68b8c7d"),
        ("master_wbs_tasks",  "2de3b2f3d9b74481bc88511ea94de45e"),
        ("work_sessions",     "308c193fbba34a1ebe8d817fd72e9d9a"),
    ]:
        if db_id in db_id_to_entry:
            ordered_entries.append(db_id_to_entry[db_id])

    # WBS project databases last (they depend on Projects DB for project_id links)
    for db_id, entry in dbs.items():
        if db_id not in {e["db_id"] for e in ordered_entries}:
            ordered_entries.append(entry)

    # Apply optional filter
    if db_filter:
        ordered_entries = [
            e for e in ordered_entries
            if db_filter.lower() in e.get("label", "").lower()
            or db_filter == e.get("db_id")
        ]
        if not ordered_entries:
            sys.exit(f"❌  No databases matched filter: {db_filter}")
        print(f"  Filter applied: restoring {len(ordered_entries)} database(s)")

    # ── Pass 1: upsert all pages (scalar properties only) ─────────────────────
    id_map: dict[str, str] = {}
    for entry in ordered_entries:
        restore_database(token, entry, id_map, dry_run=dry_run, verbose=verbose)

    # ── Pass 2: fix relation properties ───────────────────────────────────────
    if id_map:
        print(f"\n🔗  Remapping relations ({len(id_map)} re-created page(s)) …")
    else:
        print("\n🔗  Relation pass (no pages were re-created, patching originals) …")
    for entry in ordered_entries:
        restore_relations_pass(token, entry, id_map, dry_run=dry_run)

    # ── Restore local mapping files ────────────────────────────────────────────
    local_files = snapshot.get("local_files", {})
    if local_files and not db_filter:
        print("\n📄  Restoring local mapping files …")
        for fname, content in local_files.items():
            fpath = os.path.join(BASE_DIR, fname)
            if not dry_run:
                with open(fpath, "w") as f:
                    json.dump(content, f, indent=2)
            print(f"  {'[dry] ' if dry_run else ''}→ {fname}")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}✅  Restore complete")
    if id_map:
        remap_path = os.path.join(
            BACKUP_DIR, f"remap_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        if not dry_run:
            with open(remap_path, "w") as f:
                json.dump(id_map, f, indent=2)
            print(f"   ID remap saved → {remap_path}")


# ── List backups ───────────────────────────────────────────────────────────────
def list_backups() -> None:
    if not os.path.exists(BACKUP_DIR):
        print("No backups directory found.")
        return
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith("notion_backup_") and f.endswith(".json")],
        reverse=True,  # newest first
    )
    if not files:
        print("No backups found.")
        return
    print(f"{'Backup file':<50}  {'Size':>8}  {'Pages'}")
    print("-" * 72)
    for fname in files:
        fpath = os.path.join(BACKUP_DIR, fname)
        size  = os.path.getsize(fpath) / 1024
        try:
            with open(fpath) as f:
                snap = json.load(f)
            n_pages = sum(len(e.get("pages", [])) for e in snap.get("databases", {}).values())
            ts      = snap.get("timestamp", "")[:16]
        except Exception:
            n_pages, ts = "?", ""
        print(f"{fname:<50}  {size:>6.0f} KB  {n_pages:>5} pages")


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore Notion PM databases from a backup snapshot")
    parser.add_argument("--list",    action="store_true", help="List available backups")
    parser.add_argument("--backup",  help="Path to backup JSON file to restore from")
    parser.add_argument("--dry-run", action="store_true", help="Simulate restore without writing to Notion")
    parser.add_argument("--db",      help="Only restore databases matching this label substring or ID")
    parser.add_argument("--verbose", action="store_true", help="Show each page being updated/created")
    args = parser.parse_args()

    if args.list:
        list_backups()
    elif args.backup:
        run_restore(
            backup_path=args.backup,
            dry_run=args.dry_run,
            db_filter=args.db,
            verbose=args.verbose,
        )
    else:
        parser.print_help()
