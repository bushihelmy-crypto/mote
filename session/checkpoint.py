"""Whole-tree checkpoints — user-facing turn-boundary rollback (the ``/rewind`` layer).

The user-facing sibling of the file-snapshot layer (:mod:`mote.session.snapshot`
/ :mod:`mote.session.history`). Where that layer captures a *single* file's
before-image just before a tool overwrites it, this captures the **entire
working tree** once per user turn, so a user can roll the whole tree back to any
prior turn — undoing Bash-created files, deletions, and multi-file edits at once,
which the per-file layer cannot.

Two pieces:

* :class:`CheckpointStore` — git plumbing over the session's **dedicated bare**
  ``{session}/git`` object db (the same repo :class:`~mote.session.snapshot.GitBlobStore`
  uses), *never* the user's own repo. Because that repo owns its own index, no
  scratch-index / ``GIT_INDEX_FILE`` juggling is needed: ``capture`` stages the
  work-tree into the dedicated index, writes a tree + commit, and pins it under a
  ``refs/mote/checkpoints/<n>`` ref so a user's ``git gc --prune`` can never reap
  it. ``restore`` resets the work-tree to a checkpoint commit and removes
  agent-created untracked files.
* :func:`list_checkpoints` — the read side: a forward scan of the same
  ``rollout.jsonl`` (no second index), yielding :class:`CheckpointEntry`.

All git operations shell out and are best-effort — a capture failure returns
``None`` and never breaks the turn.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from mote.common.logs import log_class, logger
from mote.session.events import CheckpointEvent, parse_event
from mote.session.log import SessionLog

#: Ref namespace pinning each checkpoint commit reachable in the dedicated repo,
#: so the user's own ``git gc --prune`` never reaps our snapshots.
CHECKPOINT_REF_PREFIX = "refs/mote/checkpoints"

#: Identity stamped on checkpoint commits — deliberately synthetic so we never
#: read (or depend on) the user's global git ``user.name``/``user.email`` config.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "mote",
    "GIT_AUTHOR_EMAIL": "mote@localhost",
    "GIT_COMMITTER_NAME": "mote",
    "GIT_COMMITTER_EMAIL": "mote@localhost",
}


@dataclass
class CheckpointEntry:
    """One captured whole-tree checkpoint (a flattened :class:`CheckpointEvent`).

    ``index`` is this checkpoint's position within the session's checkpoint list
    (0-based, chronological — the value the user types to ``/rewind``); ``ts`` is
    the event's ISO timestamp. ``after_commit`` is the twin turn-end snapshot (the
    tree the agent left behind), folded in from the matching after-event by
    :func:`list_checkpoints`; empty when the turn had no recorded end (still
    in-flight, or a pre-``after_commit`` rollout). Diffing it against the live tree
    at rewind time surfaces files an external process changed after the agent
    finished.
    """

    commit: str
    prompt_index: int
    prompt_preview: str
    working_dir: str
    ts: str
    index: int
    after_commit: str = ""


@log_class(level="DEBUG")
class CheckpointStore:
    """Whole-tree checkpoint git plumbing over the dedicated ``{session}/git`` repo.

    ``git_dir`` is the bare object db (``{session}/git``); ``work_dir`` is the
    working tree the checkpoint is taken against (the role's live working dir).
    Both capture and restore run every ``git`` invocation with an explicit
    ``--git-dir``/``--work-tree`` pair and ``cwd=work_dir`` so pathspecs resolve
    against the tree and nothing touches the user's own ``.git``.
    """

    def __init__(self, git_dir: Path, work_dir: Path):
        self._git_dir = Path(git_dir)
        self._work_dir = Path(work_dir)

    def _ensure_repo(self) -> None:
        if not (self._git_dir / "HEAD").exists():
            self._git_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "init", "--bare", "--quiet", str(self._git_dir)],
                check=True,
                capture_output=True,
            )

    def _run(self, args: List[str], *, extra_env: Optional[dict] = None) -> subprocess.CompletedProcess:
        env = {**os.environ, **extra_env} if extra_env else None
        return subprocess.run(
            ["git", "--git-dir", str(self._git_dir), "--work-tree", str(self._work_dir), *args],
            cwd=str(self._work_dir),
            capture_output=True,
            env=env,
        )

    def capture(
        self,
        *,
        parent: Optional[str] = None,
        ref: Optional[str] = None,
        message: str = "checkpoint",
    ) -> Optional[str]:
        """Snapshot the whole work-tree as a commit; return its sha (``None`` on failure).

        Stages the entire work-tree into the dedicated index (``add -A`` — honors
        the work-tree's ``.gitignore``), writes it to a tree, and wraps that tree
        in a commit (parented on ``parent`` when given, so checkpoints chain).
        The commit is pinned under ``ref`` (default
        ``{CHECKPOINT_REF_PREFIX}/<sha>``) to keep it reachable. Best-effort: any
        git failure logs + returns ``None`` (a lost checkpoint must not break the
        turn).
        """
        try:
            if not self._work_dir.is_dir():
                return None
            self._ensure_repo()
            add = self._run(["add", "-A"])
            if add.returncode != 0:
                logger.debug(f"checkpoint: add -A failed: {add.stderr.decode('utf-8', 'replace').strip()}")
                return None
            wt = self._run(["write-tree"])
            if wt.returncode != 0:
                logger.debug(f"checkpoint: write-tree failed: {wt.stderr.decode('utf-8', 'replace').strip()}")
                return None
            tree = wt.stdout.decode("ascii").strip()
            commit_args = ["commit-tree", tree, "-m", message]
            if parent:
                commit_args += ["-p", parent]
            ct = self._run(commit_args, extra_env=_COMMIT_ENV)
            if ct.returncode != 0:
                logger.debug(f"checkpoint: commit-tree failed: {ct.stderr.decode('utf-8', 'replace').strip()}")
                return None
            commit = ct.stdout.decode("ascii").strip()
            self._run(["update-ref", ref or f"{CHECKPOINT_REF_PREFIX}/{commit}", commit])
            return commit
        except Exception as exc:  # noqa: BLE001 — capture must never break the turn
            logger.warning(f"checkpoint: capture failed: {exc}")
            return None

    def restore(self, commit: str) -> bool:
        """Reset the work-tree to checkpoint ``commit``; return success.

        ``read-tree --reset -u`` resets the dedicated index + working tree to the
        commit's tree (re-creating deleted tracked files, reverting edits), then a
        **scoped** ``git clean -fd .`` removes files the agent created after the
        checkpoint (untracked). Deliberately **no ``-x``**: gitignored build
        artifacts survive. Scoped to the work-tree root (``cwd=work_dir`` + ``.``
        pathspec) so nothing outside the tree is ever touched. Best-effort:
        returns ``False`` on any git failure.
        """
        try:
            if not (self._git_dir / "HEAD").exists():
                return False
            rt = self._run(["read-tree", "--reset", "-u", commit])
            if rt.returncode != 0:
                logger.debug(f"checkpoint: read-tree failed: {rt.stderr.decode('utf-8', 'replace').strip()}")
                return False
            # Remove agent-created untracked files (NO -x: gitignored artifacts
            # survive), scoped to the work-tree root.
            self._run(["clean", "-fd", "."])
            return True
        except Exception as exc:  # noqa: BLE001 — restore is best-effort
            logger.warning(f"checkpoint: restore of '{commit}' failed: {exc}")
            return False

    def diff_tree(self, a: str, b: str) -> List[str]:
        """Paths that differ between two checkpoint commits' trees (``a`` → ``b``).

        A plain ``git diff --name-only <a> <b>`` in the dedicated repo. Used at
        rewind to compare the agent's turn-end snapshot (``after_commit``) against
        the live tree (captured as the "before rewind" safety commit) — the names
        it returns are the files an external process (or the user) changed *after*
        the agent finished, i.e. edits the rewind is about to overwrite. Purely
        informational (the rewind still proceeds). Best-effort: returns ``[]`` when
        either commit is empty/missing or git fails.
        """
        if not (a and b) or not (self._git_dir / "HEAD").exists():
            return []
        try:
            dt = self._run(["diff", "--name-only", a, b])
            if dt.returncode != 0:
                logger.debug(f"checkpoint: diff-tree failed: {dt.stderr.decode('utf-8', 'replace').strip()}")
                return []
            return [line for line in dt.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        except Exception as exc:  # noqa: BLE001 — diff is best-effort/informational
            logger.warning(f"checkpoint: diff of '{a}'..'{b}' failed: {exc}")
            return []


def list_checkpoints(log: SessionLog) -> List[CheckpointEntry]:
    """Every ``checkpoint`` event in ``log``, chronological, with a 0-based ``index``.

    A forward scan of the same ``rollout.jsonl`` (mirrors
    :func:`mote.session.history.file_history`) — no second index. Each entry's
    ``index`` is its position in this list (the value the user types to
    ``/rewind``).

    Each user turn writes up to two ``CheckpointEvent``s: a *before* event (the
    tree entering the turn — carries ``commit``) and, at turn end, an *after* event
    (the tree the agent left — carries only ``after_commit``, ``commit=""``, same
    ``prompt_index``). Only before-events become entries; an after-event is folded
    onto the entry sharing its ``prompt_index`` (setting ``after_commit``), so a
    checkpoint knows both the tree it opened with and the tree the agent produced.
    """
    out: List[CheckpointEntry] = []
    by_prompt: dict = {}
    for record in log.iter_raw():
        event = parse_event(record)
        if not isinstance(event, CheckpointEvent):
            continue
        if event.commit:
            entry = CheckpointEntry(
                commit=event.commit,
                prompt_index=event.prompt_index,
                prompt_preview=event.prompt_preview,
                working_dir=event.working_dir,
                ts=record.get("ts", ""),
                index=len(out),
                after_commit=event.after_commit,
            )
            out.append(entry)
            by_prompt[event.prompt_index] = entry
        elif event.after_commit:
            entry = by_prompt.get(event.prompt_index)
            if entry is not None:
                entry.after_commit = event.after_commit
    return out


__all__ = ["CheckpointStore", "CheckpointEntry", "list_checkpoints", "CHECKPOINT_REF_PREFIX"]
