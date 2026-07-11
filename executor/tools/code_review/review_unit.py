"""Agent leaf — review one file's diff with a full Role tool-loop.

Each reviewed file gets its own child :class:`Role` configured with read-only
investigation tools (Read/Grep/Glob), the native command protocol, and a bypass
permission stance. The Role is told to investigate the diff (dynamically
recalling surrounding context via its tools) and emit its findings as a JSON
array in its final summary. We parse that JSON, then hand each comment's
``existing_code`` to the deterministic resolver to recover line numbers.

Mirrors :class:`mote.executor.tools.agent_tool.Agent`'s spawn → run →
cleanup pattern: a child Role can run with its default context/config, and its
terminal summary is read off ``role.state.last_end_output``.
"""
from __future__ import annotations

from typing import List

from mote.common.logs import logger

from ._agent import build_child_role, extract_json_array, run_child_for_text
from .format import Finding
from .parser import FileDiff
from .resolver import resolve_comment

_REVIEW_SYSTEM_PROMPT = """\
You are an expert code reviewer. You are given the diff of a single changed file.
Investigate the change using your tools (Read the full file, Grep for related
usages, Glob for related files) so your review is grounded in the surrounding
code — do not guess.

Review focus (in priority order):
1. Bugs / correctness (logic errors, off-by-one, nil/None handling, races).
2. Security (injection, unsafe input handling, secrets, auth gaps).
3. Resource / performance issues (leaks, N+1, needless allocation).
4. API/contract regressions and error handling.
5. Readability / maintainability (only if material).

Only comment on the CHANGED (added) lines and their immediate context. Do not
nitpick style that a linter would catch. Do not suggest defensive checks for
inputs that cannot actually occur given how the code is called (investigate the
call sites with your tools before flagging a missing guard).

When you are done investigating, end your turn with ONLY a JSON array of
comments (no prose around it), each shaped:

[
  {
    "existing_code": "<the exact lines from the file you are commenting on, copied verbatim>",
    "severity": "critical" | "warning" | "info",
    "message": "<concise explanation of the issue and the suggested fix>"
  }
]

Rules for "existing_code":
- Copy the lines EXACTLY as they appear in the new version of the file
  (whitespace/indentation included), so they can be located precisely.
- Prefer the smallest snippet that uniquely identifies the spot (1-5 lines).
If there are no issues, output an empty array: []
"""

_USER_PROMPT_TEMPLATE = """\
Review the following change to `{path}`.

Diff:
```diff
{diff}
```
{related}
Investigate with your tools as needed, then output your findings as the JSON
array described in your instructions.
"""

_RELATED_BLOCK = """\

Related files you may want to read for context (changed alongside this one or
likely callers/definitions):
{paths}
"""


def _render_related(file_diff: FileDiff) -> str:
    """Render the related-files hint block, or '' when there are none."""
    related = getattr(file_diff, "related", None) or []
    if not related:
        return ""
    paths = "\n".join(f"- {p}" for p in related)
    return _RELATED_BLOCK.format(paths=paths)


def _render_file_diff(file_diff: FileDiff) -> str:
    """Render a FileDiff back to a compact unified-diff text for the prompt."""
    parts: List[str] = [f"--- a/{file_diff.old_path or file_diff.path}", f"+++ b/{file_diff.path}"]
    for hunk in file_diff.hunks:
        parts.append(f"@@ +{hunk.new_start} @@")
        for _lineno, text in hunk.lines:
            parts.append(text)
    return "\n".join(parts)


# Backwards-compatible alias: the shared helper does the extraction now, but
# existing tests reference ``ru._extract_json_array`` directly.
_extract_json_array = extract_json_array


def _comments_to_findings(comments: list, file_diff: FileDiff) -> List[Finding]:
    """Turn raw comment dicts into Findings with resolved line numbers."""
    findings: List[Finding] = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        existing = str(c.get("existing_code", ""))
        severity = str(c.get("severity", "info")) or "info"
        message = str(c.get("message", "")).strip()
        if not message:
            continue
        span = resolve_comment(existing, file_diff) if existing else None
        start = span[0] if span else None
        end = span[1] if span else None
        findings.append(
            Finding(
                file=file_diff.path,
                severity=severity,
                message=message,
                existing_code=existing,
                start_line=start,
                end_line=end,
            )
        )
    return findings


def _build_review_role(repo_dir: str, parent_session_id: str = ""):
    """Construct a read-only child Role for reviewing one file."""
    return build_child_role(
        name="CodeReviewer",
        system_prompt=_REVIEW_SYSTEM_PROMPT,
        repo_dir=repo_dir,
        parent_session_id=parent_session_id,
        tools=["Read", "Grep", "Glob"],
    )


async def review_one_file(
    file_diff: FileDiff,
    repo_dir: str,
    parent_session_id: str = "",
) -> List[Finding]:
    """Run a child Role to review *file_diff* and return resolved findings.

    Unparseable / empty output yields an empty findings list. Run failures are
    NOT swallowed here — per-file isolation lives in the batch node's
    ``_safe_review`` (so one bad file never sinks the batch) while structural
    wiring bugs still surface. The child Role is always cleaned up by the spawn
    handle.
    """
    role = _build_review_role(repo_dir, parent_session_id)
    diff_text = _render_file_diff(file_diff)
    prompt = _USER_PROMPT_TEMPLATE.format(
        path=file_diff.path,
        diff=diff_text,
        related=_render_related(file_diff),
    )
    output = await run_child_for_text(role, prompt, label=f"review {file_diff.path}")
    if output is None:
        return []

    comments = _extract_json_array(output)
    if comments is None:
        logger.warning(f"code_review: could not parse review JSON for {file_diff.path}")
        return []
    return _comments_to_findings(comments, file_diff)
