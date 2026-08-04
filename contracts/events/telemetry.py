"""Domain-owned event contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, List, Optional

from mote.contracts.events.envelope import JsonValue

if TYPE_CHECKING:
    pass

DIAGNOSTICS = "diagnostics"

RECOVERY = "recovery"

RESOURCE_REPORT = "resource_report"

SPAN_START = "span_start"

SPAN_END = "span_end"


class RecoveryPhase(StrEnum):
    RECOVERED = "recovered"
    GIVE_UP = "give_up"


class SpanStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


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

    phase: RecoveryPhase = RecoveryPhase.RECOVERED
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
    value: JsonValue = None
    extra: Optional[Mapping[str, JsonValue]] = None
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
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)

    name: ClassVar[str] = SPAN_START


@dataclass
class SpanEndEvent:
    """A trace span closed (paired with :class:`SpanStartEvent` by ``span_id``)."""

    span_id: str = ""
    trace_id: str = ""
    status: SpanStatus = SpanStatus.OK
    error: str = ""
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)

    name: ClassVar[str] = SPAN_END
