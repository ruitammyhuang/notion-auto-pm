"""
routes/dashboard_routes.py  (focal — two-layer)
─────────────────────────────────────────────────
Focus task list, workload dashboard, design doc pages.
"""

from __future__ import annotations

import datetime
import json
import os

from flask import Blueprint, jsonify, render_template, request

from ..config import (
    FOCUS_CACHE_FILE,
    WORK_SESSIONS_DB_ID,
    load_config,
    load_sessions_mappings,
)
from ..notion_client import NotionClient, extract
from ..sync_engine import regenerate_focus_cache

bp = Blueprint("dashboard", __name__)


# ── Focus Task List ────────────────────────────────────────────────────────────

@bp.route("/focus")
def focus_page():
    return render_template("focus.html")


@bp.route("/api/focus-tasks", methods=["POST"])
def api_focus_tasks():
    """
    Classify tasks from the focus cache into Overdue / Due Today / Due This Week.

    Two-layer simplification: completion is a single status check on the cached
    ws_status field — no rollup counts, no Work Session page fetches needed.
    """
    body  = request.json or {}
    token = body.get("token", "").strip() or load_config().get("token", "").strip()
    if not token:
        return jsonify({"error": "No token — save one in the Sync Tool first"}), 400

    today    = datetime.date.today()
    week_end = today + datetime.timedelta(days=7)
    today_s  = today.isoformat()
    week_s   = week_end.isoformat()

    try:
        with open(FOCUS_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Cannot read cache: {e}"}), 500

    all_tasks    = cache.get("tasks", [])
    generated_at = cache.get("generated_at", "")
    cache_count  = cache.get("task_count", len(all_tasks))

    buckets: dict[str, list] = {"overdue": [], "due_today": [], "this_week": []}
    for task in all_tasks:
        pe = task.get("planned_end", "")
        if not pe:
            continue
        if pe < today_s:
            buckets["overdue"].append(task)
        elif pe == today_s:
            buckets["due_today"].append(task)
        elif today_s < pe <= week_s:
            buckets["this_week"].append(task)

    priority_order = {"Urgent": 0, "High": 1, "Normal": 2, "Low": 3}

    def is_completed(task: dict) -> bool:
        # Two-layer: completion status is cached in ws_status.
        # Optionally re-check Notion on demand (not done here for speed;
        # use /api/regenerate-focus-cache to get fresh statuses).
        return task.get("ws_status") == "Completed"

    def filter_and_sort(tasks: list, sort_by_date: bool = False) -> list:
        result = [t for t in tasks if not is_completed(t)]
        if sort_by_date:
            result.sort(key=lambda t: t.get("planned_end", ""))
        else:
            result.sort(key=lambda t: priority_order.get(t.get("priority", "Normal"), 2))
        return result

    project_names = sorted({t.get("project_name", "") for t in all_tasks if t.get("project_name")})

    return jsonify({
        "today":            today_s,
        "week_end":         week_s,
        "overdue":          filter_and_sort(buckets["overdue"],   sort_by_date=True),
        "due_today":        filter_and_sort(buckets["due_today"], sort_by_date=False),
        "this_week":        filter_and_sort(buckets["this_week"], sort_by_date=True),
        "all_uncompleted":  all_tasks,
        "project_names":    project_names,
        "generated_at":     generated_at,
        "cache_task_count": cache_count,
    })


@bp.route("/api/regenerate-focus-cache", methods=["POST"])
def api_regenerate_focus_cache():
    """Force-rebuild focus-task-list-cache.json from Notion."""
    body  = request.json or {}
    token = body.get("token", "").strip() or load_config().get("token", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400
    try:
        result = regenerate_focus_cache(NotionClient(token))
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Workload Dashboard ─────────────────────────────────────────────────────────

@bp.route("/workload")
def workload_page():
    return render_template("workload.html")


@bp.route("/design")
def design_page():
    return render_template("design.html")


@bp.route("/api/workload", methods=["POST"])
def api_workload():
    """
    Query Work Sessions for a date range and return aggregated workload data.

    Two-layer simplification: Work Type is read directly from the Work Session
    (no secondary fetch from Master WBS Tasks).

    Body: {token?, mode, start_date?, end_date?}
    mode: "today" | "this_week" | "last_week" | "this_month" | "custom"
    """
    body  = request.json or {}
    token = body.get("token", "").strip() or load_config().get("token", "").strip()
    if not token:
        return jsonify({"error": "No token — save one in the Sync Tool first"}), 400

    mode  = body.get("mode", "this_week")
    today = datetime.date.today()

    if mode == "today":
        start = end = today
    elif mode == "this_week":
        start = today - datetime.timedelta(days=today.weekday())
        end   = start + datetime.timedelta(days=6)
    elif mode == "last_week":
        start = today - datetime.timedelta(days=today.weekday() + 7)
        end   = start + datetime.timedelta(days=6)
    elif mode == "this_month":
        start = today.replace(day=1)
        end   = today
    elif mode == "custom":
        try:
            start = datetime.date.fromisoformat(body.get("start_date", ""))
            end   = datetime.date.fromisoformat(body.get("end_date", ""))
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid custom date range"}), 400
    else:
        start = today - datetime.timedelta(days=today.weekday())
        end   = start + datetime.timedelta(days=6)

    start_s = start.isoformat()
    end_s   = end.isoformat()

    client = NotionClient(token)
    # Use the next calendar day as the upper bound so sessions logged in any
    # US timezone (up to UTC-8) are included even when stored in UTC.
    filter_end = (end + datetime.timedelta(days=1)).isoformat()
    filter_body = {"and": [
        {"property": "Session Start", "date": {"on_or_after":  start_s}},
        {"property": "Session Start", "date": {"on_or_before": filter_end}},
    ]}
    try:
        raw_sessions = client.query_db(WORK_SESSIONS_DB_ID, filter_body)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Batch-fetch project names
    project_ids: set[str] = set()
    for s in raw_sessions:
        for rel in s["properties"].get("Project", {}).get("relation", []):
            project_ids.add(rel["id"])

    project_names: dict[str, str] = {}
    for pid in project_ids:
        try:
            r = client.get_page(pid)
            if r.ok:
                props = r.json().get("properties", {})
                for val in props.values():
                    if val.get("type") == "title":
                        name = extract(val)
                        if name:
                            project_names[pid] = name
                            break
        except Exception:
            pass

    result_sessions = []
    by_project:   dict[str, float] = {}
    by_work_type: dict[str, float] = {}
    total_hours = 0.0

    for s in raw_sessions:
        props = s["properties"]
        name  = extract(props.get("Session Name", {})) or "Work Session"

        start_raw  = extract(props.get("Session Start", {}))
        sess_start = start_raw["start"] if isinstance(start_raw, dict) else (start_raw or "")
        end_raw    = extract(props.get("Session End", {}))
        sess_end   = end_raw["start"] if isinstance(end_raw, dict) else (end_raw or "")

        dur_formula = props.get("Duration", {}).get("formula", {})
        duration    = dur_formula.get("number") if dur_formula.get("type") == "number" else None

        if duration is None and sess_start and sess_end:
            try:
                def _parse_iso(ts: str):
                    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                diff = (_parse_iso(sess_end) - _parse_iso(sess_start)).total_seconds()
                if diff != 0:
                    duration = diff / 3600.0
            except Exception:
                pass

        # Work Type: read directly from the Work Sessions Notion field.
        # Set by sync (from WBS) and editable in Notion for standalone sessions.
        work_type = extract(props.get("Work Type", {})) or "Unclassified"
        status    = extract(props.get("Status", {})) or "—"

        proj_rels = props.get("Project", {}).get("relation", [])
        proj_id   = proj_rels[0]["id"] if proj_rels else ""
        proj_name = project_names.get(proj_id, "Unknown Project")

        pid_clean = s["id"].replace("-", "")
        result_sessions.append({
            "name":      name,
            "start":     sess_start,
            "end":       sess_end,
            "duration":  duration,
            "work_type": work_type,
            "status":    status,
            "project":   proj_name,
            "url":       f"https://app.notion.com/p/{pid_clean}",
            "ws_id":     s["id"],   # raw page ID for inline work-type editing
        })

        if duration is not None and duration > 0:
            total_hours             += duration
            by_project[proj_name]   = by_project.get(proj_name, 0)   + duration
            by_work_type[work_type] = by_work_type.get(work_type, 0) + duration

    result_sessions.sort(key=lambda s: s["start"], reverse=True)
    unclassified = sum(1 for s in result_sessions if s["work_type"] == "Unclassified")
    return jsonify({
        "start_date":         start_s,
        "end_date":           end_s,
        "total_hours":        round(total_hours, 2),
        "session_count":      len(result_sessions),
        "project_count":      len(by_project),
        "unclassified_count": unclassified,
        "by_project":  [{"name": k, "hours": round(v, 2)}
                         for k, v in sorted(by_project.items(), key=lambda x: x[1], reverse=True)],
        "by_work_type": [{"name": k, "hours": round(v, 2)}
                          for k, v in sorted(by_work_type.items(), key=lambda x: x[1], reverse=True)],
        "sessions":      result_sessions,
    })

