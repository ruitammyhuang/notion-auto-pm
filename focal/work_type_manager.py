"""
work_type_manager.py
────────────────────
Persistent work type definitions backed by work_types.json.

Replaces the hardcoded WORK_TYPE_OPTIONS list in config.py with a file-backed
store that supports hot-reload (no server restart) and ad-hoc creation.

Usage:
    from focal.work_type_manager import get_work_types, get_valid_names, save_work_type

All callers that previously imported WORK_TYPE_OPTIONS or VALID_WORK_TYPES from
config should use get_work_types() / get_valid_names() instead.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# ── File location ──────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_TYPES_FILE = os.path.join(BASE_DIR, "work_types.json")

# ── Module-level cache (mtime-based hot-reload) ────────────────────────────────
_cache_mtime: float = 0.0
_cache_data:  dict  = {}

NOTION_COLORS = [
    "blue", "brown", "default", "gray", "green",
    "orange", "pink", "purple", "red", "yellow",
]


def _reload_if_stale() -> dict:
    """Return parsed work_types.json, reloading from disk only when mtime changed."""
    global _cache_mtime, _cache_data
    try:
        mtime = os.path.getmtime(WORK_TYPES_FILE)
    except FileNotFoundError:
        return _empty_store()
    if mtime != _cache_mtime:
        with open(WORK_TYPES_FILE, encoding="utf-8") as f:
            _cache_data  = json.load(f)
        _cache_mtime = mtime
    return _cache_data


def _empty_store() -> dict:
    return {"version": 1, "work_types": [], "creation_log": []}


def _save(store: dict) -> None:
    """Write store to disk and invalidate the in-memory cache so next read picks up the change."""
    global _cache_mtime
    with open(WORK_TYPES_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)
    _cache_mtime = os.path.getmtime(WORK_TYPES_FILE)
    global _cache_data
    _cache_data = store


# ── Public API ─────────────────────────────────────────────────────────────────

def get_work_types(include_deprecated: bool = False) -> list[dict]:
    """Return active work type dicts [{name, color, description, ...}].

    Pass include_deprecated=True to get all types (used by health check).
    """
    store = _reload_if_stale()
    types = store.get("work_types", [])
    if include_deprecated:
        return types
    return [t for t in types if not t.get("deprecated", False)]


def get_valid_names(include_deprecated: bool = False) -> list[str]:
    """Return list of valid work type name strings (replaces VALID_WORK_TYPES)."""
    return [t["name"] for t in get_work_types(include_deprecated=include_deprecated)]


def get_work_type_options(include_deprecated: bool = False) -> list[dict]:
    """Return [{name, color}] dicts compatible with Notion select options format."""
    return [
        {"name": t["name"], "color": t["color"]}
        for t in get_work_types(include_deprecated=include_deprecated)
    ]


def save_work_type(
    name: str,
    color: str,
    description: str = "",
    examples: list[str] | None = None,
    context: str = "",
) -> dict:
    """Create a new work type and persist it to work_types.json.

    Returns the new work type dict.
    Raises ValueError if name already exists (active or deprecated) or color invalid.
    """
    name = name.strip()
    if not name:
        raise ValueError("Work type name cannot be empty")
    if color not in NOTION_COLORS:
        raise ValueError(f"color must be one of: {', '.join(NOTION_COLORS)}")

    store = _reload_if_stale()
    existing_names = {t["name"] for t in store.get("work_types", [])}
    if name in existing_names:
        raise ValueError(f"Work type '{name}' already exists")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    new_type = {
        "name":         name,
        "color":        color,
        "description":  description.strip(),
        "examples":     examples or [],
        "created_at":   now,
        "deprecated":   False,
        "version":      1,
        "former_names": [],
    }
    store.setdefault("work_types", []).append(new_type)
    store.setdefault("creation_log", []).append({
        "event":     "created",
        "name":      name,
        "timestamp": now,
        "context":   context.strip(),
    })
    _save(store)
    return new_type


def deprecate_work_type(name: str) -> None:
    """Mark a work type as deprecated (soft-delete). Does not remove from file."""
    store = _reload_if_stale()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    found = False
    for t in store.get("work_types", []):
        if t["name"] == name:
            t["deprecated"] = True
            found = True
            break
    if not found:
        raise ValueError(f"Work type '{name}' not found")
    store.setdefault("creation_log", []).append({
        "event":     "deprecated",
        "name":      name,
        "timestamp": now,
        "context":   "Deprecated via work_type_manager",
    })
    _save(store)


def update_work_type(name: str, **fields) -> dict:
    """Update description, examples, or color for an existing work type.

    Increments version and logs the change. Allowed fields: color, description, examples.
    """
    store = _reload_if_stale()
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for t in store.get("work_types", []):
        if t["name"] == name:
            for k in ("color", "description", "examples"):
                if k in fields:
                    t[k] = fields[k]
            t["version"] = t.get("version", 1) + 1
            store.setdefault("creation_log", []).append({
                "event":     "updated",
                "name":      name,
                "timestamp": now,
                "context":   f"Updated fields: {list(fields.keys())}",
            })
            _save(store)
            return t
    raise ValueError(f"Work type '{name}' not found")
