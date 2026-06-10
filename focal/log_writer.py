"""
log_writer.py
─────────────
Write a timestamped sync log file when errors or skipped records occur.
"""

from __future__ import annotations

import os
from datetime import datetime

from .config import BASE_DIR


def write_sync_log(total: dict) -> str | None:
    """Write a timestamped log for every sync run.
    Returns the log file path."""
    errors        = total.get("errors", [])
    skipped_tasks = total.get("skipped_tasks", [])

    log_dir = os.path.join(BASE_DIR, "sync_logs")
    os.makedirs(log_dir, exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"sync_log_{ts}.txt")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Notion WBS Sync Log — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 64 + "\n\n")

        f.write("SUMMARY\n")
        f.write(f"  Master WBS  — Created: {total.get('created', 0)}, "
                f"Updated: {total.get('updated', 0)}, "
                f"Skipped: {total.get('skipped', 0)}, "
                f"Deleted: {total.get('deleted', 0)}\n")
        f.write(f"  Work Sessions — Created: {total.get('ws_created', 0)}, "
                f"Already existed: {total.get('ws_skipped', 0)}\n")
        f.write(f"  Errors: {len(errors)}   Skipped records: {len(skipped_tasks)}\n\n")

        if errors:
            f.write("ERRORS\n")
            f.write("-" * 64 + "\n")
            for i, e in enumerate(errors, 1):
                f.write(f"  {i:3}. {e}\n")
            f.write("\n")

        if skipped_tasks:
            f.write("SKIPPED RECORDS (not synced — check column mapping or add a title)\n")
            f.write("-" * 64 + "\n")
            for i, t in enumerate(skipped_tasks, 1):
                src    = f"[{t['source']}] " if t.get("source") else ""
                target = t.get("url") or t.get("page_id", "unknown")
                reason = t.get("reason", "")
                f.write(f"  {i:3}. {src}{target}\n       → {reason}\n")

    return log_path
