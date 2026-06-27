"""Plan gate for the code-review pipeline.

Sits between ``parse_filter`` and ``review_batch``. Looks at the *whole* list of
reviewable files at once and produces a lightweight review plan:

* a one-paragraph **strategy** note (what this changeset seems to be about, what
  to watch for) — surfaced in the final report header, and
* a **prioritized ordering** of the files, so the highest-risk files are
  reviewed in the earliest batches (useful when ``batch_size`` < file count and
  the user reads results as waves complete).

This is a single child-Role call with read-only tools. It is strictly
best-effort: any failure (or an unparseable response) degrades to the identity
plan — original order, empty strategy — so the pipeline never stalls on it.
Unlike OCR's heavier plan stage, the MVP plan does **not** drop files; it only
reorders and annotates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from metagpt.common.logs import logger

from ._agent import build_child_role, run_child_for_text
from .parser import FileDiff

_PLAN_SYSTEM_PROMPT = """\
You are the lead of a code review. You are given the list of files changed in a
diff (paths + rough change size). Before the per-file review starts, produce a
short plan.

Investigate lightly with your tools if a path is unfamiliar (Glob/Grep/Read) —
but do NOT review the code in depth here; that happens per-file next.

End your turn with ONLY a JSON object (no prose around it):

{
  "strategy": "<1-3 sentences: what this change appears to be, and the top risks to watch for during review>",
  "order": ["<path>", "<path>", ...]
}

Rules:
- "order" must be a permutation of the given paths (highest review priority
  first — e.g. security-sensitive, core logic, large changes before trivial
  ones). Include every path exactly once; unknown paths are ignored.
- Keep "strategy" concise and concrete.
"""

_PLAN_USER_TEMPLATE = """\
Files changed in this diff:
{file_list}

Produce the review plan (strategy + prioritized order) as the JSON object
described in your instructions.
"""


@dataclass
class ReviewPlan:
    """Output of the plan gate."""

    strategy: str = ""
    ordered: List[FileDiff] = field(default_factory=list)


def _render_file_list(files: List[FileDiff]) -> str:
    lines: List[str] = []
    for f in files:
        added = f.added_count()
        tag = "new" if f.is_new else ("rename" if f.is_rename else "modified")
        lines.append(f"- {f.path}  ({tag}, +{added} lines)")
    return "\n".join(lines)


def _reorder(files: List[FileDiff], order: List[str]) -> List[FileDiff]:
    """Reorder *files* to follow *order* (by path); unlisted files keep tail."""
    by_path = {f.path: f for f in files}
    seen: set[str] = set()
    out: List[FileDiff] = []
    for p in order:
        f = by_path.get(p)
        if f is not None and p not in seen:
            out.append(f)
            seen.add(p)
    # Append any files the plan omitted, preserving original order.
    for f in files:
        if f.path not in seen:
            out.append(f)
    return out


def _identity_plan(files: List[FileDiff]) -> ReviewPlan:
    return ReviewPlan(strategy="", ordered=list(files))


async def make_plan(
    files: List[FileDiff],
    repo_dir: str = "",
    parent_session_id: str = "",
) -> ReviewPlan:
    """Produce a :class:`ReviewPlan` for *files* (best-effort).

    Degrades to the identity plan (original order, empty strategy) on any
    failure or unparseable response. Skips the agent call entirely for a
    trivially small changeset (0 or 1 files — nothing to prioritize).
    """
    if len(files) <= 1:
        return _identity_plan(files)

    role = build_child_role(
        name="ReviewPlanner",
        system_prompt=_PLAN_SYSTEM_PROMPT,
        repo_dir=repo_dir,
        parent_session_id=parent_session_id,
        tools=["Read", "Grep", "Glob"],
    )
    prompt = _PLAN_USER_TEMPLATE.format(file_list=_render_file_list(files))
    output = await run_child_for_text(role, prompt, label="plan")
    if not output:
        return _identity_plan(files)

    obj = _extract_plan_object(output)
    if obj is None:
        logger.warning("code_review: could not parse plan JSON; using identity plan")
        return _identity_plan(files)

    strategy = str(obj.get("strategy", "")).strip()
    order = obj.get("order")
    ordered = _reorder(files, order) if isinstance(order, list) else list(files)
    return ReviewPlan(strategy=strategy, ordered=ordered)


def _extract_plan_object(text: str) -> dict | None:
    """Extract the plan JSON object from the agent's output.

    Reuses the array extractor's fence handling by trying the same candidates,
    but accepts an object (``{...}``) rather than an array.
    """
    import json
    import re

    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates: List[str] = []
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first : last + 1])
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


__all__ = ["ReviewPlan", "make_plan"]
