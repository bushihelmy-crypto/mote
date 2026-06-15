"""Model-facing tool descriptions and tool-result text.

Every built-in tool's ``description`` (the text the model reads to learn what a
tool does) lives here as a named constant, plus a couple of tool-result strings
that are really prompt text (FILE_UNCHANGED_STUB) and the AskUserQuestion long
form. Keeping them in one place lets the prompts be reviewed/edited together,
decoupled from the tool implementation code.
"""

# --- Filesystem tools ------------------------------------------------------

EDIT_DESCRIPTION = (
    "Performs exact string replacements in files. You must Read the file at "
    "least once before editing it. The edit fails if old_string is not unique "
    "in the file — provide more surrounding context to make it unique, or set "
    "replace_all to replace every occurrence (useful for renaming)."
)

WRITE_DESCRIPTION = (
    "Write a file to the local filesystem. Creates the file and any missing "
    "parent directories, or overwrites it if it already exists. Prefer editing "
    "an existing file over rewriting it when only part changes."
)

READ_DESCRIPTION = (
    "Read a file from the local filesystem. Text files return contents with "
    "line numbers (offset/limit select a slice); images and PDFs are shown "
    "to the model visually; Jupyter notebooks are rendered as text."
)

# Returned in place of file contents when an already-read file is unchanged on
# disk — prompt text, not a real read result.
FILE_UNCHANGED_STUB = (
    "File unchanged since last read. The content from the earlier Read "
    "tool_result in this conversation is still current — refer to that "
    "instead of re-reading."
)

NOTEBOOK_EDIT_DESCRIPTION = (
    "Completely replaces the contents of a specific cell in a Jupyter "
    "notebook (.ipynb file) with new source. The notebook_path must be "
    "absolute. Use edit_mode=insert to add a new cell after the cell named "
    "by cell_id (or at the start if omitted), and edit_mode=delete to "
    "remove the cell named by cell_id."
)

# --- Search tools ----------------------------------------------------------

GLOB_DESCRIPTION = (
    "Fast file pattern matching tool that works with any codebase size. "
    "Supports glob patterns like \"**/*.js\" or \"src/**/*.ts\". Returns "
    "matching file paths sorted by modification time (most recent first). "
    "Use this to find files by name; for content search use Grep instead."
)

GREP_DESCRIPTION = (
    "A powerful search tool built on ripgrep. Searches file CONTENTS with a "
    "regular expression. Also searches inside rich documents — PDF (.pdf), "
    "Word (.docx) and Excel (.xlsx) — by extracting their text first; CSV "
    "and other plain-text files are searched directly. Filter by glob or "
    "file type; choose output mode files_with_matches (default), content, or "
    "count. ALWAYS use this for content search instead of running grep/rg "
    "through the Bash tool."
)

# --- Execution tools -------------------------------------------------------

BASH_DESCRIPTION = "Execute a bash command. State (cwd) persists across calls within a session."

PYTHON_DESCRIPTION = (
    "Execute Python code in a persistent Jupyter kernel kept alive across "
    "calls (one per session). Variables, imports, and functions persist, so "
    "you can build up state step by step. Set interrupt=true to send a "
    "KeyboardInterrupt, restart=true to clear all state, close=true to shut "
    "the kernel down. For shell commands use the Bash tool."
)

TERMINAL_DESCRIPTION = (
    "Type into a persistent interactive terminal kept alive across calls (one "
    "per session). State (cwd, env, venv) persists; typing a program like "
    "'python3' puts it in the foreground so later input is fed to it. Set "
    "interrupt=true to send Ctrl-C, close=true to shut the terminal down. For "
    "ordinary one-shot commands prefer the Bash tool."
)

# --- ApplyPatch grammar (embedded in the tool description so the model learns
# the exact patch format on both the native and XML channels) ---------------
APPLY_PATCH_GRAMMAR = """\
Apply a structured patch that can Add, Update, Delete, and Move/rename multiple \
files in a single call. Pass the whole patch as the single `input` string, in \
this format:

*** Begin Patch
*** Add File: path/to/new_file.py
+line one of the new file
+line two
*** Delete File: path/to/remove_me.py
*** Update File: path/to/edit_me.py
*** Move to: path/to/renamed.py
@@ optional context anchor (e.g. a function or class signature)
 a context line that already exists (note the single leading space)
-a line to remove
+a line to add
*** End Patch

Rules:
- The patch MUST start with `*** Begin Patch` and end with `*** End Patch`.
- Inside an `*** Update File` hunk, every line starts with one of: a single \
space (unchanged context), `+` (added line), or `-` (removed line). A `@@` line \
optionally followed by a one-line context anchor begins a new chunk and helps \
locate the edit. You do NOT use line numbers; provide a few surrounding context \
lines so the edit can be located unambiguously.
- `*** Move to:` (Update only) renames the file as it is updated.
- `*** End of File` marks a chunk as anchored to the end of the file.
- You must Read a file before Updating, Deleting, or Moving it.
"""

# --- Agent / human-interaction tools ---------------------------------------

AGENT_DESCRIPTION = "Spawn a typed child agent for bounded subtasks."

END_DESCRIPTION = "End the current task and produce a final summary."

ASK_HUMAN_DESCRIPTION = (
    "Use this when you fail the current task or if you are unsure of the situation encountered."
)

REPLY_TO_HUMAN_DESCRIPTION = "Reply to human user with the content provided."

# AskUserQuestion long-form description (ported verbatim from Claude Code's
# AskUserQuestionTool/prompt.ts). The parameter schema is defined as pydantic
# models in metagpt.schema (AskUserQuestionInput).
ASK_USER_QUESTION_PROMPT = """Use this tool when you need to ask the user questions during execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multiSelect: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label
"""
