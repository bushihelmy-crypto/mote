"""Strict Git-backed whole-worktree snapshots used by checkpoint and rewind flows."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from mote.contracts.file.errors import RewindFailedError
from mote.runtime.telemetry.logging import log_class

CHECKPOINT_REF_PREFIX = "refs/mote/checkpoints"
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "mote",
    "GIT_AUTHOR_EMAIL": "mote@localhost",
    "GIT_COMMITTER_NAME": "mote",
    "GIT_COMMITTER_EMAIL": "mote@localhost",
}


@log_class(level="DEBUG")
class WorktreeCheckpointStore:
    """Owns durable snapshots in a session-private Git object database.

    Methods are strict: invalid worktrees, Git failures, and failed ref updates
    raise :class:`RewindFailedError`. Observation callers may explicitly choose
    to drop that error; managed rewind never does.
    """

    def __init__(self, git_dir: Path, work_dir: Path) -> None:
        self.git_dir = Path(git_dir)
        self.work_dir = Path(work_dir)

    def capture(
        self,
        *,
        parent: Optional[str] = None,
        ref: Optional[str] = None,
        message: str = "checkpoint",
    ) -> str:
        if not self.work_dir.is_dir():
            raise RewindFailedError(
                f"checkpoint worktree does not exist: {self.work_dir}",
                working_dir=str(self.work_dir),
            )
        self._ensure_repo()
        self._run_checked(("add", "-A"), stage="add")
        tree = self._run_checked(("write-tree",), stage="write_tree").stdout.decode("ascii").strip()
        args = ["commit-tree", tree, "-m", message]
        if parent:
            args.extend(("-p", parent))
        commit = (
            self._run_checked(
                args,
                stage="commit_tree",
                extra_env=_COMMIT_ENV,
            )
            .stdout.decode("ascii")
            .strip()
        )
        self._run_checked(
            ("update-ref", ref or f"{CHECKPOINT_REF_PREFIX}/{commit}", commit),
            stage="update_ref",
        )
        return commit

    def restore(self, commit: str) -> None:
        if not (self.git_dir / "HEAD").exists():
            raise RewindFailedError(
                "checkpoint repository does not exist",
                git_dir=str(self.git_dir),
            )
        self._run_checked(
            ("read-tree", "--reset", "-u", commit),
            stage="read_tree",
        )
        self._run_checked(("clean", "-fd", "."), stage="clean")

    def diff_tree(self, before: str, after: str) -> list[str]:
        if not before or not after:
            return []
        result = self._run_checked(
            ("diff", "--name-only", "-z", before, after),
            stage="diff_tree",
        )
        return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]

    def tree_id(self, commit: str) -> str:
        return (
            self._run_checked(
                ("rev-parse", f"{commit}^{{tree}}"),
                stage="resolve_tree",
            )
            .stdout.decode("ascii")
            .strip()
        )

    def same_tree(self, left: str, right: str) -> bool:
        return self.tree_id(left) == self.tree_id(right)

    def _ensure_repo(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["git", "init", "--bare", "--quiet", str(self.git_dir)],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RewindFailedError(
                "cannot initialize checkpoint repository",
                git_dir=str(self.git_dir),
                stage="init",
                cause=exc,
            ) from exc

    def _run_checked(
        self,
        args: Sequence[str],
        *,
        stage: str,
        extra_env: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        env = {**os.environ, **extra_env} if extra_env else None
        try:
            result = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(self.git_dir),
                    "--work-tree",
                    str(self.work_dir),
                    *args,
                ],
                cwd=str(self.work_dir),
                capture_output=True,
                env=env,
            )
        except OSError as exc:
            raise RewindFailedError(
                f"checkpoint Git command failed during {stage}",
                stage=stage,
                cause=exc,
            ) from exc
        if result.returncode != 0:
            raise RewindFailedError(
                f"checkpoint Git command failed during {stage}",
                stage=stage,
                returncode=result.returncode,
                stderr=result.stderr.decode("utf-8", errors="surrogateescape").strip(),
            )
        return result


__all__ = ["CHECKPOINT_REF_PREFIX", "WorktreeCheckpointStore"]
