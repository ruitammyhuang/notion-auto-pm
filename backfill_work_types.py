"""
backfill_work_types.py
──────────────────────────────────────────────────────────────────────────────
Analyze every task in focal_sessions_mappings.json, infer the best-fit Work
Type from the task name + project context, and write the value to:
  • The Project WBS page (Work Type column)
  • The linked Work Session page (Work Type column)
  • The local sessions_mappings cache

Run in dry-run mode first to review classifications before writing:
    python3 backfill_work_types.py --dry-run

Then apply:
    python3 backfill_work_types.py
"""

import json
import re
import sys
import time
from pathlib import Path

import requests

# ── Setup ──────────────────────────────────────────────────────────────────────
BASE_DIR         = Path(__file__).parent
CONFIG_FILE      = BASE_DIR / "focal_config.json"
MAPPINGS_FILE    = BASE_DIR / "focal_sessions_mappings.json"

sys.path.insert(0, str(BASE_DIR))
from focal.config import WORK_TYPE_OPTIONS, VALID_WORK_TYPES, WORK_SESSIONS_DB_ID

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

TOKEN = cfg["token"]
HEADERS = {
    "Authorization":  f"Bearer {TOKEN}",
    "Content-Type":   "application/json",
    "Notion-Version": "2022-06-28",
}
NOTION_API = "https://api.notion.com/v1"

DRY_RUN = "--dry-run" in sys.argv


# ── Classification logic ───────────────────────────────────────────────────────
DISSERTATION_DB_ID = "f0c29cac-ec31-45fa-9193-85567f4d0d77"


def classify(task_name: str, project_name: str, source_db_id: str) -> str:
    """
    Return the best-fit Work Type for a task based on its name and project.

    Priority order: Meeting → Review & Assessment → Writing →
                    Analysis → Design & Build → Communication → Admin
    """
    n = task_name.lower().strip()
    p = project_name.lower()

    # ── 🤝 Meeting ─────────────────────────────────────────────────────────────
    # Student-name entries in Dissertation Mentoring are advising meetings
    if source_db_id == DISSERTATION_DB_ID:
        # Single capitalised word or two-word name = student advising session
        if re.match(r'^[A-Z][a-z]+(?:\s[A-Z][a-z]+)?$', task_name.strip()):
            return "🤝 Meeting"

    meeting_kw = [
        "meeting", "calibration meeting", "planning meeting", "team meeting",
        "prep meeting", " meet ", "kickoff", "kick-off", "discussing",
        "discussion plan", "guest speaker", "pre-meeting",
    ]
    if any(k in n for k in meeting_kw):
        return "🤝 Meeting"

    # ── ✅ Review & Assessment ─────────────────────────────────────────────────
    review_kw = [
        "grade ", "grading", "feedback summary", "monitor discussion",
        "respond to student", "regrading", "quality review", "quality check",
        "consistency check", "full course quality", "verify irb",
        "irb training verification", "canvas navigation flow verification",
    ]
    if any(k in n for k in review_kw):
        return "✅ Review & Assessment"

    # ── ✍️ Writing ─────────────────────────────────────────────────────────────
    writing_kw = [
        "draft ", "writing", "manuscript", "script", "syllabus", "outline",
        "proposal", "annotation", "revise course", "finalize syllabus",
        "promo video script", "qual exam overview presentation",
    ]
    # Week N Announcement = writing a course announcement
    if re.search(r'\bweek\s+\d+\b', n):
        return "✍️ Writing"
    if any(k in n for k in writing_kw):
        return "✍️ Writing"

    # ── 🔍 Analysis ────────────────────────────────────────────────────────────
    analysis_kw = [
        "analysis", "analyze", "analysing", "open code", "open coding",
        " code ", "coding", "literature review", "eda", "preprocessing",
        "text preprocessing", "synthesis", "synthesize", "thematic",
        "extraction", "cross-occupation", "competency extraction",
        "data audit", "exploratory data", "pivot analysis", "calibrate",
        "independent open coding", "interpret", "trend analysis",
        "method review", "pilot analysis", "validation",
        "integrate ", "connect findings", "research questions",
        "competency model", "framework review", "theory repository",
        "data analysis",
    ]
    if any(k in n for k in analysis_kw):
        return "🔍 Analysis"

    # ── 📐 Design & Build ──────────────────────────────────────────────────────
    design_kw = [
        "design", "build ", "create ", "set up", "setup",
        "module ", "rubric", "assignment page", "landing page",
        "architecture", "configure", "navigation", "infrastructure",
        "develop", "guiding slides", "matchmaking materials",
        "qualifying exam", "qual exam module", "course architecture",
        "developmental sequencing", "cognitive load", "visual consistency",
        "preparation support", "discussion prompt", "test all required",
        "individual and collaborative assignment", "standardization",
        "canvas course module",
    ]
    if any(k in n for k in design_kw):
        return "📐 Design & Build"

    # ── 📣 Communication ───────────────────────────────────────────────────────
    comm_kw = [
        "email", "contact ", "invite ", "invitation", "send ", "outreach",
        "welcome email", "reminder", "communicate", "recruit",
        "plan, track, and communicate", "two-week communication",
        "announcement",   # catch standalone "announcement" not caught by Writing
    ]
    if any(k in n for k in comm_kw):
        return "📣 Communication"

    # ── ⚙️ Admin ───────────────────────────────────────────────────────────────
    admin_kw = [
        "submit", "submission", "register", "registration", "form",
        "logistics", "travel", "schedule", "open week", "tour registration",
        "photo/video shooting", "textbook adoption", "confirm ",
        "simple syllabus", "hiperga", "verify", "upload to canvas",
        "test all", "open week",
    ]
    if any(k in n for k in admin_kw):
        return "⚙️ Admin"

    # ── Fallback by project type ───────────────────────────────────────────────
    if any(x in p for x in ["scoping review", "job posts", "pjbl", "ai &"]):
        return "🔍 Analysis"
    if any(x in p for x in ["instruction"]):
        return "✅ Review & Assessment"
    if "course design" in p:
        return "📐 Design & Build"
    if "dissertation" in p:
        return "🤝 Meeting"
    if "program management" in p or "professional services" in p:
        return "⚙️ Admin"

    return "⚙️ Admin"   # last resort


