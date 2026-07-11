"""L0 — the structured view of a conversation every reducer stands on.

A :class:`Transcript` is a segmentation of the flat ``list[Message]`` stored
history into indivisible **segments**. The whole point of this layer is to make
one class of bug *physically unrepresentable*: an assistant turn that invoked
tools plus **all** of that turn's ``tool_result`` messages form a single atomic
``TOOL_GROUP`` segment, and the only cut the Transcript exposes
(:meth:`split_keep_tail`) always lands on a segment boundary. So a summarize /
drop can never separate a ``tool_use`` from its ``tool_result`` — the exact
mistake the old flat ``autocompact._split_keep_tail`` made, which sent an orphan
``tool_result`` to Anthropic and 400'd.

Segment kinds:

- ``SYSTEM_ANCHOR`` — a ``role="system"`` message (rare in stored history, since
  the system prompt is assembled at request time). Pinned: never summarized or
  dropped.
- ``TASK`` — reserved for a pinned task/requirement segment. Not produced by
  :meth:`from_messages` today (would change compaction behavior); kept as a
  documented extension point so a segment tagged ``TASK`` is treated as pinned.
- ``TOOL_GROUP`` — one assistant message carrying ``tool_calls`` plus the
  contiguous ``tool_result`` messages answering those calls. Atomic and, when
  every invoked tool is in the caller-supplied ``compactable`` set (tools whose
  results are re-derivable), ``reconstructable`` (its bodies may be folded in
  place by the fold reducer).
- ``MESSAGE`` — any other single message (plain user / assistant text, or an
  orphan tool result the input already contained).

Provider pairing rules (Anthropic is strict: every ``tool_use`` must be followed
by its ``tool_result``) are enforced here, once, in :meth:`from_messages` — the
reducers above never have to think about them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from mote.common.const import RETENTION, RETENTION_PIN, TOOL_CALL_ID, TOOL_CALLS
from mote.common.schema import Message
from mote.common.utils.token_counter import count_string_tokens

_SYSTEM_ROLE = "system"


class SegmentKind(Enum):
    SYSTEM_ANCHOR = "system_anchor"
    TASK = "task"
    TOOL_GROUP = "tool_group"
    MESSAGE = "message"


# Segments of these kinds survive every reduction — never summarized, never
# dropped. The fold reducer may still shrink a *reconstructable* group's bodies
# in place, but that is content-level and does not touch pinned segments.
PINNED_KINDS: frozenset[SegmentKind] = frozenset({SegmentKind.SYSTEM_ANCHOR, SegmentKind.TASK})


@dataclass
class Segment:
    """An indivisible run of one or more messages."""

    kind: SegmentKind
    messages: list[Message]
    # For TOOL_GROUP only: every invoked tool was in the caller-supplied
    # ``compactable`` set, so the group's result bodies can be cleared/re-derived.
    # Always False otherwise.
    reconstructable: bool = False

    @property
    def pinned(self) -> bool:
        if self.kind in PINNED_KINDS:
            return True
        # A tool result the producer tagged RETENTION_PIN survives every
        # reduction (never summarized, never dropped) — the same protection a
        # SYSTEM_ANCHOR gets by kind, granted here per-result via metadata. The
        # fold reducer honours the same tag at the message level; recognising it
        # here extends that protection through summarize and head-drop too.
        return any(m.metadata.get(RETENTION) == RETENTION_PIN for m in self.messages)

    def token_count(self, model: str) -> int:
        """Best-effort token size of this segment's message contents."""
        return sum(count_string_tokens(m.content or "", model) for m in self.messages)


