#!/usr/bin/env python3
"""
restore_archived_tasks.py  (v2)
────────────────────────────────
Restores archived Master WBS Tasks and rebuilds focal_mappings.json.

STRATEGY
  The original restore attempt (v1) tried to find master task IDs via the
  "Master WBS" backlink on source WBS tasks. That failed because write_backlink
  silently errors and never populated those relation fields.

  v2 uses focal_sessions_mappings.json instead:
    - Its KEYS are master task page IDs (one per master task with a work session)
    - We subtract the 38 healthy EME 6209 IDs → get the 121 orphaned master IDs

  For each orphaned master ID:
    1. Unarchive it via PATCH /v1/pages/{id}
    2. Read its title from the API response
    3. Match it against source WBS task titles (queried per-database)
    4. Rebuild the source_id → {master_id, db} mapping entry
    5. Write the "Master WBS" backlink back to the source WBS task

  For any master tasks NOT in sessions_mappings (≈54 tasks), the sync tool
  will create fresh master tasks on the next run — work session history for
  those is not recoverable without the original mappings file.

RUN FROM TERMINAL
  cd /Users/rui.huang/Documents/Claude/Projects/Notion_Auto_PM
  python3 restore_archived_tasks.py

REQUIREMENTS
  pip3 install requests   (or: pip install requests --break-system-packages)
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

# EME 6209 WBS — its 38 tasks are already healthy; skip
SKIP_DB_IDS = {"54631775-3dac-47db-8b5f-1f7b9aa57073"}

RATE_SLEEP = 0.35   # seconds between API calls (~3 req/s)


# ── Helpers ──────────────────────────────────────────────────────────────────

def hdrs(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_title(page: dict) -> str:
    """Extract the plain-text title from a Notion page."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            return "".join(p.get("plain_text", "") for p in parts)
    # Fallback: check top-level title array (for pages not in a DB)
    for item in page.get("title", []):
        if isinstance(item, dict):
            return item.get("plain_text", "")
    return ""


def get_project_id(page: dict) -> str:
    """Extract the first relation ID from the 'Project' property (if present)."""
    props = page.get("properties", {})
    for key in ("Project", "project"):
        prop = props.get(key, {})
        rels = prop.get("relation", [])
        if rels:
            return rels[0].get("id", "")
    return ""


def fetch_page(token: str, page_id: str) -> dict | None:
    """GET a single Notion page by ID."""
    resp = requests.get(f"{NOTION_API}/pages/{page_id}", headers=hdrs(token))
    if resp.ok:
        return resp.json()
    print(f"    ⚠️  Could not fetch page {page_id[:8]}…: {resp.status_code}")
    return None


def patch_page(token: str, page_id: str, data: dict) -> bool:
    resp = requests.patch(
        f"{NOTION_API}/pages/{page_id}", headers=hdrs(token), json=data
    )
    return resp.ok


