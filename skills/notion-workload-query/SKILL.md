---
name: notion-workload-query
description: >
  Use this skill whenever querying the faculty Notion PM system for work data —
  work sessions, task status, project summaries, time logs, or workload overviews.
  Trigger on requests like: "what did I work on today/this week", "show me my
  active tasks", "how much time did I log on X", "what's overdue", "summarize
  my workload", "what sessions are logged", "what tasks are in progress", or any
  question that requires fetching live data from Work Sessions, Master WBS Tasks,
  or the Projects database. This skill defines the minimal correct query sequence
  so Claude fetches only what's needed and avoids bloating context with unnecessary
  Notion API response data. ALWAYS load this skill before making Notion data queries.
---

# notion-workload-query

Fetching Notion data naively — querying full databases and reading all properties —
is the primary cause of context bloat in this project. This skill defines lean,
targeted query patterns: fetch exactly the right database, filter to the right rows,
and extract only the fields the task requires.

---

## Pre-loaded database IDs (do not look these up)

```
⏱️ Work Sessions    page: 308c193fbba34a1ebe8d817fd72e9d9a
                    collection: b3982f2e-a253-4a3b-8201-c1a84e34e70b

📋 Master WBS Tasks db:   2de3b2f3d9b74481bc88511ea94de45e
                    collection: 94fa9ee4-ec0e-4cc3-8a8f-238d2b6a835c

📁 Projects         page: 01705badbb854f019baf7d0ec68b8c7d
                    collection: 80ee95ce-1458-4353-aeab-48f3c7e49be8
```

---

## Core principle: Work Sessions is the primary source

**Do not start with Master WBS Tasks or Projects.** Work Sessions is the leaf node
that already contains relations to both Task and Project. Starting here gives you
everything in one fetch and avoids the need to join from the other direction.

```
Work Session
  ├── Session Name (title)
  ├── Session Start / Session End (dates)
  ├── Duration (formula — hours as number)
  ├── Work Type (select)
  ├── Status (select: In Progress / Completed)
  ├── Task → relation to Master WBS Tasks (gives task name + work type)
  └── Project → relation to 📁 Projects (gives project name + category)
```

---

## Query recipes by use case

### "What did I work on today / this week?"

```
Tool: notion-fetch
Target: b3982f2e-a253-4a3b-8201-c1a84e34e70b  (Work Sessions collection)
Filter: Session Start on_or_after <date>
        Session Start on_or_before <date>  (omit for open-ended)
```

Extract from each row: Session Name, Session Start, Session End, Duration,
Work Type, Status, Task (relation title), Project (relation title).

**Do not** follow each Task or Project relation ID to fetch more data unless the
user explicitly asks for task-level details like Priority or Notes. The relation
title is usually enough for a summary.

### "What tasks are active / in progress?"

```
Tool: notion-fetch
Target: 94fa9ee4-ec0e-4cc3-8a8f-238d2b6a835c  (Master WBS Tasks collection)
Filter: Status = "In Progress"  (or: Status != "Completed")
```

Extract: Task Name, Status, Priority, Planned End, Work Type, Project (relation title).

For a project-scoped view, filter by Project relation = `<project_entry_id>`.
Known project entry IDs are in the `notion-new-project` skill.

### "What's overdue?"

```
Tool: notion-fetch
Target: 94fa9ee4-ec0e-4cc3-8a8f-238d2b6a835c
Filter: Planned End before <today>
        Status != "Completed"
```

### "How much time did I log on project X?"

```
Tool: notion-fetch
Target: b3982f2e-a253-4a3b-8201-c1a84e34e70b
Filter: Project = <project_entry_id>
        Status = "Completed"  (optional: omit to include in-progress)
```

Sum the Duration field across rows.

### "Give me a summary of this week's work"

Do this in two steps to keep context lean:

**Step 1** — Fetch Work Sessions for the date range (single call, as above).

**Step 2** — Summarize in-context. Do NOT make additional fetches to enrich task
details unless the user asks for them. The Work Sessions rows already contain task
name (via relation title) and project name — that's enough for a summary.

If Work Sessions has no entries for the period, say so and suggest the user check
whether sessions were logged. Do not fetch Master WBS Tasks as a fallback.

---

## What NOT to fetch

Avoid these patterns — they pull in far more data than needed:

| ❌ Don't do this | ✅ Do this instead |
|-----------------|-------------------|
| Fetch the full Projects DB to get project names | Read project name from the Work Session's `Project` relation title — it's already there |
| Fetch each linked Task page to get task names | Read task name from the Work Session's `Task` relation title |
| Fetch Work Sessions + Master WBS Tasks + Projects in parallel | Start with Work Sessions; only fetch others if a specific follow-up field is needed |
| Query without a date or status filter | Always filter — an unfiltered Work Sessions fetch returns all 241+ entries |

---

## Handling empty / sparse data

Work Sessions are only populated when the sync tool's "Add to Work Sessions" button
is used, or when the user manually creates entries in Notion. If a query returns
nothing, the correct response is to say so clearly — not to fall back to fetching
WBS Tasks and guessing at what was worked on.

Partial data (e.g., Session Start but no Session End) is normal. Duration will be
null in this case; show it as "in progress" rather than 0 or blank.

---

## Formatting query results

Keep summaries concise. A good workload summary covers:

- **Total time** (sum of Duration, or "No sessions logged" if empty)
- **By project** (group by Project relation title, sum Duration)
- **By work type** (group by Work Type, sum Duration)
- **Notable tasks** (any with Priority = Urgent, or Status = In Progress)

Do not produce a row-by-row dump of every Work Session unless the user asks for it.
Prose with embedded numbers is cleaner than a long table for daily summaries.
