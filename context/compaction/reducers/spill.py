"""OversizedSpillReducer (FREE) — lossless spill of runaway single parts.

The per-tool size cap (:func:`mote.executor.tool_result_limit.enforce_tool_result_limit`)
runs only at the ``ToolExecutor.run_command`` chokepoint, so it caps *a tool's
output* and nothing else. Two classes of oversized content slip past it straight
into stored history, and no other reducer can surgically reach them:

- a **runaway assistant response** (a model generation that blows up to 100k+ chars);
- a **giant tool-call ``args`` blob** the model placed into a call's arguments —
  now normally persisted *at record time* (the native channel's ``record_call``
  runs :meth:`ToolExecutor.persist_large_args` before the message enters memory),
  so for fresh turns this reducer's args-branch is a **backstop** that only fires
  on resumed/legacy content or args written by some other path; it stays because
  it is idempotent (already-``<persisted-output>`` args are left alone);
- (bonus) a **stray oversized tool result loaded from a resumed session** that
  predates limiting.

``fold`` only clears *reconstructable* tool-result bodies, ``erase`` pair-deletes
marked calls, ``summarize`` is an LLM pass, ``drop`` discards whole turns — a
single pathological *part* is invisible to all four. This reducer fills that gap.

It is **lossless**: the full content is written to disk (session-scoped, via the
same :class:`WorkspaceStore`-routed primitive the tool path uses) and the
in-history part is replaced by the existing ``<persisted-output>`` envelope that
*names the on-disk file*, so the model can re-read it. That is why the cost is
``FREE`` (like fold, no information loss beyond a retrievable pointer).

Gating mirrors the tool path, not fold's count gate: it is purely **size-gated**
per part (the reused primitive returns content unchanged when at/under the
threshold) and **idempotent** (content already wrapped in ``<persisted-output>``
is left alone), so re-runs are no-ops and the prompt prefix stays stable.

Config is the *same policy knob* as the tool-output path — one policy, two
transports. It reads a :class:`ToolResultLimitConfig` (threshold
``default_max_result_size_chars``, ``persist_large_tool_results``, and the
``enable_tool_result_limit`` master switch). The :class:`ToolExecutor` owns this
policy (it enforces it at the tool chokepoint); the ContextManager borrows *that
one instance* and threads it here, so both transports move in lockstep with no
drift. Standalone/test use passes nothing → a defaulted ``ToolResultLimitConfig()``
that matches the executor's own default.
"""

from __future__ import annotations

from mote.common.const import RESOURCE_STICKY, RETENTION, RETENTION_PIN, TOOL_CALLS
from mote.common.schema import ContextManagerConfig, ToolResultLimitConfig, serialize_tool_call_args
from mote.common.utils.token_counter import count_string_tokens
from mote.common.workspace import WorkspaceStore
from mote.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from mote.context.compaction.request import ReductionRequest
from mote.context.compaction.transcript import PINNED_KINDS, Transcript
from mote.executor.tool_result_limit import enforce_tool_result_limit

_TOOL_NAME = "compaction"


class OversizedSpillReducer:
    """FREE strategy: size-gated, idempotent spill of oversized parts to disk."""

    cost = ReducerCost.FREE

    def __init__(
        self,
        config: ContextManagerConfig | None = None,
        *,
        model: str = "gpt-4",
        session_id: str = "",
        store: WorkspaceStore | None = None,
        limit_config: ToolResultLimitConfig | None = None,
    ) -> None:
        # ``config`` is kept for wiring parity with the sibling reducers (the
        # manager builds them all with the same ContextManagerConfig); the spill
        # policy itself lives in ``limit_config`` — the same knobs as the tool
        # path, defaulted here exactly as ToolExecutor / the task pool default it.
        self._cfg = config or ContextManagerConfig()
        self._limit = limit_config or ToolResultLimitConfig()
        self._model = model
        self._session_id = session_id
        self._store = store

    def _spill(self, content: str, result_id: str) -> str:
        """Run one part through the shared persist+preview+pointer primitive.

        Returns the (possibly unchanged) content. The primitive is size-gated and
        idempotent, so under-threshold or already-``<persisted-output>`` content
        comes back untouched.
        """
        return enforce_tool_result_limit(
            content,
            _TOOL_NAME,
            result_id=result_id,
            session_id=self._session_id,
            max_result_size_chars=self._limit.default_max_result_size_chars,
            persist=self._limit.persist_large_tool_results,
            store=self._store,
        )

    async def reduce(self, transcript: Transcript, request: ReductionRequest) -> ReductionOutcome:
        if not self._limit.enable_tool_result_limit:
            return ReductionOutcome(transcript, strategy="spill")

        model = self._model
        changed = False
        tokens_freed = 0

        for seg in transcript.segments:
            # Pinned kinds (SYSTEM_ANCHOR / TASK) survive every reduction untouched.
            if seg.kind in PINNED_KINDS:
                continue
            for msg in seg.messages:
                # A producer-pinned or sticky (re-projected) body is never spilled
                # — the same protection fold/erase honour at the message level.
                if msg.metadata.get(RETENTION) == RETENTION_PIN or msg.metadata.get(RESOURCE_STICKY):
                    continue

                # (1) Message content — runaway assistant/user text or a stray
                #     oversized tool result loaded from a resumed session.
                content = msg.content
                if content:
                    spilled = self._spill(content, msg.id)
                    if spilled != content:
                        tokens_freed += count_string_tokens(content, model) - count_string_tokens(spilled, model)
                        msg.content = spilled
                        changed = True

                # (2) Tool-call args — a giant blob the model put into a call's
                #     arguments. Serialize (dict → json, or the raw str), spill,
                #     and set the envelope string back as ``args`` (AIMessage
                #     serialization already accepts a str args, passed through as
                #     the arguments string).
                calls = msg.metadata.get(TOOL_CALLS)
                if not calls:
                    continue
                for call in calls:
                    serialized = serialize_tool_call_args(call.get("args"))
                    spilled = self._spill(serialized, f"{call.get('id') or ''}-args")
                    if spilled != serialized:
                        tokens_freed += count_string_tokens(serialized, model) - count_string_tokens(spilled, model)
                        call["args"] = spilled
                        changed = True

        if not changed:
            return ReductionOutcome(transcript, strategy="spill")

        target_met = transcript.token_count(model) <= request.target_tokens
        return ReductionOutcome(
            transcript,
            tokens_freed=tokens_freed,
            changed=True,
            strategy="spill",
            target_met=target_met,
        )
