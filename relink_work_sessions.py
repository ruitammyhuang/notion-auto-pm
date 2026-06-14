"""
relink_work_sessions.py
─────────────────────────────────────────────────────────
Re-sets the Task relation on every Work Session using the
exact ws_id → master_id pairs in focal_sessions_mappings.json.

Run:  python3 relink_work_sessions.py
"""

import json
import time
import requests
from pathlib import Path

BASE_DIR     = Path(__file__).parent
MAPPINGS_FILE = BASE_DIR / "focal_sessions_mappings.json"
CONFIG_FILE   = BASE_DIR / "focal_config.json"

NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# ── load token ────────────────────────────────────────────────────────────────
with open(CONFIG_FILE) as f:
    cfg = json.load(f)
TOKEN = cfg["token"]

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": NOTION_VERSION,
}

# ── load mappings ─────────────────────────────────────────────────────────────
with open(MAPPINGS_FILE) as f:
    mappings = json.load(f)

entries = [
    (wbs_id, info)
    for wbs_id, info in mappings.items()
    if isinstance(info, dict)
    and not info.get("deleted")
    and info.get("ws_id")
    and info.get("master_id")
]

print(f"Entries to relink: {len(entries)}")
print("─" * 60)

ok = 0
skip = 0
err = 0

for i, (wbs_id, info) in enumerate(entries):
    ws_id     = info["ws_id"]
    master_id = info["master_id"]
    name      = info.get("name", wbs_id)[:55]

    resp = requests.patch(
        f"{NOTION_API}/pages/{ws_id}",
        headers=HEADERS,
        json={
            "properties": {
                "Task": {
                    "relation": [{"id": master_id}]
                }
            }
        },
    )

    if resp.status_code == 200:
        ok += 1
        print(f"[{i+1:3d}/{len(entries)}] ✓  {name}")
    elif resp.status_code == 404:
        skip += 1
        print(f"[{i+1:3d}/{len(entries)}] –  NOT FOUND: {name}")
    else:
        err += 1
        body = resp.text[:120]
        print(f"[{i+1:3d}/{len(entries)}] ✗  ERROR {resp.status_code}: {name}\n         {body}")

    # Notion rate limit: ~3 req/s
    time.sleep(0.35)

print("─" * 60)
print(f"Done — ✓ {ok} linked   – {skip} not found   ✗ {err} errors")
