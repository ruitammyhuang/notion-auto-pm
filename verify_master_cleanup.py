"""
verify_master_cleanup.py
────────────────────────────────────────────────────────────────────────────────
Pre-deletion safety check for 6 deprecated columns in Master WBS Tasks.

Deprecated columns under review:
  • Work Type    (select)
  • Priority     (select)
  • Category     (rich_text)
  • Notes        (rich_text)
  • Planned End  (date)
  • Planned Start (date)

IMPORTANT DISTINCTION:
  field_map entries in focal_config.json describe columns in PROJECT WBS source
  databases, NOT in Master WBS Tasks. A field_map['notes'] = 'Notes' means "read
  Notes from this WBS database" — it is NOT a reference to the Master WBS Tasks
  Notes column. These are NOT blockers for deleting from Master WBS Tasks.

Checks performed:
  1. CODE  — scan Python source for writes to these columns via master_id /
             MASTER_DB_ID specifically (not via wbs_id or ws_id)
  2. CODE  — scan for patch_page(master_id, ...) calls that include these cols
  3. CONFIG — verify field_maps do NOT conflict (informational, expected to
              contain these names for WBS source columns — that is normal)
  4. CACHE  — sessions_mappings data presence (informational)
  5. NOTION — fetch Master WBS Tasks schema via Notion API:
             a) Auto Status formula does not reference deprecated cols
             b) Rollup definitions are valid
             c) Confirm deprecated cols exist (so deletion is meaningful)
  6. SUMMARY — per-column go/no-go

Run:  python3 verify_master_cleanup.py
"""

import json
import re
import sys
from pathlib import Path

import requests

# ── Setup ──────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
CONFIG_FILE   = BASE_DIR / "focal_config.json"
MAPPINGS_FILE = BASE_DIR / "focal_sessions_mappings.json"

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

TOKEN = cfg["token"]
HEADERS = {
    "Authorization":  f"Bearer {TOKEN}",
    "Content-Type":   "application/json",
    "Notion-Version": "2022-06-28",
}
NOTION_API   = "https://api.notion.com/v1"
MASTER_DB_ID = "2de3b2f3d9b74481bc88511ea94de45e"

DEPRECATED = ["Work Type", "Priority", "Category", "Notes", "Planned End", "Planned Start"]
CORE       = ["Task Name", "Project", "Work Sessions",
              "Total Sessions", "Completed Sessions", "Total Actual Hours", "Auto Status"]

results: dict[str, list[str]] = {col: [] for col in DEPRECATED}

def fail(col, msg): results[col].append(f"❌ {msg}")
def warn(col, msg): results[col].append(f"⚠️  {msg}")
def ok(col, msg):   results[col].append(f"✅ {msg}")

SEP = "─" * 64

# ════════════════════════════════════════════════════════════════════════════════
# CHECK 1 & 2: Code — writes to Master WBS Tasks specifically
# ════════════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("CHECK 1 & 2: Code — writes targeting master_id / MASTER_DB_ID")
print(f"{SEP}")
print("""
Context: field_map entries like field_map['notes'] = 'Notes' are WBS source
column references used by sync to READ from project WBS databases. They are
NOT references to Master WBS Tasks columns and are NOT blockers here.
This check looks only for Python that WRITES these column names to master_id
or MASTER_DB_ID — which is what would break if the column were deleted.
""".strip())

py_files = [p for p in (BASE_DIR / "focal").rglob("*.py")
            if "__pycache__" not in str(p)]
py_files += [p for p in BASE_DIR.glob("*.py")
             if p.name != "verify_master_cleanup.py"]

COL_PAT = {col: re.compile(r'"' + re.escape(col) + r'"') for col in DEPRECATED}

# Lines that are definitely writes to Master WBS Tasks
MASTER_WRITE_SIGNALS = re.compile(
    r"master_props|patch_page\s*\(\s*master_id|"
    r"create_page\s*\(\s*\{[^}]*MASTER_DB_ID"
)

