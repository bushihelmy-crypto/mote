"""Node functions for the code-review pipeline graph.

Four deterministic-skeleton nodes plus a router, following the bggraph
``async def node(state) -> Stage`` contract:

* ``load_diff``    — fetch the git diff into ``raw_diff``.
* ``parse_filter`` — parse + filter into ``remaining`` (list[FileDiff]).
* ``review_batch`` — pop ``batch_size`` files, review them concurrently with
  ``asyncio.gather`` (one Role tool-loop per file), append to ``findings``,
  write back the tail to ``remaining``.
* ``aggregate``    — format the accumulated findings into ``report``.

The ring is the conditional self-edge on ``review_batch``: while ``remaining``
is non-empty the router sends it back to itself; once empty it routes to
``aggregate``. This keeps the topology static and N-independent.
"""
from __future__ import annotations

import asyncio
from typing import List

from mote.common.logs import logger
from mote.executor.tasks.bggraph import Stage

from .bundle import attach_related
from .filter import should_review
from .format import Finding, format_findings
from .gitdiff import get_diff
from .parser import parse_unified_diff
from .plan import make_plan
from .review_filter import filter_findings
from .review_unit import review_one_file
from .state import ReviewState


def _result(update: dict):
    """Return a submit coroutine that yields a fixed update dict."""

    async def _inner():
        return update

    return _inner()


# ---------------------------------------------------------------------------
# load_diff
# ---------------------------------------------------------------------------


async def load_diff_node(state: ReviewState) -> Stage:
    """Fetch the git diff for the requested scope into ``raw_diff``.

    Params:
        repo_dir: $input.repo_dir — git 仓库工作目录
        from_ref: $input.from_ref — 范围 diff 的基准 ref
        to_ref: $input.to_ref — 范围 diff 的目标 ref
        commit: $input.commit — 单个提交（git show）
    """

    async def submit():
        diff = await get_diff(
            state.repo_dir,
            from_ref=state.from_ref,
            to_ref=state.to_ref,
            commit=state.commit,
        )
        return {"raw_diff": diff}

    return Stage(submit=submit())


# ---------------------------------------------------------------------------
# parse_filter
# ---------------------------------------------------------------------------


async def parse_filter_node(state: ReviewState) -> Stage:
    """Parse the unified diff and filter to reviewable files → ``remaining``.

    Params:
        raw_diff: raw_diff — load_diff 节点产出的原始 diff 文本
    """

    async def submit():
        files = parse_unified_diff(state.raw_diff or "")
        # Attach related-file hints from the *whole* changeset (tests/excluded
        # files make good context) before filtering down to reviewable files.
        attach_related(files)
        remaining = [f for f in files if should_review(f)]
        return {"remaining": remaining}

    return Stage(submit=submit())


# ---------------------------------------------------------------------------
# plan (gate)
# ---------------------------------------------------------------------------


async def plan_node(state: ReviewState) -> Stage:
    """Produce a review plan: prioritized file order + a strategy note.

    Looks at the whole reviewable file list and reorders ``remaining`` so the
    highest-risk files land in the earliest review waves. Best-effort: degrades
    to the original order (empty strategy) on failure.

    Params:
        remaining: remaining — parse_filter 产出的待审查文件列表
        repo_dir: $input.repo_dir — git 仓库工作目录
        parent_session_id: $input.parent_session_id — 父会话 id（血缘）
    """

    async def submit():
        files = list(state.remaining or [])
        if not files:
            return {"strategy": ""}
        plan = await make_plan(
            files,
            repo_dir=state.repo_dir,
            parent_session_id=state.parent_session_id,
        )
        return {"remaining": plan.ordered, "strategy": plan.strategy}

    return Stage(submit=submit())


# ---------------------------------------------------------------------------
# review_batch (ring + batch)
# ---------------------------------------------------------------------------


async def review_batch_node(state: ReviewState) -> Stage:
    """Review up to ``batch_size`` files concurrently; append findings.

    Pops the head ``batch_size`` slice of ``remaining``, runs one child Role per
    file via ``asyncio.gather`` (failures isolated per file), and writes the
    tail back to ``remaining`` so the router can decide whether to loop.

    Params:
        remaining: remaining — 待审查文件列表（FileDiff）
        repo_dir: $input.repo_dir — git 仓库工作目录
        batch_size: $input.batch_size — 每波并发上限
        parent_session_id: $input.parent_session_id — 父会话 id（血缘）
    """

    remaining: List = list(state.remaining or [])
    k = max(1, int(state.batch_size or 8))
    batch = remaining[:k]
    tail = remaining[k:]

    async def submit():
        if not batch:
            return {"findings": [], "remaining": []}

        async def _safe_review(file_diff) -> List[Finding]:
            try:
                return await review_one_file(
                    file_diff,
                    state.repo_dir,
                    parent_session_id=state.parent_session_id,
                )
            except Exception as e:  # noqa: BLE001 — isolate one file's failure
                logger.warning(f"code_review: batch review failed for " f"{getattr(file_diff, 'path', '?')}: {e}")
                return []

        results = await asyncio.gather(*(_safe_review(f) for f in batch))
        findings: List[Finding] = [f for sub in results for f in sub]
        return {"findings": findings, "remaining": tail}

    return Stage(submit=submit())


def _route_after_batch(state: ReviewState) -> str:
    """Loop back to review_batch while files remain, else go to review_filter."""
    return "loop" if state.remaining else "done"


# ---------------------------------------------------------------------------
# review_filter (self-critique)
# ---------------------------------------------------------------------------


async def review_filter_node(state: ReviewState) -> Stage:
    """Self-critique the accumulated findings, dropping low-value ones.

    Runs once after the ring drains. Writes the kept subset to ``kept_findings``
    (last-value); ``aggregate`` formats that when present. Best-effort: degrades
    to keeping all findings on failure.

    Params:
        findings: findings — review_batch 累积的 Finding 列表
        repo_dir: $input.repo_dir — git 仓库工作目录
        parent_session_id: $input.parent_session_id — 父会话 id（血缘）
    """

    async def submit():
        findings = list(state.findings or [])
        kept = await filter_findings(
            findings,
            repo_dir=state.repo_dir,
            parent_session_id=state.parent_session_id,
        )
        return {"kept_findings": kept}

    return Stage(submit=submit())


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------


async def aggregate_node(state: ReviewState) -> Stage:
    """Format the kept findings into the final ``report``.

    Prefers ``kept_findings`` (the review_filter output) when present, else
    falls back to the raw accumulated ``findings``. Prepends the plan
    ``strategy`` note (if any) to a text report.

    Params:
        findings: findings — review_batch 累积的 Finding 列表
        fmt: $input.fmt — 输出格式 text|json
    """

    async def submit():
        kept = state.kept_findings
        findings = list(kept if kept is not None else (state.findings or []))
        # Stable ordering: by file path, then by start line (None last).
        findings.sort(key=lambda f: (f.file, f.start_line if f.start_line is not None else 1 << 30))
        fmt = state.fmt or "text"
        report = format_findings(findings, fmt=fmt)
        strategy = (state.strategy or "").strip()
        if strategy and fmt != "json":
            report = f"Review strategy: {strategy}\n\n{report}"
        return {"report": report}

    return Stage(submit=submit())
