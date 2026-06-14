#!/usr/bin/env python3
"""
cleanup_all_orphans.py
──────────────────────
Finds and archives orphaned Master WBS Tasks across ALL configured projects.

An "orphaned" task is one that:
  • Exists in Master WBS Tasks, linked to a known project
  • Has NO corresponding Project WBS row (no mapping entry)
  • Has NO real logged hours (Session End not set on any linked Work Session)

Before archiving anything, every orphan's full Notion page data is saved to a
timestamped backup JSON file.  Use --restore to undo the cleanup if needed.

Usage
─────
  # Dry run — show what would be archived, write backup, make NO changes
  python cleanup_all_orphans.py

  # Archive orphans with no logged hours (backup is always written first)
  python cleanup_all_orphans.py --execute

  # Restore all pages archived by a previous run
  python cleanup_all_orphans.py --restore orphan_backup_20260612_143000.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from focal.config import MASTER_DB_ID, load_mappings
from focal.notion_client import NotionClient
from focal.sync_engine import has_logged_hours, _archive_page, _get_page_title


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_token_and_sources() -> tuple[str, list[dict]]:
    cfg_path = BASE_DIR / "focal_config.json"
    with open(cfg_path) as f:
        cfg = json.load(f)
    token = cfg.get("token", "").strip()
    if not token:
        print("ERROR: No Notion token found in focal_config.json")
        sys.exit(1)
    raw_sources = cfg.get("sources", {})
    sources = []
    if isinstance(raw_sources, dict):
        for db_id, v in raw_sources.items():
            sources.append({
                "db_id":      db_id,
                "project_id": v.get("project_id", ""),
                "db_title":   v.get("db_title", db_id),
            })
    elif isinstance(raw_sources, list):
        sources = raw_sources
    return token, sources


def _fetch_page_data(client: NotionClient, page_id: str) -> dict:
    """Fetch a page's full raw data for backup. Returns {} on error."""
    try:
        r = client.get_page(page_id)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return {}


def _extract_ws_title(page_data: dict) -> str:
    """Extract a plain-text title from a raw Notion page dict."""
    for prop_val in page_data.get("properties", {}).values():
        if prop_val.get("type") == "title":
            parts = prop_val.get("title", [])
            return "".join(p.get("plain_text", "") for p in parts).strip()
    return ""


# ── Scan ───────────────────────────────────────────────────────────────────────

def scan_all_projects(client: NotionClient, sources: list[dict]) -> list[dict]:
    """
    For each configured project, find Master WBS tasks that have no mapping entry.
    Returns a list of orphan dicts — does NOT archive anything.
    """
    mappings = load_mappings()

    # Global set of all actively tracked master IDs (across all projects)
    # keyed per source_db_id for scoped lookup
    def active_masters_for(source_db_id: str) -> set[str]:
        return {
            v["master_id"]
            for v in mappings.values()
            if isinstance(v, dict)
            and not v.get("deleted")
            and v.get("db") == source_db_id
        }

    orphans: list[dict] = []

    for src in sources:
        source_db_id = src["db_id"]
        project_id   = src.get("project_id", "")
        db_title     = src.get("db_title", source_db_id)

        if not project_id:
            continue

        active_ids = active_masters_for(source_db_id)

        print(f"  Scanning: {db_title} ({len(active_ids)} tracked master IDs)…")

        try:
            master_pages = client.query_db(MASTER_DB_ID, filter_body={
                "property": "Project",
                "relation": {"contains": project_id},
            })
        except Exception as e:
            print(f"    ERROR querying project: {e}")
            continue

        for m_page in master_pages:
            master_id = m_page["id"]
            if master_id in active_ids:
                continue  # tracked — skip

            title  = _get_page_title(m_page)
            ws_rel = m_page.get("properties", {}).get("Work Sessions", {}).get("relation", [])
            ws_ids = [ws["id"] for ws in ws_rel]

            # Check for real logged hours on any linked Work Session
            session_has_hours = any(has_logged_hours(client, ws_id) for ws_id in ws_ids)

            orphans.append({
                "project_id":    project_id,
                "source_db_id":  source_db_id,
                "db_title":      db_title,
                "master_id":     master_id,
                "master_title":  title,
                "has_hours":     session_has_hours,
                "ws_ids":        ws_ids,
                # Raw page data populated in build_backup()
                "master_page_data": {},
                "work_sessions":    [],
            })

        if orphans:
            project_orphans = [o for o in orphans if o["source_db_id"] == source_db_id]
            no_hours = [o for o in project_orphans if not o["has_hours"]]
            has_hrs  = [o for o in project_orphans if o["has_hours"]]
            if no_hours or has_hrs:
                print(f"    → {len(no_hours)} orphan(s) safe to archive, "
                      f"{len(has_hrs)} with hours (will preserve)")

    return orphans


# ── Backup ─────────────────────────────────────────────────────────────────────

def build_backup(client: NotionClient, orphans: list[dict],
                 executed: bool) -> tuple[dict, Path]:
    """Fetch full page data for every orphan and write the backup JSON file."""
    print("\nFetching full page data for backup…")
    for o in orphans:
        o["master_page_data"] = _fetch_page_data(client, o["master_id"])
        sessions = []
        for ws_id in o["ws_ids"]:
            ws_data = _fetch_page_data(client, ws_id)
            sessions.append({
                "ws_id":       ws_id,
                "ws_title":    _extract_ws_title(ws_data),
                "ws_page_data": ws_data,
            })
        o["work_sessions"] = sessions

    timestamp  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = BASE_DIR / f"orphan_backup_{timestamp}.json"

    backup = {
        "created_at":   datetime.now(timezone.utc).isoformat(),
        "executed":     executed,
        "restore_hint": (
            f"Run:  python cleanup_all_orphans.py --restore {backup_path.name}"
        ),
        "orphan_count": len(orphans),
        "orphans":      orphans,
    }

    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)

    print(f"Backup written → {backup_path.name}")
    return backup, backup_path