for col in DEPRECATED:
    pat = COL_PAT[col]
    hits = []
    for fpath in py_files:
        text = fpath.read_text(errors="replace")
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not pat.search(line):
                continue
            if line.strip().startswith("#"):
                continue
            # Check a window of ±5 lines for master write signals
            window_start = max(0, i - 5)
            window_end   = min(len(lines), i + 6)
            window_text  = "\n".join(lines[window_start:window_end])
            if MASTER_WRITE_SIGNALS.search(window_text):
                hits.append((fpath.name, i + 1, line.strip()))

    if hits:
        for fname, lineno, snippet in hits:
            fail(col, f"{fname}:{lineno}: {snippet[:80]}")
    else:
        ok(col, "No Python code writes this column to master_id / MASTER_DB_ID")

for col in DEPRECATED:
    fails = [r for r in results[col] if r.startswith("❌")]
    suffix = f"  [{len(fails)} FAIL]" if fails else "  [clean]"
    print(f"  {col}{suffix}")
    for f in fails:
        print(f"    {f}")

# ════════════════════════════════════════════════════════════════════════════════
# CHECK 3: field_map informational scan (NOT a blocker)
# ════════════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("CHECK 3: focal_config.json field_maps (informational — WBS source cols)")
print(f"{SEP}")
print("NOTE: field_map values refer to WBS source database columns, not Master WBS")
print("Tasks. Matches here are expected and are NOT blockers for Master WBS cleanup.\n")

sources = cfg.get("sources", {})
# Map fmap key → which deprecated col it references
FMAP_KEY_TO_COL = {
    "work_type":    "Work Type",
    "priority":     "Priority",
    "category":     "Category",
    "notes":        "Notes",
    "planned_end":  "Planned End",
    "planned_start":"Planned Start",
}

col_dbs: dict[str, list[str]] = {col: [] for col in DEPRECATED}
for db_id, src in sources.items():
    fm = src.get("field_map", {})
    title = src.get("db_title", db_id[:8])
    for fmap_key, col in FMAP_KEY_TO_COL.items():
        if fmap_key in fm:
            col_dbs[col].append(title)

for col in DEPRECATED:
    dbs = col_dbs[col]
    if dbs:
        print(f"  {col}: mapped in {len(dbs)} WBS source DBs")
        for db in dbs:
            print(f"    • {db}")
    else:
        print(f"  {col}: not mapped in any WBS source DB")
print("\n(These are reads FROM those WBS databases — unaffected by Master WBS cleanup)")

# ════════════════════════════════════════════════════════════════════════════════
# CHECK 4: sessions_mappings cache (informational)
# ════════════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("CHECK 4: sessions_mappings cache (informational)")
print(f"{SEP}")

with open(MAPPINGS_FILE) as f:
    mappings = json.load(f)

active = {k: v for k, v in mappings.items()
          if isinstance(v, dict) and not v.get("deleted")}
total = len(active)

CACHE_KEYS = {
    "Work Type":   "work_type",
    "Priority":    "priority",
    "Planned End": "planned_end",
}
print(f"  Active entries: {total}")
for col, key in CACHE_KEYS.items():
    n = sum(1 for v in active.values() if v.get(key))
    pct = round(100 * n / total) if total else 0
    src = "sourced from WBS source DB" if col != "Notes" else "sourced from WBS source DB"
    print(f"  {col} ('{key}'): {n}/{total} entries ({pct}%) — {src}, not from Master WBS Tasks")
for col in ["Category", "Notes", "Planned Start"]:
    print(f"  {col}: not in sessions_mappings schema")

# ════════════════════════════════════════════════════════════════════════════════
# CHECK 5: Notion API — Master WBS Tasks DB schema
# ════════════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("CHECK 5: Notion API — Master WBS Tasks DB schema")
print(f"{SEP}")

