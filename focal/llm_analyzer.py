"""
llm_analyzer.py
───────────────
Claude API integration for work type health check analysis.

Three analysis functions:
  analyze_coding_consistency()  -- are existing work type assignments correct?
  detect_emerging_types()       -- do descriptions suggest gaps in the taxonomy?
  analyze_overlaps()            -- are current type boundaries clear and distinct?

Each function batches records and calls the Anthropic API with structured JSON
output. All prompts include the current work type definitions as system context.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

MODEL         = "claude-sonnet-4-6"
BATCH_SIZE    = 15   # records per API call
RETRY_DELAYS  = [2, 5, 15]  # seconds between retries on rate limit


def _make_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def _build_type_guide(work_type_defs: list[dict]) -> str:
    lines = []
    for wt in work_type_defs:
        desc = wt.get("description", "")
        examples = wt.get("examples", [])
        line = f'- {wt["name"]}: {desc}'
        if examples:
            line += f'  (e.g. {", ".join(examples[:3])})'
        lines.append(line)
    return "\n".join(lines)


def _call_with_retry(client: anthropic.Anthropic, **kwargs) -> Any:
    """Call client.messages.create with exponential backoff on rate limits."""
    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            if delay is None:
                raise
            time.sleep(delay)
        except Exception:
            raise


# ── Public analysis functions ──────────────────────────────────────────────────

def analyze_coding_consistency(
    records: list[dict],
    work_type_defs: list[dict],
    api_key: str,
) -> list[dict]:
    """Check whether each record's current work_type is appropriate.

    Args:
        records: List of {id, name, work_type, notes, project_name, record_type}
        work_type_defs: Active work types from work_type_manager.get_work_types()
        api_key: Anthropic API key

    Returns:
        List of {id, current_work_type, suggested_work_type, confidence,
                 rationale, is_correct, record_type, name, project_name}
    """
    if not records:
        return []

    client     = _make_client(api_key)
    type_guide = _build_type_guide(work_type_defs)
    valid_names = [wt["name"] for wt in work_type_defs]
    results: list[dict] = []

    system_prompt = f"""You are an expert work type classifier for a faculty workload management system.

Work type taxonomy (use ONLY these exact names):
{type_guide}

Your job: For each task/work session provided, assess whether its current work type is correct.
Respond ONLY with a JSON array. One object per record:
{{
  "id": "<record id>",
  "is_correct": true/false,
  "suggested_work_type": "<exact name from taxonomy, or same as current if correct>",
  "confidence": <integer 0-100>,
  "rationale": "<1-2 sentence explanation>"
}}"""

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        user_content = "Classify these records:\n\n" + json.dumps(
            [
                {
                    "id":           r["id"],
                    "name":         r.get("name", ""),
                    "current_work_type": r.get("work_type", ""),
                    "notes":        r.get("notes", ""),
                    "project_name": r.get("project_name", ""),
                }
                for r in batch
            ],
            ensure_ascii=False,
            indent=2,
        )

        try:
            response = _call_with_retry(
                client,
                model=MODEL,
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            batch_results = json.loads(raw)
        except (json.JSONDecodeError, IndexError, Exception) as e:
            # On parse failure, mark all in batch as unanalyzed
            for r in batch:
                results.append({
                    "id":                  r["id"],
                    "name":                r.get("name", ""),
                    "project_name":        r.get("project_name", ""),
                    "record_type":         r.get("record_type", "work_session"),
                    "current_work_type":   r.get("work_type", ""),
                    "suggested_work_type": r.get("work_type", ""),
                    "confidence":          0,
                    "rationale":           f"Analysis failed: {e}",
                    "is_correct":          True,
                    "error":               True,
                })
            continue

        # Merge API output with original record metadata
        id_to_record = {r["id"]: r for r in batch}
        for item in batch_results:
            rec = id_to_record.get(item.get("id"), {})
            suggested = item.get("suggested_work_type", rec.get("work_type", ""))
            if suggested not in valid_names:
                suggested = rec.get("work_type", "")
            results.append({
                "id":                  item.get("id", ""),
                "name":                rec.get("name", ""),
                "project_name":        rec.get("project_name", ""),
                "record_type":         rec.get("record_type", "work_session"),
                "current_work_type":   rec.get("work_type", ""),
                "suggested_work_type": suggested,
                "confidence":          min(100, max(0, int(item.get("confidence", 50)))),
                "rationale":           item.get("rationale", ""),
                "is_correct":          bool(item.get("is_correct", True)),
            })

    return results


def detect_emerging_types(
    descriptions: list[str],
    current_types: list[dict],
    api_key: str,
    sample_ids: list[str] | None = None,
) -> list[dict]:
    """Analyze task descriptions for patterns not captured by current work types.

    Returns:
        List of {proposed_name, rationale, example_indices, projected_impact_pct}
    """
    if not descriptions:
        return []

    client     = _make_client(api_key)
    type_guide = _build_type_guide(current_types)

    system_prompt = f"""You are analyzing a faculty workload system's task taxonomy for gaps.

Current work type taxonomy:
{type_guide}

Study the provided task descriptions. Identify recurring patterns of work that
are NOT well captured by the existing types. Only propose new types if there is
a clear, distinct pattern appearing in multiple tasks.

Respond ONLY with a JSON array (may be empty if no gaps found):
[
  {{
    "proposed_name": "<emoji + name for new type>",
    "rationale": "<why this is distinct from existing types>",
    "example_descriptions": ["<up to 3 example task descriptions>"],
    "projected_impact_pct": <estimated % of total tasks that might fit this type>
  }}
]"""

    # Chunk descriptions to avoid token limits
    chunk = descriptions[:100]
    user_content = "Task descriptions to analyze:\n\n" + "\n".join(
        f"{i+1}. {d}" for i, d in enumerate(chunk)
    )

    try:
        response = _call_with_retry(
            client,
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as e:
        return [{"error": str(e), "proposed_name": "", "rationale": "", "example_descriptions": [], "projected_impact_pct": 0}]


def analyze_overlaps(
    work_types: list[dict],
    sample_records: list[dict],
    api_key: str,
) -> list[dict]:
    """Evaluate current work type definitions for conceptual overlaps or confusion.

    Returns:
        List of {type_a, type_b, overlap_description, suggested_action, examples}
    """
    if len(work_types) < 2:
        return []

    client     = _make_client(api_key)
    type_guide = _build_type_guide(work_types)

    # Build a few examples per type for context
    type_examples: dict[str, list[str]] = {}
    for r in sample_records:
        wt = r.get("work_type", "")
        if wt:
            type_examples.setdefault(wt, [])
            if len(type_examples[wt]) < 5:
                type_examples[wt].append(r.get("name", ""))

    examples_text = "\n".join(
        f"{wt}: {', '.join(names)}"
        for wt, names in type_examples.items()
        if names
    )

    system_prompt = """You are evaluating a work type taxonomy for a faculty workload system.
Identify pairs of types that have overlapping or confusing boundaries.

Respond ONLY with a JSON array (may be empty if boundaries are clear):
[
  {
    "type_a": "<name>",
    "type_b": "<name>",
    "overlap_description": "<what makes them confusable>",
    "suggested_action": "merge" or "clarify" or "rename",
    "suggestion_detail": "<specific proposed change>",
    "examples": ["<task that is hard to classify>"]
  }
]"""

    user_content = f"Work type taxonomy:\n{type_guide}\n\nSample task assignments:\n{examples_text}"

    try:
        response = _call_with_retry(
            client,
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except Exception as e:
        return [{"error": str(e), "type_a": "", "type_b": "", "overlap_description": "", "suggested_action": ""}]
