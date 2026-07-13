"""Model-facing tool descriptions and tool-result text.

Every built-in tool's ``description`` (the text the model reads to learn what a
tool does) lives here as a named constant, plus a couple of tool-result strings
that are really prompt text (FILE_UNCHANGED_STUB) and the AskUserQuestion long
form. Keeping them in one place lets the prompts be reviewed/edited together,
decoupled from the tool implementation code.
"""

# --- Filesystem tools ------------------------------------------------------

EDIT_DESCRIPTION = (
    "Performs exact string replacements in files.\n"
    "\n"
    "You must use the Read tool at least once on the file before editing it — "
    "the edit fails otherwise. Preserve the exact indentation (tabs/spaces) as "
    "it appears in the file, but do NOT include the line-number prefix that Read "
    "adds to its output.\n"
    "\n"
    "The edit fails if old_string is not unique in the file: either add more "
    "surrounding context so the match is unique, or set replace_all=true to "
    "change every occurrence (useful for renaming a variable across the file). "
    "Prefer editing an existing file over rewriting it whole with Write."
)

WRITE_DESCRIPTION = (
    "Writes a file to the local filesystem, creating any missing parent "
    "directories.\n"
    "\n"
    "- If the file already exists, it is OVERWRITTEN; you must use the Read tool "
    "on it first, so you are editing from its current contents rather than "
    "clobbering changes you have not seen.\n"
    "- ALWAYS prefer editing an existing file with the Edit tool when only part "
    "of it changes. Only use Write to create a new file or to fully replace one.\n"
    "- NEVER proactively create documentation (*.md) or README files unless the "
    "user explicitly asks for them."
)

READ_DESCRIPTION = (
    "Reads a file from the local filesystem. The file_path may be absolute, or "
    "relative to the working directory; ~ is expanded.\n"
    "\n"
    "- By default it reads up to 2000 lines from the start of the file. Use "
    "offset (1-indexed start line) and limit for large files; a Grep hit "
    "reported as path:42 is read with offset=42.\n"
    "- Output is returned with cat -n style line numbers (a right-aligned number "
    "then an arrow then the line). These numbers are for your reference only — "
    "never reproduce the number+arrow prefix when quoting or editing content.\n"
    "- Images (png/jpg/jpeg/gif/webp) and PDFs (mode='visual') are shown to you "
    "visually; Jupyter notebooks (.ipynb) are rendered as text; rich documents "
    "(PDF/Word/Excel) are extracted to text with line numbers by default.\n"
    "- You may read multiple distinct files in a single turn by making several "
    "Read calls at once; prefer this over reading them one at a time.\n"
    "- ALWAYS use this tool to read files instead of shell commands like cat / "
    "head / tail: it handles line numbering, large-file slicing, and media. If a "
    "file was read and is unchanged, a short 'unchanged' note may be returned in "
    "place of the body — that is expected."
)

# Returned in place of file contents when an already-read file is unchanged on
# disk — prompt text, not a real read result. Note: the referenced earlier
# result MAY have been cleared by context folding, so the wording must not
# promise it is still visible; it gives a recovery path when it is not.
FILE_UNCHANGED_STUB = (
    "File unchanged on disk since your last read. If that earlier Read result "
    "is still visible above, use it. If it has been cleared from context and "
    "you can no longer see the content, do NOT fall back to shell cat — re-read "
    "with an explicit offset/limit slice (any range you have not requested at "
    "this exact same offset+limit before) to force fresh content."
)

# --- Search tools ----------------------------------------------------------

GLOB_DESCRIPTION = (
    "Fast file-name pattern matching that works with any codebase size.\n"
    "\n"
    '- Supports glob patterns like "**/*.js" or "src/**/*.ts". Returns matching '
    "file paths sorted by modification time (most recent first), limited to the "
    "first 100.\n"
    "- Use this to find files BY NAME or path shape. To search file CONTENTS, "
    "use Grep instead. To explore a deep tree with several rounds of globbing "
    "and grepping, launch an agent to reduce round-trips.\n"
    "- ALWAYS use this tool instead of running find / ls through Bash."
)

GREP_DESCRIPTION = (
    "A powerful content search tool built on ripgrep. ALWAYS use this for "
    "searching file contents — never run grep / rg through the Bash tool.\n"
    "\n"
    '- Searches file CONTENTS with full regex syntax (e.g. "log.*Error", '
    '"function\\s+\\w+"). Plain-text files (including .csv) are searched '
    "directly; rich documents — PDF (.pdf), Word (.docx), Excel (.xlsx) — are "
    "searched by extracting their text first.\n"
    '- Filter the file set with glob (e.g. "*.py", "*.{ts,tsx}") or type '
    '(e.g. "py", "rust", "pdf"). Choose output_mode: files_with_matches '
    "(default), content, or count.\n"
    "- In the default mode each result is 'path:line', where line is the first "
    "match — pass that number straight to Read's offset to jump to it. Use "
    "content mode with context (-A/-B/-C) when you need the surrounding lines.\n"
    "- Escape literal braces in the regex ({} is regex syntax). For patterns "
    "that must span lines, set multiline=true."
)

# --- Execution tools -------------------------------------------------------

BASH_DESCRIPTION = (
    "Runs a bash command. Use the `workdir` param to run in a subdirectory; a "
    "`cd` does not persist across calls. Optionally pass `inputs` (an object) to "
    "feed typed values into the command: the whole object is exported as the "
    "$INPUTS env var (JSON), and each scalar entry with an identifier-safe key is "
    "also exported as its own env var. If the command's stdout is valid JSON it "
    "is parsed into the structured result (so a caller can index into it), else "
    "the structured result is the raw stdout text."
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

END_DESCRIPTION = "End the current task. Your last message stands as the final reply."

ASK_USER_DESCRIPTION = "Use this when you fail the current task or if you are unsure of the situation encountered."

REPLY_TO_USER_DESCRIPTION = "Reply to the user with the content provided."

# AskUserQuestion long-form description. The parameter schema is defined as
# pydantic models in mote.schema (AskUserQuestionInput).
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
