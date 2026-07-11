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
    'Supports glob patterns like "**/*.js" or "src/**/*.ts". Returns '
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

BASH_DESCRIPTION = (
    "Runs a bash command. Use the `workdir` param to run in a subdirectory; a " "`cd` does not persist across calls."
)

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

WEB_BROWSER_DESCRIPTION = (
    "Drive a persistent web browser kept alive across calls (one per session). "
    "The open tabs, navigated URLs, and logged-in session persist between calls, "
    "so you build up browsing state step by step. Pick an action: "
    "snapshot (return a unified indented tree of the page — prose text and "
    "clickable elements (each tagged with an index like [5]) interleaved in "
    "reading order, so you can both read the page in context and act on it in "
    "one call; a leading * marks elements new since your last snapshot; pass "
    "interactive_only=true to drop the prose and get a compact controls-only "
    "list when tokens are tight), navigate (go to url), click (selector — an element index from the "
    "latest snapshot like '5', or a CSS selector), type (selector + text; set "
    "clear=false to append instead of replace), wait (block until a selector "
    "appears or a JS expression is truthy — for dynamic/SPA content), "
    "detect_forms (list the page's forms and their fillable fields with "
    "selectors), fill_form (fill many fields at once via a {selector: value} "
    "mapping, with an optional submit selector), extract (pull structured data "
    "via a {key: 'selector[@attr]'} schema, returning JSON), read (return the "
    "page's main content as a pure Markdown prose dump — no clickable [N] refs; "
    "use it for long-form reading when you don't need to act. Images and link "
    "URLs are dropped by "
    "default to keep it concise, which is what you want for just reading a "
    "page's text. Only pass extract_links=true when you actually intend to "
    "navigate to a URL on the page, or extract_images=true when you need an "
    "image's src — don't enable them pre-emptively), screenshot (capture the page as an image), "
    "eval (run JavaScript and return its result as JSON), "
    "assist (pause and ask the user to supply something only they can — their "
    "own private data (phone number, email, account, address), a one-time code, "
    "scan a login QR code, clear a graphical captcha; pass a prompt describing "
    "what you need. Never invent a user's personal details (phone, email, ID) — "
    "ask via assist. Code-by-phone/email login is two assists: first ask for the "
    "phone number or email and type it to trigger the code, then ask for the code "
    "the user received. Headless: a "
    "screenshot of the page is saved to disk and the user is told where to view "
    "it, then replies with the value; works for OTP / QR / graphical captcha. "
    "assist only asks — it does not fill anything; act on the user's reply with "
    "type/fill_form. Interactive challenges like sliders need a headed browser), "
    "back (history back — prefer over re-navigating to a page you just left), "
    "tabs (list open tabs), new_tab (open url in a new tab), switch_tab (index), "
    "close_tab (index), close (shut the browser down). "
    "Typical loop: snapshot to see the page (prose + element indices in reading "
    "order), then click/type by index. Prefer snapshot for interaction — it "
    "shows the prose around each control so you know what to click. Re-snapshot "
    "after navigation or any DOM change — indices are only valid for the latest "
    "snapshot. For forms, detect_forms then "
    "fill_form is faster than typing fields one by one. Use read for a pure "
    "prose dump when you only need to read (no refs); use screenshot when layout "
    "matters. When you hit a step only a "
    "human can complete (one-time code, login QR scan, graphical captcha), use "
    "assist to let the user supply it — never try to bypass such a check."
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

ASK_HUMAN_DESCRIPTION = "Use this when you fail the current task or if you are unsure of the situation encountered."

REPLY_TO_HUMAN_DESCRIPTION = "Reply to human user with the content provided."

# AskUserQuestion long-form description (ported verbatim from Claude Code's
# AskUserQuestionTool/prompt.ts). The parameter schema is defined as pydantic
# models in mote.schema (AskUserQuestionInput).
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

# --- Background pipeline brief ------------------------------------------------
# Injected into the system prompt when pipeline tools are available. Kept concise
# and aligned with the MCP description: the pipeline tools themselves are listed
# in the system-prompt "# Pipeline Tools" section; this brief only explains how
# they behave and how to inspect / steer a running one. The engine is a langgraph-style
# transition scheduler (not a static topological DAG) — keep the wording aligned.

BACKGROUND_PIPELINE_SECTION = """\
# Background Pipelines

Some commands are background pipeline tools (listed under "# Pipeline Tools"): each \
runs a multi-step node graph asynchronously — a langgraph-style transition engine. \
Calling one returns immediately with a `task_id`; \
progress and the final result are pushed to you automatically.

A run pauses and notifies you when it needs a decision: a node failed, or an LLM \
edge is asking you to pick a route. Then:
 - `get_node_state(task_id, nodes=...)` — inspect node status / inputs / output.
 - `resume_tasks(task_id, from_node=..., skip_node=..., overrides=...)` — pick a \
route, re-run or skip a node, or restart; `overrides` changes graph inputs.
Read the information and fix the root cause before resuming.
"""
