"""REVIEW_FILTER — self-critique pass over the accumulated findings.

Runs once after the review ring drains, before ``aggregate``. Hands the whole
findings list to a single child Role that re-reads each comment critically and
drops the ones that are false positives, redundant, or too low-value to surface
(style nits a linter would catch, speculative "consider…" with no concrete
issue). This is OCR's REVIEW_FILTER step: a cheap quality gate that trades one
extra LLM call for a higher signal-to-noise report.

The agent decides per finding by **index** (it returns the indices to keep), so
we never trust it to faithfully echo back the finding objects — we keep the
originals it points at. Best-effort: any failure or unparseable response
degrades to passthrough (keep everything), and an empty list is never produced
from a non-empty input unless the agent explicitly keeps nothing *and* parses
cleanly.
"""
from __future__ import annotations

import json
from typing import List

from mote.common.logs import logger

from ._agent import build_child_role, extract_json_array, run_child_for_text
from .format import Finding

_FILTER_SYSTEM_PROMPT = """\
You are a senior reviewer doing a final quality pass on a set of draft review
comments produced by other reviewers. Your job is to keep only the comments
worth showing the author and discard the rest.

Discard a comment if it is:
- a false positive or speculative ("this might be slow") with no concrete issue,
- a pure style nit a linter would already catch,
- redundant with another comment,
- defensive hardening for a case that cannot actually occur given how the code
  is called (e.g. type-guarding an argument that is always a class because the
  function is only ever used as a class decorator), or
- too trivial to be worth the author's attention.

Keep a comment if it flags a real bug, security issue, correctness/contract
problem, or a materially useful improvement.

You will be given the comments as a numbered JSON array. Investigate with your
tools if needed to judge a comment.

End your turn with ONLY a JSON array of the integer indices to KEEP (0-based),
e.g. [0, 2, 3]. To keep none, output []. Do not output anything else.
"""

_FILTER_USER_TEMPLATE = """\
Draft review comments (index: comment):
{comment_list}

Output the JSON array of indices to keep.
"""


def _render_comments(findings: List[Finding]) -> str:
    lines: List[str] = []
    for i, f in enumerate(findings):
        payload = {
            "severity": f.severity,
            "message": f.message,
            "file": f.file,
            "existing_code": f.existing_code,
        }
        lines.append(f"{i}: {json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines)


def _parse_keep_indices(output: str, n: int) -> List[int] | None:
    """Parse the agent's kept-index array; return valid indices or ``None``."""
    arr = extract_json_array(output)
    if arr is None:
        return None
    indices: List[int] = []
    for v in arr:
        try:
            idx = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n and idx not in indices:
            indices.append(idx)
    return indices


async def filter_findings(
    findings: List[Finding],
    repo_dir: str = "",
    parent_session_id: str = "",
) -> List[Finding]:
    """Self-critique *findings*, returning the kept subset (best-effort).

    Degrades to returning *findings* unchanged on any failure or unparseable
    response. Skips the agent call only for an empty list — a lone finding still
    goes through the gate, since a single comment can itself be the low-value one
    that should be dropped.
    """
    if not findings:
        return list(findings)

    role = build_child_role(
        name="ReviewFilter",
        system_prompt=_FILTER_SYSTEM_PROMPT,
        repo_dir=repo_dir,
        parent_session_id=parent_session_id,
        tools=["Read", "Search"],
    )
    prompt = _FILTER_USER_TEMPLATE.format(comment_list=_render_comments(findings))
    output = await run_child_for_text(role, prompt, label="review_filter")
    if output is None:
        return list(findings)

    keep = _parse_keep_indices(output, len(findings))
    if keep is None:
        logger.warning("code_review: could not parse review_filter output; keeping all findings")
        return list(findings)
    return [findings[i] for i in keep]


__all__ = ["filter_findings"]
