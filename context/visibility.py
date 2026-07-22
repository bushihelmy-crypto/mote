"""ContextVisibility — the single authority on "is this result still visible?".

A *reconstructable* tool result (Read, Search, ...) may have its body folded
away in place (:mod:`mote.context.compaction.reducers.fold` replaces the content
with :data:`TOOL_RESULT_CLEARED_MESSAGE`) or pair-erased outright
(:mod:`~mote.context.compaction.reducers.erase` removes the message). Both are
legitimate — the information is re-derivable by re-running the tool. But a tool
that *deduplicates* against its own past output (Read's "you already read this,
unchanged" short-circuit) is implicitly betting that its earlier result is still
in front of the model. Once that result has been folded or erased, the bet is
wrong: pointing the model back at a cleared body loses the content entirely.

This service removes the guesswork. The stored history is the single source of
truth for what the model can currently see; this class reads that truth and
answers one question — *is the most recent result derived from resource X still
present (real content, not a cleared placeholder)?* — so a deduplicating tool
can ask before short-circuiting, instead of maintaining its own drifting mirror
of the context.

Design:

- **One question, resource-keyed.** Tools think in resources (Read keys its
  dedup cache by file path), not in opaque ``tool_call_id``. So the surface is
  :meth:`is_resource_visible(path)`, and the provenance link is carried on the
  result message's metadata (``TOOL_RESULT_RESOURCE_PATH``, stamped by the
  channel from ``ToolResult.resource_path``). A tool never has to learn the id
  of its own past call.

- **Live, not snapshot.** The service holds a zero-arg ``messages_provider``
  returning the *current* stored history (normally ``lambda:
  role.state.context.messages``). Every query reflects the latest folds/erases —
  there is no cache to invalidate. Fold mutates those Message objects in place
  and erase rewrites the list; either way the next query sees the new truth.

- **Read-only, no back-reference.** It only reads messages. The ContextManager
  owns/ mutates them; this is a separate collaborator so the "can the model see
  it?" concern is a first-class object reusable by any reconstructable tool,
  rather than a method bolted onto the manager. The Role publishes
  :meth:`is_resource_visible` as a narrow tool capability (same pattern as
  ``get_file_read_mtime``), so the executor→context dependency direction is
  never inverted.
"""

from __future__ import annotations

from typing import Callable, Sequence

from mote.common.const import TOOL_CALL_ID, TOOL_RESULT_RESOURCE_PATH
from mote.common.const.context import TOOL_RESULT_CLEARED_MESSAGE
from mote.common.schema import Message

_TOOL_ROLE = "tool"


class ContextVisibility:
    """Answers whether a reconstructable result is still present in context.

    Args:
        messages_provider: Zero-arg callable returning the current stored
            history (the live ``list[Message]``). Called fresh on every query so
            answers always reflect the latest compaction state.
    """

    def __init__(self, messages_provider: Callable[[], Sequence[Message]]) -> None:
        self._messages = messages_provider

    def is_resource_visible(self, path: str) -> bool:
        """Is the most-recent tool result derived from ``path`` still present?

        Scans the current history for tool-result messages tagged with this
        resource path (``TOOL_RESULT_RESOURCE_PATH``) and returns True iff the
        latest such result still holds real content — i.e. it has not been folded
        to :data:`TOOL_RESULT_CLEARED_MESSAGE` and has not been erased out of the
        history entirely.

        Returns False when the resource was never read, when its last result was
        folded/cleared, or when it was erased — every case in which a dedup
        short-circuit would strand the model with no content. The caller should
        then return real content instead of a "you already saw it" stub.

        Only the *most recent* result for the path matters: that is the view the
        dedup cache is standing in for. An older, still-present read of the same
        file does not make a newer cleared one visible, and vice versa.
        """
        if not path:
            return False
        latest_present: bool | None = None
        for msg in self._messages():
            if msg.role != _TOOL_ROLE:
                continue
            if msg.metadata.get(TOOL_RESULT_RESOURCE_PATH) != path:
                continue
            if not msg.metadata.get(TOOL_CALL_ID):
                continue
            # A folded body is a placeholder — content is gone from the model's
            # view even though the pairing survives. Anything else is real
            # content the model can still read.
            latest_present = msg.content != TOOL_RESULT_CLEARED_MESSAGE
        # None => no result for this path survives in history (never read, or
        # erased away). Either way, not visible.
        return bool(latest_present)
