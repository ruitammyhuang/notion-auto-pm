"""
routes/health_check_routes.py
──────────────────────────────
Work type health check: sampling, LLM analysis, report storage.

Endpoints:
  POST /api/health-check/run          -- start async health check, returns batch_id
  GET  /api/health-check/status       -- poll running job status
  GET  /api/health-check/reports      -- list all report summaries
  GET  /api/health-check/report/<id>  -- full report JSON
  GET  /api/health-check/cache-age    -- seconds since last sync (staleness check)
  GET  /health-check                  -- render health check dashboard template
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from flask import Blueprint, jsonify, render_template, request

from ..config import (
    BASE_DIR,
    FOCUS_CACHE_FILE,
    load_config,
    load_sessions_mappings,
)
from ..work_type_manager import get_work_types, get_valid_names

bp = Blueprint("health_check", __name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
REPORTS_DIR   = os.path.join(BASE_DIR, "health_check_reports")
REPORTS_INDEX = os.path.join(REPORTS_DIR, "index.json")

# ── In-memory job state ────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}  # batch_id -> {status, progress, error, result}
_jobs_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _batch_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"hc_{ts}"


def _load_index() -> list[dict]:
    if not os.path.exists(REPORTS_INDEX):
        return []
    with open(REPORTS_INDEX, encoding="utf-8") as f:
        return json.load(f)


def _save_index(entries: list[dict]) -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(REPORTS_INDEX, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def _report_path(batch_id: str) -> str:
    return os.path.join(REPORTS_DIR, f"report_{batch_id}.json")


def _save_report(batch_id: str, report: dict) -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(_report_path(batch_id), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Update index
    index = _load_index()
    index = [e for e in index if e.get("batch_id") != batch_id]  # dedup
    index.insert(0, {
        "batch_id":             batch_id,
        "generated_at":         report.get("generated_at", ""),
        "overall_confidence":   report.get("summary", {}).get("overall_confidence_pct", 0),
        "miscoded_count":       report.get("summary", {}).get("miscoded_count", 0),
        "ambiguous_count":      report.get("summary", {}).get("ambiguous_count", 0),
        "proposed_recodings":   len(report.get("proposed_recodings", [])),
        "proposed_new_types":   len(report.get("proposed_new_types", [])),
        "sampled":              report.get("sampling", {}).get("sampled", 0),
        "status":               report.get("status", "complete"),
    })
    _save_index(index)


def _cache_age_seconds() -> float | None:
    """Seconds since the focus cache was last written. None if cache missing."""
    if not os.path.exists(FOCUS_CACHE_FILE):
        return None
    mtime = os.path.getmtime(FOCUS_CACHE_FILE)
    return time.time() - mtime


# ── Sampling logic ─────────────────────────────────────────────────────────────

def _build_sample(
    mappings: dict,
    sample_pct: float = 0.12,
    min_sample: int = 20,
    max_sample: int = 150,
    date_window_months: int = 6,
    recent_weight: float = 0.70,
    exclude_ids: set | None = None,
) -> list[dict]:
    """
    Stratified random sample from sessions_mappings.

    Strategy:
      - Split records into "recent" (within date_window_months) and "older"
      - Draw recent_weight fraction from recent, rest from older
      - Within each stratum, further stratify by project (proportional) and
        ensure each active work type gets at least 2 samples
      - Exclude record IDs already analyzed in last 90 days (exclude_ids)
    """
    exclude_ids = exclude_ids or set()
    cutoff_date = _months_ago(date_window_months)

    recent: list[dict] = []
    older:  list[dict] = []

    for wbs_id, info in mappings.items():
        if not isinstance(info, dict):
            continue
        if wbs_id in exclude_ids:
            continue
        if not info.get("work_type"):
            continue
        record = {
            "id":           wbs_id,
            "ws_id":        info.get("ws_id", ""),
            "name":         info.get("name", ""),
            "work_type":    info.get("work_type", ""),
            "project_name": info.get("project_name", ""),
            "source_db_id": info.get("source_db_id", ""),
            "planned_end":  info.get("planned_end", ""),
            "notes":        "",
            "record_type":  "work_session",
        }
        if info.get("planned_end", "") >= cutoff_date:
            recent.append(record)
        else:
            older.append(record)

    total     = len(recent) + len(older)
    target    = max(min_sample, min(max_sample, int(total * sample_pct)))

    n_recent = min(len(recent), int(target * recent_weight))
    n_older  = min(len(older),  target - n_recent)
    n_recent = min(len(recent), target - n_older)

    sampled = (
        random.sample(recent, n_recent) if n_recent <= len(recent) else recent[:]
    ) + (
        random.sample(older, n_older) if n_older <= len(older) else older[:]
    )

    # Ensure minimum coverage per work type
    active_types = get_valid_names()
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in mappings.values():
        if isinstance(r, dict) and r.get("work_type") in active_types:
            by_type[r["work_type"]].append(r)

    sampled_ids = {r["id"] for r in sampled}
    for wt in active_types:
        count_in_sample = sum(1 for r in sampled if r["work_type"] == wt)
        if count_in_sample < 2:
            pool = [
                r for r in by_type[wt]
                if r.get("id") not in sampled_ids and r.get("id") not in exclude_ids
            ]
            need = 2 - count_in_sample
            extras = random.sample(pool, min(need, len(pool)))
            for e in extras:
                rec = {
                    "id":           e.get("id") or list(mappings.keys())[0],
                    "ws_id":        e.get("ws_id", ""),
                    "name":         e.get("name", ""),
                    "work_type":    e.get("work_type", ""),
                    "project_name": e.get("project_name", ""),
                    "source_db_id": e.get("source_db_id", ""),
                    "planned_end":  e.get("planned_end", ""),
                    "notes":        "",
                    "record_type":  "work_session",
                }
                sampled.append(rec)
                sampled_ids.add(rec["id"])

    random.shuffle(sampled)
    return sampled


def _months_ago(n: int) -> str:
    now = datetime.now(timezone.utc)
    month = now.month - n
    year  = now.year + month // 12
    month = month % 12 or 12
    return f"{year:04d}-{month:02d}-01"


# ── Async health check runner ──────────────────────────────────────────────────

def _run_health_check(batch_id: str, options: dict) -> None:
    """Execute health check in a background thread. Updates _jobs in-place."""

    def set_progress(pct: int, msg: str = "") -> None:
        with _jobs_lock:
            _jobs[batch_id]["progress_pct"] = pct
            if msg:
                _jobs[batch_id]["progress_msg"] = msg

    try:
        set_progress(5, "Loading configuration")
        cfg   = load_config()
        token = cfg.get("token", "")

        api_key = options.get("api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("No Anthropic API key. Set ANTHROPIC_API_KEY env var or pass api_key in request.")

        set_progress(10, "Loading sessions mappings")
        mappings = load_sessions_mappings()

        work_type_defs  = get_work_types(include_deprecated=False)
        sample_pct      = options.get("sample_pct", 0.12)
        date_window     = options.get("date_window_months", 6)
        min_confidence  = options.get("min_confidence", 0)

        set_progress(15, "Building stratified sample")
        sample = _build_sample(
            mappings,
            sample_pct=sample_pct,
            date_window_months=date_window,
        )

        projects_covered = sorted({r["project_name"] for r in sample if r["project_name"]})

        set_progress(25, f"Analyzing {len(sample)} records for coding consistency")
        from ..llm_analyzer import analyze_coding_consistency, detect_emerging_types, analyze_overlaps

        consistency_results = analyze_coding_consistency(
            records=sample,
            work_type_defs=work_type_defs,
            api_key=api_key,
        )

        set_progress(60, "Detecting emerging work types")
        descriptions = [r["name"] for r in sample if r["name"]]
        emerging     = detect_emerging_types(
            descriptions=descriptions,
            current_types=work_type_defs,
            api_key=api_key,
        )

        set_progress(80, "Analyzing taxonomy overlaps")
        overlaps = analyze_overlaps(
            work_types=work_type_defs,
            sample_records=sample,
            api_key=api_key,
        )

        set_progress(90, "Building report")

        # Build proposed recodings (miscoded records)
        proposed_recodings = []
        for r in consistency_results:
            if not r.get("is_correct") and r.get("confidence", 0) >= min_confidence:
                proposed_recodings.append({
                    "record_id":           r["id"],
                    "ws_id":               next(
                        (s["ws_id"] for s in sample if s["id"] == r["id"]), ""
                    ),
                    "record_type":         r.get("record_type", "work_session"),
                    "task_name":           r.get("name", ""),
                    "project":             r.get("project_name", ""),
                    "current_work_type":   r.get("current_work_type", ""),
                    "suggested_work_type": r.get("suggested_work_type", ""),
                    "confidence":          r.get("confidence", 0),
                    "rationale":           r.get("rationale", ""),
                    "batch_id":            batch_id,
                })

        # Per-type stats
        by_type: dict[str, dict] = {}
        for r in consistency_results:
            wt = r.get("current_work_type", "Unknown")
            if wt not in by_type:
                by_type[wt] = {"name": wt, "sampled": 0, "correct": 0, "miscoded": 0, "confidences": []}
            by_type[wt]["sampled"] += 1
            if r.get("is_correct"):
                by_type[wt]["correct"] += 1
            else:
                by_type[wt]["miscoded"] += 1
            by_type[wt]["confidences"].append(r.get("confidence", 0))

        per_type_stats = []
        for wt, s in by_type.items():
            confs = s.pop("confidences", [])
            s["avg_confidence"] = round(sum(confs) / len(confs), 1) if confs else 0
            per_type_stats.append(s)
        per_type_stats.sort(key=lambda x: x["sampled"], reverse=True)

        total_analyzed  = len(consistency_results)
        correct_count   = sum(1 for r in consistency_results if r.get("is_correct"))
        overall_conf    = round(
            sum(r.get("confidence", 0) for r in consistency_results) / total_analyzed, 1
        ) if total_analyzed else 0

        report = {
            "batch_id":       batch_id,
            "generated_at":   _now_iso(),
            "status":         "complete",
            "sampling": {
                "total_records":        len(mappings),
                "sampled":              len(sample),
                "sample_pct":           sample_pct,
                "date_window_months":   date_window,
                "projects_covered":     projects_covered,
            },
            "summary": {
                "overall_confidence_pct": overall_conf,
                "total_analyzed":         total_analyzed,
                "correct_count":          correct_count,
                "miscoded_count":         len(proposed_recodings),
                "ambiguous_count":        sum(
                    1 for r in consistency_results
                    if not r.get("is_correct") and 50 <= r.get("confidence", 0) < 75
                ),
                "emerging_types_proposed": len([e for e in emerging if not e.get("error")]),
                "overlaps_found":         len([o for o in overlaps if not o.get("error")]),
            },
            "per_type_stats":    per_type_stats,
            "proposed_recodings": proposed_recodings,
            "proposed_new_types": [e for e in emerging if not e.get("error")],
            "proposed_mergers":   [o for o in overlaps if not o.get("error")],
            "raw_analysis":       consistency_results,
        }

        _save_report(batch_id, report)

        set_progress(100, "Complete")
        with _jobs_lock:
            _jobs[batch_id]["status"]  = "complete"
            _jobs[batch_id]["report"]  = report

    except Exception as e:
        with _jobs_lock:
            _jobs[batch_id]["status"]  = "error"
            _jobs[batch_id]["error"]   = str(e)
            _jobs[batch_id]["progress_pct"] = 0


# ── Routes ─────────────────────────────────────────────────────────────────────

@bp.route("/health-check")
def health_check_page():
    return render_template("health_check.html")


@bp.route("/api/health-check/cache-age", methods=["GET"])
def api_cache_age():
    """Return seconds since the last sync wrote the focus cache."""
    age = _cache_age_seconds()
    if age is None:
        return jsonify({"age_seconds": None, "stale": True, "message": "No cache found — run a sync first"})
    stale = age > 86400  # > 24 hours
    return jsonify({
        "age_seconds": round(age),
        "stale": stale,
        "message": "Cache is fresh" if not stale else f"Cache is {int(age // 3600)}h old — consider syncing first",
    })


@bp.route("/api/health-check/run", methods=["POST"])
def api_health_check_run():
    """
    Start an async health check job.

    Body (JSON, all optional):
        api_key             str    -- Anthropic API key (falls back to ANTHROPIC_API_KEY env var)
        sample_pct          float  -- fraction of records to sample (default 0.12)
        date_window_months  int    -- recency window for stratification (default 6)
        min_confidence      int    -- minimum confidence to include in proposed recodings (default 0)

    Returns: { batch_id, status: "running" }
    """
    body = request.json or {}
    bid  = _batch_id()

    with _jobs_lock:
        _jobs[bid] = {
            "batch_id":    bid,
            "status":      "running",
            "progress_pct": 0,
            "progress_msg": "Starting",
            "error":       None,
            "report":      None,
        }

    thread = threading.Thread(
        target=_run_health_check,
        args=(bid, body),
        daemon=True,
    )
    thread.start()

    return jsonify({"batch_id": bid, "status": "running"})


@bp.route("/api/health-check/status", methods=["GET"])
def api_health_check_status():
    """
    Poll status of the most recent (or a specific) health check job.

    Query params:
        batch_id  -- optional, defaults to most recent job

    Returns: { batch_id, status, progress_pct, progress_msg, error? }
    """
    bid = request.args.get("batch_id", "")
    with _jobs_lock:
        if bid and bid in _jobs:
            job = dict(_jobs[bid])
        elif _jobs:
            job = dict(next(reversed(_jobs.values())))
        else:
            return jsonify({"status": "idle", "batch_id": None})

    job.pop("report", None)  # don't send full report in status poll
    return jsonify(job)


@bp.route("/api/health-check/reports", methods=["GET"])
def api_health_check_reports():
    """List all health check report summaries."""
    return jsonify({"reports": _load_index()})


@bp.route("/api/health-check/report/<batch_id>", methods=["GET"])
def api_health_check_report(batch_id: str):
    """Return the full report for a given batch_id."""
    path = _report_path(batch_id)
    if not os.path.exists(path):
        # Check if it's still in-memory (just completed)
        with _jobs_lock:
            job = _jobs.get(batch_id, {})
        if job.get("report"):
            return jsonify(job["report"])
        return jsonify({"error": f"Report {batch_id} not found"}), 404
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))
