"""Subscriber contracts — the two planes the :class:`EventBus` fans out to.

The bus dispatches every event in **two phases**, and which phase a subscriber
runs in is a *declared*, nominal property of the subscriber — it inherits the ABC
for its plane — not something the bus sniffs by capability. This is the contract
core: violations fail loud at class-definition / construction time, never degrade
silently at runtime.

* :class:`ControlSubscriber` (phase 1) — awaited inline. It declares which events
  it ``handles`` (by name) so the bus routes each event only to its interested
  subscribers, and a :class:`ControlStage` that orders the few subscribers that
  share a bucket. Its ``handle_control`` may return a :class:`ControlOutcome`
  which the bus *folds* (via the outcome's own ``merge``) and hands back to the
  emitter (veto / mutate args / inject context / stop). Because only this plane
  folds, influence over the host is confined to subscribers registered here.

* :class:`ObservationSubscriber` (phase 2) — fan-out sinks (recorder, renderer,
  logger, tracing). ``handle`` returns nothing the bus reads: an observer can
  **never** veto, by construction. Each declares a :data:`DeliveryPolicy`:

    - ``MIRROR`` (default): best-effort, isolated, time-boxed — a slow or failing
      mirror is dropped+counted and never stalls or breaks the turn.
    - ``DURABLE``: the rollout recorder — failures are surfaced (logged loud +
      counted on the bus), not silently swallowed, because a missing rollout
      record is real data loss.

Optional capabilities are declared with the same nominal discipline — an observer
that also handles sync events inherits :class:`SyncObserver`; one that also emits
back onto its own bus inherits :class:`BusAware`. Each mixin carries an
``@abstractmethod`` so a *misnamed* ``handle_sync``/``on_subscribed`` fails at
construction rather than being silently ignored at dispatch.

The bus classifies a subscriber by ``isinstance`` against these ABCs — a
non-subscriber raises ``TypeError`` at ``subscribe`` — so the plane is a
compile-time-ish guarantee, and config attributes are read as *typed class
attributes* (a typo'd ``deliverY`` is not a silent fall-back to a default, it is
just not the attribute the bus reads).

Leaf module: imports only ``abc``/``inspect``/``typing`` + the enum tiers below.
"""

from __future__ import annotations

import abc
import inspect
from enum import IntEnum
from typing import Any, ClassVar, Generic, Literal, Optional, TypeVar

#: How the observation plane delivers an event to a subscriber.
DeliveryPolicy = Literal["mirror", "durable"]
MIRROR: DeliveryPolicy = "mirror"
DURABLE: DeliveryPolicy = "durable"

#: What the control plane does when a subscriber crashes or times out. The
#: control-plane analogue of :data:`DeliveryPolicy`:
#:   - ``open`` (default): a failed subscriber is treated as *no influence* — its
#:     outcome is dropped and the turn proceeds. Correct for advisory subscribers
#:     (the hook layer) where a broken hook must not brick the agent.
#:   - ``closed``: a failed subscriber *denies* the call. Correct for
#:     security-critical gates (the permission engine) where "we could not decide"
#:     must fail safe, not fail permissive.
FailMode = Literal["open", "closed"]
FAIL_OPEN: FailMode = "open"
FAIL_CLOSED: FailMode = "closed"


class ControlStage(IntEnum):
    """Ordering within one control bucket — a *named* contract, not a magic int.

    Control subscribers are routed by event name into per-event buckets; the vast
    majority of buckets hold a single subscriber, for which the stage is
    immaterial. The only shared bucket today is ``PreToolUse`` — the hook rewrites
    args (:attr:`REWRITE`), then the permission gate evaluates the *already
    rewritten* call (:attr:`GATE`) — so ``REWRITE < GATE`` encodes that causal
    dependency by name rather than by comparing bare integers. A future ordered
    bucket adds a named member here (one line, with meaning) instead of guessing a
    non-clashing number.
    """

    REWRITE = 1  # mutate the call/args (must run before anything that judges it)
    GATE = 2  # allow/deny the (possibly rewritten) call — the final say


#: Default stage for a control subscriber that does not declare one. Gate is the
#: safe default: an undeclared subscriber judges *after* any declared rewriter.
DEFAULT_STAGE = ControlStage.GATE