@dataclass
class Transcript:
    """A conversation as an ordered list of atomic :class:`Segment`."""

    segments: list[Segment] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction / round-trip
    # ------------------------------------------------------------------

    @classmethod
    def from_messages(cls, messages: Sequence[Message], *, compactable: frozenset[str] = frozenset()) -> "Transcript":
        """Segment a flat stored history, grouping tool_call turns with their results.

        A ``role="system"`` message becomes a pinned ``SYSTEM_ANCHOR``. An
        assistant message carrying ``TOOL_CALLS`` starts a ``TOOL_GROUP`` that
        greedily absorbs the immediately-following ``tool_result`` messages whose
        ``TOOL_CALL_ID`` belongs to that turn's call ids. Anything else is a
        standalone ``MESSAGE``.
        """
        segments: list[Segment] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            if m.role == _SYSTEM_ROLE:
                segments.append(Segment(SegmentKind.SYSTEM_ANCHOR, [m]))
                i += 1
                continue

            calls = m.metadata.get(TOOL_CALLS)
            if calls:
                call_ids = {c.get("id") for c in calls if c.get("id")}
                group = [m]
                j = i + 1
                # Absorb the contiguous tool-result turns answering this turn's
                # calls. Stop at the first message that is not one of ours — that
                # keeps the group atomic and the pairing intact.
                while j < n:
                    cid = messages[j].metadata.get(TOOL_CALL_ID)
                    if cid is not None and cid in call_ids:
                        group.append(messages[j])
                        j += 1
                    else:
                        break
                names = [c.get("name") for c in calls]
                reconstructable = bool(names) and all(nm in compactable for nm in names)
                segments.append(Segment(SegmentKind.TOOL_GROUP, group, reconstructable=reconstructable))
                i = j
                continue

            segments.append(Segment(SegmentKind.MESSAGE, [m]))
            i += 1
        return cls(segments)

    def to_messages(self) -> list[Message]:
        """Flatten back to the stored-history message list (segment order preserved)."""
        out: list[Message] = []
        for seg in self.segments:
            out.extend(seg.messages)
        return out

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        return self.to_messages()

    def message_count(self) -> int:
        return sum(len(seg.messages) for seg in self.segments)

    def is_empty(self) -> bool:
        return not self.segments

    def token_count(self, model: str) -> int:
        return sum(seg.token_count(model) for seg in self.segments)

    # ------------------------------------------------------------------
    # Boundary-safe operations (the only ways to carve the transcript)
    # ------------------------------------------------------------------

    def split_keep_tail(self, *, keep_tail_messages: int, keep_tail_tokens: int, model: str) -> int:
        """Segment index where the preserved tail begins.

        Walks backward over whole segments accumulating messages+tokens until
        both the ``keep_tail_messages`` and ``keep_tail_tokens`` floors are met,
        then returns the boundary index ``s`` so ``segments[:s]`` is the head to
        summarize and ``segments[s:]`` is the tail kept verbatim.

        Boundary-safe by construction: because it only ever returns a segment
        index, a ``TOOL_GROUP`` (assistant + its tool_results) is never split.
        Returns ``len(segments)`` (keep everything → nothing to summarize) when
        the history is too short to carve a head. Always leaves at least one head
        segment when it does split.
        """
        total_msgs = self.message_count()
        if total_msgs <= keep_tail_messages + 1:
            return len(self.segments)

        tail_tokens = 0
        tail_msgs = 0
        split = len(self.segments)
        for idx in range(len(self.segments) - 1, -1, -1):
            seg = self.segments[idx]
            tail_msgs += len(seg.messages)
            tail_tokens += seg.token_count(model)
            split = idx
            if tail_msgs >= keep_tail_messages and tail_tokens >= keep_tail_tokens:
                break

        # Always leave at least one head segment to summarize.
        if split <= 0:
            split = 1
        return split

    def first_unpinned_index(self) -> int:
        """Index of the oldest non-pinned segment, or ``len(segments)`` if none."""
        for idx, seg in enumerate(self.segments):
            if not seg.pinned:
                return idx
        return len(self.segments)

    def drop(self, indices: Iterable[int]) -> "Transcript":
        """Return a new transcript with the given segment indices removed."""
        drop_set = set(indices)
        return Transcript([seg for idx, seg in enumerate(self.segments) if idx not in drop_set])

    def replace_range(self, start: int, end: int, replacement: Sequence[Message]) -> "Transcript":
        """Return a new transcript with ``segments[start:end]`` replaced.

        The replacement messages are re-segmented (so any tool groups inside them
        are grouped correctly) and spliced in place of the range.
        """
        head = self.segments[:start]
        tail = self.segments[end:]
        middle = Transcript.from_messages(replacement).segments
        return Transcript([*head, *middle, *tail])

    def erase_pairs(self, call_ids: Iterable[str]) -> "Transcript":
        """Return a new transcript with the given tool calls removed *as pairs*.

        For each call id this drops both halves together: the ``tool_result``
        message answering it AND the matching entry in the invoking assistant
        turn's ``TOOL_CALLS`` metadata. Because both sides go at once, every
        surviving ``tool_use`` still has its ``tool_result`` and vice-versa — the
        pairing invariant the whole Transcript layer exists to protect stays
        intact (no orphan ``tool_result`` → no Anthropic 400). This is the true
        deletion counterpart to the fold reducer's placeholder rewrite.

        A ``TOOL_GROUP`` whose assistant is left with no calls and blank content
        collapses and is dropped whole; otherwise the trimmed assistant plus its
        remaining results are kept, preserving the segment's ``reconstructable``
        flag. Non-tool-group segments and unaffected groups pass through
        untouched. The assistant's ``TOOL_CALLS`` list is rewritten in place
        (consistent with the fold reducer, which mutates result bodies in place).
        """
        erase = {cid for cid in call_ids if cid}
        if not erase:
            return self

        new_segments: list[Segment] = []
        for seg in self.segments:
            if seg.kind is not SegmentKind.TOOL_GROUP:
                new_segments.append(seg)
                continue

            assistant = seg.messages[0]
            calls = assistant.metadata.get(TOOL_CALLS) or []
            if not any(c.get("id") in erase for c in calls):
                new_segments.append(seg)
                continue

            kept_calls = [c for c in calls if c.get("id") not in erase]
            kept_results = [m for m in seg.messages[1:] if m.metadata.get(TOOL_CALL_ID) not in erase]

            # The whole turn collapses when nothing meaningful is left: no
            # surviving calls and no assistant prose to preserve.
            if not kept_calls and not (assistant.content or "").strip():
                continue

            assistant.metadata[TOOL_CALLS] = kept_calls
            new_segments.append(
                Segment(SegmentKind.TOOL_GROUP, [assistant, *kept_results], reconstructable=seg.reconstructable)
            )
        return Transcript(new_segments)
