"""
routes/dashboard_routes.py
───────────────────────────
Focus task list, workload dashboard, design doc pages, and related APIs.
"""

from __future__ import annotations

import datetime
import json
import os
import time

import requests
from flask import Blueprint, jsonify, render_template, request

from ..config import (
    FOCUS_CACHE_FILE,
    WORK_SESSIONS_DB_ID,
    MASTER_DB_ID,
    load_config,
    load_mappings,
    load_sessions_mappings,
)
from ..notion_client import NotionClient, extract, p_date
from ..sync_engine import regenerate_focus_cache

bp = Blueprint("dashboard", __name__)


# ── Focus Task List ────────────────────────────────────────────────────────────

@bp.route("/focus")
def focus_page():
    return render_template("focus.html")


@bp.route("/api/focus-tasks", methods=["POST"])
def api_focus_tasks():
    """
    Classify tasks from the focus cache into Overdue / Due Today / Due This Week,
    then check each task's Work Sessions for completion status.

    Body: {token?} — falls back to saved config token if omitted.
    """
    body  = request.json or {}
    token = body.get("token", "").strip()
    if not token:
        token = load_config().get("token", "").strip()
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

    client = NotionClient(token)

    def is_completed(task: dict) -> bool:
        ws_urls = task.get("work_sessions", [])
        if not ws_urls:
            return False
        for url in ws_urls:
            page_id = url.rstrip("/").split("/")[-1].replace("-", "")
            if len(page_id) == 32:
                page_id = f"{page_id[:8]}-{page_id[8:12]}-{page_id[12:16]}-{page_id[16:20]}-{page_id[20:]}"
            try:
                r    = client.get_page(page_id)
                if not r.ok:
                    return False
                data = r.json()
                if data.get("archived") or data.get("in_trash"):
                    continue
                status = extract(data.get("properties", {}).get("Status", {}))
                if status != "Completed":
                    return False
            except Exception:
                return False
        return True

    priority_order = {"Urgent": 0, "High": 1, "Normal": 2, "Low": 3}

    def filter_and_sort(tasks: list, sort_by_date: bool = False) -> list:
        result = [t for t in tasks if not is_completed(t)]
        if sort_by_date:
            result.sort(key=lambda t: t.get("planned_end", ""))
        else:
            result.sort(key=lambda t: priority_order.get(t.get("priority", "Normal"), 2))
        return result

    return jsonify({
        "today":            today_s,
        "week_end":         week_s,
        "overdue":          filter_and_sort(buckets["overdue"],   sort_by_date=True),
        "due_today":        filter_and_sort(buckets["due_today"], sort_by_date=False),
        "this_week":        filter_and_sort(buckets["this_week"], sort_by_date=True),
        "generated_at":     generated_at,
        "cache_task_count": cache_count,
    })


@bp.route("/api/regenerate-focus-cache", methods=["POST"])
def api_regenerate_focus_cache():
    """Force-rebuild focus-task-list-cache.json from Notion."""
    body  = request.json or {}
    token = body.get("token", "").strip()
    if not token:
        token = load_config().get("token", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400
    try:
        regenerate_focus_cache(NotionClient(token))
        return jsonify({"ok": True})
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

    Body: {token?, mode, start_date?, end_date?}
    mode: "today" | "this_week" | "last_week" | "this_month" | "custom"
    """
    body  = request.json or {}
    token = body.get("token", "").strip()
    if not token:
        token = load_config().get("token", "").strip()
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

    client      = NotionClient(token)
    filter_body = {"and": [
        {"property": "Session Start", "date": {"on_or_after":  start_s}},
        {"property": "Session Start", "date": {"on_or_before": end_s + "T23:59:59"}},
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
        })

        if duration:
            total_hours              += duration
            by_project[proj_name]    = by_project.get(proj_name, 0)    + duration
            by_work_type[work_type]  = by_work_type.get(work_type, 0)  + duration

    result_sessions.sort(key=lambda s: s["start"], reverse=True)
    by_project_list   = sorted(by_project.items(),   key=lambda x: x[1], reverse=True)
    by_work_type_list = sorted(by_work_type.items(), key=lambda x: x[1], reverse=True)

    return jsonify({
        "start_date":    start_s,
        "end_date":      end_s,
        "total_hours":   round(total_hours, 2),
        "session_count": len(result_sessions),
        "project_count": len(by_project),
        "by_project":    [{"name": k, "hours": round(v, 2)} for k, v in by_project_list],
        "by_work_type":  [{"name": k, "hours": round(v, 2)} for k, v in by_work_type_list],
        "sessions":      result_sessions,
    })


@bp.route("/api/writeback-dates", methods=["POST"])
def api_writeback_dates():
    """
    Read Planned Start + Planned End from every Master WBS task that has a mapping,
    then write those dates back to the corresponding Project WBS row.
    """
    body  = request.json or {}
    token = body.get("token", "").strip()
    if not token:
        token = load_config().get("token", "").strip()
    if not token:
        return jsonify({"error": "No token"}), 400

    mappings = load_mappings()
    inverted: dict[str, dict] = {}
    for src_page, info in mappings.items():
        mid   = info.get("master_id", "")
        db_id = info.get("db", "")
        if mid:
            inverted[mid] = {"source_page_id": src_page, "db_id": db_id}

    if not inverted:
        return jsonify({"error": "No task mappings found — run a sync first"}), 400

    sources   = load_config().get("sources", {})
    db_fields = {
        db_id: {
            "planned_start": src.get("field_map", {}).get("planned_start", ""),
            "planned_end":   src.get("field_map", {}).get("planned_end",   ""),
        }
        for db_id, src in sources.items()
    }

    client = NotionClient(token)
    try:
        master_pages = client.query_db(MASTER_DB_ID, {"or": [
            {"property": "Planned End",   "date": {"is_not_empty": True}},
            {"property": "Planned Start", "date": {"is_not_empty": True}},
        ]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    updated, skipped, errors = 0, 0, []
    for page in master_pages:
        mid = page["id"]
        if mid not in inverted:
            skipped += 1
            continue

        src_page_id = inverted[mid]["source_page_id"]
        db_id       = inverted[mid]["db_id"]
        fields      = db_fields.get(db_id, {})

        props = page["properties"]
        ps_raw = extract(props.get("Planned Start", {}))
        pe_raw = extract(props.get("Planned End",   {}))
        planned_start = ps_raw["start"] if isinstance(ps_raw, dict) else ps_raw
        planned_end   = pe_raw["start"] if isinstance(pe_raw, dict) else pe_raw

        patch: dict = {}
        if planned_start and fields.get("planned_start"):
            patch[fields["planned_start"]] = p_date({"start": planned_start})
        if planned_end and fields.get("planned_end"):
            patch[fields["planned_end"]]   = p_date({"start": planned_end})

        if not patch:
            skipped += 1
            continue

        for attempt in range(2):
            try:
                r = client.patch_page(src_page_id, {"properties": patch})
                r.raise_for_status()
                updated += 1
                break
            except requests.exceptions.Timeout:
                if attempt == 0:
                    time.sleep(2)
                    continue
                errors.append(f"Timeout after retry: page {src_page_id}")
            except Exception as e:
                errors.append(str(e))
                break

    return jsonify({"ok": True, "updated": updated,
                    "skipped": skipped, "errors": errors[:5]})