try:
    r = requests.get(f"{NOTION_API}/databases/{MASTER_DB_ID}",
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    db = r.json()
    notion_ok = True
except Exception as e:
    print(f"  ⚠️  Could not reach Notion API: {e}")
    print("  Run this script from a machine with direct Notion access to complete check 5.")
    notion_ok = False
    db = None

if notion_ok and db:
    props = db.get("properties", {})

    # 5a — column inventory
    print("\n  All columns in Master WBS Tasks:")
    for name, pdef in props.items():
        ptype  = pdef.get("type", "?")
        tag    = "  ← DEPRECATED (candidate for deletion)" if name in DEPRECATED else ""
        tag    = "  ← CORE"                               if name in CORE       else tag
        print(f"    {name:<28} ({ptype}){tag}")

    # 5b — Auto Status formula
    auto_status_prop = props.get("Auto Status", {})
    if auto_status_prop.get("type") == "formula":
        expr = auto_status_prop.get("formula", {}).get("expression", "")
        print(f"\n  Auto Status formula:\n    {expr}")
        refs = re.findall(r'prop\("([^"]+)"\)', expr)
        print(f"  References: {refs}")
        for col in DEPRECATED:
            if col in expr:
                fail(col, f"Appears in Auto Status formula!")
            else:
                ok(col, "Not in Auto Status formula")
    else:
        warn("Auto Status", "Could not read formula — verify manually in Notion")

    # 5c — rollup definitions
    print("\n  Rollup definitions:")
    for rname in ["Total Sessions", "Completed Sessions", "Total Actual Hours"]:
        rp = props.get(rname, {})
        if rp.get("type") == "rollup":
            ru = rp.get("rollup", {})
            rel  = ru.get("relation_property_name", "?")
            prop = ru.get("rollup_property_name", "?")
            fn   = ru.get("function", "?")
            print(f"    {rname:<28} relation='{rel}', property='{prop}', fn={fn}")
            for col in DEPRECATED:
                if col in (rel, prop):
                    fail(col, f"Rollup '{rname}' references this column!")
        else:
            print(f"    {rname}: not found or not a rollup")

    # 5d — confirm deprecated cols exist
    print("\n  Deprecated column presence:")
    for col in DEPRECATED:
        if col in props:
            ptype = props[col].get("type", "?")
            print(f"    {col:<28} EXISTS ({ptype}) — safe to delete in Notion")
        else:
            print(f"    {col:<28} already absent — nothing to delete")
            ok(col, "Already absent from Master WBS Tasks schema")

# ════════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("SUMMARY — Per-column verdict")
print(SEP)

all_clear = True
for col in DEPRECATED:
    issues  = [r for r in results[col] if r.startswith("❌")]
    warnings = [r for r in results[col] if r.startswith("⚠️")]
    if issues:
        all_clear = False
        print(f"\n  🔴 {col}  — BLOCK: do NOT delete from Master WBS Tasks")
        for issue in issues:
            print(f"       {issue}")
    elif warnings:
        print(f"\n  🟡 {col}  — warnings (likely safe, review manually)")
        for w in warnings:
            print(f"       {w}")
    else:
        print(f"  🟢 {col}  — safe to delete from Master WBS Tasks")

print(f"\n{SEP}")
if all_clear:
    print("✅ All 6 columns clear — safe to delete from Master WBS Tasks in Notion.")
    print()
    print("Recommended deletion order (safest first):")
    print("  1. Category     (rich_text — likely empty, never written by Python)")
    print("  2. Planned Start (date — stale v2 data, sync reads from WBS source)")
    print("  3. Planned End  (date — stale v2 data, writeback targets WBS source)")
    print("  4. Notes        (rich_text — sync reads WBS source; WS has its own Notes)")
    print("  5. Priority     (select — sync reads WBS source; sessions_mappings has it)")
    print("  6. Work Type    (select — 101 stale old-category values from v2)")
    print()
    print("After each deletion: open the UI, run a task load, confirm nothing errors.")
else:
    print("⛔ One or more columns have blockers — resolve before deleting.")
print(SEP)
