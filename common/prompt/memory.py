"""
Memory prompt — persistent, file-based memory system for a Role.

An auto-memory prompt (individual mode). The memory module itself is not yet
implemented in Mote; this file prepares the prompt so the behavioral contract
is ready when the storage/injection layer lands.

Two-part split:

  1. MEMORY_INSTRUCTIONS — the *behavioral rules* (taxonomy, how/when to save
     and recall). Static: goes into the system prompt, where it stays cacheable
     across turns. Only placeholder is ${memory_dir}.

  2. MEMORY_CONTEXT — the *current MEMORY.md content*. Dynamic: injected via
     user context each turn (like CLAUDE.md), so a changing index never busts
     the system-prompt cache prefix. Only placeholder is ${memory_content}.

Assembly (mirrors role / PromptBuilder):
    from string import Template
    from mote.common.prompt.memory import (
        MEMORY_INSTRUCTIONS, MEMORY_CONTEXT, MEMORY_EMPTY_STATE,
    )

    # system prompt section (static, cacheable)
    instructions = Template(MEMORY_INSTRUCTIONS).safe_substitute(
        memory_dir="/path/to/.mote/memory/",
    )
    # user-context attachment (dynamic, per-turn)
    context = Template(MEMORY_CONTEXT).safe_substitute(
        memory_content=current_memory_md or MEMORY_EMPTY_STATE,
    )

Memories are constrained to four types capturing context NOT derivable from
the current project state. Code patterns, architecture, git history, and file
structure are derivable (via grep/git) and must NOT be saved as memories.
"""

# Shown in place of MEMORY.md content when the index is still empty.
MEMORY_EMPTY_STATE = "Your MEMORY.md is currently empty. When you save new memories, they will appear here."

# Frontmatter every memory file carries. The name/description fields drive
# future relevance decisions, so they must stay specific and up-to-date.
MEMORY_FRONTMATTER_EXAMPLE = """```markdown
---
name: {memory name}
description: {one-line description — used to decide relevance in future conversations, so be specific}
type: {user, feedback, project, reference}
---

{memory content — for feedback/project types, structure as: rule/fact, then **Why:** and **How to apply:** lines}
```"""

# --- System prompt section (static, cacheable) -----------------------------
# Behavioral rules only — NO MEMORY.md content. Placeholder: ${memory_dir}.
MEMORY_INSTRUCTIONS = """
# Memory
You have a persistent, file-based memory at `${memory_dir}` (already exists — write directly). Build it up over time so future conversations know who the user is, how to collaborate, what to repeat or avoid, and the context behind their work. If the user asks you to remember something, save it now as the best-fit type; if they ask you to forget something, remove that entry.

## Types (capture only what is NOT derivable from current project state)
- user: the user's role, goals, preferences, responsibilities, knowledge — to tailor your behavior.
- feedback: how to approach work (what to avoid / keep doing), from failure AND success. Include *why*, so you can judge edge cases. Structure as: rule/fact, then **Why:** and **How to apply:**.
- project: ongoing work, goals, bugs, or incidents not in code or git. Convert relative dates to absolute (e.g. "Thursday" → "2026-03-05").
- reference: pointers to external systems (issue trackers, chat channels, dashboards).

## What NOT to save (even if asked)
Anything derivable from current state: code patterns/architecture/paths/structure (read the project), git history/who-changed-what (`git log`/`blame`), fix recipes (the fix is in the code). Nor ephemeral state: in-progress work, current-conversation context, step-by-step task progress — that belongs in working state, not long-term memory. If asked to save a PR list or activity summary, keep only what was *surprising* or *non-obvious*.

## How to save (two steps)
**Step 1** — write the memory to its own file (e.g. `user_role.md`, `feedback_testing.md`) with this frontmatter:

${frontmatter_example}

**Step 2** — add a one-line pointer in `MEMORY.md` (an index, not a memory; no frontmatter; never put memory content here): `- [Title](file.md) — one-line hook`.

- `MEMORY.md` is always loaded into context — keep it concise.
- Organize semantically by topic, not chronologically. Keep name/description/type up-to-date.
- Before writing, check for an existing memory to update instead — no duplicates. Remove memories that prove wrong or outdated.

## Accessing memory
- Access when relevant or when the user references prior work; you MUST access when explicitly asked to check/recall/remember.
- If the user says to *ignore* memory: proceed as if MEMORY.md were empty — don't apply, cite, or mention it.
- Memory is a snapshot of what was true when written — it can be stale. A memory naming a file/function/flag claims it existed *then*; it may be renamed or gone. Before recommending it (or when the user is about to act on it), verify: check the path exists, grep the symbol. For *recent*/*current* repo state, prefer `git log` or reading the code over a frozen snapshot. If memory conflicts with what you observe, trust the observation and update the stale memory.
- Search past context with narrow terms (error messages, paths, function names), not broad keywords:
```
Terminal.run: grep -rn "<search term>" ${memory_dir} --include="*.md"
```
""".replace(
    "${frontmatter_example}", MEMORY_FRONTMATTER_EXAMPLE
)

# --- User-context attachment (dynamic, per-turn) ---------------------------
# Current MEMORY.md content only. Placeholder: ${memory_content}. Injected each
# turn like CLAUDE.md so a changing index never busts the system-prompt cache.
MEMORY_CONTEXT = """# MEMORY.md
${memory_content}"""
