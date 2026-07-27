"""One dynamically-loaded capability body tracked for re-projection."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ResourceUnit:
    """A single loaded resource (e.g. a Skill body) held by the ResourceRegistry.

    ``id`` is the stable identity (skill name); a second load with the same id
    replaces the earlier one (last-write-wins), matching the per-skill side-store.
    ``invoked_at`` orders the most-recent-first projection under a token budget.
    """

    id: str
    kind: str
    content: str
    sticky: bool = True
    invoked_at: float = field(default_factory=time.monotonic)
    # Number of post-compaction re-projections this unit has actually taken part
    # in. Used by kinds with a bounded projection lifetime (e.g. a task result the
    # model never consumed): once it exceeds the kind's ``POST_COMPACT_MAX_ROUNDS``
    # cap the registry unloads it. A Skill body sets no cap so it never reaps.
    projection_rounds: int = 0
