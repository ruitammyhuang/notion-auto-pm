"""
routes/sync_routes.py  (focal — three-layer relay)
────────────────────────────────────────────────────
Endpoints for running WBS → Master WBS Tasks → Work Sessions sync jobs.

Simplified vs v2:
  - No orphan detection or confirmation dialog
  - backlink_field read from per-source config and passed through
"""

from __future__ import annotations

import os
import threading
import uuid

from flask import Blueprint, jsonify, request

from ..config import (
    BASE_DIR,
    load_sessions_mappings,
    save_sessions_mappings,
)
from ..notion_client import NotionClient
from ..sync_engine import sync_one_database, regenerate_focus_cache
from ..log_writer import write_sync_log

bp = Blueprint("sync", __name__)

# In-memory job store: job_id → {events, done, result, error, cancel_requested}
_sync_jobs: dict[str, dict] = {}


def _is_cancelled(job_id: str | None) -> bool:
    return bool(job_id and _sync_jobs.get(job_id, {}).get("cancel_requested"))


def _run_full_sync(token: str, sources: list, job_id: str | None = None) -> dict:
    """
    Single-phase sync: for each source WBS database, sync tasks directly
    to Work Sessions.  Updates sessions_mappings and rebuilds focus cache.
    """
    client   = NotionClient(token)
    mappings = load_sessions_mappings()

    total = {
        "created": 0, "updated": 0, "skipped": 0, "deleted": 0,
        "errors": [], "new_tasks": [], "skipped_tasks": [],
    }
    source_labels = {s["db_id"]: s.get("db_title", "?") for s in sources}

    def emit(event: dict) -> None:
        if job_id:
            _sync_jobs[job_id]["events"].append(event)

    emit({"type": "start", "total_dbs": len(sources)})

    for db_n, src in enumerate(sources, 1):
        if _is_cancelled(job_id):
            emit({"type": "cancelled"})
            save_sessions_mappings(mappings)
            return total

        db_title = source_labels[src["db_id"]]
        emit({"type": "db_start", "db": db_title,
              "db_n": db_n, "total_dbs": len(sources)})
        try:
            _acps_raw = src.get("auto_calc_planned_start", 7)
            auto_calc_days = (7 if _acps_raw is True
                              else 0 if _acps_raw is False
                              else int(_acps_raw or 0))

            result = sync_one_database(
                client,
                src["db_id"],
                src["project_id"],
                src["field_map"],
                mappings,
                backlink_field=src.get("backlink_field", ""),
                auto_calc_planned_start=auto_calc_days,
                emit=emit,
            )
            total["created"]      += result["created"]
            total["updated"]      += result["updated"]
            total["skipped"]      += result["skipped"]
            total["deleted"]      += result["deleted"]
            total["new_tasks"]    += result["new_tasks"]
            for e in result["errors"]:
                total["errors"].append(f"[{db_title}] {e}")
            for t in result.get("skipped_tasks", []):
                total["skipped_tasks"].append({**t, "source": db_title})

            emit({"type": "db_done", "db": db_title,
                  "created": result["created"], "updated": result["updated"],
                  "skipped": result["skipped"], "deleted": result["deleted"],
                  "errors": len(result["errors"])})

        except Exception as e:
            msg = f"[{db_title}] Fatal: {e}"
            total["errors"].append(msg)
            emit({"type": "db_done", "db": db_title, "created": 0, "updated": 0,
                  "skipped": 0, "deleted": 0, "errors": 1, "fatal": str(e)})

    save_sessions_mappings(mappings)

    # Rebuild focus cache from updated mappings
    try:
        regenerate_focus_cache(client)
    except Exception as e:
        total["errors"].append(f"[focus-cache] {e}")

    log_path = write_sync_log(total)
    if log_path:
        total["log_file"] = os.path.basename(log_path)
        total["log_dir"]  = BASE_DIR

    emit({"type": "finished"})
    return total


@bp.route("/api/sync", methods=["POST"])
def api_sync():
    """Blocking sync — waits until complete before returning."""
    data    = request.json
    token   = data.get("token", "").strip()
    sources = data.get("sources", [])
    if not token:
        return jsonify({"error": "No token"}), 400
    try:
        return jsonify(_run_full_sync(token, sources))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/sync-start", methods=["POST"])
def api_sync_start():
    """Start a background sync.  Returns job_id; poll /api/sync-status."""
    data    = request.json or {}
    token   = data.get("token", "").strip()
    sources = data.get("sources", [])
    if not token:
        return jsonify({"error": "No token"}), 400
    if not sources:
        return jsonify({"error": "No sources selected"}), 400

    job_id = uuid.uuid4().hex[:12]
    _sync_jobs[job_id] = {
        "events":           [],
        "done":             False,
        "result":           None,
        "error":            None,
        "cancel_requested": False,
    }

    def run() -> None:
        try:
            result = _run_full_sync(token, sources, job_id=job_id)
            _sync_jobs[job_id]["result"] = result
        except Exception as e:
            _sync_jobs[job_id]["error"] = str(e)
            _sync_jobs[job_id]["events"].append({"type": "error", "message": str(e)})
        finally:
            _sync_jobs[job_id]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@bp.route("/api/sync-status/<job_id>", methods=["GET"])
def api_sync_status(job_id: str):
    """Poll a running sync job.  Pass ?offset=N to receive only new events."""
    job = _sync_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    offset = max(0, int(request.args.get("offset", 0)))
    return jsonify({
        "events": job["events"][offset:],
        "done":   job["done"],
        "result": job["result"],
        "error":  job["error"],
    })


@bp.route("/api/sync-cancel/<job_id>", methods=["POST"])
def api_sync_cancel(job_id: str):
    """Request the running sync to stop at the next checkpoint."""
    job = _sync_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job_id"}), 404
    job["cancel_requested"] = True
    return jsonify({"ok": True})