# ── Archive ────────────────────────────────────────────────────────────────────

def archive_orphans(client: NotionClient, orphans: list[dict]) -> tuple[int, list[str]]:
    """Archive Master tasks (and their empty Work Sessions) for orphans with no hours."""
    archived = 0
    errors: list[str] = []

    for o in orphans:
        if o["has_hours"]:
            continue  # never touch tasks with real work

        # Archive linked Work Sessions first
        for ws in o["work_sessions"]:
            _archive_page(client, ws["ws_id"], errors)

        # Archive the Master WBS task
        if _archive_page(client, o["master_id"], errors):
            archived += 1
            print(f"  ✓ Archived: {o['master_title']!r}  [{o['db_title']}]")
        else:
            print(f"  ✗ Failed:   {o['master_title']!r}  [{o['db_title']}]")

    return archived, errors


# ── Restore ────────────────────────────────────────────────────────────────────

def restore_from_backup(client: NotionClient, backup_file: str) -> None:
    """Un-archive every Master task and Work Session saved in a backup file."""
    path = Path(backup_file)
    if not path.exists():
        print(f"ERROR: Backup file not found: {backup_file}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        backup = json.load(f)

    orphans   = backup.get("orphans", [])
    restored  = 0
    errors: list[str] = []

    print(f"Restoring {len(orphans)} orphan(s) from {path.name}…\n")

    for o in orphans:
        master_id = o["master_id"]
        title     = o.get("master_title", master_id[:8])

        # Restore Work Sessions first (so the task has its sessions back)
        for ws in o.get("work_sessions", []):
            ws_id = ws["ws_id"]
            try:
                r = client.patch_page(ws_id, {"archived": False})
                if r.ok:
                    print(f"  ✓ Restored session: {ws.get('ws_title') or ws_id[:8]!r}")
                else:
                    errors.append(f"Session {ws_id[:8]}: {r.status_code} {r.text[:80]}")
            except Exception as e:
                errors.append(f"Session {ws_id[:8]}: {e}")

        # Restore the Master WBS task
        try:
            r = client.patch_page(master_id, {"archived": False})
            if r.ok:
                restored += 1
                print(f"  ✓ Restored task:    {title!r}  [{o.get('db_title', '?')}]")
            else:
                errors.append(f"Master {master_id[:8]}: {r.status_code} {r.text[:80]}")
        except Exception as e:
            errors.append(f"Master {master_id[:8]}: {e}")

    print(f"\nRestored {restored}/{len(orphans)} task(s).")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  • {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def run(execute: bool) -> None:
    token, sources = load_token_and_sources()
    client = NotionClient(token)

    print(f"Scanning {len(sources)} project(s) for untracked orphans…\n")
    orphans = scan_all_projects(client, sources)

    safe_to_archive = [o for o in orphans if not o["has_hours"]]
    must_preserve   = [o for o in orphans if o["has_hours"]]

    print(f"\n{'='*65}")
    print(f"TOTAL ORPHANS FOUND: {len(orphans)}")
    print(f"  Safe to archive (no hours): {len(safe_to_archive)}")
    print(f"  Preserved (has hours):      {len(must_preserve)}")
    print(f"{'='*65}")

    if must_preserve:
        print(f"\n[PRESERVED — has logged hours]")
        for o in must_preserve:
            print(f"  ⚠  {o['master_title']!r}  [{o['db_title']}]  "
                  f"(id: {o['master_id'][:8]}…, {len(o['ws_ids'])} session(s))")
        print("  Review these manually — real work has been logged.")

    if not orphans:
        print("\nNo orphans found across all projects. Everything is in sync.")
        return

    # Always write a backup before any action
    _, backup_path = build_backup(client, orphans, executed=execute)

    if not execute:
        if safe_to_archive:
            print(f"\n[DRY RUN — would archive ({len(safe_to_archive)} tasks)]")
            for o in safe_to_archive:
                print(f"  → {o['master_title']!r}  [{o['db_title']}]")
        print(f"\nNo changes made. Re-run with --execute to archive {len(safe_to_archive)} task(s).")
        return

    if not safe_to_archive:
        print("\nNothing to archive (all orphans have hours).")
        return

    print(f"\nArchiving {len(safe_to_archive)} orphan(s)…")
    archived, errors = archive_orphans(client, orphans)

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  • {e}")

    print(f"\nDone. {archived}/{len(safe_to_archive)} orphans archived.")
    print(f"Backup saved at: {backup_path.name}")
    print(f"To restore:  python cleanup_all_orphans.py --restore {backup_path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--execute", action="store_true",
        help="Archive orphans with no logged hours (backup written first)",
    )
    group.add_argument(
        "--restore", metavar="BACKUP_FILE",
        help="Restore all pages from a previous backup JSON file",
    )
    args = parser.parse_args()

    token, _ = load_token_and_sources()
    if args.restore:
        restore_from_backup(NotionClient(token), args.restore)
    else:
        run(execute=args.execute)
