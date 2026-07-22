#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ThrashDetector — the pure per-Role state machine behind the loop guard.

Zero I/O, zero framework imports: it consumes small facts about *finished* tool
calls (name, a stable args signature, success, whether the tool is read-only,
and a result fingerprint) and returns a :class:`Verdict` when a call is
thrashing. All wiring — reading those facts off a ``PostToolUseEvent`` and
turning a verdict into an in-band nudge — lives in the subscriber, so this class
can be unit-tested in complete isolation.

Two orthogonal thrash shapes are tracked, each keyed by ``(tool_name, sig)`` so
different arguments never share a streak:

- **repeated failure** — the SAME call (same args) fails ``failure_threshold``
  times in a row. It is a *streak*: any success on that signature clears the
  count, so only an unbroken run of identical failures trips it. This catches
  the model reissuing a doomed call verbatim instead of adapting.

- **no progress** — a read-only (PURE) call returns the SAME result
  ``no_progress_threshold`` times in a row. A read that keeps yielding identical
  bytes is spinning, not observing fresh state. Only PURE tools are eligible: a
  LOCAL/EXTERNAL tool legitimately repeats (a deploy loop, a status poll) and a
  stable result there is not evidence of a stuck loop.

The two streaks are mutually exclusive per record: a failed call feeds the
failure streak and resets its no-progress streak; a successful call resets its
failure streak and (for PURE tools) feeds the no-progress streak. So one call is
only ever evidence for one shape, and a verdict names exactly which one fired.

State is a plain dict on the instance, one instance per Role/session (the
subscriber owns it), so there is no cross-session leakage and no global
singleton. There is no per-turn reset: the streak semantics are self-healing
(one contrary outcome clears the relevant count), matching the sliding-window
spirit of the think-layer ``check_duplicate_calls`` rather than depending on a
turn-boundary event the loop does not currently emit.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal, Optional

#: Which thrash shape a verdict names.
ThrashKind = Literal["repeated_failure", "no_progress"]

#: Cap on distinct ``(tool, sig)`` keys held per streak map. A key survives only
#: until its *contrary* outcome pops it (a success ends a failure streak, a
#: failure/changed-read ends a no-progress streak), so a call issued once and
#: never repeated would otherwise linger forever — and ``sig`` embeds the full
#: args, so the keyspace is the set of distinct calls, not distinct tools. Over a
#: long-lived Role that grows without bound, so both maps are LRU-capped: only
#: recent repetition is evidence of thrash, and evicting the coldest key at most
#: forgets a stale count-of-1 (a self-healing reset, never a false verdict).
_MAX_TRACKED_KEYS = 2048


@dataclass(frozen=True)
class Verdict:
    """A tripped thrash streak for one ``(tool, sig)`` — the fact the subscriber
    turns into an in-band nudge.

    ``count`` is the streak length at the moment it crossed the threshold, so the
    nudge can quote the concrete number ("failed 3 times") rather than a vague
    "repeatedly".
    """

    kind: ThrashKind
    tool_name: str
    count: int


class ThrashDetector:
    """Per-Role streak counter for repeated-failure / no-progress tool calls.

    Not thread-safe by design: the control plane awaits ``handle_control`` inline
    on the single react-loop task, so records arrive serialized per Role.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        no_progress_threshold: int = 3,
        max_tracked_keys: int = _MAX_TRACKED_KEYS,
    ) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._no_progress_threshold = max(1, no_progress_threshold)
        self._max_tracked_keys = max(1, max_tracked_keys)
        # (tool_name, sig) -> consecutive identical-args failure count. LRU-ordered
        # (most-recently-touched last) so a full map evicts its coldest key.
        self._failure_streak: OrderedDict[tuple[str, str], int] = OrderedDict()
        # (tool_name, sig) -> (last_result_fingerprint, consecutive-repeat count).
        self._no_progress: OrderedDict[tuple[str, str], tuple[str, int]] = OrderedDict()

    def _touch(self, streak: OrderedDict, key: tuple[str, str], value) -> None:
        """Write ``key`` as the hottest entry, evicting the coldest past the cap.

        Shared by both streak maps: ``move_to_end`` keeps LRU order on rewrite,
        and ``popitem(last=False)`` drops the least-recently-touched key once the
        map is over ``max_tracked_keys``. Evicting a cold key only forgets a stale
        streak (almost always a count-of-1 that never repeated), which is a
        self-healing reset — it can never manufacture a false verdict.
        """
        streak[key] = value
        streak.move_to_end(key)
        if len(streak) > self._max_tracked_keys:
            streak.popitem(last=False)

    def record(
        self,
        *,
        tool_name: str,
        sig: str,
        success: bool,
        is_readonly: bool,
        result_fingerprint: str,
    ) -> Optional[Verdict]:
        """Fold one finished call into the streaks; return a :class:`Verdict` if it
        just crossed a threshold, else ``None``.

        Args:
            tool_name: The tool's canonical name (the executor canonicalizes
                aliases upstream, so a streak is keyed on the real tool).
            sig: A stable, order-insensitive signature of the call's args. The
                caller builds it (see the subscriber) so this class stays free of
                any serialization policy.
            success: The tool-body outcome (``PostToolUseEvent.success``).
            is_readonly: Whether the tool is PURE (read-only). Only PURE tools are
                eligible for no-progress detection.
            result_fingerprint: A cheap fingerprint of the result text, compared
                across calls to spot an unchanging read. Only consulted for a
                successful PURE call.
        """
        key = (tool_name, sig)

        if not success:
            # A failure feeds the failure streak and voids any no-progress streak
            # on this signature (a failed read produced no stable observation).
            self._no_progress.pop(key, None)
            streak = self._failure_streak.get(key, 0) + 1
            self._touch(self._failure_streak, key, streak)
            if streak >= self._failure_threshold:
                return Verdict(kind="repeated_failure", tool_name=tool_name, count=streak)
            return None

        # A success clears the failure streak (the call finally worked / changed).
        self._failure_streak.pop(key, None)

        # Only read-only tools are eligible for no-progress: a mutating/external
        # tool that repeats is doing real work, not spinning.
        if not is_readonly:
            self._no_progress.pop(key, None)
            return None

        prev = self._no_progress.get(key)
        if prev is not None and prev[0] == result_fingerprint:
            count = prev[1] + 1
        else:
            count = 1
        self._touch(self._no_progress, key, (result_fingerprint, count))
        if count >= self._no_progress_threshold:
            return Verdict(kind="no_progress", tool_name=tool_name, count=count)
        return None
