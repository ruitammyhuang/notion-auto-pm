"""
run_health_check.py
────────────────────────────────────────────────────────────────────────────────
CLI entry point for the work type health check analysis.

Equivalent to clicking "Run Health Check" in the /health-check UI but usable
from the terminal and suitable for cron scheduling.

Usage:
  python3 run_health_check.py
  python3 run_health_check.py --sample-pct 0.15 --date-window 3
  python3 run_health_check.py --dry-run   # print sampling plan, don't call Claude

Environment:
  ANTHROPIC_API_KEY   Required unless --dry-run is set.

Cron example (monthly, first of the month at 9am):
  0 9 1 * * cd /path/to/Notion_Auto_PM && python3 run_health_check.py >> sync_logs/health_check.log 2>&1

Output:
  Report saved to health_check_reports/report_<batch_id>.json
  Summary printed to stdout
  Exit code 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from focal.config import load_config, load_sessions_mappings
from focal.work_type_manager import get_work_types, get_valid_names


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _batch_id() -> str:
    return f"hc_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_cli"


def _months_ago(n: int) -> str:
    now   = datetime.now(timezone.utc)
    month = now.month - n
    year  = now.year + month // 12
    month = month % 12 or 12
    return f"{year:04d}-{month:02d}-01"


def _build_sample(
    mappings: dict,
    sample_pct: float,
    date_window: int,
) -> list[dict]:
    cutoff = _months_ago(date_window)
    active_types = get_valid_names()
    recent: list[dict] = []
    older:  list[dict] = []

    for wbs_id, info in mappings.items():
        if not isinstance(info, dict) or not info.get("work_type"):
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
        if info.get("planned_end", "") >= cutoff:
            recent.append(record)
        else:
            older.append(record)

    total    = len(recent) + len(older)
    target   = max(20, min(150, int(total * sample_pct)))
    n_recent = min(len(recent), int(target * 0.70))
    n_older  = min(len(older),  target - n_recent)
    n_recent = min(len(recent), target - n_older)

    sampled = (
        random.sample(recent, n_recent) if n_recent <= len(recent) else recent[:]
    ) + (
        random.sample(older, n_older) if n_older <= len(older) else older[:]
    )

    # Ensure each active work type has at least 2 samples
    by_type: dict[str, list] = defaultdict(list)
    for wbs_id, info in mappings.items():
        if isinstance(info, dict) and info.get("work_type") in active_types:
            by_type[info["work_type"]].append({**info, "id": wbs_id})

    sampled_ids = {r["id"] for r in sampled}
    for wt in active_types:
        count = sum(1 for r in sampled if r["work_type"] == wt)
        if count < 2:
            pool = [r for r in by_type[wt] if r.get("id") not in sampled_ids]
            extras = random.sample(pool, min(2 - count, len(pool)))
            for e in extras:
                rec = {
                    "id": e.get("id", ""), "ws_id": e.get("ws_id", ""),
                    "name": e.get("name", ""), "work_type": e.get("work_type", ""),
                    "project_name": e.get("project_name", ""),
                    "source_db_id": e.get("source_db_id", ""),
                    "planned_end": e.get("planned_end", ""),
                    "notes": "", "record_type": "work_session",
                }
                sampled.append(rec)
                sampled_ids.add(rec["id"])

    random.shuffle(sampled)
    return sampled


def _save_report(batch_id: str, report: dict) -> Path:
    reports_dir = BASE_DIR / "health_check_reports"
    reports_dir.mkdir(exist_ok=True)
    path = reports_dir / f"report_{batch_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    index_path = reports_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    index = [e for e in index if e.get("batch_id") != batch_id]
    index.insert(0, {
        "batch_id":           batch_id,
        "generated_at":       report.get("generated_at", ""),
        "overall_confidence": report.get("summary", {}).get("overall_confidence_pct", 0),
        "miscoded_count":     report.get("summary", {}).get("miscoded_count", 0),
        "ambiguous_count":    report.get("summary", {}).get("ambiguous_count", 0),
        "proposed_recodings": len(report.get("proposed_recodings", [])),
        "proposed_new_types": len(report.get("proposed_new_types", [])),
        "sampled":            report.get("sampling", {}).get("sampled", 0),
        "status":             report.get("status", "complete"),
    })
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Work type health check CLI")
    parser.add_argument("--sample-pct",   type=float, default=0.12,  help="Fraction of records to sample (default 0.12)")
    parser.add_argument("--date-window",  type=int,   default=6,     help="Recent window in months (default 6)")
    parser.add_argument("--min-conf",     type=int,   default=0,     help="Min confidence threshold for report recodings (default 0)")
    parser.add_argument("--api-key",      type=str,   default="",    help="Anthropic API key (overrides ANTHROPIC_API_KEY env var)")
    parser.add_argument("--dry-run",      action="store_true",       help="Print sampling plan, skip LLM calls")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not args.dry_run:
        print("ERROR: Set ANTHROPIC_API_KEY env var or pass --api-key.")
        return 1

    print(f"[{_now_iso()}] Loading configuration...")
    mappings       = load_sessions_mappings()
    work_type_defs = get_work_types(include_deprecated=False)
    total_records  = len(mappings)

    print(f"[{_now_iso()}] Building sample (pct={args.sample_pct}, window={args.date_window}m)...")
    sample = _build_sample(mappings, args.sample_pct, args.date_window)
    projects_covered = sorted({r["project_name"] for r in sample if r["project_name"]})

    print(f"  Total records: {total_records}")
    print(f"  Sample size:   {len(sample)}")
    print(f"  Projects:      {', '.join(projects_covered)}")
    print()

    # Work type distribution in sample
    by_wt: dict[str, int] = defaultdict(int)
    for r in sample:
        by_wt[r["work_type"]] += 1
    print("  Sample distribution:")
    for wt, count in sorted(by_wt.items(), key=lambda x: -x[1]):
        print(f"    {wt}: {count}")
    print()

    if args.dry_run:
        print("[dry-run] Skipping LLM analysis. Would analyze the above sample.")
        return 0

    batch_id = _batch_id()
    print(f"[{_now_iso()}] Batch ID: {batch_id}")
    print(f"[{_now_iso()}] Analyzing coding consistency...")

    from focal.llm_analyzer import analyze_coding_consistency, detect_emerging_types, analyze_overlaps

    consistency = analyze_coding_consistency(sample, work_type_defs, api_key)
    print(f"  Done. {len(consistency)} records analyzed.")

    print(f"[{_now_iso()}] Detecting emerging work types...")
    descriptions = [r["name"] for r in sample if r["name"]]
    emerging     = detect_emerging_types(descriptions, work_type_defs, api_key)
    emerging     = [e for e in emerging if not e.get("error")]
    print(f"  Done. {len(emerging)} emerging type(s) proposed.")

    print(f"[{_now_iso()}] Analyzing taxonomy overlaps...")
    overlaps = analyze_overlaps(work_type_defs, sample, api_key)
    overlaps = [o for o in overlaps if not o.get("error")]
    print(f"  Done. {len(overlaps)} overlap(s) found.")

    # Build report
    proposed_recodings = [
        {
            "record_id":           r["id"],
            "ws_id":               next((s["ws_id"] for s in sample if s["id"] == r["id"]), ""),
            "record_type":         r.get("record_type", "work_session"),
            "task_name":           r.get("name", ""),
            "project":             r.get("project_name", ""),
            "current_work_type":   r.get("current_work_type", ""),
            "suggested_work_type": r.get("suggested_work_type", ""),
            "confidence":          r.get("confidence", 0),
            "rationale":           r.get("rationale", ""),
            "batch_id":            batch_id,
        }
        for r in consistency
        if not r.get("is_correct") and r.get("confidence", 0) >= args.min_conf
    ]

    by_type: dict[str, dict] = {}
    for r in consistency:
        wt = r.get("current_work_type", "Unknown")
        if wt not in by_type:
            by_type[wt] = {"name": wt, "sampled": 0, "correct": 0, "miscoded": 0, "confs": []}
        by_type[wt]["sampled"] += 1
        if r.get("is_correct"):
            by_type[wt]["correct"] += 1
        else:
            by_type[wt]["miscoded"] += 1
        by_type[wt]["confs"].append(r.get("confidence", 0))

    per_type_stats = []
    for wt, s in by_type.items():
        confs = s.pop("confs", [])
        s["avg_confidence"] = round(sum(confs) / len(confs), 1) if confs else 0
        per_type_stats.append(s)
    per_type_stats.sort(key=lambda x: x["sampled"], reverse=True)

    total_analyzed = len(consistency)
    overall_conf   = round(
        sum(r.get("confidence", 0) for r in consistency) / total_analyzed, 1
    ) if total_analyzed else 0

    report = {
        "batch_id":     batch_id,
        "generated_at": _now_iso(),
        "status":       "complete",
        "sampling": {
            "total_records":      total_records,
            "sampled":            len(sample),
            "sample_pct":         args.sample_pct,
            "date_window_months": args.date_window,
            "projects_covered":   projects_covered,
        },
        "summary": {
            "overall_confidence_pct": overall_conf,
            "total_analyzed":         total_analyzed,
            "correct_count":          sum(1 for r in consistency if r.get("is_correct")),
            "miscoded_count":         len(proposed_recodings),
            "ambiguous_count":        sum(
                1 for r in consistency
                if not r.get("is_correct") and 50 <= r.get("confidence", 0) < 75
            ),
            "emerging_types_proposed": len(emerging),
            "overlaps_found":          len(overlaps),
        },
        "per_type_stats":     per_type_stats,
        "proposed_recodings": proposed_recodings,
        "proposed_new_types": emerging,
        "proposed_mergers":   overlaps,
        "raw_analysis":       consistency,
    }

    path = _save_report(batch_id, report)
    print()
    print("=" * 60)
    print(f"[{_now_iso()}] Health check complete.")
    print(f"  Report: {path}")
    print(f"  Overall confidence:  {overall_conf}%")
    print(f"  Records analyzed:    {total_analyzed}")
    print(f"  Suggested recodings: {len(proposed_recodings)}")
    print(f"  Emerging types:      {len(emerging)}")
    print(f"  Overlaps found:      {len(overlaps)}")
    print()
    print("Review and apply recodings at: http://localhost:8765/health-check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
