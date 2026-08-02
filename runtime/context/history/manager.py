"""ContextManager — the facade for stored conversation history.

This is the integration point that finally wires the building blocks of this
package into the Role react loop. It plays two roles at once:

1. Model-context store. It owns the live context sent to the model, backed by
   ``RoleState.context`` (``LLMCallContext.messages``). Durable message facts are
   reduced separately into both the full logical transcript and this model-context
   projection. It exposes the small slice of the message-store API the loop /
   channel / think-engine depend on: ``get`` / ``add`` / ``add_batch`` / ``delete`` /
   ``count``.

2. Context orchestrator. Across the two history-level scopes it runs the cheap
   pass first then the expensive one:
     - ``microcompact`` folds old tool-result bodies in place (no LLM), and
     - ``autocompact`` summarizes+rebuilds when still over the token threshold,
       with the freed-token count threaded between them so the pricey summarize
       only fires when folding wasn't enough.
   Request-level recovery is requested through the canonical ModelGateway
   transformer; ``ContextManager`` only manages the *stored* history.

Why it backs ``RoleState.context`` directly (not a private list): this is the
live model projection the context manager exists to manage. The journal remains
the durable source of truth; resume rebuilds the projection from committed facts.

Layering: ``context`` may depend on ``executor`` (downward) but never
the reverse. This facade imports only sibling context modules plus
``schema`` — no Role / loop imports — so the Role depends on the manager, not
the other way around.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

import mote.runtime.context.history.budget as budget
from mote.contracts.conversation import ContextManagerConfig, FoldState, LLMCallContext
from mote.contracts.conversation.fields import CACHE_INTENT, CACHE_INTENT_EPHEMERAL_TAIL
from mote.contracts.events.conversation import (
    HistoryEditedEvent,
    MessageAppendedEvent,
    ModelContextRebuiltEvent,
    PostCompactEvent,
)
from mote.runtime.context.markers import is_system_reminder
from mote.runtime.telemetry.logging import log_class

if TYPE_CHECKING:
    from mote.contracts.config.tool import ToolResultLimitConfig
    from mote.contracts.conversation import Message, UserMessage
else:
    from mote.contracts.conversation import Message, UserMessage

from mote.runtime.context.compaction import (
    ContextEngine,
    EraseReducer,
    FoldReducer,
    HeadDropReducer,
    OversizedSpillReducer,
    RecoveryContextReducer,
    ReductionPipeline,
    ReductionReason,
    ReductionRequest,
    SummarizeReducer,
    Transcript,
    Urgency,
)
from mote.runtime.context.history.budget import TokenAccountant

if TYPE_CHECKING:
    from mote.contracts.ports.session.facts import SessionFactSink
    from mote.runtime.session.workspace import SessionWorkspace


# Sentinel distinguishing "argument omitted" from an explicit ``None`` in
# :meth:`ContextManager.rebuild_compression` (``None`` is a meaningful route
# value — it disables summarize — so it cannot double as "leave unchanged").
class _Unset:
    pass


_UNSET = _Unset()


@log_class(
    level="DEBUG",
    # The message-store CRUD slice is called on every turn for every message —
    # tracing it would flood the log. The compaction/request orchestration
    # methods (manage_history / token_state / prepare_request) stay traced.
    exclude={"get", "add", "add_batch", "delete", "count", "clear"},
)
class ContextManager:
    """Owns the live model context and orchestrates its compaction.

    Args:
        context: The ``LLMCallContext`` to back the store with (normally
            ``RoleState.context``). A fresh one is created when omitted
            (standalone / test use).
        model_route: Canonical Gateway route used by autocompact. May be None
            when only the message-store API is exercised.
        config: Tunable knobs; defaults reproduce the reference behavior.
        model: Model name for token math. Falls back to the route profile then a
            generic default.
        compactable: Names of tools whose result bodies may be folded/cleared
            (re-derivable by re-running the tool). The Role derives this from the
            live ToolExecutor (``reconstructable_tool_names()``) so the set tracks
            whatever tools are actually bound. Defaults to the empty set —
            standalone/test use folds nothing until a set is injected.
        write_fold_names: Names (primary + aliases) routing to the Edit tool, so
            the fold reducer can recognise a whole-file write's
            ``new_string`` (recorded under the raw emitted name) and fold it to
            the neutral marker alongside tool-result bodies. Derived by the Role
            from the executor (``tool_alias_names("Edit")``). Empty by default.
        session_id: Owning session id; scopes where the spill reducer persists an
            oversized part. Empty in standalone/test use.
        store: Workspace layout owner resolving the on-disk spill location.
            Defaults to the standard workspace root when omitted.
    """

    def __init__(
        self,
        context: LLMCallContext | None = None,
        *,
        model_route=None,
        config: ContextManagerConfig | None = None,
        model: str | None = None,
        context_tokens: int = 0,
        telemetry=None,
        sticky_provider=None,
        rehydrate_provider=None,
        compactable: frozenset[str] = frozenset(),
        compactable_provider: Callable[[], frozenset[str]] | None = None,
        write_fold_names: frozenset[str] = frozenset(),
        session_id: str = "",
        store: "SessionWorkspace | None" = None,
        limit_config: "ToolResultLimitConfig | None" = None,
        compaction_policy=None,
        session_fact_sink: "SessionFactSink | None" = None,
        history_edited: Callable[[HistoryEditedEvent], None] | None = None,
        model_context_rebuilt: Callable[[ModelContextRebuiltEvent], Awaitable[None]] | None = None,
    ):
        self._context = context if context is not None else LLMCallContext()
        self._model_route = model_route
        self.config = config or ContextManagerConfig()
        self._model = model
        self._context_tokens = context_tokens
        # Session id + workspace layout owner threaded to the spill reducer so a
        # spilled oversized part co-locates under the session directory (swept
        # with the session). Empty / None fall back to the default workspace root.
        self._session_id = session_id
        self._store = store
        # The large-result persistence policy. Owned by the executor (which
        # enforces it at the tool chokepoint) and borrowed here so the spill
        # reducer applies the SAME threshold/persist policy to runaway history
        # parts — one owner, no drift. None => the reducer's own default, which
        # matches the executor's default (standalone/test use).
        self._limit_config = limit_config
        # Tools whose result bodies are re-derivable (fold/clear-safe). Threaded
        # into ``Transcript.from_messages`` (the single place the reconstructable
        # judgment is made — the FoldReducer only consumes the resulting segment
        # flag). Derived by the Role from the live executor; empty by default
        # (standalone/test use folds nothing until a set is injected).
        self._compactable = compactable
        self._compactable_provider = compactable_provider
        # Names (primary + aliases) routing to the Edit tool, so the fold reducer
        # can recognise a whole-file-write's ``new_string`` (recorded under
        # the RAW emitted name) and fold it to the neutral marker in the same pass
        # as tool-result bodies. Derived by the Role from the live executor
        # (``tool_alias_names("Edit")``); empty by default (standalone/test use
        # folds no write args until injected). Unlike ``_compactable`` this is NOT
        # refreshed on ToolsChangedEvent: Edit is a static built-in whose alias set
        # is fixed at class definition, so hot tool (de)registration never changes
        # it (and it is baked into the reducer at _build_compression time anyway).
        self._write_fold_names = write_fold_names
        # Optional zero-arg callable returning sticky Messages to re-insert right
        # after an autocompaction summary (e.g. loaded Skill bodies from the
        # ResourceRegistry). None => nothing re-projected (standalone/test use).
        self._sticky_provider = sticky_provider
        # Optional zero-arg callable returning file-snapshot Messages (the recent
        # working set re-read from disk) to re-insert after the summary. None =>
        # no eager rehydration; the lazy re-read advisory still fires.
        self._rehydrate_provider = rehydrate_provider
        self._history_edited = history_edited
        self._model_context_rebuilt = model_context_rebuilt
        # Optional telemetry runtime. When set, appended messages emit
        # MessageAppendedEvent and a changed model projection emits
        # ContextCompactedEvent/PostCompactEvent observations. Durability is owned
        # by the explicit session fact sink, not telemetry. Injected by Role; None
        # in standalone/test use means no observations are emitted.
        self._telemetry = telemetry
        self._session_fact_sink = session_fact_sink
        self._compaction_policy = compaction_policy
        # Server-truth token reader with a local tokenization fallback.
        self._accountant = TokenAccountant()

        # Build the reduction pipeline from the current inputs (config / model /
        # route / providers). Extracted so the pipeline is *swappable at runtime*:
        # ``rebuild_compression`` re-runs this after retuning the config or
        # rebinding the compression LLM, and the caches (accountant, model-derived
        # thresholds) re-derive with it.
        self._build_compression()

    def bind_telemetry(self, telemetry) -> None:
        self._telemetry = telemetry
        self._engine.bind_telemetry(telemetry)

    def _build_compression(self) -> None:
        """(Re)construct the reduction pipeline, engine, and recovery reducer.

        The single place the compression stack is assembled, from the manager's
        current ``config`` / ``model`` / ``_model_route`` / providers. Called once at
        construction and again by :meth:`rebuild_compression` whenever any of
        those inputs change, so a runtime swap rebuilds every dependent piece
        (reducers, the threshold engine, the recovery reducer) coherently rather
        than leaving a half-updated stack.

        The reducers are the pluggable strategies; the engine commits every
        changed model-context projection before publishing compaction observations.
        Erase and fold are both FREE, so the pipeline's stable cost-sort keeps
        this insertion order: erase (true pair-delete of results the model tagged
        erasable) runs before fold (placeholder-shrink of what remains).
        """
        model = self.model
        self._erase = EraseReducer(self.config, model=model)
        self._fold = FoldReducer(self.config, model=model, write_fold_names=self._write_fold_names)
        # Lossless spill of runaway single parts (message content / tool-call
        # args) to disk, leaving a ``<persisted-output>`` pointer. FREE, so the
        # pipeline's cost-sort runs it opportunistically before summarize (LLM)
        # and drop (DESTRUCTIVE). Reuses the tool path's persist primitive routed
        # through the session's SessionWorkspace.
        self._spill = OversizedSpillReducer(
            self.config,
            model=model,
            session_id=self._session_id,
            store=self._store,
            limit_config=self._limit_config,
        )
        self._summarize = SummarizeReducer(
            self._model_route,
            self.config,
            model=model,
            sticky_provider=self._sticky_provider,
            rehydrate_provider=self._rehydrate_provider,
        )
        self._drop = HeadDropReducer(self.config, model=model)
        pipeline = ReductionPipeline(
            [self._erase, self._fold, self._spill, self._summarize, self._drop],
            model=model,
        )
        self._engine = ContextEngine(
            pipeline,
            telemetry=self._telemetry,
            summarize_reducer=self._summarize,
            policy=self._compaction_policy,
            session_fact_sink=self._session_fact_sink,
        )
        # Reactive (HARD) reducer for Gateway request transformation. It runs the SAME
        # boundary-safe machinery and escalates fold → summarize → drop, stopping
        # as soon as the target is met. Summarize (LLM-condense the head, keep the
        # tail) is far less lossy than the destructive head-drop, so on overflow we
        # preserve as much history as possible — with drop kept as the guaranteed
        # floor when summarize can't free enough (or is unavailable). Summarize
        # issues its own canonical call, but ``_model_route`` is the router's
        # dedicated COMPRESSION route with no request transformer. The
        # fold→summarize→drop cycle is broken at the injection layer.
        self._recovery_reducer = RecoveryContextReducer(
            [self._erase, self._fold, self._spill, self._summarize, self._drop],
            model=model,
        )

    def rebuild_compression(
        self,
        *,
        model_route: object = _UNSET,
        config: ContextManagerConfig | None | _Unset = _UNSET,
    ) -> None:
        """Swap the compression stack at runtime — rebind the LLM and/or retune.

        The clean seam a control tool hangs on to change how history is
        compacted mid-session: pass a fresh ``model_route`` and/or a new
        ``config`` (retuned thresholds), and the whole stack rebuilds coherently.
        Rebinding the route also re-derives the model from its immutable profile
        and refreshes the token accountant, so route and caches change together
        with the pipeline rebuild and never drift apart.

        Both arguments are optional (sentinel-guarded so ``None`` stays a
        meaningful value): omitting one leaves that input untouched.
        """
        if model_route is not _UNSET:
            self._model_route = model_route
            self._accountant = TokenAccountant()
        if config is not _UNSET and config is not None:
            self.config = cast(ContextManagerConfig, config)
        self._build_compression()

    @property
    def _consecutive_failures(self) -> int:
        """Summarize circuit-breaker counter (kept on the summarize reducer)."""
        return self._summarize.consecutive_failures

    @property
    def recovery_reducer(self) -> "RecoveryContextReducer":
        """The HARD fold+drop reducer the Role injects into the LLM for COMPRESS recovery."""
        return self._recovery_reducer

    # ------------------------------------------------------------------
    # Message-store API (the slice of the store the loop depends on)
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        """The live model-context projection (mutable; backs RoleState.context)."""
        return self._context.messages

    @property
    def model(self) -> str:
        profile = getattr(self._model_route, "profile", None)
        return self._model or getattr(profile, "model", None) or "gpt-4"

    def get(self, k: int = 0) -> list[Message]:
        """Return the most-recent ``k`` messages (``k=0`` → all).

        The contract the react loop and think-engine call: ``k`` slices the
        tail, ``0`` returns the whole history.
        """
        if k <= 0:
            return list(self._context.messages)
        return self._context.messages[-k:]

    async def add(self, message: Message) -> None:
        """Append one message to the transcript and live model context.

        Commits ``MessageAppendedEvent`` as a session fact, then mirrors it to
        telemetry handlers. Telemetry is a no-op when no runtime is wired.
        """
        if message is None:
            return
        event = MessageAppendedEvent(message=message)
        await self._commit_fact(event)
        self._context.messages.append(message)
        if self._telemetry is not None:
            await self._telemetry.emit(event)

    async def add_batch(self, messages) -> None:
        """Append several messages, skipping falsy entries (old ``add_batch``)."""
        for m in messages:
            await self.add(m)

    def delete(self, message: Message) -> None:
        """Remove a message if present (old ``Memory.delete``; used on recovery).

        No-ops when the message isn't in the store so the recovery path
        (role_raise_decorator deleting the latest observed message) is safe to
        call unconditionally.
        """
        try:
            self._context.messages.remove(message)
        except ValueError:
            pass

    def count(self) -> int:
        return len(self._context.messages)

    async def clear(self) -> None:
        """Clear the transcript and live model context.

        Commits one :class:`HistoryEditedEvent` (``reason="clear"``) before
        mutating the live view, then publishes it so context-derived frontiers and
        side stores re-derive against the empty projections.
        """
        event = HistoryEditedEvent(
            remaining_messages=[],
            removed_message_ids=[str(message.id) for message in self._context.messages],
            clear_all=True,
            reason="clear",
        )
        await self._commit_fact(event)
        await self._apply_history_edit(event)
        self._context.messages.clear()
        if self._telemetry is not None:
            await self._telemetry.emit(event)

    # ------------------------------------------------------------------
    # Direct history editing (user-driven delete of react-units)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_human_prompt(message: Message) -> bool:
        """True iff *message* is a human's own typed prompt (a react-unit anchor).

        A react-unit begins at a human prompt and runs up to (excluding) the next
        one. Only a ``role="user"`` message that is NOT an injected
        ``<system-reminder>`` envelope counts — the per-turn context blocks the
        turn-context collector wraps as user messages are part of a turn, never
        a boundary.
        """
        role = str(getattr(message, "role", "") or "")
        if role != "user":
            return False
        return not is_system_reminder(getattr(message, "content", "") or "")

    @staticmethod
    def _react_unit_drop_indices(
        messages: list[Message],
        anchor_ids,
        is_human_prompt,
    ) -> set[int]:
        """Indices to drop for the react-units anchored at ``anchor_ids`` (pure).

        For each human-prompt message whose id is in ``anchor_ids``, drop it and
        every following message up to (but excluding) the next human prompt — one
        whole turn (prompt → reply → its tool calls). Unknown ids are ignored, so
        a stale/duplicate anchor never crashes and never over-deletes.
        """
        wanted = {a for a in (anchor_ids or []) if a}
        drop: set[int] = set()
        n = len(messages)
        i = 0
        while i < n:
            if is_human_prompt(messages[i]) and getattr(messages[i], "id", None) in wanted:
                drop.add(i)
                j = i + 1
                while j < n and not is_human_prompt(messages[j]):
                    drop.add(j)
                    j += 1
                i = j
            else:
                i += 1
        return drop

    async def delete_react_units(self, anchor_ids) -> int:
        """Delete the react-units anchored at ``anchor_ids`` (one slice+swap).

        Computes the drop set once via :meth:`_react_unit_drop_indices`, rebuilds
        the backing model context without those messages, and commits a single
        :class:`HistoryEditedEvent` carrying their stable IDs. Replay applies the
        same removal to both transcript and model-context projections. Returns the number of
        messages removed; a no-op (empty selection / all-unknown ids) removes
        nothing and emits nothing.
        """
        messages = self._context.messages
        drop = self._react_unit_drop_indices(messages, anchor_ids, self._is_human_prompt)
        if not drop:
            return 0
        kept = [m for idx, m in enumerate(messages) if idx not in drop]
        event = HistoryEditedEvent(
            remaining_messages=list(kept),
            removed_message_ids=[str(message.id) for index, message in enumerate(messages) if index in drop],
            reason="delete",
        )
        await self._commit_fact(event)
        await self._apply_history_edit(event)
        self._context.messages[:] = kept
        if self._telemetry is not None:
            await self._telemetry.emit(event)
        return len(drop)

    # ------------------------------------------------------------------
    # History-level orchestration (microcompact → autocompact)
    # ------------------------------------------------------------------

    async def manage_history(self, *, custom_instructions: str | None = None) -> bool:
        """Reduce the live model context toward the autocompact threshold (SOFT).

        Builds a SOFT :class:`ReductionRequest` (target = the autocompact
        threshold) and hands the segmented history to the :class:`ContextEngine`.
        The engine runs the pipeline cheapest-first: the FREE fold always runs
        opportunistically (its own count gate), the LLM summarize runs only when
        the history is still over target, and the destructive head-drop is *not*
        reachable under SOFT urgency. The engine also owns compaction-policy
        evaluation, durable projection commit, and PostCompact observation.

        Replaces the backing model context with the reduced projection only after
        its fact commits, and returns True iff it changed. The logical transcript
        remains intact. Safe to call every turn: each strategy is gated and
        no-ops cheaply when its trigger isn't met.
        """
        if not self._context.messages:
            return False

        target = budget.autocompact_threshold(self.model, context_tokens=self._context_tokens)
        request = ReductionRequest(
            target_tokens=target,
            urgency=Urgency.SOFT,
            reason=ReductionReason.THRESHOLD,
        )
        transcript = Transcript.from_messages(
            [message.model_copy(deep=True) for message in self._context.messages],
            compactable=self._current_compactable(),
        )
        outcome = await self._engine.reduce(
            transcript,
            request,
            trigger="auto",
            custom_instructions=custom_instructions,
        )
        if not outcome.changed:
            return False
        if self._model_context_rebuilt is not None:
            await self._model_context_rebuilt(
                PostCompactEvent(
                    trigger="auto",
                    summary=outcome.summary or "",
                )
            )
        # Swap the reduced history into the backing context (fold mutates the
        # same Message objects in place; summarize rebuilds [summary] + tail).
        self._context.messages[:] = outcome.transcript.to_messages()
        return True

    async def _commit_fact(self, event: MessageAppendedEvent | HistoryEditedEvent) -> None:
        if self._session_fact_sink is not None:
            await self._session_fact_sink.commit_fact(event)

    async def _apply_history_edit(self, event: HistoryEditedEvent) -> None:
        if self._history_edited is not None:
            self._history_edited(event)
        if self._model_context_rebuilt is not None:
            await self._model_context_rebuilt(event)

    def _current_compactable(self) -> frozenset[str]:
        if self._compactable_provider is not None:
            return self._compactable_provider()
        return self._compactable

    def token_state(self):
        """Current token budget snapshot for the live model context (TokenState)."""
        return budget.evaluate(
            self._context.messages,
            self.model,
            autocompact_enabled=self.config.enable_autocompact,
            observed_tokens=self._accountant.observed(),
            context_tokens=self._context_tokens,
        )

    def fold_state(self) -> FoldState:
        """Current count-based fold snapshot for the live model context.

        The count-based sibling of ``token_state``: how many foldable
        reconstructable model tool-call turns are live versus the trigger at which
        the FREE fold clears eligible results from old rounds. Reuses the
        reducer's own ``active_groups`` count (built from the same segmented
        transcript the fold acts on) so a pre-fold warning can never disagree
        with what the fold will actually do.
        """
        active = FoldReducer.active_groups(
            Transcript.from_messages(
                self._context.messages,
                compactable=self._current_compactable(),
            )
        )
        return FoldState(
            enabled=self.config.enable_microcompact,
            active_count=len(active),
            trigger=self.config.microcompact_trigger_threshold,
            keep_recent=max(1, self.config.microcompact_keep_recent),
        )

    # ------------------------------------------------------------------
    # Request assembly (history + the current user prompt)
    # ------------------------------------------------------------------

    async def prepare_request(self, user_prompt: str | Message | None = None, *, manage: bool = True) -> list[Message]:
        """Build the ``req`` the think step sends: managed history + user prompt.

        Runs history-level management first (unless ``manage=False``), then
        returns ``stored_history + [user_prompt]``. The returned list is a fresh
        list (the caller may pass it straight to ``llm.aask`` / ``aask_tool``);
        the user prompt is NOT added to the live model context here — only the
        request gets it.
        """
        if manage:
            await self.manage_history()

        req: list[Message] = list(self._context.messages)
        if user_prompt is not None:
            tail = user_prompt if isinstance(user_prompt, Message) else UserMessage(content=user_prompt)
            # This appended tail is the per-turn command + <system-reminder> prompt:
            # re-synthesized every turn, never stored in history, and reappearing
            # next turn with different bytes. Declare that intent so providers never
            # anchor a cache breakpoint on it (which would strand the next turn's
            # prefix and force the whole history to re-write). Absence == durable.
            tail.metadata[CACHE_INTENT] = CACHE_INTENT_EPHEMERAL_TAIL
            req.append(tail)
        return req
