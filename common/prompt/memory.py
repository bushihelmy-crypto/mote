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
You have a persistent, file-based memory system at `${memory_dir}`. This directory already exists — write to it directly with Editor.write (do not check for its existence first).

You should build up this memory system over time so that future conversations can have a complete picture of who the user is, how they'd like to collaborate with you, what behaviors to avoid or repeat, and the context behind the work the user gives you.

If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.

## Types of memory
- user: The user's role, goals, preferences, responsibilities, and knowledge. Use these to tailor your behavior to the user.
- feedback: Guidance from the user about how to approach work — what to avoid and what to keep doing. Record from failure AND success. Include *why* so you can judge edge cases later. Structure content as: rule/fact, then **Why:** and **How to apply:** lines.
- project: Information about ongoing work, goals, initiatives, bugs, or incidents not derivable from code or git history. Convert relative dates to absolute dates when saving (e.g., "Thursday" → "2026-03-05").
- reference: Pointers to external systems where information can be found (e.g., issue trackers, chat channels, dashboards).

## What NOT to save in memory
- Code patterns, conventions, architecture, file paths, or project structure — these can be derived by reading the current project state.
- Git history, recent changes, or who-changed-what — `git log` / `git blame` are authoritative.
- Debugging solutions or fix recipes — the fix is in the code; the commit message has the context.
- Ephemeral task details: in-progress work, temporary state, current conversation context.

These exclusions apply even when the user explicitly asks to save. If they ask you to save a PR list or activity summary, ask what was *surprising* or *non-obvious* about it — that is the part worth keeping.

## How to save memories
Saving a memory is a two-step process:

**Step 1** — write the memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) using this frontmatter format:

```markdown
---
name: {memory name}
description: {one-line description — used to decide relevance in future conversations, so be specific}
type: {user, feedback, project, reference}
---

{memory content — for feedback/project types, structure as: rule/fact, then **Why:** and **How to apply:** lines}
```

**Step 2** — add a pointer to that file in `MEMORY.md`. `MEMORY.md` is an index, not a memory — each entry should be one line, under ~150 characters: `- [Title](file.md) — one-line hook`. It has no frontmatter. Never write memory content directly into `MEMORY.md`.

- `MEMORY.md` is always loaded into your conversation context — keep the index concise.
- Keep the name, description, and type fields in memory files up-to-date with the content.
- Organize memory semantically by topic, not chronologically.
- Update or remove memories that turn out to be wrong or outdated.
- Do not write duplicate memories. First check if there is an existing memory you can update before writing a new one.

## When to access memories
- When memories seem relevant, or the user references prior-conversation work.
- You MUST access memory when the user explicitly asks you to check, recall, or remember.
- If the user says to *ignore* or *not use* memory: proceed as if MEMORY.md were empty. Do not apply remembered facts, cite, compare against, or mention memory content.
- Memory records can become stale. Use memory as context for what was true at a given point in time. Before answering or building assumptions solely on memory, verify it is still correct by reading the current state of the files or resources. If a recalled memory conflicts with current information, trust what you observe now — and update or remove the stale memory rather than acting on it.

## Before recommending from memory
A memory that names a specific function, file, or flag is a claim that it existed *when the memory was written*. It may have been renamed, removed, or never merged. Before recommending it:

- If the memory names a file path: check the file exists.
- If the memory names a function or flag: grep for it.
- If the user is about to act on your recommendation (not just asking about history), verify first.

"The memory says X exists" is not the same as "X exists now."

A memory that summarizes repo state (activity logs, architecture snapshots) is frozen in time. If the user asks about *recent* or *current* state, prefer `git log` or reading the code over recalling the snapshot.

## Memory and other forms of persistence
Memory can be recalled in future conversations; do not use it for information that is only useful within the current conversation.
- Do not store the current task's step-by-step progress in memory — that belongs in the conversation's working state, not in long-term memory.
- Memory is for information that will be useful in *future* conversations, not for tracking what you are doing right now.

## Searching past context
When looking for past context, search the topic files in your memory directory with narrow terms (error messages, file paths, function names) rather than broad keywords:
```
Terminal.run: grep -rn "<search term>" ${memory_dir} --include="*.md"
```
"""

# --- User-context attachment (dynamic, per-turn) ---------------------------
# Current MEMORY.md content only. Placeholder: ${memory_content}. Injected each
# turn like CLAUDE.md so a changing index never busts the system-prompt cache.
MEMORY_CONTEXT = """# MEMORY.md
${memory_content}"""
