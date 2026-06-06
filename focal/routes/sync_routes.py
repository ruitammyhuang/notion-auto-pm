"""
routes/sync_routes.py
──────────────────────
Endpoints for running WBS → Master WBS → Work Sessions sync jobs.
Includes polling-based background sync (sync-start / sync-status).
"""

from __future__ import annotations

import json
import os
import threading
import uuid

from flask import Blueprint, jsonify, request

from ..config import (
    BASE_DIR,
    load_mappings,
    save_mappings,
    load_sessions_mappings,
    save_sessions_mappings,
)
from ..notion_client import NotionClient
from ..sync_engine import (
    sync_one_database,
    sync_work_sessions_for_project,
    cleanup_orphaned_mappings,
)
from ..log_writer import write_sync_log

bp = Blueprint("sync", __name__)

# In-memory job store: job_id → {events, done, result, error}
_sync_jobs: dict[str, dict] = {}


def _run_full_sync(token: str, sources: list, job_id: str | None = None) -> dict:
    """
    Core sync logic shared by /api/sync (blocking) and /api/sync-start (threaded).
    Returns the totals dict. If job_id is set, publishes events to _sync_jobs.
    """
    client            = NotionClient(token)
    mappings          = load_mappings()
    sessions_mappings = load_sessions_mappings()
    total = {
        "created": 0, "updated": 0, "skipped": 0, "deleted": 0,
        "ws_created": 0, "ws_skipped": 0,
        "errors": [], "new_tasks": [], "skipped_tasks": [],
    }
    source_labels = {s["db_id"]: s.get("db_title", "?") for s in sources}

    def emit(event: dict) -> None:
        if job_id:
            _sync_jobs[job_id]["events"].append(event)

    emit({"type": "start", "total_dbs": len(sources)})

    # Phase 1: Project WBS → Master WBS Tasks
    all_current_src_ids: set = set()
    for db_n, src in enumerate(sources, 1):
        db_title = source_labels[src["db_id"]]
        emit({"type": "db_start", "db": db_title,
              "db_n": db_n, "total_dbs": len(sources)})
        try:
            wt_map = src.get("work_type_map") or {}
            if isinstance(wt_map, str):
                try:
                    wt_map = json.loads(wt_map)
                except Exception:
                    wt_map = {}

            result = sync_one_database(
                client,
                src["db_id"],
                src["project_id"],
                src["field_map"],
                mappings,
                backlink_field=src.get("backlink_field", "Master WBS"),
                work_type_map=wt_map,
                auto_calc_planned_start=src.get("auto_calc_planned_start", True),
                sessions_mappings=sessions_mappings,
                emit=emit,
            )
            all_current_src_ids |= result.get("current_src_ids", set())
            total["created"]   += result["created"]
            total["updated"]   += result["updated"]
            total["skipped"]   += result["skipped"]
            total["deleted"]   += result["deleted"]
            total["new_tasks"] += result["new_tasks"]
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

    # Clean up legacy db=None orphans
    orphans = cleanup_orphaned_mappings(
        client, all_current_src_ids, mappings, sessions_mappings)
    if orphans:
        total["deleted"] += orphans
        emit({"type": "orphans_cleaned", "count": orphans})

    save_mappings(mappings)

    # Phase 2: Master WBS Tasks → Work Sessions
    processed_projects: set = set()
    unique_srcs = [
        s for s in sources
        if s.get("project_id")
        and s["project_id"] not in processed_projects
        and not processed_projects.add(s["project_id"])
    ]
    emit({"type": "phase2_start", "project_count": len(unique_srcs)})

    for ws_n, src in enumerate(unique_srcs, 1):
        db_title   = source_labels[src["db_id"]]
        project_id = src["project_id"]
        try:
            ws_result = sync_work_sessions_for_project(client, project_id, sessions_mappings)
            total["ws_created"] += ws_result["created"]
            total["ws_skipped"] += ws_result["skipped"]
            for e in ws_result["errors"]:
                total["errors"].append(f"[{db_title} · Sessions] {e}")
            emit({"type": "ws_done", "project": db_title,
                  "n": ws_n, "total": len(unique_srcs),
                  "created": ws_result["created"], "skipped": ws_result["skipped"]})
        except Exception as e:
            total["errors"].append(f"[{db_title} · Sessions] Fatal: {e}")
            emit({"type": "ws_done", "project": db_title,
                  "n": ws_n, "total": len(unique_srcs),
                  "created": 0, "skipped": 0, "error": str(e)})

    save_sessions_mappings(sessions_mappings)

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
        total = _run_full_sync(token, sources)
        return jsonify(total)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/sync-start", methods=["POST"])
def api_sync_start():
    """Start a background sync job. Returns job_id immediately; poll /api/sync-status."""
    data    = request.json or {}
    token   = data.get("token", "").strip()
    sources = data.get("sources", [])
    if not token:
        return jsonify({"error": "No token"}), 400
    if not sources:
        return jsonify({"error": "No sources selected"}), 400

    job_id = uuid.uuid4().hex[:12]
    _sync_jobs[job_id] = {"events": [], "done": False, "result": None, "error": None}

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
    """Poll progress of a running sync job. Pass ?offset=N to receive only new events."""
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


@bp.route("/api/sync-work-sessions", methods=["POST"])
def api_sync_work_sessions():
    """Idempotent Work Sessions sync — creates sessions for tasks that lack one."""
    body    = request.json
    token   = body.get("token", "").strip()
    sources = body.get("sources", [])
    if not token:
        return jsonify({"error": "No token"}), 400

    client            = NotionClient(token)
    sessions_mappings = load_sessions_mappings()
    total = {"created": 0, "skipped": 0, "errors": []}

    for src in sources:
        project_id = src.get("project_id", "")
        db_title   = src.get("db_title", "?")
        if not project_id:
            continue
        try:
            result = sync_work_sessions_for_project(client, project_id, sessions_mappings)
            total["created"] += result["created"]
            total["skipped"] += result["skipped"]
            for e in result["errors"]:
                total["errors"].append(f"[{db_title}] {e}")
        except Exception as e:
            total["errors"].append(f"[{db_title}] Fatal: {e}")

    save_sessions_mappings(sessions_mappings)
    return jsonify(total)
