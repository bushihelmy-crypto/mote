"""FileRehydrator — re-read the recent working set after a compaction.

When a summarize compaction discards the head, the *prose* summary keeps a
recollection of what the older turns did, but the actual file bytes the model
was working with are gone. This rehydrator re-reads the files the session most
recently touched and re-injects their *current* on-disk bytes right after the
summary, so the model resumes with a fresh view of its working set rather than
relying on the summary's paraphrase.

It is the eager sibling of the lazy advisory in
``CompactionNoticeContextSource`` ("re-read the relevant files if you need
specifics"): the advisory always fires; this rehydrator only fires when a file
trajectory exists and stays inside a token budget, so a session that touched no
files (or is over budget) simply falls back to the advisory.

Design mirrors ``ResourceRegistry.project`` (the sticky re-projection seam it
plugs in beside): most-recent-first, per-file head-truncation, whole-file drop
once the running total would exceed the budget. It is injected into
``SummarizeReducer`` as a callable returning ``list[Message]``; a ``None``
provider (standalone / test use) re-projects nothing.

Dedup against the preserved tail:
the summarize reducer keeps a verbatim tail, and any file the tail *already*
shows through a ``Read`` tool call is still fully visible to the model. Re-reading
it here would just duplicate those bytes (measured at up to ~25K tokens
per compaction). So ``project`` takes the preserved tail messages, extracts the
paths their ``Read`` calls already surface, and skips re-reading those files —
the freed budget goes to files the summary only paraphrased.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Iterable, Optional

from mote.contracts.conversation import Message, UserMessage
from mote.contracts.conversation.constants import (
    POST_COMPACT_REHYDRATE_MAX_FILES,
    POST_COMPACT_REHYDRATE_MAX_TOKENS_PER_FILE,
    POST_COMPACT_REHYDRATE_TOKEN_BUDGET,
)
from mote.contracts.conversation.fields import TOOL_CALLS
from mote.runtime.context.token_budget import count_tokens, truncate_to_tokens
from mote.runtime.context.tokenizer import DEFAULT_TEXT_TOKENIZER
from mote.runtime.telemetry.logging import logger

# The Read tool's file-path argument name (``executor/tools/read.py``); the tail
# already surfaces the bytes of any file read through it, so dedup keys off this.
_READ_TOOL_NAME = "Read"
_READ_PATH_ARG = "file_path"

# A zero-arg callable returning the absolute paths the session has read, most
# useful ones last (the observed-snapshot trajectory). The rehydrator reverses
# it to most-recent-first. Matches ``Role._touched_files``.
TouchedFilesProvider = Callable[[], list[str]]


class FileRehydrator:
    """Re-reads the recent working-set files into post-compaction messages."""

    def __init__(
        self,
        touched_files: Optional[TouchedFilesProvider] = None,
        *,
        max_files: int = POST_COMPACT_REHYDRATE_MAX_FILES,
        max_tokens_per_file: int = POST_COMPACT_REHYDRATE_MAX_TOKENS_PER_FILE,
        token_budget: int = POST_COMPACT_REHYDRATE_TOKEN_BUDGET,
    ) -> None:
        self._touched_files = touched_files
        self._max_files = max_files
        self._max_tokens_per_file = max_tokens_per_file
        self._token_budget = token_budget

    def project(self, preserved: Optional[list[Message]] = None) -> list[Message]:
        """Build the file-snapshot messages to re-insert after the summary.

        Most-recent-first, capped at ``max_files``; each file's current bytes are
        head-truncated to ``max_tokens_per_file`` and whole files are dropped once
        the running total would exceed ``token_budget``. Best-effort: a file that
        vanished or is unreadable is silently skipped (the lazy advisory covers
        it). Returns ``[]`` when no provider is wired or nothing was touched.

        ``preserved`` is the verbatim tail the summarize reducer keeps. Any file a
        ``Read`` call in that tail already surfaces is skipped — re-reading it here
        would just duplicate bytes the model can already see (path dedup).
        ``None`` => no dedup (standalone use).
        """
        if self._touched_files is None:
            return []
        try:
            paths = list(self._touched_files() or [])
        except Exception as e:  # noqa: BLE001 — rehydration must not break compaction
            logger.warning(f"rehydrate: touched-files provider failed: {e}")
            return []

        already_shown = self._paths_in_preserved_tail(preserved)

        # Most-recent-first (the trajectory is oldest→newest insertion order).
        paths = list(reversed(paths))
        out: list[Message] = []
        used = 0
        for path in paths:
            if path in already_shown:
                # The preserved tail already shows this file — don't duplicate it.
                continue
            if len(out) >= self._max_files:
                break
            body = self._read(path)
            if body is None:
                continue
            if count_tokens(body, tokenizer=DEFAULT_TEXT_TOKENIZER) > self._max_tokens_per_file:
                body = truncate_to_tokens(
                    body,
                    self._max_tokens_per_file,
                    tokenizer=DEFAULT_TEXT_TOKENIZER,
                )
            cost = count_tokens(body, tokenizer=DEFAULT_TEXT_TOKENIZER)
            if used + cost > self._token_budget:
                # Over budget: stop (remaining files are older / lower priority).
                break
            used += cost
            out.append(_snapshot_message(path, body))
        return out

    @classmethod
    def _paths_in_preserved_tail(cls, preserved: Optional[Iterable[Message]]) -> set[str]:
        """Absolute paths the preserved tail already surfaces via ``Read`` calls.

        Walks each message's ``TOOL_CALLS`` metadata (``[{id, name, args}]``) and
        collects the ``file_path`` argument of every ``Read`` invocation, normalized
        the same way File Operations stores them (abspath + expanduser) so the
        touched-files trajectory and these paths compare on equal footing. ``args``
        may be a dict (native) or a JSON string (recovery wire form) — both handled.
        Best-effort: a malformed call is skipped, never raised.
        """
        if not preserved:
            return set()
        shown: set[str] = set()
        for m in preserved:
            calls = getattr(m, "metadata", None)
            calls = calls.get(TOOL_CALLS) if isinstance(calls, dict) else None
            if not calls:
                continue
            for c in calls:
                if not isinstance(c, dict) or c.get("name") != _READ_TOOL_NAME:
                    continue
                path = cls._read_path_arg(c.get("args"))
                if path:
                    shown.add(os.path.abspath(os.path.expanduser(path)))
        return shown

    @staticmethod
    def _read_path_arg(args) -> Optional[str]:
        """Pull the ``file_path`` out of a Read call's args (dict or JSON string)."""
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                return None
        if isinstance(args, dict):
            val = args.get(_READ_PATH_ARG)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    @staticmethod
    def _read(path: str) -> Optional[str]:
        """Return the file's current text, or None if it vanished / is unreadable."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, ValueError) as e:  # missing, dir, decode, ...
            logger.debug(f"rehydrate: skip '{path}': {e}")
            return None


def _snapshot_message(path: str, body: str) -> UserMessage:
    name = os.path.basename(path) or path
    header = f"# File snapshot: {name} (re-read after compaction; current on-disk contents)\n" f"Path: {path}"
    return UserMessage(content=f"{header}\n\n{body}")


__all__ = ["FileRehydrator", "TouchedFilesProvider"]
