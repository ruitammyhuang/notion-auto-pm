"""
orphan_audit.py
───────────────
Orphan audit engine for the Focal PM system.

Detects 7 categories of broken data in the three-layer architecture:

    Project WBS row  →  Master WBS Task  →  Work Session
                              ↕
                focal_sessions_mappings.json

Cases
─────
  1  WBS source row deleted/archived — Master WBS Task + WS still live
  2  Master WBS Task archived/deleted — Work Session still linked to it
  3  Work Session has no Task relation at all
  4  Mapping file drift — entry references a WBS page that no longer exists
  5  Untracked Master WBS Task — exists in Notion but not in mapping (ghost/duplicate)
  6  Continuation chain broken — a continuation WS exists but its parent chain is orphaned
  7  Partial archive failure — Master archived but linked WS was never cleaned up

Severity grouping
─────────────────
  auto      Safe to clean up automatically (no logged hours, clear dead end)
  review    Has logged hours or ambiguous state — user must decide
  warning   Informational only — no automated action recommended

Public API
──────────
  run_audit(client, sources, emit)  →  AuditResult dict
  fix_items(client, items)          →  FixResult dict   (writes backup to data/)
  restore_backup(client, path)      →  RestoreResult dict
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Callable

from .config import (
    BASE_DIR,
    MASTER_DB_ID,
    WORK_SESSIONS_DB_ID,
    load_sessions_mappings,
    save_sessions_mappings,
)
from .notion_client import NotionClient, extract


# ── Helpers ────────────────────────────────────────────────────────────────────

def _item_id(*parts: str) -> str:
    """Deterministic ID for an audit item so the frontend can reference it."""
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:16]


def _page_status(client: NotionClient, page_id: str) -> str:
    """
    Returns "alive", "archived", "deleted", or "error".
    Never raises.
    """
    try:
        r = client.get_page(page_id)
        if r.status_code == 404:
            return "deleted"
        if not r.ok:
            return "error"
        return "archived" if r.json().get("archived") else "alive"
    except Exception:
        return "error"


def _get_title(client: NotionClient, page_id: str) -> str:
    """Best-effort title fetch. Returns empty string on failure."""
    try:
        r = client.get_page(page_id)
        if r.ok:
            props = r.json().get("properties", {})
            for v in props.values():
                if v.get("type") == "title":
                    return "".join(p.get("plain_text", "") for p in v.get("title", []))
    except Exception:
        pass
    return ""


def _has_session_end(ws_page: dict) -> bool:
    """True if Session End date is set on a Work Session page dict."""
    se = ws_page.get("properties", {}).get("Session End", {})
    if se.get("type") == "date":
        return se.get("date") is not None
    return False


def _ws_title(ws_page: dict) -> str:
    props = ws_page.get("properties", {})
    for v in props.values():
        if v.get("type") == "title":
            return "".join(p.get("plain_text", "") for p in v.get("title", []))
    return "(untitled)"


def _is_continuation(name: str) -> bool:
    """True if name has a continuation suffix pattern like 'Task name-2'."""
    import re
    return bool(re.search(r'-\d+$', name))


# ── Backup / Restore ───────────────────────────────────────────────────────────

def _write_backup(items: list[dict]) -> str:
    """
    Write a backup JSON to data/ before any fix action.
    Returns the backup file path.
    """
    data_dir = os.path.join(BASE_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(data_dir, f"orphan_audit_backup_{ts}.json")
    backup = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "restore_hint": f"Use /api/orphan-restore with this file path to undo.",
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)
    return path


# ── Main audit ─────────────────────────────────────────────────────────────────

def run_audit(
    client: NotionClient,
    sources: dict,
    emit: Callable[[dict], None] | None = None,
) -> dict:
    """
    Run the full 7-case orphan audit.

    Parameters
    ----------
    client  : authenticated NotionClient
    sources : config["sources"] dict  {db_id → {project_id, db_title, ...}}
    emit    : optional progress callback  emit({"type": ..., "msg": ..., "pct": N})

    Returns
    -------
    AuditResult dict with keys:
        generated_at, summary, auto_fixable, needs_review, warnings
    """

    def _emit(msg: str, pct: int, event_type: str = "progress"):
        if emit:
            emit({"type": event_type, "msg": msg, "pct": pct})

    auto_fixable: list[dict] = []
    needs_review: list[dict] = []
    warnings: list[dict] = []

    # ── Phase 0: Load local state ──────────────────────────────────────────────
    _emit("Loading local mapping file…", 2)
    mappings = load_sessions_mappings()

    # Build index structures
    known_master_ids: set[str] = set()
    known_ws_ids: set[str] = set()
    master_to_wbs: dict[str, str] = {}   # master_id → wbs_key
    ws_to_wbs: dict[str, str] = {}        # ws_id → wbs_key

    for wbs_key, entry in mappings.items():
        if not isinstance(entry, dict):
            continue
        mid = entry.get("master_id", "")
        wid = entry.get("ws_id", "")
        if mid:
            known_master_ids.add(mid)
            master_to_wbs[mid] = wbs_key
        if wid:
            known_ws_ids.add(wid)
            ws_to_wbs[wid] = wbs_key

    # ── Phase 1: Fetch all Work Sessions ──────────────────────────────────────
    _emit("Fetching all Work Sessions from Notion…", 5)
    try:
        all_ws = client.query_db(WORK_SESSIONS_DB_ID)
    except Exception as e:
        return {"error": f"Failed to fetch Work Sessions: {e}"}

    ws_by_id: dict[str, dict] = {ws["id"]: ws for ws in all_ws}

    _emit(f"Found {len(all_ws)} Work Sessions. Scanning…", 10)

    # ── Phase 2: Case 3 — WS with no Task relation ────────────────────────────
    _emit("Checking for Work Sessions with no Task relation…", 12)
    master_ids_from_ws: set[str] = set()

    for ws in all_ws:
        ws_id    = ws["id"]
        task_rel = ws.get("properties", {}).get("Task", {}).get("relation", [])
        has_end  = _has_session_end(ws)
        name     = _ws_title(ws)

        if not task_rel:
            item = {
                "id":           _item_id("c3", ws_id),
                "case":         3,
                "case_label":   "No Task relation",
                "ws_id":        ws_id,
                "ws_title":     name,
                "ws_url":       f"https://notion.so/{ws_id.replace('-','')}",
                "has_hours":    has_end,
                "in_mapping":   ws_id in known_ws_ids,
            }
            if has_end:
                item["severity"] = "review"
                item["reason"]   = "Work Session has logged hours but is unlinked from any task. Consider archiving or re-linking manually."
                needs_review.append(item)
            else:
                item["severity"] = "auto"
                item["action"]   = "archive_ws"
                item["reason"]   = "Placeholder session — no hours logged, no task linked."
                auto_fixable.append(item)
            continue

        master_id = task_rel[0]["id"]
        master_ids_from_ws.add(master_id)

    # ── Phase 3: Check WBS source pages (Cases 1, 4) ─────────────────────────
    _emit("Checking WBS source pages in Notion…", 15)
    total_mappings  = len(mappings)
    checked_wbs     = 0
    # Master IDs that need explicit checks (dead WBS source)
    dead_wbs_entries: list[tuple[str, dict]] = []   # [(wbs_key, entry)]

    for wbs_key, entry in mappings.items():
        if not isinstance(entry, dict):
            checked_wbs += 1
            continue

        pct = 15 + int((checked_wbs / max(total_mappings, 1)) * 35)
        if checked_wbs % 20 == 0:
            _emit(f"WBS check {checked_wbs}/{total_mappings}…", pct)

        status = _page_status(client, wbs_key)
        master_id  = entry.get("master_id", "")
        ws_id      = entry.get("ws_id", "")
        task_name  = entry.get("name", "")
        project    = entry.get("project_name", "")
        source_db  = entry.get("source_db_id", "")
        db_title   = sources.get(source_db, {}).get("db_title", "") if isinstance(sources, dict) else ""

        if status in ("alive",):
            # WBS page fine — mapping entry is valid, skip
            checked_wbs += 1
            continue

        # WBS page is gone or archived → Case 4 (mapping drift) + potential Case 1
        if status in ("deleted", "archived", "error"):
            dead_wbs_entries.append((wbs_key, entry))

            # Case 4: flag mapping drift (always)
            auto_fixable.append({
                "id":          _item_id("c4", wbs_key),
                "case":        4,
                "case_label":  "Stale mapping entry",
                "severity":    "auto",
                "action":      "remove_mapping",
                "wbs_key":     wbs_key,
                "master_id":   master_id,
                "ws_id":       ws_id,
                "task_name":   task_name,
                "project":     project,
                "db_title":    db_title,
                "wbs_status":  status,
                "reason":      f"WBS source page is {status} — mapping entry is stale and can be removed.",
            })

        checked_wbs += 1

    # ── Phase 4: Case 1 — check Masters for dead WBS entries ─────────────────
    _emit("Checking Master WBS Tasks for dead WBS sources…", 52)
    for wbs_key, entry in dead_wbs_entries:
        master_id  = entry.get("master_id", "")
        ws_id      = entry.get("ws_id", "")
        task_name  = entry.get("name", "")
        project    = entry.get("project_name", "")
        source_db  = entry.get("source_db_id", "")
        db_title   = sources.get(source_db, {}).get("db_title", "") if isinstance(sources, dict) else ""

        if not master_id:
            continue

        master_status = _page_status(client, master_id)
        if master_status != "alive":
            # Master already gone too — no Case 1, just Case 4 cleanup is sufficient
            continue

        # Master is still alive — Case 1
        ws_page  = ws_by_id.get(ws_id)
        has_end  = _has_session_end(ws_page) if ws_page else False

        item = {
            "id":          _item_id("c1", wbs_key, master_id),
            "case":        1,
            "case_label":  "WBS row deleted — Master + Session still live",
            "wbs_key":     wbs_key,
            "master_id":   master_id,
            "ws_id":       ws_id,
            "task_name":   task_name,
            "project":     project,
            "db_title":    db_title,
            "has_hours":   has_end,
            "master_url":  f"https://notion.so/{master_id.replace('-','')}",
        }

        if has_end:
            item["severity"] = "review"
            item["reason"]   = "WBS source row is gone but this session has logged hours. Preserve for history or archive manually."
            needs_review.append(item)
        else:
            item["severity"] = "auto"
            item["action"]   = "archive_master_and_ws"
            item["reason"]   = "WBS source row is gone and no hours were logged. Safe to archive."
            auto_fixable.append(item)

    # ── Phase 5: Cases 2 and 7 — WS linked to archived/deleted Master ─────────
    _emit("Checking Master WBS Task status for all Work Sessions…", 60)
    unique_masters_to_check = master_ids_from_ws - known_master_ids
    all_masters_to_check = master_ids_from_ws  # check all to catch Case 2

    checked_masters  = 0
    total_to_check   = len(all_masters_to_check)
    # Cache: master_id → status (to avoid re-checking in Case 5)
    master_status_cache: dict[str, str] = {}

    for ws in all_ws:
        ws_id    = ws["id"]
        task_rel = ws.get("properties", {}).get("Task", {}).get("relation", [])
        if not task_rel:
            continue

        master_id = task_rel[0]["id"]
        has_end   = _has_session_end(ws)
        ws_name   = _ws_title(ws)

        # Get (cached) master status
        if master_id not in master_status_cache:
            pct = 60 + int((checked_masters / max(total_to_check, 1)) * 20)
            if checked_masters % 20 == 0:
                _emit(f"Master check {checked_masters}/{total_to_check}…", pct)
            master_status_cache[master_id] = _page_status(client, master_id)
            checked_masters += 1

        m_status = master_status_cache[master_id]
        if m_status == "alive":
            continue

        # Master is dead — Case 2 (or Case 7 if no hours and master is archived)
        wbs_key_for_ws = ws_to_wbs.get(ws_id, "")
        item = {
            "id":          _item_id("c2", ws_id, master_id),
            "case":        2,
            "case_label":  "Master WBS Task archived/deleted — Work Session still linked",
            "ws_id":       ws_id,
            "ws_title":    ws_name,
            "ws_url":      f"https://notion.so/{ws_id.replace('-','')}",
            "master_id":   master_id,
            "master_status": m_status,
            "has_hours":   has_end,
            "in_mapping":  bool(wbs_key_for_ws),
        }

        # Case 7 check: is this a partial archive failure?
        # (Master archived/deleted but WS not archived, no hours)
        case_num = 7 if (m_status == "archived" and not has_end) else 2

        if not has_end:
            item["case"]       = case_num
            item["case_label"] = ("Partial archive — Master archived but WS not cleaned up"
                                  if case_num == 7
                                  else "Master deleted — orphaned Work Session (no hours)")
            item["severity"]   = "auto"
            item["action"]     = "archive_ws"
            item["reason"]     = ("Master was archived but this placeholder session was never cleaned up."
                                  if case_num == 7
                                  else "Master task is gone and no hours were logged. Safe to archive.")
            auto_fixable.append(item)
        else:
            item["severity"] = "review"
            item["reason"]   = (f"Master WBS Task is {m_status} but this session has logged hours. "
                                "Keep for historical record or archive if no longer needed.")
            needs_review.append(item)

    # ── Phase 6: Case 5 — Untracked Master WBS Tasks (ghost/duplicate rows) ───
    _emit("Scanning for untracked Master WBS Tasks per project…", 82)
    for db_id, src in (sources.items() if isinstance(sources, dict) else []):
        project_id = src.get("project_id", "")
        db_title   = src.get("db_title", db_id)
        if not project_id:
            continue
        try:
            master_pages = client.query_db(MASTER_DB_ID, filter_body={
                "property": "Project",
                "relation": {"contains": project_id},
            })
        except Exception:
            continue

        for m_page in master_pages:
            mid = m_page["id"]
            if mid in known_master_ids:
                continue  # tracked — fine
            if m_page.get("archived"):
                continue  # already archived — skip

            title  = ""
            for v in m_page.get("properties", {}).values():
                if v.get("type") == "title":
                    title = "".join(p.get("plain_text", "") for p in v.get("title", []))
                    break
            ws_rel = m_page.get("properties", {}).get("Work Sessions", {}).get("relation", [])
            ws_ids = [r["id"] for r in ws_rel]
            has_hrs = any(
                _has_session_end(ws_by_id[wid]) if wid in ws_by_id
                else False
                for wid in ws_ids
            )

            warnings.append({
                "id":          _item_id("c5", mid),
                "case":        5,
                "case_label":  "Untracked Master WBS Task",
                "severity":    "warning",
                "master_id":   mid,
                "master_title": title,
                "master_url":  f"https://notion.so/{mid.replace('-','')}",
                "project":     db_title,
                "ws_count":    len(ws_ids),
                "has_hours":   has_hrs,
                "reason":      ("This Master WBS Task exists in Notion but is not tracked in the mapping file. "
                                "It may be a duplicate created by a failed sync, or a manually created entry."),
            })

    # ── Phase 7: Case 6 — Broken continuation chains ─────────────────────────
    _emit("Checking continuation session chains…", 92)
    # Collect all WS names that are continuations
    orphaned_master_ids = {
        item.get("master_id") for item in (auto_fixable + needs_review)
        if item.get("master_id")
    }
    for ws in all_ws:
        ws_id   = ws["id"]
        ws_name = _ws_title(ws)
        if not _is_continuation(ws_name):
            continue
        task_rel  = ws.get("properties", {}).get("Task", {}).get("relation", [])
        master_id = task_rel[0]["id"] if task_rel else None
        if not master_id:
            continue
        # If this continuation WS's master is itself fine but another item in the
        # chain is being flagged, warn about the chain.
        if master_id in orphaned_master_ids:
            continue  # already flagged as Case 1/2
        # Check: is this continuation WS's master alive but the chain context is broken?
        # (e.g. parent -1 is orphaned but -2 is fine)
        # We flag these as warnings since automated fix is risky.
        import re
        base_name = re.sub(r'-\d+$', '', ws_name)
        chain_orphaned = any(
            base_name in item.get("task_name", "")
            for item in (auto_fixable + needs_review)
        )
        if chain_orphaned:
            warnings.append({
                "id":          _item_id("c6", ws_id),
                "case":        6,
                "case_label":  "Continuation chain — parent task is orphaned",
                "severity":    "warning",
                "ws_id":       ws_id,
                "ws_title":    ws_name,
                "ws_url":      f"https://notion.so/{ws_id.replace('-','')}",
                "master_id":   master_id,
                "reason":      (f"'{ws_name}' is a continuation session but its parent task chain "
                                "contains orphaned entries. Review the full chain before cleaning up."),
            })

    _emit("Audit complete.", 100, "done")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_mappings":    len(mappings),
            "total_ws":          len(all_ws),
            "auto_fixable":      len(auto_fixable),
            "needs_review":      len(needs_review),
            "warnings":          len(warnings),
        },
        "auto_fixable": auto_fixable,
        "needs_review":  needs_review,
        "warnings":      warnings,
    }


# ── Fix ────────────────────────────────────────────────────────────────────────

def fix_items(client: NotionClient, items: list[dict]) -> dict:
    """
    Execute fix actions for a list of audit items.
    Always writes a backup to data/ first.

    Each item must have: id, action, and the relevant IDs (master_id, ws_id, etc.)

    Actions:
      archive_master_and_ws  →  archive Master WBS Task + WS, remove mapping entry
      archive_ws             →  archive WS only, remove ws ref from mapping
      archive_ghost_master   →  archive untracked Master + its linked WS
      remove_mapping         →  remove stale entry from mapping file (no Notion call)
    """
    if not items:
        return {"ok": True, "archived": 0, "mapping_cleaned": 0, "backup_file": None, "errors": []}

    # Write backup first
    backup_path = _write_backup(items)
    errors: list[str] = []
    archived    = 0
    map_cleaned = 0

    mappings = load_sessions_mappings()

    def _archive(page_id: str, label: str) -> bool:
        nonlocal archived
        if not page_id:
            return False
        try:
            r = client.patch_page(page_id, {"archived": True})
            if r.ok:
                archived += 1
                return True
            errors.append(f"Archive {label} ({page_id[:8]}…): {r.status_code}")
            return False
        except Exception as e:
            errors.append(f"Archive {label} ({page_id[:8]}…): {e}")
            return False

    for item in items:
        action    = item.get("action", "")
        master_id = item.get("master_id", "")
        ws_id     = item.get("ws_id", "")
        wbs_key   = item.get("wbs_key", "")

        if action == "archive_master_and_ws":
            _archive(ws_id,     "Work Session")
            _archive(master_id, "Master WBS Task")
            # Remove from mapping
            if wbs_key and wbs_key in mappings:
                del mappings[wbs_key]
                map_cleaned += 1
            elif master_id:
                # Find by master_id
                to_del = [k for k, v in mappings.items()
                          if isinstance(v, dict) and v.get("master_id") == master_id]
                for k in to_del:
                    del mappings[k]
                    map_cleaned += 1

        elif action == "archive_ws":
            _archive(ws_id, "Work Session")
            # Remove ws_id from mapping entry (but keep the entry)
            if ws_id:
                for k, v in mappings.items():
                    if isinstance(v, dict) and v.get("ws_id") == ws_id:
                        del mappings[k]
                        map_cleaned += 1
                        break

        elif action == "archive_ghost_master":
            # Archive the untracked master and its WS
            _archive(master_id, "Ghost Master WBS Task")
            for wid in item.get("ws_ids", []):
                _archive(wid, "Ghost Work Session")

        elif action == "remove_mapping":
            # Just clean the mapping — no Notion call
            if wbs_key and wbs_key in mappings:
                del mappings[wbs_key]
                map_cleaned += 1

    save_sessions_mappings(mappings)

    return {
        "ok":             len(errors) == 0,
        "archived":       archived,
        "mapping_cleaned": map_cleaned,
        "backup_file":    backup_path,
        "errors":         errors,
    }


# ── Restore ────────────────────────────────────────────────────────────────────

def restore_backup(client: NotionClient, backup_path: str) -> dict:
    """
    Unarchive all pages recorded in a backup file.
    Does NOT restore mapping entries (re-run sync to rebuild the mapping).
    """
    if not os.path.exists(backup_path):
        return {"ok": False, "error": f"Backup file not found: {backup_path}"}

    with open(backup_path, encoding="utf-8") as f:
        backup = json.load(f)

    items     = backup.get("items", [])
    restored  = 0
    errors: list[str] = []

    for item in items:
        for page_id in [item.get("master_id", ""), item.get("ws_id", "")]:
            if not page_id:
                continue
            try:
                r = client.patch_page(page_id, {"archived": False})
                if r.ok:
                    restored += 1
                else:
                    errors.append(f"Restore {page_id[:8]}…: {r.status_code}")
            except Exception as e:
                errors.append(f"Restore {page_id[:8]}…: {e}")

    return {
        "ok":       len(errors) == 0,
        "restored": restored,
        "errors":   errors,
    }
