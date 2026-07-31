from typing import Protocol

from mote.contracts.inference.persisted_event import PersistedLifecycleEvent


class LifecycleEventStore(Protocol):
    async def append_event(self, event: PersistedLifecycleEvent) -> PersistedLifecycleEvent:
        ...

    async def read_events(
        self, execution_id: str, *, after_sequence: int, limit: int = 256
    ) -> tuple[PersistedLifecycleEvent, ...]:
        ...