class ObserverPriority(IntEnum):
    """Fan-out ordering for the observation plane — a *named* contract.

    The observation-plane analogue of :class:`ControlStage`: observers run in
    ascending order of this value. Unlike the control plane (where order encodes
    a hard causal dependency — rewrite must precede gate), observer ordering is
    only *cosmetic sequencing* — no observer folds anything, so a "wrong" order
    can at most change the interleaving of side effects (log lines, screen
    paints), never correctness. It is named anyway so a new observer picks a tier
    *with meaning* ("I persist, so I sit at ``PERSIST``") instead of guessing a
    non-clashing integer — the exact ambiguity that let two subscribers silently
    collide on a bare ``90`` before this enum existed.

    Several observers may legitimately share a tier (e.g. the CLI renderer and
    the LSP doc-sync both at :attr:`LIVE`); ties keep registration order, which
    is fine precisely because observer order is non-semantic. The tiers ascend
    from *live reactions* through *persistence/observability* down to *pure
    bookkeeping that just needs to land last*:

    Note a subscriber that is *also* a turn-context source (the LSP
    ``DiagnosticsBuffer``) orders by :class:`~mote.common.interface.turn_context.TurnContextPriority`
    instead — its turn-context render order is the authoritative meaning of its
    ``priority``, and its observer-dispatch position is immaterial (it only
    accumulates, folding nothing).
    """

    LIVE = 50  # live reactions: interactive rendering / LSP document sync
    STREAM = 60  # mirror streamed LLM tokens to a reporter queue
    REPORT = 70  # POST non-streaming resource observations to a UI endpoint
    PERSIST = 80  # durable rollout recorder (after any control veto is folded)
    TRACE = 85  # export spans / generations to a tracer backend
    LOG = 90  # one concise semantic log line per event ("what finally happened")
    BOOKKEEPING = 95  # pure internal bookkeeping (file-watch self-write notes)


#: Default fan-out priority for an observer that does not declare one. Mid-tier
#: (``LIVE``) so an undeclared observer sits with the live sinks rather than
#: pretending to be a durable/persist-class consumer.
DEFAULT_PRIORITY = ObserverPriority.LIVE


#: Self-type of a concrete outcome — the CRTP parameter that binds ``merge`` to
#: *this* outcome type so folding two outcomes of *different* events is a static
#: error, not a runtime ``isinstance`` catch. ``ToolCallOutcome`` subclasses
#: ``ControlOutcome["ToolCallOutcome"]``, so its ``merge`` takes/returns exactly a
#: ``ToolCallOutcome``. (A plain ``Self`` parameter reads as a covariant override
#: and pyright rejects it; CRTP is the checker-accepted encoding.)
_TOutcome = TypeVar("_TOutcome", bound="ControlOutcome")


class ControlOutcome(abc.ABC, Generic[_TOutcome]):
    """The typed influence a control subscriber folds back onto the host.

    Each control event has its own concrete outcome (see
    ``common/events/outcomes.py``); the bus drives them all generically through
    just these three members, so a new event's outcome plugs in without touching
    the bus:

    * ``is_blocking`` — the outcome short-circuits the rest of the bucket.
    * ``merge`` — fold two outcomes *of the same event* into one.
    * ``rebind`` — thread a rewrite forward so the next subscriber sees it,
      stamped with ``by`` = the rewriting subscriber's name (provenance).

    A nominal ABC (not a structural Protocol): a new outcome that forgets
    ``is_blocking``/``merge`` cannot be instantiated. It is **CRTP-generic** in its
    own concrete type (``_TOutcome``) so ``merge`` is typed *same-event only* —
    ``tool_outcome.merge(spawn_outcome)`` is a compile-time error, not a runtime
    surprise. ``rebind`` is *concrete* here — the identity default (rewrite
    nothing) — so a non-rewriting outcome is inert for free; only the two
    rewriting outcomes override it.
    """

    @property
    @abc.abstractmethod
    def is_blocking(self) -> bool:
        """True when this outcome halts the action and short-circuits the bucket."""
        ...

    @abc.abstractmethod
    def merge(self, other: _TOutcome) -> _TOutcome:
        """Fold ``other`` (same event type) into ``self``, returning the result."""
        ...

    def rebind(self, event: Any, *, by: str = "") -> Any:
        """Return ``event`` with this outcome's rewrite threaded in (or unchanged).

        Identity by default — the common case (a deny, a stop, a context injection
        mutates no event field). ``by`` is the name of the subscriber that
        produced this outcome, stamped by the bus at the single pairing point so a
        rewrite records *who* changed the event. Only the two rewriting outcomes
        (``ToolCallOutcome``, ``ToolResultOutcome``) override this.
        """
        return event


