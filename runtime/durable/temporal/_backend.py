"""Temporal durable backend with stable, serializable activity identities."""

from __future__ import annotations

from mote.contracts.config.tool import TemporalConfig
from mote.runtime.durable.temporal._activities import StepActivities, TemporalActivityCatalog
from mote.runtime.ledger import RunJournal


class TemporalBackend:
    """Tier-2 durable backend dispatching steps as Temporal activities.

    Temporal workflows dispatch only handlers installed in the frozen
    :class:`TemporalActivityCatalog` during Product application activation.
    """

    def __init__(
        self,
        config: TemporalConfig,
        journal: RunJournal,
        *,
        activity_catalog: TemporalActivityCatalog | None = None,
    ) -> None:
        self._config = config
        self._journal = journal
        self._activity_catalog = activity_catalog or TemporalActivityCatalog()
        self._activity_catalog.freeze()
        self._activities = StepActivities(self._activity_catalog)

    @property
    def journal(self) -> RunJournal:
        """The shared run journal both tiers memoize steps into."""
        return self._journal

    @property
    def config(self) -> TemporalConfig:
        """The Temporal wiring config (server/namespace/task-queue + seam policy)."""
        return self._config

    @property
    def temporal_activities(self) -> list:
        """The activity functions a worker must register for this backend."""
        return [self._activities.run_step_activity]


__all__ = ["TemporalBackend"]