# ── Notion patch helper ────────────────────────────────────────────────────────
def patch_page(page_id: str, work_type: str) -> bool:
    payload = {"properties": {"Work Type": {"select": {"name": work_type}}}}
    for attempt in range(2):
        try:
            r = requests.patch(f"{NOTION_API}/pages/{page_id}",
                               headers=HEADERS, json=payload, timeout=15)
            if r.status_code == 200:
                return True
            print(f"    ✗ HTTP {r.status_code}: {r.text[:120]}")
            return False
        except requests.exceptions.Timeout:
            if attempt == 0:
                time.sleep(3)
                continue
            return False
    return False


# ── Main ───────────────────────────────────────────────────────────────────────
with open(MAPPINGS_FILE) as f:
    mappings = json.load(f)

sources = cfg.get("sources", {})

# Build source_db_id → work_type column name map
wt_col = {db_id: src.get("field_map", {}).get("work_type", "")
          for db_id, src in sources.items()}

print(f"{'DRY RUN — ' if DRY_RUN else ''}Classifying {len(mappings)} tasks…\n")

# Collect results for summary
by_type: dict[str, list[str]] = {wt["name"]: [] for wt in WORK_TYPE_OPTIONS}
unchanged = skipped = patched = errors = 0

rows = []   # (wbs_id, info, inferred_wt)

for wbs_id, info in mappings.items():
    if not isinstance(info, dict) or info.get("deleted"):
        skipped += 1
        continue

    task_name  = info.get("name", "")
    proj_name  = info.get("project_name", "")
    src_db_id  = info.get("source_db_id", "")
    cur_wt     = info.get("work_type", "")

    inferred = classify(task_name, proj_name, src_db_id)
    rows.append((wbs_id, info, inferred))
    by_type.setdefault(inferred, []).append(task_name)

# ── Print classifications ──────────────────────────────────────────────────────
print("=" * 62)
for wt, names in by_type.items():
    if not names:
        continue
    print(f"\n{wt}  ({len(names)} tasks)")
    for name in names[:6]:
        print(f"  • {name}")
    if len(names) > 6:
        print(f"  … and {len(names) - 6} more")

print(f"\n{'=' * 62}")
total_classified = sum(len(v) for v in by_type.values())
print(f"Total to classify: {total_classified}  |  Skipped (deleted): {skipped}")

if DRY_RUN:
    print("\nDry run complete — no changes written.")
    print("Run without --dry-run to apply.")
    sys.exit(0)

# ── Apply patches ──────────────────────────────────────────────────────────────
print("\nApplying to Notion…")

for wbs_id, info, inferred_wt in rows:
    task_name = info.get("name", wbs_id[:8])
    ws_id     = info.get("ws_id", "")
    src_db_id = info.get("source_db_id", "")
    col_name  = wt_col.get(src_db_id, "")

    ok_wbs = ok_ws = True

    # Patch WBS page (only if the DB has a Work Type column mapped)
    if col_name:
        wbs_payload = {"properties": {col_name: {"select": {"name": inferred_wt}}}}
        for attempt in range(2):
            try:
                r = requests.patch(f"{NOTION_API}/pages/{wbs_id}",
                                   headers=HEADERS, json=wbs_payload, timeout=15)
                if r.status_code == 200:
                    break
                ok_wbs = False
                print(f"  WBS ✗ {task_name[:40]}: HTTP {r.status_code}")
                break
            except requests.exceptions.Timeout:
                if attempt == 0:
                    time.sleep(3)
                    continue
                ok_wbs = False

    # Patch Work Session page
    if ws_id:
        ok_ws = patch_page(ws_id, inferred_wt)
        if not ok_ws:
            print(f"  WS  ✗ {task_name[:40]}")
            errors += 1

    # Update local mapping
    info["work_type"] = inferred_wt
    patched += 1

    # Gentle rate-limit
    time.sleep(0.15)

# ── Save updated mappings ──────────────────────────────────────────────────────
with open(MAPPINGS_FILE, "w") as f:
    json.dump(mappings, f, indent=2, ensure_ascii=False)

print(f"\nDone — {patched} tasks classified, {errors} errors.")
print("Local focal_sessions_mappings.json updated.")
print("Run a sync to propagate any fingerprint-level changes.")