class ControlSubscriber(abc.ABC):
    """Phase-1 subscriber: may fold a :class:`ControlOutcome` to influence the host.

    Declares, as *typed class attributes* the bus reads directly (no ``getattr``
    fall-back that a typo could silently defeat):

    * ``handles`` — the event *names* (``event.name`` discriminators) it consumes.
      The bus routes each event only to the subscribers that handle it, so a
      subscriber is never even invoked for events it ignores. **Required**
      (non-empty) — enforced at class-definition time.
    * ``stage`` — its :class:`ControlStage` within a shared bucket; single-subscriber
      buckets keep the default (:data:`DEFAULT_STAGE`).
    * ``name`` — a stable label the bus stamps onto any rewrite this subscriber
      produces (rewrite provenance — *who* changed the event); defaults to the
      class name at the bus.
    * ``fail_mode`` — only security-critical gates opt into ``FAIL_CLOSED``. A
      fail-closed subscriber MUST also define ``on_failure(reason) -> ControlOutcome``
      so the bus can synthesize the correct typed deny when the subscriber itself
      crashes/times out — enforced at class-definition time.
    """

    handles: ClassVar[tuple[str, ...]] = ()
    stage: ClassVar[ControlStage] = DEFAULT_STAGE
    fail_mode: ClassVar[FailMode] = FAIL_OPEN
    name: ClassVar[str] = ""

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        # Only validate concrete subclasses — an intermediate ABC (still carrying
        # an abstractmethod) is not yet a usable subscriber and need not declare
        # `handles`/`on_failure`.
        if inspect.isabstract(cls):
            return
        if not cls.handles:
            raise TypeError(f"{cls.__name__} must declare a non-empty `handles` tuple of event names")
        if cls.fail_mode == FAIL_CLOSED and not callable(getattr(cls, "on_failure", None)):
            raise TypeError(
                f"fail-closed {cls.__name__} must define on_failure(reason) so the bus can " "synthesize its typed deny"
            )

    @abc.abstractmethod
    async def handle_control(self, event) -> "Optional[ControlOutcome]":
        """Handle a control event; return a typed influence or ``None``."""
        ...


class ObservationSubscriber(abc.ABC):
    """Phase-2 fan-out sink: consumes events, never influences the host.

    Typed class attributes the bus reads directly:

    * ``priority`` — an :class:`ObserverPriority` tier ordering the fan-out; it is
      cosmetic sequencing, never correctness (no observer folds).
    * ``delivery`` — its :data:`DeliveryPolicy`; only the durable recorder opts
      into ``DURABLE``.
    """

    priority: ClassVar[int] = DEFAULT_PRIORITY
    delivery: ClassVar[DeliveryPolicy] = MIRROR

    @abc.abstractmethod
    async def handle(self, event) -> None:
        """Consume one event. The return value is structurally ignored."""
        ...


class SyncObserver(abc.ABC):
    """Mixin: an observer that also consumes *synchronous* observation events.

    Received via :meth:`EventBus.emit_sync` — for observation events raised from
    synchronous call sites (e.g. a tool capturing a file snapshot before writing).
    The ``@abstractmethod`` means a misnamed ``handle_sync`` fails at construction
    instead of being silently skipped at dispatch.
    """

    @abc.abstractmethod
    def handle_sync(self, event) -> None:
        """Consume one event synchronously. The return value is ignored."""
        ...


class BusAware(abc.ABC):
    """Mixin: a subscriber that also *emits* back onto its own bus.

    ``on_subscribed(bus)`` is an optional lifecycle hook invoked once on
    registration, handing the subscriber its own bus handle. This generalizes the
    observer-that-also-emits pattern (e.g. the LSP service consumes FileMutated
    and re-emits Diagnostics) so the host need not special-case a back-reference —
    the subscriber declares the capability and the bus wires it, keeping the bus a
    leaf. The ``@abstractmethod`` means a misnamed ``on_subscribed`` fails at
    construction.
    """

    @abc.abstractmethod
    def on_subscribed(self, bus) -> None:
        """Receive the bus handle once, at registration."""
        ...


__all__ = [
    "DeliveryPolicy",
    "MIRROR",
    "DURABLE",
    "FailMode",
    "FAIL_OPEN",
    "FAIL_CLOSED",
    "ControlStage",
    "DEFAULT_STAGE",
    "ObserverPriority",
    "DEFAULT_PRIORITY",
    "ControlOutcome",
    "ControlSubscriber",
    "ObservationSubscriber",
    "SyncObserver",
    "BusAware",
]
