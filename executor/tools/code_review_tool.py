"""CodeReview — git-diff code review pipeline tool.

Replicates Alibaba's open-code-review (OCR) skeleton on top of the bggraph
engine: fetch a git diff, deterministically filter to reviewable source files,
review each file with its own agent tool-loop (concurrently, in batches), locate
each comment back onto concrete line numbers, and aggregate a report.

Like MediaPipeline, this is opt-in: registered for discovery but not in the
default RoleSchema.tools. Add ``"CodeReview"`` to a role's tools to enable it.
"""
from __future__ import annotations

from typing import Optional

from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tasks.types import BgTaskResult
from metagpt.executor.tool_registry import register_tool
from metagpt.executor.tools.code_review.graph import build_code_review_graph


@register_tool
class CodeReview(BaseTool):
    name = "CodeReview"
    aliases = ["code_review", "review_diff"]
    description = (
        "Review a git diff with one AI reviewer per changed file. Use this when "
        "the user asks to review code, review a commit/branch/PR, or check changes "
        "for bugs.\n\n"
        "Behavior:\n"
        "  - Fetches the diff (working tree by default, or a commit / ref range).\n"
        "  - Filters to reviewable source files (skips binary, deleted, tests, "
        "vendored/generated files).\n"
        "  - Reviews each file concurrently (one agent tool-loop per file, "
        "batch_size files per wave) — the reviewer reads surrounding code to "
        "ground its findings.\n"
        "  - Locates each comment onto concrete line numbers, self-critiques to "
        "drop false positives, and aggregates a report.\n\n"
        "The report is already reviewed and filtered — report it directly, do not "
        "re-review the same diff.\n\n"
        "Parameters:\n"
        "  - repo_dir: (REQUIRED) the git repository directory to review.\n"
        "  - from_ref / to_ref: review the diff of a ref range (from..to).\n"
        "  - commit: review a single commit (git show). Takes precedence over refs.\n"
        "  - batch_size: files reviewed per concurrent wave (default 8).\n"
        "  - fmt: 'text' (default, human-readable) or 'json'.\n"
        "If neither commit nor refs are given, the working-tree diff (git diff "
        "HEAD) is reviewed."
    )

    def __init__(self):
        super().__init__()

        self._graph = build_code_review_graph()
        self._executor = self._graph.compile()

    async def call(
        self,
        *,
        repo_dir: str,
        from_ref: Optional[str] = None,
        to_ref: Optional[str] = None,
        commit: Optional[str] = None,
        batch_size: int = 8,
        fmt: str = "text",
    ) -> BgTaskResult:
        """Run the code-review pipeline as a background task.

        Args:
            repo_dir: The git repository directory to review.
            from_ref: Base ref for a range diff (e.g. a branch or SHA).
            to_ref: Target ref for a range diff. Requires from_ref.
            commit: A single commit to review via ``git show`` (takes precedence
                over from_ref/to_ref).
            batch_size: Number of files reviewed concurrently per wave (the
                concurrency cap, aligned with OCR's --concurrency). Default 8.
            fmt: Output format — "text" (human-readable, grouped by file) or
                "json" (machine-readable array of findings).
        """
        return await self._executor(
            repo_dir=repo_dir,
            from_ref=from_ref,
            to_ref=to_ref,
            commit=commit,
            batch_size=batch_size,
            fmt=fmt,
            parent_session_id=self.session_id,
            raw_diff="",
            remaining=[],
            strategy="",
            findings=[],
            kept_findings=None,
            report="",
        )
