"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from mote.contracts.activity import ActivityKind, ActivityNodeState, ActivityOutcome, ActivityTopology
from mote.contracts.task.progress import (
    ActivityProgressEvent,
    ActivityProgressIdentity,
    BackgroundTaskProgressEvent,
    DurableWorkflowRunProgress,
    ProgressEvent,
    ProgressPhase,
)

if TYPE_CHECKING:
    pass

TASK_PROGRESS = "task_progress"

ACTIVITY_STARTED = "activity_started"

ACTIVITY_COMPLETED = "activity_completed"


@dataclass
class TaskProgressEvent:
    """A background task reported a progress line (already rendered).

    Emitted by the bggraph progress writer alongside the disk append (the
    :class:`TaskAttachmentGenerator` disk output stays the source of truth);
    this lets subscribers mirror live progress without polling the store.
    """

    progress: ProgressEvent
    #: Execution lineage (``ScopePath``) this ping belongs to. ``()`` for a plain
    #: background-task progress line (today's behavior — folds to a flat
    #: ``TaskProgress`` view event). A non-empty scope whose head is an open
    #: activity routes the ping into that activity's subtree (a per-node update).
    scope: tuple[object, ...] = ()

    name: ClassVar[str] = TASK_PROGRESS

    @classmethod
    def activity(
        cls,
        *,
        run_id: str,
        definition_id: str,
        stage: str,
        phase: ProgressPhase,
        detail: str | None = None,
        scope: tuple[object, ...] = (),
    ) -> "TaskProgressEvent":
        return cls(
            ActivityProgressEvent(
                ActivityProgressIdentity(run_id, definition_id),
                stage,
                phase,
                detail,
            ),
            scope,
        )

    @property
    def stage(self) -> str:
        if isinstance(self.progress, BackgroundTaskProgressEvent):
            return self.progress.stage
        if isinstance(self.progress, DurableWorkflowRunProgress):
            return self.progress.node_id
        return self.progress.stage

    @property
    def status(self) -> str:
        if isinstance(self.progress, BackgroundTaskProgressEvent):
            return self.progress.phase.value
        return self.progress.phase.value

    @property
    def detail(self) -> str:
        if isinstance(self.progress, BackgroundTaskProgressEvent):
            return self.progress.detail or ""
        return self.progress.detail or ""


@dataclass
class ActivityStartedEvent:
    """A nested orchestration (a ``run_graph`` graph today; a sub-agent / bg task
    in future) began — the machine-side signal the projector folds into an
    :class:`~mote.product.cli.contracts.view.events.ActivityStarted` ViewEvent.

    ``scope`` identifies the activity; ``topology`` is the canonical neutral
    contract describing the declared graph without importing its implementation.
    Purely observational — mirrors *that an activity opened* so a renderer can
    draw its shape before any node runs.
    """

    scope: tuple[object, ...] = ()
    activity_kind: ActivityKind = ActivityKind.GRAPH
    label: str = ""
    topology: ActivityTopology | None = None

    name: ClassVar[str] = ACTIVITY_STARTED

    def __post_init__(self) -> None:
        if not isinstance(self.activity_kind, ActivityKind):
            raise TypeError("activity_kind must be ActivityKind")
        if self.topology is not None and not isinstance(self.topology, ActivityTopology):
            raise TypeError("activity topology must be ActivityTopology or None")


@dataclass
class ActivityCompletedEvent:
    """A nested orchestration finished — the terminal, **self-sufficient** signal.

    Carries the full outcome read straight off the graph's terminal state
    (``node_states`` + ``outcome`` + ``summary``), so a replayed / resumed
    transcript renders the outcome from this event alone, never reconstructing it
    from the live :class:`TaskProgressEvent` stream (which a replay does not
    have). ``node_states`` and ``outcome`` use the canonical activity DTOs.
    Purely observational.
    """

    scope: tuple[object, ...] = ()
    outcome: ActivityOutcome = ActivityOutcome.SUCCESS
    node_states: tuple[ActivityNodeState, ...] = ()
    summary: str = ""

    name: ClassVar[str] = ACTIVITY_COMPLETED

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ActivityOutcome):
            raise TypeError("activity outcome must be ActivityOutcome")
        if not isinstance(self.node_states, tuple) or not all(
            isinstance(state, ActivityNodeState) for state in self.node_states
        ):
            raise TypeError("activity node_states must be ActivityNodeState tuple")
