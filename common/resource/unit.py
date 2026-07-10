"""ResourceUnit: one dynamically-loaded capability body tracked for re-projection."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ResourceUnit:
    """A single loaded resource (e.g. a Skill body) held by the ResourceRegistry.

    ``id`` is the stable identity (skill name); a second load with the same id
    replaces the earlier one (last-write-wins), matching cc's per-skill side-store.
    ``invoked_at`` orders the most-recent-first projection under a token budget.
    """

    id: str
    kind: str
    content: str
    sticky: bool = True
    invoked_at: float = field(default_factory=time.monotonic)
