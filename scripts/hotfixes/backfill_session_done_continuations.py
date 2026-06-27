"""
backfill_session_done_continuations.py
──────────────────────────────────────
Finds all Work Sessions with Status = "Session Done" that are missing a
continuation session, and creates the missing ones.

Usage (dry-run — just report):
    python3 scripts/backfill_session_done_continuations.py

Usage (actually create the missing continuations):
    python3 scripts/backfill_session_done_continuations.py --fix
"""

import argparse
import json
import re
import sys
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "focal_config.json"
WORK_SESSIONS_DB_ID = "308c193fbba34a1ebe8d817fd72e9d9a"

def load_token() -> str:
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    token = cfg.get("token", "").strip()
    if not token:
        sys.exit("No token found in focal_config.json")
    return token

def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

def query_db(token: str, db_id: str, filter_body: dict = None) -> list:
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    body = {"page_size": 100}
    if filter_body:
        body["filter"] = filter_body
    pages = []
    while True:
        r = requests.post(url, headers=headers(token), json=body)
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        body["start_cursor"] = data["next_cursor"]
    return pages

def get_title(props: dict, field: str) -> str:
    rich = props.get(field, {}).get("title", [])
    return rich[0]["plain_text"] if rich else ""

def p_title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text}}]}

def p_select(value: str) -> dict:
    return {"select": {"name": value}}

def create_page(token: str, parent: dict, properties: dict) -> dict:
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers(token),
        json={"parent": parent, "properties": properties},
    )
    r.raise_for_status()
    return r.json()

def next_continuation_name(token: str, base_name: str, task_id: str) -> str:
    base = re.sub(r'-\d+$', '', base_name)
    pages = query_db(token, WORK_SESSIONS_DB_ID,
                     {"property": "Task", "relation": {"contains": task_id}})
    max_n = 1
    for page in pages:
        name = get_title(page.get("properties", {}), "Session Name")
        if name == base:
            max_n = max(max_n, 1)
        elif name.startswith(base + "-"):
            suffix = name[len(base) + 1:]
            if suffix.isdigit():
                max_n = max(max_n, int(suffix))
    return f"{base}-{max_n + 1}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true",
                        help="Create the missing continuation sessions (default: dry-run)")
    args = parser.parse_args()

    token = load_token()

    print("Querying Work Sessions with Status = 'Session Done'…")
    session_done_pages = query_db(
        token, WORK_SESSIONS_DB_ID,
        {"property": "Status", "select": {"equals": "Session Done"}}
    )
    print(f"Found {len(session_done_pages)} Session Done session(s).\n")

    if not session_done_pages:
        print("Nothing to do.")
        return

    missing = []
    for page in session_done_pages:
        props    = page.get("properties", {})
        name     = get_title(props, "Session Name")
        task_id  = (props.get("Task",    {}).get("relation") or [{}])[0].get("id")
        proj_id  = (props.get("Project", {}).get("relation") or [{}])[0].get("id")
        work_type = props.get("Work Type", {}).get("select", {}).get("name", "")
        ws_id    = page["id"]

        if not task_id:
            print(f"  SKIP (no Task relation): {name}")
            continue

        # Check if a continuation already exists
        base = re.sub(r'-\d+$', '', name)
        siblings = query_db(token, WORK_SESSIONS_DB_ID,
                            {"property": "Task", "relation": {"contains": task_id}})
        has_continuation = any(
            s.get("id") != ws_id and
            get_title(s.get("properties", {}), "Session Name").startswith(base + "-")
            for s in siblings
        )

        if has_continuation:
            print(f"  OK (continuation exists): {name}")
        else:
            print(f"  MISSING continuation:     {name}  [ws_id={ws_id[:8]}…]")
            missing.append({
                "ws_id": ws_id, "name": name,
                "task_id": task_id, "proj_id": proj_id, "work_type": work_type,
            })

    print(f"\n{len(missing)} session(s) need a continuation.")

    if not missing:
        return

    if not args.fix:
        print("\nRun with --fix to create the missing continuation sessions.")
        return

    print("\nCreating missing continuation sessions…")
    for item in missing:
        cont_name = next_continuation_name(token, item["name"], item["task_id"])
        cont_props = {
            "Session Name": p_title(cont_name),
            "Task":         {"relation": [{"id": item["task_id"]}]},
            "Project":      {"relation": [{"id": item["proj_id"]}]},
            "Status":       p_select("In Progress"),
        }
        if item["work_type"]:
            cont_props["Work Type"] = p_select(item["work_type"])

        new_page = create_page(token, {"database_id": WORK_SESSIONS_DB_ID}, cont_props)
        new_id   = new_page["id"].replace("-", "")
        print(f"  Created: {cont_name}")
        print(f"    → https://app.notion.com/p/{new_id}")

    print("\nDone.")


if __name__ == "__main__":
    main()