def query_database(token: str, db_id: str) -> list:
    """Return all (non-archived) pages from a Notion database."""
    pages, body = [], {"page_size": 100}
    url = f"{NOTION_API}/databases/{db_id}/query"
    while True:
        resp = requests.post(url, headers=hdrs(token), json=body)
        if not resp.ok:
            print(f"    ⚠️  Could not query DB {db_id}: {resp.status_code} {resp.text[:120]}")
            break
        data = resp.json()
        pages.extend(data.get("results", []))
        if data.get("has_more"):
            body["start_cursor"] = data["next_cursor"]
        else:
            break
    return pages


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    # Load config
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    token   = config.get("token", "").strip()
    sources = config.get("sources", {})

    # Load mappings
    with open(MAPPINGS_FILE) as f:
        mappings: dict = json.load(f)
    with open(SESSIONS_MAP_FILE) as f:
        sessions: dict = json.load(f)

    print(f"📄  focal_mappings.json:          {len(mappings)} entries")
    print(f"📄  focal_sessions_mappings.json:  {len(sessions)} entries\n")

    # ── Step 1: identify orphaned master IDs ────────────────────────────────
    healthy_masters = {
        v["master_id"]
        for v in mappings.values()
        if isinstance(v, dict) and not v.get("deleted")
    }
    orphaned_master_ids = [
        mid for mid in sessions.keys()
        if mid not in healthy_masters
    ]
    print(f"🔍  Orphaned master IDs found in sessions mappings: {len(orphaned_master_ids)}")
    print()

    # ── Step 2: unarchive + collect master task metadata ────────────────────
    print("▶  Phase 1 — Unarchiving master tasks and reading their titles…")
    master_meta: dict[str, dict] = {}   # master_id → {title, project_id}

    for i, mid in enumerate(orphaned_master_ids, 1):
        print(f"   [{i}/{len(orphaned_master_ids)}] {mid[:8]}…", end=" ")
        # Unarchive
        ok = patch_page(token, mid, {"archived": False})
        if not ok:
            print("⚠️  unarchive failed — skipping")
            time.sleep(RATE_SLEEP)
            continue
        time.sleep(RATE_SLEEP)
        # Fetch the page to read its title and project relation
        page = fetch_page(token, mid)
        time.sleep(RATE_SLEEP)
        if page:
            title      = get_title(page)
            project_id = get_project_id(page)
            master_meta[mid] = {"title": title, "project_id": project_id}
            print(f'✅  "{title[:50]}"')
        else:
            print("⚠️  could not read page after unarchive")

    print(f"\n✅  Unarchived and read {len(master_meta)}/{len(orphaned_master_ids)} master tasks\n")

    # ── Step 3: query each source WBS DB, match by title + project ──────────
    print("▶  Phase 2 — Querying source WBS databases for match…")

    # Build reverse index: (title_lower, project_id) → master_id
    master_by_title_proj: dict[tuple, str] = {}
    for mid, meta in master_meta.items():
        key = (meta["title"].strip().lower(), meta["project_id"])
        master_by_title_proj[key] = mid

    # Also build index by title only (fallback when project_id is missing on either side)
    master_by_title_only: dict[str, list[str]] = {}
    for mid, meta in master_meta.items():
        t = meta["title"].strip().lower()
        master_by_title_only.setdefault(t, []).append(mid)

    new_entries    = 0
    no_match       = 0
    backlink_field = "Master WBS"

    for db_id, cfg in sources.items():
        if db_id in SKIP_DB_IDS:
            continue
        db_title       = cfg.get("db_title", db_id)
        project_id     = cfg.get("project_id", "")
        task_name_col  = cfg.get("field_map", {}).get("task_name", "Task")
        bl_field       = cfg.get("backlink_field", backlink_field)

        print(f"\n  📋  {db_title}")
        pages = query_database(token, db_id)
        time.sleep(RATE_SLEEP)
        print(f"     {len(pages)} source tasks found")

        for page in pages:
            src_id = page["id"]

            # Skip if already in mappings (healthy, not deleted)
            existing = mappings.get(src_id)
            if existing and isinstance(existing, dict) and not existing.get("deleted"):
                continue

            # Get source task title
            props = page.get("properties", {})
            title_prop = props.get(task_name_col, {})
            if title_prop.get("type") == "title":
                title = "".join(
                    t.get("plain_text", "") for t in title_prop.get("title", [])
                )
            else:
                title = get_title(page)

            title_key = title.strip().lower()

            # Try exact match on (title, project_id)
            master_id = master_by_title_proj.get((title_key, project_id))

            # Fallback: title only (only if exactly one master matches)
            if not master_id:
                candidates = master_by_title_only.get(title_key, [])
                if len(candidates) == 1:
                    master_id = candidates[0]

            if not master_id:
                no_match += 1
                print(f'     ⚪  No match: "{title[:60]}"')
                continue

            # Rebuild mapping entry
            mappings[src_id] = {"master_id": master_id, "db": db_id}
            new_entries += 1

            # Write backlink to source WBS task
            ok = patch_page(
                token, src_id,
                {"properties": {bl_field: {"relation": [{"id": master_id}]}}}
            )
            time.sleep(RATE_SLEEP)
            status = "✅" if ok else "⚠️ (backlink failed)"
            print(f'     {status}  "{title[:50]}"')

    # ── Step 4: save mappings ────────────────────────────────────────────────
    with open(MAPPINGS_FILE, "w") as f:
        json.dump(mappings, f, indent=2)

    print()
    print("═" * 60)
    print(f"✅  Master tasks unarchived:  {len(master_meta)}")
    print(f"✅  Mapping entries rebuilt:  {new_entries}")
    print(f"⚪  Source tasks unmatched:   {no_match}")
    print(f"📄  focal_mappings.json saved — {len(mappings)} total entries")
    print()
    if no_match:
        print("NOTE: Unmatched source tasks will get fresh master tasks on next sync.")
        print("      Their old archived master tasks can be deleted from Notion trash.")
    print()
    print("Done! Run a full sync in focal to verify counts look correct.")


if __name__ == "__main__":
    main()
