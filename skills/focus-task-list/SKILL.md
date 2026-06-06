---
name: focus-task-list
description: >
  Use this skill whenever the user asks to see, refresh, or update their focus
  task list — a prioritized view of tasks from Master WBS Tasks grouped by
  urgency. Trigger on: "generate my focus task list", "update my focus tasks",
  "what should I work on", "show me what's due", "refresh my Notion home tasks",
  "what's overdue", "what's due this week", or any request to create or update
  the Focus Tasks page on the Notion Home. This skill reads the live system date,
  queries Master WBS Tasks for overdue/due-today/due-this-week tasks, and
  creates or updates a dedicated 📌 Focus Tasks page linked from the Notion Home.
  ALWAYS load this skill before generating the focus task list — it has all IDs
  and the exact update procedure pre-encoded.
---

# focus-task-list

Generates or refreshes a prioritized task view on the Notion Home page by
querying Master WBS Tasks against the live system date.

---

## Pre-loaded IDs (do not look these up)

```
🏠 Home page          1cf2feec-72bc-4785-a6f3-1bea2b7dcad2
📋 Master WBS Tasks   collection: 94fa9ee4-ec0e-4cc3-8a8f-238d2b6a835c
📁 Projects           collection: 80ee95ce-1458-4353-aeab-48f3c7e49be8
```

---

## Step 1: Get the current date

Run bash to get today and the +7 day boundary — never use training-data dates:

```bash
echo "TODAY=$(date +%Y-%m-%d)"
echo "WEEK_END=$(date -d '+7 days' +%Y-%m-%d 2>/dev/null || date -v+7d +%Y-%m-%d)"
```

Save both values. All date comparisons in Steps 2–3 use these values.

---

## Step 2: Query Master WBS Tasks

Fetch the full Master WBS Tasks collection:

```
Tool: notion-fetch
ID:   collection://94fa9ee4-ec0e-4cc3-8a8f-238d2b6a835c
```

From the response, for each row extract:
- `Task Name` (title)
- `date:Planned End:start` (the due date — this is the field to compare)
- `Priority` (Urgent / High / Normal / Low)
- `Work Type`
- `Project` (relation — use the linked page title as the project name)
- `Auto Status` (formula — the computed status string)
- `Notes` (if non-empty, include as a sub-note)

### Classify each task by due date window

Using TODAY and WEEK_END from Step 1:

| Bucket | Condition |
|--------|-----------|
| 🔴 **Overdue** | `Planned End` < TODAY and Auto Status does NOT contain "Done" or "Completed" |
| 🟡 **Due Today** | `Planned End` = TODAY |
| 🔵 **Due This Week** | `Planned End` > TODAY and `Planned End` ≤ WEEK_END |

**Exclude** any task where:
- `Planned End` is null/empty (no due date set — don't include in this list)
- Auto Status contains "Done", "Completed", or "✅" (already finished)

Within each bucket, sort by Priority: Urgent first, then High, Normal, Low.

---

## Step 3: Find or create the Focus Tasks page

### Check if it already exists

```
Tool: notion-search
Query: "📌 Focus Tasks"
```

Look for a child page of Home (`1cf2feec-72bc-4785-a6f3-1bea2b7dcad2`) with
title "📌 Focus Tasks". If found, save its page ID as `focus_page_id`.

### If NOT found — create it (first run only)

```
Tool: notion-create-pages
Parent: 1cf2feec-72bc-4785-a6f3-1bea2b7dcad2   (Home page)
Title:  📌 Focus Tasks
Icon:   📌
```

Save the returned page ID as `focus_page_id`.

Then tell the user:
> "I've created a 📌 Focus Tasks page under your Home. You can pin it in your
> Notion sidebar by right-clicking → Add to Favorites for quick daily access."

---

## Step 4: Write the focus list content

```
Tool: notion-update-page
Page: focus_page_id
replace_content: true
```

Use this exact content structure:

```markdown
# 📌 Focus Tasks
*Last updated: [TODAY in readable format, e.g. "Thursday, June 5, 2026"]*
*Showing: overdue · due today · due in the next 7 days*

---

## 🔴 Overdue — [N] tasks

[If none]: ✅ Nothing overdue — great!
[If tasks]:
- **[Task Name]** | [Project name] | Due: [Planned End] | [Priority] | [Work Type]
  - [Notes if non-empty]

## 🟡 Due Today — [N] tasks

[If none]: 📭 Nothing due today.
[If tasks]:
- **[Task Name]** | [Project name] | [Priority] | [Work Type]
  - [Notes if non-empty]

## 🔵 Due This Week — [N] tasks  
*(through [WEEK_END in readable format])*

[If none]: 📭 Nothing due in the next 7 days.
[If tasks]:
- **[Task Name]** | [Project name] | Due: [Planned End] | [Priority] | [Work Type]
  - [Notes if non-empty]

---

## 📊 Summary
- Total tasks needing attention: [overdue + due today + due this week]
- Urgent: [count of Urgent across all buckets]
- High: [count of High across all buckets]
```

**Formatting rules:**
- Bold the task name
- For Overdue tasks, show the due date so the user knows how late it is
- Omit Notes line entirely if Notes is empty — don't show an empty bullet
- Show project name as plain text (not a link) — it's readable and avoids long Notion URLs in content
- If ALL three buckets are empty, replace the whole body with: "✅ No tasks with upcoming due dates found. Check that Planned End dates are set on your WBS tasks."

---

## Step 5: Report back in chat

After updating the page, respond with a brief summary:

> "Updated your 📌 Focus Tasks page on Notion Home ([link to focus_page_id]).
>
> **[N] overdue · [N] due today · [N] due this week**
>
> [List just the Urgent/High overdue and due-today tasks inline in chat so the user can see the most critical items without opening Notion]"

Keep the chat summary to ≤10 task lines. Full list is on the Notion page.

---

## Notes on Auto Status

The `Auto Status` field is a formula computed from linked Work Sessions. Its
exact string values depend on session states. When checking whether a task is
done, treat the following as "completed" and exclude from the list:
- Contains "Done" (any case)
- Contains "Completed" (any case)
- Contains "✅"
- Contains "Complete"

If `Auto Status` is null/empty (task has no Work Sessions yet), include it in
the list — it hasn't been started.

---

## Handling tasks with no Planned End

Tasks without a Planned End date won't appear in this list — that's intentional.
If the user mentions a task they expect to see but doesn't, suggest they add a
Due Date in the Project WBS and re-sync.

---

## Re-run behavior

Every time this skill runs, it replaces the full content of the Focus Tasks page
(`replace_content: true`). This is intentional — the list should always reflect
the live state of Master WBS Tasks as of the current date. Old content is
discarded on each refresh.
