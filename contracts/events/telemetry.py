"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List, Optional

if TYPE_CHECKING:
    pass

DIAGNOSTICS = "diagnostics"

RECOVERY = "recovery"

RESOURCE_REPORT = "resource_report"

SPAN_START = "span_start"

SPAN_END = "span_end"

JOURNAL = "journal"


@dataclass
class DiagnosticsEvent:
    """Language-server diagnostics changed after a file sync.

    Emitted by the :class:`LspService` once a synced edit yields a *changed*
    diagnostic set, carrying a pre-rendered context ``block`` and the affected
    ``paths``. Purely an observation: the diagnostics buffer accumulates the
    block for next-turn context injection; future subscribers (a status line,
    an error counter, an auto-fix agent) can react to the same signal without
    the producer naming them. The output counterpart of
    :class:`FileMutatedEvent` (the input that triggers the sync).
    """

    block: str = ""
    paths: List[str] = field(default_factory=list)

    name: ClassVar[str] = DIAGNOSTICS


@dataclass
class RecoveryEvent:
    """A retry/recovery loop attempt resolved (recovered or gave up).

    Emitted by the generic :class:`RecoveryRunner` so any frontend/logger can
    observe the retry/rotate/fallback/compress decisions that otherwise stay
    invisible inside the loop. Purely an observation — the runner's own control
    flow (the eventual re-raise / retry) is the real source of truth; this just
    mirrors *what the loop decided*.
    """

    phase: str = "recovered"  # recovered | give_up
    action: str = ""  # RecoveryAction.value (retry / rotate_credential / ...)
    attempt: int = 0
    error_type: str = ""
    error: str = ""

    name: ClassVar[str] = RECOVERY


@dataclass
class ResourceReportEvent:
    """A non-streaming resource observation a reporter pushed to the UI.

    Emitted by :class:`ResourceReporter` in place of its old direct HTTP POST;
    the :class:`ReporterSubscriber` (when wired) reconstructs the payload and
    POSTs it. ``name_`` is suffixed to avoid clashing with the ``name`` ClassVar
    discriminator every event carries.
    """

    block: str = ""
    name_: str = ""
    value: Any = None
    extra: Optional[dict] = None
    uuid: str = ""
    role: Optional[str] = None

    name: ClassVar[str] = RESOURCE_REPORT


@dataclass
class SpanStartEvent:
    """A trace span opened (framework-native instrumentation primitive).

    Carries explicit trace structure — ``span_id`` / ``parent_span_id`` /
    ``trace_id`` — so the trace tree is rebuilt downstream from these IDs, not
    from any backend's ambient context. Emitted by the ``span`` contextmanager
    (:mod:`~mote.runtime.events.trace`). The instance field is ``label`` (the
    human name) — ``name`` is the reserved discriminator ClassVar.
    """

    span_id: str = ""
    parent_span_id: Optional[str] = None
    trace_id: str = ""
    label: str = ""
    attributes: dict = field(default_factory=dict)

    name: ClassVar[str] = SPAN_START


@dataclass
class SpanEndEvent:
    """A trace span closed (paired with :class:`SpanStartEvent` by ``span_id``)."""

    span_id: str = ""
    trace_id: str = ""
    status: str = "ok"  # ok | error
    error: str = ""
    attributes: dict = field(default_factory=dict)

    name: ClassVar[str] = SPAN_END


@dataclass
class JournalEvent:
    """A durable run-journal step crossed a lifecycle boundary.

    Emitted (observation-only) by the durable-execution seams
    (:class:`~mote.runtime.durable.inference_journal.InferenceJournal` think steps, durable
    timers, and the EXTERNAL/LOCAL tool ledger) whenever a step is started,
    completed, failed, or reaped, so a frontend/logger can watch the otherwise
    invisible crash-resume bookkeeping (which thinks were memoized, which
    dangling calls were healed, how the long-session journal stays bounded).

    Purely a mirror: the journal's own on-disk log is the source of truth; this
    just announces *that* a record moved. ``kind`` is the step class
    (``think`` / ``tool`` / ``timer``); ``phase`` is the lifecycle transition
    (``started`` / ``completed`` / ``failed`` / ``reaped``); ``effect`` is the
    step's side-effect class (``pure`` / ``local`` / ``external``); ``step_id``
    is the journal's self-anchored key.
    """

    step_id: str = ""
    kind: str = ""  # think | tool | timer
    phase: str = ""  # started | completed | failed | reaped
    effect: str = ""  # pure | local | external
    seq: int = 0

    name: ClassVar[str] = JOURNAL
