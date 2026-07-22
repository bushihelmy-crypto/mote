"""ContextManager — the facade orchestrating Mote's context-management scopes.

This is the integration point that finally wires the building blocks of this
package into the Role react loop. It plays two roles at once:

1. Message store. It owns the conversation history, backed by ``RoleState.context``
   (``LLMCallContext.messages``) so the history survives checkpoint/recovery. It
   exposes the small slice of the message-store API the loop / channel /
   think-engine depend on: ``get`` / ``add`` / ``add_batch`` / ``delete`` / ``count``.

2. Context orchestrator. Across the two history-level scopes it runs the cheap
   pass first then the expensive one:
     - ``microcompact`` folds old tool-result bodies in place (no LLM), and
     - ``autocompact`` summarizes+rebuilds when still over the token threshold,
       with the freed-token count threaded between them so the pricey summarize
       only fires when folding wasn't enough.
   The request-level scope (per-call compression) stays inside ``base_llm`` —
   ``ContextManager`` does not duplicate it; it only manages the *stored*
   history.

Why it backs ``RoleState.context`` directly (not a private list): the stored
history IS the data the context manager exists to manage, and it must be
serializable for recovery. Holding it anywhere else would split the source of
truth.

Layering: ``context`` may depend on ``executor`` (downward) but never
the reverse. This facade imports only sibling context modules plus
``schema`` — no Role / loop imports — so the Role depends on the manager, not
the other way around.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import mote.context.budget as budget
from mote.common.const import CACHE_INTENT, CACHE_INTENT_EPHEMERAL_TAIL
from mote.common.events import HistoryEditedEvent, MessageAppendedEvent, ToolsChangedEvent
from mote.common.interface.event_subscriber import ObservationSubscriber
from mote.common.logs import log_class
from mote.common.schema import ContextManagerConfig, FoldState, LLMCallContext

if TYPE_CHECKING:
    from mote.common.schema import ToolResultLimitConfig
    from mote.common.schema.messages import Message, UserMessage
else:
    from mote.common.schema import Message, UserMessage

from mote.context.budget import TokenAccountant
from mote.context.compaction import (
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

if TYPE_CHECKING:
    from mote.common.workspace import WorkspaceStore

# Sentinel distinguishing "argument omitted" from an explicit ``None`` in
# :meth:`ContextManager.rebuild_compression` (``None`` is a meaningful llm value
# — it disables summarize — so it cannot double as "leave unchanged").
_UNSET = object()


@log_class(
    level="DEBUG",
    # The message-store CRUD slice is called on every turn for every message —
    # tracing it would flood the log. The compaction/request orchestration
    # methods (manage_history / token_state / prepare_request) stay traced.
    exclude={"get", "add", "add_batch", "delete", "count", "clear"},
)
class ContextManager(ObservationSubscriber):
    """Owns the stored conversation and orchestrates its compaction.

    Args:
        context: The ``LLMCallContext`` to back the store with (normally
            ``RoleState.context`` so it is checkpointed). A fresh one is created
            when omitted (standalone / test use).
        llm: Anything with ``async aask(msg, system_msgs=, stream=)`` — used by
            autocompact to summarize. May be None when only the message-store
            API is exercised (compaction then no-ops gracefully).
        config: Tunable knobs; defaults reproduce the reference behavior.
        model: Model name for token math. Falls back to ``llm.model`` then a
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
        llm=None,
        config: ContextManagerConfig | None = None,
        model: str | None = None,
        bus=None,
        sticky_provider=None,
        rehydrate_provider=None,
        compactable: frozenset[str] = frozenset(),
        write_fold_names: frozenset[str] = frozenset(),
        session_id: str = "",
        store: "WorkspaceStore | None" = None,
        limit_config: "ToolResultLimitConfig | None" = None,
    ):
        self._context = context if context is not None else LLMCallContext()
        self._llm = llm
        self.config = config or ContextManagerConfig()
        self._model = model
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
        # Optional event bus (``common.events.EventBus``). When set, appended
        # messages emit MessageAppendedEvent and a compaction emits
        # PreCompact/CompactionCheckpoint/PostCompact events; subscribers persist
        # to the session rollout and run hooks. Injected by Role; None in
        # standalone/test use => no events emitted.
        self._bus = bus
        # Keep the fold/clear-safe tool set current. When the executor
        # de-registers a tool it announces the fresh reconstructable set on this
        # same bus (:class:`ToolsChangedEvent`); observing it here refreshes
        # ``_compactable`` so compaction never keeps folding a result whose tool
        # has since gone (nor, conversely, treats a still-bound tool as
        # non-reconstructable). Reading the post-change facts straight off the
        # event keeps the manager decoupled from the executor (no live back-ref).
        if bus is not None:
            bus.subscribe(self)
        # Server-truth token reader (falls back to tiktoken); reads the llm's
        # shared cost manager, so no new reporting pipeline is needed.
        self._accountant = TokenAccountant(llm)

        # Build the reduction pipeline from the current inputs (config / model /
        # llm / providers). Extracted so the pipeline is *swappable at runtime*:
        # ``rebuild_compression`` re-runs this after retuning the config or
        # rebinding the compression LLM, and the caches (accountant, model-derived
        # thresholds) re-derive with it.
        self._build_compression()

    def _build_compression(self) -> None:
        """(Re)construct the reduction pipeline, engine, and recovery reducer.

        The single place the compression stack is assembled, from the manager's
        current ``config`` / ``model`` / ``_llm`` / providers. Called once at
        construction and again by :meth:`rebuild_compression` whenever any of
        those inputs change, so a runtime swap rebuilds every dependent piece
        (reducers, the threshold engine, the recovery reducer) coherently rather
        than leaving a half-updated stack.

        The reducers are the pluggable strategies; the engine wraps the pipeline
        with the compaction event lifecycle (PreCompact / checkpoint / Post).
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
        # through the session's WorkspaceStore.
        self._spill = OversizedSpillReducer(
            self.config,
            model=model,
            session_id=self._session_id,
            store=self._store,
            limit_config=self._limit_config,
        )
        self._summarize = SummarizeReducer(
            self._llm,
            self.config,
            model=model,
            sticky_provider=self._sticky_provider,
            rehydrate_provider=self._rehydrate_provider,
        )
        self._drop = HeadDropReducer(self.config, model=model)
        pipeline = ReductionPipeline([self._erase, self._fold, self._spill, self._summarize, self._drop], model=model)
        self._engine = ContextEngine(pipeline, bus=self._bus, summarize_reducer=self._summarize)
        # Reactive (HARD) reducer for the LLM recovery loop. It runs the SAME
        # boundary-safe machinery and escalates fold → summarize → drop, stopping
        # as soon as the target is met. Summarize (LLM-condense the head, keep the
        # tail) is far less lossy than the destructive head-drop, so on overflow we
        # preserve as much history as possible — with drop kept as the guaranteed
        # floor when summarize can't free enough (or is unavailable). Summarize
        # issues its own inner aask(), but ``_llm`` here is the router's dedicated
        # COMPRESSION instance, built reducer-less (context_reducer=None) so that
        # inner call cannot re-enter _compress. The fold→summarize→drop cycle is
        # thus broken at the injection layer — no runtime re-entrancy guard needed.
        self._recovery_reducer = RecoveryContextReducer(
            [self._erase, self._fold, self._spill, self._summarize, self._drop], model=model
        )

    def rebuild_compression(self, *, llm: object = _UNSET, config: ContextManagerConfig | None = _UNSET) -> None:
        """Swap the compression stack at runtime — rebind the LLM and/or retune.

        The clean seam a control tool hangs on to change how history is
        compacted mid-session: pass a fresh ``llm`` (e.g. the router hands over a
        different COMPRESSION instance, or a bigger-context model) and/or a new
        ``config`` (retuned thresholds), and the whole stack rebuilds coherently.
        Rebinding the LLM also re-derives the model (``self.model`` reads
        ``llm.model``) and refreshes the token accountant, which reads the llm's
        shared cost manager — so the "re-fetch compression model + refresh caches"
        happen together with the pipeline rebuild, never drifting apart.

        Both arguments are optional (sentinel-guarded so ``None`` stays a
        meaningful value): omitting one leaves that input untouched.
        """
        if llm is not _UNSET:
            self._llm = llm
            self._accountant = TokenAccountant(llm)
        if config is not _UNSET and config is not None:
            self.config = config
        self._build_compression()

    @property
    def _consecutive_failures(self) -> int:
        """Summarize circuit-breaker counter (kept on the summarize reducer)."""
        return self._summarize.consecutive_failures

    @property
    def recovery_reducer(self) -> "RecoveryContextReducer":
        """The HARD fold+drop reducer the Role injects into the LLM for COMPRESS recovery."""
        return self._recovery_reducer

    async def handle(self, event) -> None:
        """Observer hook — refresh the reconstructable-tool set on a tool change.

        A :class:`ToolsChangedEvent` carries the fresh reconstructable name set
        (post-change), which becomes the new ``compactable`` threaded into every
        subsequent ``Transcript.from_messages``. All other events are ignored
        (this manager also *emits* on the same bus; its own emissions fall
        through here untouched).
        """
        if isinstance(event, ToolsChangedEvent):
            self._compactable = frozenset(event.reconstructable)
        return None

    # ------------------------------------------------------------------
    # Message-store API (the slice of the store the loop depends on)
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        """The live stored history (mutable; backs RoleState.context)."""
        return self._context.messages

    @property
    def model(self) -> str:
        return self._model or getattr(self._llm, "model", None) or "gpt-4"

    def get(self, k: int = 0) -> list[Message]:
        """Return the most-recent ``k`` messages (``k=0`` → all).

        The contract the react loop and think-engine call: ``k`` slices the
        tail, ``0`` returns the whole history.
        """
        if k <= 0:
            return list(self._context.messages)
        return self._context.messages[-k:]

    async def add(self, message: Message) -> None:
        """Append one message to the stored history (old ``Memory.add``).

        Emits ``MessageAppendedEvent`` so the recorder subscriber persists it and
        any other subscriber (renderer, etc.) sees the same stream. No-op emit
        when no bus is wired (standalone/test use).
        """
        if message is None:
            return
        self._context.messages.append(message)
        if self._bus is not None:
            await self._bus.emit(MessageAppendedEvent(message=message))

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
        """Empty the stored history and announce the structural rebuild.

        Emits a single :class:`HistoryEditedEvent` (``reason="clear"``) — the same
        event a user delete fires — so the recorder persists the now-empty list as
        a replay checkpoint AND every history-derived signal (turn-context
        incremental frontiers, the resource side-store) re-derives itself against
        the emptied history. ``/clear`` and delete are the two orthogonal
        history-rebuild paths; both converge on this one event.
        """
        self._context.messages.clear()
        if self._bus is not None:
            await self._bus.emit(HistoryEditedEvent(messages=[], reason="clear"))

    # ------------------------------------------------------------------
    # Direct history editing (user-driven delete of react-units)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_human_prompt(message: Message) -> bool:
        """True iff *message* is a human's own typed prompt (a react-unit anchor).

        A react-unit begins at a human prompt and runs up to (excluding) the next
        one. Only a ``role="user"`` message that is NOT an injected
        ``<system-reminder>`` envelope counts — the per-turn context blocks the
        bus wraps as user messages are part of a turn, never a boundary.
        """
        from mote.common.text import is_system_reminder

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
        the backing history without those messages, and emits a single
        :class:`HistoryEditedEvent` so the recorder persists the pruned list as a
        replay checkpoint (survives restart/resume). Returns the number of
        messages removed; a no-op (empty selection / all-unknown ids) removes
        nothing and emits nothing.
        """
        messages = self._context.messages
        drop = self._react_unit_drop_indices(messages, anchor_ids, self._is_human_prompt)
        if not drop:
            return 0
        kept = [m for idx, m in enumerate(messages) if idx not in drop]
        self._context.messages[:] = kept
        if self._bus is not None:
            await self._bus.emit(HistoryEditedEvent(messages=list(kept), reason="delete"))
        return len(drop)

    # ------------------------------------------------------------------
    # History-level orchestration (microcompact → autocompact)
    # ------------------------------------------------------------------

    async def manage_history(self, *, custom_instructions: str | None = None) -> bool:
        """Reduce the stored history toward the autocompact threshold (SOFT).

        Builds a SOFT :class:`ReductionRequest` (target = the autocompact
        threshold) and hands the segmented history to the :class:`ContextEngine`.
        The engine runs the pipeline cheapest-first: the FREE fold always runs
        opportunistically (its own count gate), the LLM summarize runs only when
        the history is still over target, and the destructive head-drop is *not*
        reachable under SOFT urgency. The engine also owns the PreCompact veto /
        instruction supply and the checkpoint / PostCompact emission.

        Replaces the backing history with the reduced one and returns True iff it
        changed. Safe to call every turn: each strategy is gated and no-ops
        cheaply when its trigger isn't met.
        """
        if not self._context.messages:
            return False

        target = budget.autocompact_threshold(self.model)
        request = ReductionRequest(
            target_tokens=target,
            urgency=Urgency.SOFT,
            reason=ReductionReason.THRESHOLD,
        )
        transcript = Transcript.from_messages(self._context.messages, compactable=self._compactable)
        outcome = await self._engine.reduce(
            transcript,
            request,
            trigger="auto",
            custom_instructions=custom_instructions,
        )
        if not outcome.changed:
            return False
        # Swap the reduced history into the backing context (fold mutates the
        # same Message objects in place; summarize rebuilds [summary] + tail).
        self._context.messages[:] = outcome.transcript.to_messages()
        return True

    def token_state(self):
        """Current token budget snapshot for the stored history (TokenState)."""
        return budget.evaluate(
            self._context.messages,
            self.model,
            autocompact_enabled=self.config.enable_autocompact,
            observed_tokens=self._accountant.observed(),
        )

    def fold_state(self) -> FoldState:
        """Current count-based fold snapshot for the stored history.

        The count-based sibling of ``token_state``: how many foldable
        reconstructable tool results are live versus the trigger at which the
        FREE fold clears the oldest of them. Reuses the reducer's own
        ``active_results`` count (built from the same segmented transcript the
        fold acts on) so a pre-fold warning can never disagree with what the
        fold will actually do.
        """
        active = FoldReducer.active_results(
            Transcript.from_messages(self._context.messages, compactable=self._compactable)
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
        the user prompt is NOT added to the stored history here — only the
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
