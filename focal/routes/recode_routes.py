"""
routes/recode_routes.py
────────────────────────
Audited work type recoding with per-record rollback.

Endpoints:
  POST /api/recode/apply                 -- apply selected recodings from a health check report
  POST /api/recode/rollback/<audit_id>   -- roll back one specific recode
  GET  /api/recode/history/<record_id>   -- full recode history for a record
  GET  /api/recode/pending               -- proposed recodings not yet applied
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ..config import (
    BASE_DIR,
    load_config,
    load_sessions_mappings,
    save_sessions_mappings,
)
from ..notion_client import NotionClient
from ..sync_engine import regenerate_focus_cache
from ..work_type_manager import get_valid_names

bp = Blueprint("recode", __name__)

# ── Audit log ──────────────────────────────────────────────────────────────────
AUDIT_FILE = os.path.join(BASE_DIR, "recoding_audit.json")


def _load_audit() -> list[dict]:
    if not os.path.exists(AUDIT_FILE):
        return []
    with open(AUDIT_FILE, encoding="utf-8") as f:
        return json.load(f)


def _append_audit(entry: dict) -> None:
    audit = _load_audit()
    audit.append(entry)
    with open(AUDIT_FILE, "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2, ensure_ascii=False)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _new_audit_id() -> str:
    return f"ra_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


# ── Core recode logic (shared by apply and rollback) ──────────────────────────

def _apply_work_type_change(
    client: NotionClient,
    cfg: dict,
    mappings: dict,
    wbs_page_id: str,
    new_work_type: str,
) -> dict:
    """
    Update work type on the Work Session and source WBS row for a given wbs_page_id.

    Returns: {ws_updated: bool, wbs_updated: bool, warning?: str}
    """
    info         = mappings.get(wbs_page_id, {})
    ws_id        = info.get("ws_id", "") if isinstance(info, dict) else ""
    source_db_id = info.get("source_db_id", "") if isinstance(info, dict) else ""

    result: dict = {"ws_updated": False, "wbs_updated": False}

    # Update Work Session
    if ws_id:
        r = client.patch_page(ws_id, {
            "properties": {"Work Type": {"select": {"name": new_work_type}}}
        })
        result["ws_updated"] = r.ok

    # Update source WBS row
    work_type_field = cfg.get("sources", {}).get(source_db_id, {}).get("field_map", {}).get("work_type", "")
    if work_type_field:
        wr = client.patch_page(wbs_page_id, {
            "properties": {work_type_field: {"select": {"name": new_work_type}}}
        })
        result["wbs_updated"] = wr.ok
    else:
        result["warning"] = "No work_type field mapped for this source WBS"

    return result


# ── Routes ─────────────────────────────────────────────────────────────────────

@bp.route("/api/recode/apply", methods=["POST"])
def api_recode_apply():
    """
    Apply a list of selected recodings from a health check report.

    Body (JSON):
        batch_id    str   -- health check batch_id (for audit linking)
        recodings   list  -- [{record_id, ws_id, suggested_work_type, confidence, rationale, current_work_type}]

    Returns:
        { ok, applied: int, failed: int, locked: [record_id], errors: [str], audit_ids: [str] }
    """
    body      = request.json or {}
    batch_id  = body.get("batch_id", "unknown")
    recodings = body.get("recodings", [])

    if not recodings:
        return jsonify({"error": "recodings list is required and must not be empty"}), 400

    valid_wt = get_valid_names()
    cfg      = load_config()
    token    = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No Notion token configured"}), 400

    client   = NotionClient(token)
    mappings = load_sessions_mappings()

    applied = 0
    failed  = 0
    errors: list[str] = []
    audit_ids: list[str] = []

    for rec in recodings:
        record_id     = rec.get("record_id", "")
        suggested_wt  = rec.get("suggested_work_type", "")
        current_wt    = rec.get("current_work_type", "")
        confidence    = rec.get("confidence", 0)
        rationale     = rec.get("rationale", "")

        if not record_id:
            errors.append("Skipped: missing record_id")
            failed += 1
            continue
        if suggested_wt not in valid_wt:
            errors.append(f"{record_id}: invalid work_type '{suggested_wt}'")
            failed += 1
            continue

        try:
            change_result = _apply_work_type_change(client, cfg, mappings, record_id, suggested_wt)
        except Exception as e:
            errors.append(f"{record_id}: {e}")
            failed += 1
            continue

        if not change_result.get("ws_updated") and not change_result.get("wbs_updated"):
            errors.append(f"{record_id}: Notion update failed")
            failed += 1
            continue

        # Update sessions_mappings with new work_type + lock flag
        if record_id in mappings and isinstance(mappings[record_id], dict):
            audit_id = _new_audit_id()
            mappings[record_id]["work_type"]         = suggested_wt
            mappings[record_id]["locked_work_type"]  = True
            recode_history = mappings[record_id].get("recode_history", [])
            recode_history.append(audit_id)
            mappings[record_id]["recode_history"]    = recode_history

            _append_audit({
                "audit_id":          audit_id,
                "batch_id":          batch_id,
                "record_id":         record_id,
                "ws_id":             mappings[record_id].get("ws_id", ""),
                "record_type":       "work_session",
                "timestamp":         _now_iso(),
                "action":            "recode",
                "from_work_type":    current_wt,
                "to_work_type":      suggested_wt,
                "confidence":        confidence,
                "rationale":         rationale,
                "applied_by":        "user",
                "notion_result":     change_result,
            })
            audit_ids.append(audit_id)
            applied += 1
        else:
            errors.append(f"{record_id}: not found in sessions_mappings — Notion updated but mapping not locked")
            applied += 1  # Notion was updated, just couldn't lock

    save_sessions_mappings(mappings)

    # Refresh focus cache
    try:
        token = cfg.get("token", "")
        if token:
            client2 = NotionClient(token)
            regenerate_focus_cache(client2)
    except Exception:
        pass

    return jsonify({
        "ok":        True,
        "applied":   applied,
        "failed":    failed,
        "errors":    errors,
        "audit_ids": audit_ids,
    })


@bp.route("/api/recode/rollback/<audit_id>", methods=["POST"])
def api_recode_rollback(audit_id: str):
    """
    Roll back a single recode to the work type it had before.

    Returns: { ok, record_id, reverted_to, rollback_audit_id }
    """
    audit = _load_audit()
    original_entry = next((a for a in audit if a.get("audit_id") == audit_id), None)
    if not original_entry:
        return jsonify({"error": f"Audit entry {audit_id} not found"}), 404

    action = original_entry.get("action", "")
    if action == "rollback":
        return jsonify({"error": "Cannot rollback a rollback. Use reapply instead."}), 400

    record_id   = original_entry.get("record_id", "")
    revert_to   = original_entry.get("from_work_type", "")
    current_wt  = original_entry.get("to_work_type", "")

    valid_wt = get_valid_names()
    if revert_to not in valid_wt:
        return jsonify({"error": f"Cannot revert: '{revert_to}' is no longer a valid work type"}), 400

    cfg   = load_config()
    token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No Notion token configured"}), 400

    client   = NotionClient(token)
    mappings = load_sessions_mappings()

    try:
        change_result = _apply_work_type_change(client, cfg, mappings, record_id, revert_to)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Update sessions_mappings
    rollback_audit_id = _new_audit_id()
    if record_id in mappings and isinstance(mappings[record_id], dict):
        mappings[record_id]["work_type"]  = revert_to
        # Keep locked (still manually managed, just reverted)
        mappings[record_id]["locked_work_type"] = True
        mappings[record_id].setdefault("recode_history", []).append(rollback_audit_id)

    save_sessions_mappings(mappings)

    _append_audit({
        "audit_id":            rollback_audit_id,
        "batch_id":            original_entry.get("batch_id", ""),
        "record_id":           record_id,
        "ws_id":               original_entry.get("ws_id", ""),
        "record_type":         original_entry.get("record_type", "work_session"),
        "timestamp":           _now_iso(),
        "action":              "rollback",
        "from_work_type":      current_wt,
        "to_work_type":        revert_to,
        "confidence":          original_entry.get("confidence", 0),
        "rationale":           f"Rollback of audit_id={audit_id}",
        "applied_by":          "user",
        "rolls_back_audit_id": audit_id,
        "notion_result":       change_result,
    })

    # Refresh focus cache
    try:
        regenerate_focus_cache(client)
    except Exception:
        pass

    return jsonify({
        "ok":                True,
        "record_id":         record_id,
        "reverted_to":       revert_to,
        "rollback_audit_id": rollback_audit_id,
    })


@bp.route("/api/recode/history/<record_id>", methods=["GET"])
def api_recode_history(record_id: str):
    """Return the full recode/rollback history for a specific record."""
    audit   = _load_audit()
    history = [a for a in audit if a.get("record_id") == record_id]
    history.sort(key=lambda a: a.get("timestamp", ""))

    mappings = load_sessions_mappings()
    info     = mappings.get(record_id, {})
    current  = info.get("work_type", "") if isinstance(info, dict) else ""
    locked   = info.get("locked_work_type", False) if isinstance(info, dict) else False

    return jsonify({
        "record_id":      record_id,
        "current_type":   current,
        "locked":         locked,
        "history":        history,
    })


@bp.route("/api/recode/pending", methods=["GET"])
def api_recode_pending():
    """
    Return proposed recodings from the most recent health check that have not yet been applied.

    Query params:
        batch_id        -- optional, defaults to most recent report
        min_confidence  -- int, filter threshold (default 0)
    """
    batch_id       = request.args.get("batch_id", "")
    min_confidence = int(request.args.get("min_confidence", 0))

    reports_dir = os.path.join(BASE_DIR, "health_check_reports")
    index_path  = os.path.join(reports_dir, "index.json")
    if not os.path.exists(index_path):
        return jsonify({"pending": [], "batch_id": None})

    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)
    if not index:
        return jsonify({"pending": [], "batch_id": None})

    if not batch_id:
        batch_id = index[0].get("batch_id", "")

    report_path = os.path.join(reports_dir, f"report_{batch_id}.json")
    if not os.path.exists(report_path):
        return jsonify({"pending": [], "batch_id": batch_id, "error": "Report not found"})

    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)

    # Filter out already-applied recodings
    audit       = _load_audit()
    applied_ids = {a["record_id"] for a in audit if a.get("action") == "recode" and a.get("batch_id") == batch_id}

    pending = [
        r for r in report.get("proposed_recodings", [])
        if r.get("record_id") not in applied_ids
        and r.get("confidence", 0) >= min_confidence
    ]

    return jsonify({"pending": pending, "batch_id": batch_id, "total": len(pending)})
