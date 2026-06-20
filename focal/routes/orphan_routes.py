"""
routes/orphan_routes.py
────────────────────────
Orphan audit endpoints.

Routes
──────
  POST /api/orphan-audit/start    → start background audit, return {job_id}
  GET  /api/orphan-audit/stream/<job_id> → SSE progress stream; final event has full results
  POST /api/orphan-fix            → fix a list of items (synchronous)
  POST /api/orphan-restore        → restore from a backup file (synchronous)
  GET  /api/orphan-backups        → list available backup files in data/
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, stream_with_context

from ..config import BASE_DIR, load_config
from ..notion_client import NotionClient
from ..orphan_audit import fix_items, restore_backup, run_audit

bp = Blueprint("orphan", __name__)

# In-memory job store: job_id → {events, done, result, error}
_audit_jobs: dict[str, dict] = {}


# ── Background runner ──────────────────────────────────────────────────────────

def _run_audit_thread(job_id: str, token: str, sources: dict) -> None:
    job = _audit_jobs[job_id]

    def emit(event: dict) -> None:
        job["events"].append(event)

    try:
        client = NotionClient(token)
        result = run_audit(client, sources, emit=emit)
        job["result"] = result
    except Exception as e:
        job["error"] = str(e)
        job["events"].append({"type": "error", "msg": str(e), "pct": 100})
    finally:
        job["done"] = True


# ── Routes ─────────────────────────────────────────────────────────────────────

@bp.route("/api/orphan-audit/start", methods=["POST"])
def api_orphan_audit_start():
    cfg   = load_config()
    token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No Notion token configured"}), 400

    sources  = cfg.get("sources", {})
    job_id   = str(uuid.uuid4())
    _audit_jobs[job_id] = {"events": [], "done": False, "result": None, "error": None}

    t = threading.Thread(
        target=_run_audit_thread,
        args=(job_id, token, sources),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@bp.route("/api/orphan-audit/stream/<job_id>")
def api_orphan_audit_stream(job_id: str):
    if job_id not in _audit_jobs:
        return jsonify({"error": "Unknown job_id"}), 404

    @stream_with_context
    def generate():
        import time
        cursor = 0
        while True:
            job = _audit_jobs.get(job_id, {})
            events = job.get("events", [])

            while cursor < len(events):
                evt = events[cursor]
                yield f"data: {json.dumps(evt)}\n\n"
                cursor += 1

            if job.get("done"):
                result = job.get("result")
                error  = job.get("error")
                if error:
                    yield f"data: {json.dumps({'type': 'error', 'msg': error})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'result', 'result': result})}\n\n"
                # Clean up job after streaming
                _audit_jobs.pop(job_id, None)
                break

            time.sleep(0.3)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":       "no-cache",
            "X-Accel-Buffering":   "no",
            "Transfer-Encoding":   "chunked",
        },
    )


@bp.route("/api/orphan-fix", methods=["POST"])
def api_orphan_fix():
    """
    Fix a list of audit items.

    Body: { "items": [{id, action, master_id, ws_id, wbs_key, ...}, ...] }
    """
    body  = request.json or {}
    items = body.get("items", [])
    if not items:
        return jsonify({"error": "No items provided"}), 400

    cfg   = load_config()
    token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No Notion token configured"}), 400

    try:
        client = NotionClient(token)
        result = fix_items(client, items)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/orphan-restore", methods=["POST"])
def api_orphan_restore():
    """
    Restore pages from a backup file.

    Body: { "backup_file": "/absolute/path/to/backup.json" }
    """
    body        = request.json or {}
    backup_file = body.get("backup_file", "").strip()
    if not backup_file:
        return jsonify({"error": "No backup_file provided"}), 400

    # Safety: only allow files inside the project's data/ directory
    data_dir = os.path.join(BASE_DIR, "data")
    abs_path = os.path.abspath(backup_file)
    if not abs_path.startswith(os.path.abspath(data_dir)):
        return jsonify({"error": "Backup file must be inside the data/ directory"}), 400

    cfg   = load_config()
    token = cfg.get("token", "").strip()
    if not token:
        return jsonify({"error": "No Notion token configured"}), 400

    try:
        client = NotionClient(token)
        result = restore_backup(client, abs_path)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/orphan-backups", methods=["GET"])
def api_orphan_backups():
    """List orphan audit backup files in data/, newest first."""
    data_dir = os.path.join(BASE_DIR, "data")
    if not os.path.isdir(data_dir):
        return jsonify({"backups": []})

    files = []
    for fname in os.listdir(data_dir):
        if fname.startswith("orphan_audit_backup_") and fname.endswith(".json"):
            full_path = os.path.join(data_dir, fname)
            stat      = os.stat(full_path)
            files.append({
                "filename":  fname,
                "path":      full_path,
                "size_kb":   round(stat.st_size / 1024, 1),
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })

    files.sort(key=lambda x: x["filename"], reverse=True)
    return jsonify({"backups": files})
