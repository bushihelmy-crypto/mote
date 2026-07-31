"""Domain-owned event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List, Optional

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

    task_id: str = ""
    stage: str = ""
    status: str = ""
    detail: str = ""  # rendered, no trailing newline
    #: Execution lineage (``ScopePath``) this ping belongs to. ``()`` for a plain
    #: background-task progress line (today's behavior — folds to a flat
    #: ``TaskProgress`` view event). A non-empty scope whose head is an open
    #: activity routes the ping into that activity's subtree (a per-node update).
    scope: tuple[object, ...] = ()

    name: ClassVar[str] = TASK_PROGRESS


@dataclass
class ActivityStartedEvent:
    """A nested orchestration (a ``run_graph`` graph today; a sub-agent / bg task
    in future) began — the machine-side signal the projector folds into an
    :class:`~mote.product.cli.contracts.view.events.ActivityStarted` ViewEvent.

    ``scope`` identifies the activity (its :class:`~mote.runtime.events.scope.
    ScopePath`); ``topology`` is a neutral pre-computed structure describing the
    declared graph (plain dicts/lists, so this leaf imports nothing from bggraph).
    Purely observational — mirrors *that an activity opened* so a renderer can
    draw its shape before any node runs.
    """

    scope: tuple[object, ...] = ()
    activity_kind: str = ""  # "graph" | "agent" | "task"
    label: str = ""
    topology: Optional[dict[str, Any]] = None

    name: ClassVar[str] = ACTIVITY_STARTED


@dataclass
class ActivityCompletedEvent:
    """A nested orchestration finished — the terminal, **self-sufficient** signal.

    Carries the full outcome read straight off the graph's terminal state
    (``node_states`` + ``outcome`` + ``summary``), so a replayed / resumed
    transcript renders the outcome from this event alone, never reconstructing it
    from the live :class:`TaskProgressEvent` stream (which a replay does not
    have). ``node_states`` is a list of neutral dicts; ``outcome`` is
    ``"success"`` | ``"failed"``. Purely observational.
    """

    scope: tuple[object, ...] = ()
    outcome: str = "success"  # success | failed
    node_states: List[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    name: ClassVar[str] = ACTIVITY_COMPLETED
