"""
config.py
─────────
All constants, file paths, and JSON persistence helpers.
No Flask or Notion API dependencies — importable anywhere.
"""

import json
import os

# ── File locations ─────────────────────────────────────────────────────────────
BASE_DIR              = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE           = os.path.join(BASE_DIR, "focal_config.json")
MAPPING_FILE          = os.path.join(BASE_DIR, "focal_mappings.json")
SESSIONS_MAPPING_FILE = os.path.join(BASE_DIR, "focal_sessions_mappings.json")
FOCUS_CACHE_FILE      = os.path.join(BASE_DIR, "focus-task-list-cache.json")
STUDENTS_FILE         = os.path.join(BASE_DIR, "focal_students.json")

# ── Notion API constants ───────────────────────────────────────────────────────
NOTION_API     = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# ── Hard-coded master database IDs ────────────────────────────────────────────
MASTER_DB_ID        = "2de3b2f3d9b74481bc88511ea94de45e"   # 📋 Master WBS Tasks
PROJECTS_DB_ID      = "01705badbb854f019baf7d0ec68b8c7d"   # 📁 Projects
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


# ── Task-to-master mapping persistence ────────────────────────────────────────
def load_mappings() -> dict:
    """source_page_id → {"master_id": master_page_id, "db": source_db_id or None}.
    Auto-migrates old flat format (string values) on first load."""
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE) as f:
            data = json.load(f)
        migrated = False
        for k, v in list(data.items()):
            if isinstance(v, str):
                data[k] = {"master_id": v, "db": None}
                migrated = True
        if migrated:
            save_mappings(data)
        return data
    return {}


def save_mappings(m: dict) -> None:
    with open(MAPPING_FILE, "w") as f:
        json.dump(m, f, indent=2)


# ── Work-session mapping persistence ──────────────────────────────────────────
def load_sessions_mappings() -> dict:
    """master_task_page_id → work_session_page_id"""
    if os.path.exists(SESSIONS_MAPPING_FILE):
        with open(SESSIONS_MAPPING_FILE) as f:
            return json.load(f)
    return {}


def save_sessions_mappings(m: dict) -> None:
    with open(SESSIONS_MAPPING_FILE, "w") as f:
        json.dump(m, f, indent=2)


# ── Dissertation Students persistence ─────────────────────────────────────────
def load_students() -> list:
    """Return list of student profile dicts, sorted by student_name."""
    if os.path.exists(STUDENTS_FILE):
        with open(STUDENTS_FILE) as f:
            return json.load(f)
    return []


def save_students(students: list) -> None:
    students_sorted = sorted(students, key=lambda s: s.get("student_name", "").lower())
    with open(STUDENTS_FILE, "w") as f:
        json.dump(students_sorted, f, indent=2)


def upsert_student(record: dict) -> None:
    """Insert or update a student by student_name."""
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
    """Update only the current_phase for a student. Returns True if found."""
    students = load_students()
    for s in students:
        if s.get("student_name") == student_name:
            s["current_phase"] = current_phase
            save_students(students)
            return True
    return False
