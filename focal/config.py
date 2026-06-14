"""
config.py  (focal — three-layer relay)
───────────────────────────────────────
Constants, file paths, and JSON persistence helpers.

Architecture: Project WBS → Master WBS Tasks (relay) → Work Sessions
  - Master WBS Tasks is a thin central table so Work Sessions can hold a
    single "Task" relation pointing there (Notion relations target one DB).
  - Individual WBS rows backlink to their Master WBS Tasks entry via the
    "Master WBS" relation field.

Sessions mapping: wbs_page_id → rich metadata dict

  {
    "<wbs_page_id>": {
      "master_id":   "<master_wbs_page_id>",
      "ws_id":       "<work_session_page_id>",
      "fp":          "<md5 fingerprint of last-synced fields>",
      "name":        "Task name",
      "planned_end": "2026-06-20",
      "priority":    "High",
      "work_type":   "🔵 Deep Work",
      "project_id":  "<projects_db_entry_id>",
      "project_name":"EDG 6648",
      "source_db_id":"<wbs_db_id>"
    }
  }
"""

import json
import os

# ── File locations ─────────────────────────────────────────────────────────────
BASE_DIR              = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE           = os.path.join(BASE_DIR, "focal_config.json")
SESSIONS_MAPPING_FILE = os.path.join(BASE_DIR, "focal_sessions_mappings.json")
FOCUS_CACHE_FILE      = os.path.join(BASE_DIR, "focus-task-list-cache.json")
STUDENTS_FILE         = os.path.join(BASE_DIR, "focal_students.json")

# ── Notion API constants ───────────────────────────────────────────────────────
NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# ── Hard-coded global database IDs ────────────────────────────────────────────
PROJECTS_DB_ID      = "01705badbb854f019baf7d0ec68b8c7d"   # 📁 Projects
MASTER_DB_ID        = "2de3b2f3d9b74481bc88511ea94de45e"   # 📋 Master WBS Tasks (relay)
WORK_SESSIONS_DB_ID = "308c193fbba34a1ebe8d817fd72e9d9a"   # ⏱️ Work Sessions

# ── Value normalisation maps ───────────────────────────────────────────────────
PRIORITY_MAP = {
    "urgent": "Urgent", "critical": "Urgent", "blocker": "Urgent",
    "high": "High", "important": "High",
    "medium": "Normal", "normal": "Normal", "mid": "Normal",
    "low": "Low", "minor": "Low", "nice to have": "Low",
}
VALID_PRIORITIES = ["Urgent", "High", "Normal", "Low"]
VALID_WORK_TYPES = [
    "🔵 Deep Work", "🟡 Meeting & Call", "🟠 Admin & Ops", "🟢 Communication"
]


# ── Config persistence ─────────────────────────────────────────────────────────
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"token": "", "sources": {}}


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ── Sessions mapping persistence ───────────────────────────────────────────────
def load_sessions_mappings() -> dict:
    """
    Returns {wbs_page_id: {master_id, ws_id, fp, name, planned_end, priority,
                            work_type, project_id, project_name, source_db_id}}.

    Auto-migrates the v2 flat format (string values) on first load so a
    prior focal_sessions_mappings.json doesn't break startup.
    Entries already in the enriched dict format (from rebuild_task_relations.py)
    are left unchanged.
    """
    if not os.path.exists(SESSIONS_MAPPING_FILE):
        return {}
    with open(SESSIONS_MAPPING_FILE) as f:
        data = json.load(f)
    migrated = False
    for k, v in list(data.items()):
        if isinstance(v, str):
            # Very old format: master_id (key) → ws_id (string value).
            # Wrap minimally; sync will repopulate master_id + metadata.
            data[k] = {
                "master_id": "", "ws_id": v, "fp": "", "name": "",
                "planned_end": "", "priority": "", "work_type": "",
                "project_id": "", "project_name": "", "source_db_id": "",
            }
            migrated = True
    if migrated:
        save_sessions_mappings(data)
    return data


def save_sessions_mappings(m: dict) -> None:
    with open(SESSIONS_MAPPING_FILE, "w") as f:
        json.dump(m, f, indent=2)


# ── Dissertation students persistence ──────────────────────────────────────────
def load_students() -> list:
    if os.path.exists(STUDENTS_FILE):
        with open(STUDENTS_FILE) as f:
            return json.load(f)
    return []


def save_students(students: list) -> None:
    students_sorted = sorted(students, key=lambda s: s.get("student_name", "").lower())
    with open(STUDENTS_FILE, "w") as f:
        json.dump(students_sorted, f, indent=2)


def upsert_student(record: dict) -> None:
    students = load_students()
    name = record.get("student_name", "").strip()
    if not name:
        raise ValueError("student_name is required")
    idx = next((i for i, s in enumerate(students) if s.get("student_name") == name), None)
    if idx is not None:
        students[idx].update(record)
    else:
        students.append(record)
    save_students(students)


def update_student_phase(student_name: str, current_phase: str) -> bool:
    students = load_students()
    for s in students:
        if s.get("student_name") == student_name:
            s["current_phase"] = current_phase
            save_students(students)
            return True
    return False
