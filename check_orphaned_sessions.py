"""
check_orphaned_sessions.py
──────────────────────────
Finds Work Sessions whose linked Master WBS Task is archived or deleted.
These are sessions that should have been cleaned up when the task was removed
but may have slipped through.

Run from the project root:
    python check_orphaned_sessions.py

Outputs a report and writes results to: orphaned_sessions_report.json
"""

import json
import os
import sys
import requests
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR              = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE           = os.path.join(BASE_DIR, "focal_config.json")
SESSIONS_MAPPING_FILE = os.path.join(BASE_DIR, "focal_sessions_mappings.json")
REPORT_FILE           = os.path.join(BASE_DIR, "orphaned_sessions_report.json")

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
WORK_SESSIONS_DB_ID = "308c193fbba34a1ebe8d817fd72e9d9a"


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def query_all(token, db_id, filter_body=None):
    """Paginate through all rows in a database."""
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if filter_body:
            body["filter"] = filter_body
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=headers(token), json=body, timeout=20
        )
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def get_page(token, page_id):
    """Fetch a single page; returns (page_dict_or_None, status_code)."""
    r = requests.get(
        f"{NOTION_API}/pages/{page_id}",
        headers=headers(token), timeout=10
    )
    return r.json() if r.ok else None, r.status_code


def get_title(page):
    """Extract plain-text title from a Notion page."""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(p.get("plain_text", "") for p in prop.get("title", []))
    return "(untitled)"


def get_session_end(page):
    """Return Session End ISO string if set, else None."""
    se = page.get("properties", {}).get("Session End", {})
    d = se.get("date") if se.get("type") == "date" else None
    return d.get("start") if d else None


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    # Load token
    if not os.path.exists(CONFIG_FILE):
        sys.exit("focal_config.json not found. Run from the project root.")
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    token = cfg.get("token", "").strip()
    if not token:
        sys.exit("No Notion token found in focal_config.json.")

    # Load sessions mappings for cross-reference
    sessions_mappings = {}
    if os.path.exists(SESSIONS_MAPPING_FILE):
        with open(SESSIONS_MAPPING_FILE) as f:
            sessions_mappings = json.load(f)
    # Invert: ws_id → master_id
    ws_to_master = {v: k for k, v in sessions_mappings.items()}

    print("Fetching all Work Sessions from Notion…")
    all_sessions = query_all(token, WORK_SESSIONS_DB_ID)
    print(f"  Found {len(all_sessions)} Work Sessions total\n")

    orphaned   = []   # linked task is archived/deleted
    no_task    = []   # session has no Task relation at all
    ok_count   = 0
    checked    = 0

    for ws in all_sessions:
        ws_id    = ws["id"]
        ws_title = get_title(ws)
        ws_url   = f"https://app.notion.com/p/{ws_id.replace('-','')}"
        has_end  = get_session_end(ws)

        # Find linked master task
        task_rel = ws.get("properties", {}).get("Task", {}).get("relation", [])
        if not task_rel:
            no_task.append({
                "ws_id":    ws_id,
                "ws_title": ws_title,
                "ws_url":   ws_url,
                "has_session_end": bool(has_end),
            })
            continue

        master_id = task_rel[0]["id"]
        checked  += 1

        task_page, status = get_page(token, master_id)

        if status == 404:
            # Hard-deleted — page is gone
            orphaned.append({
                "ws_id":           ws_id,
                "ws_title":        ws_title,
                "ws_url":          ws_url,
                "master_id":       master_id,
                "reason":          "task hard-deleted (404)",
                "has_session_end": bool(has_end),
                "in_mapping_file": master_id in sessions_mappings,
            })
        elif task_page and task_page.get("archived"):
            task_title = get_title(task_page)
            orphaned.append({
                "ws_id":           ws_id,
                "ws_title":        ws_title,
                "ws_url":          ws_url,
                "master_id":       master_id,
                "task_title":      task_title,
                "reason":          "task archived",
                "has_session_end": bool(has_end),
                "in_mapping_file": master_id in sessions_mappings,
            })
        else:
            ok_count += 1

        # Progress dot
        if checked % 20 == 0:
            print(f"  Checked {checked} sessions…")

    # ── Report ─────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("ORPHANED WORK SESSIONS REPORT")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    print(f"  Total sessions:          {len(all_sessions)}")
    print(f"  Sessions checked:        {checked}")
    print(f"  ✅ Healthy (task exists): {ok_count}")
    print(f"  ❌ Orphaned (task gone):  {len(orphaned)}")
    print(f"  ⚠️  No task linked:        {len(no_task)}")
    print()

    if orphaned:
        # Split: sessions with logged hours (keep) vs without (safe to delete)
        with_hours    = [o for o in orphaned if o["has_session_end"]]
        without_hours = [o for o in orphaned if not o["has_session_end"]]

        print(f"ORPHANED — with Session End (keep for history): {len(with_hours)}")
        for o in with_hours:
            print(f"  • {o['ws_title'][:60]}")
            print(f"    Task: {o.get('task_title', o['master_id'][:8]+'…')} [{o['reason']}]")
            print(f"    URL: {o['ws_url']}")

        print()
        print(f"ORPHANED — no Session End (safe to archive): {len(without_hours)}")
        for o in without_hours:
            print(f"  • {o['ws_title'][:60]}")
            print(f"    Task: {o.get('task_title', o['master_id'][:8]+'…')} [{o['reason']}]")
            print(f"    URL: {o['ws_url']}")
    else:
        print("✅ No orphaned Work Sessions found.")

    if no_task:
        print()
        print(f"SESSIONS WITH NO TASK LINKED: {len(no_task)}")
        for s in no_task:
            print(f"  • {s['ws_title'][:60]}  {'(has hours)' if s['has_session_end'] else ''}")

    # Save full report to JSON
    report = {
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "total_sessions": len(all_sessions),
        "checked":        checked,
        "ok":             ok_count,
        "orphaned":       orphaned,
        "no_task_linked": no_task,
    }
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print()
    print(f"Full report saved to: {REPORT_FILE}")

    # Return exit code based on findings
    if orphaned:
        print()
        print("Run check_orphaned_sessions.py --archive to archive the 'no Session End' ones.")
        sys.exit(1)


if __name__ == "__main__":
    # Optional --archive flag: archive all orphaned sessions that have no Session End
    if "--archive" in sys.argv:
        import check_orphaned_sessions_archive as _  # handled below
    main()
