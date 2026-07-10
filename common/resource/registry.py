"""ResourceRegistry: process-local side-store of loaded capability bodies.

The registry's ONLY job is post-compaction re-projection. During normal turns a
loaded Skill body already lives in history as its Skill tool-result, and Skill is
not a COMPACTABLE_TOOL, so microcompact never folds it. A body only vanishes when
autocompact discards the head; the registry then re-projects the still-sticky
bodies right after the summary so the model keeps its loaded capabilities.

This mirrors claude-code's ``createSkillAttachmentIfNeeded`` over the
``getInvokedSkillsForAgent`` side-store: most-recent-first, per-unit truncation
keeping the head, whole-unit drop once the total budget is exceeded.

Pure in-memory (not persisted to rollout): resume rebuilds it by scanning the
replayed history for RESOURCE_STICKY messages (see Role.resume_session).
"""
from __future__ import annotations

from metagpt.common.resource.unit import ResourceUnit
from metagpt.common.schema import ResourceMessage
from metagpt.common.utils.prompt_sanitizer import count_tokens, truncate_to_tokens

# cc budget constants (src/services/compact.ts:133-134): each re-projected unit
# is truncated (head-kept) to at most PER_UNIT tokens; units are added
# most-recent-first until the running total would exceed TOTAL, after which the
# remaining (older) units are dropped whole.
POST_COMPACT_MAX_TOKENS_PER_UNIT = 5_000
POST_COMPACT_TOKEN_BUDGET = 25_000


class ResourceRegistry:
    """Holds loaded ResourceUnits and re-projects the sticky ones after compaction."""

    def __init__(self) -> None:
        self._units: dict[str, ResourceUnit] = {}

    def load(self, *, id: str, kind: str, content: str, sticky: bool = True) -> None:
        """Register (or replace, last-write-wins) a loaded resource by id."""
        self._units[id] = ResourceUnit(id=id, kind=kind, content=content, sticky=sticky)

    def unload(self, id: str) -> bool:
        """Stop projecting a resource. Returns True if it was present.

        Unload only removes it from the side-store so future projections skip it;
        it does not touch history already sent.
        """
        return self._units.pop(id, None) is not None

    def get_all(self) -> list[ResourceUnit]:
        """All held units, most-recent-first (newest invoked_at first)."""
        return sorted(self._units.values(), key=lambda u: u.invoked_at, reverse=True)

    def __contains__(self, id: str) -> bool:
        return id in self._units

    def __len__(self) -> int:
        return len(self._units)

    def project(self, model: str = "") -> list[ResourceMessage]:
        """Build the sticky-body messages to re-insert after a compaction summary.

        Most-recent-first, each unit truncated (head kept) to PER_UNIT tokens,
        dropping whole units once the running total would exceed the budget.
        ``model`` is accepted for signature symmetry with the token subsystem; the
        shared approximate tokenizer is model-agnostic here.
        """
        out: list[ResourceMessage] = []
        used = 0
        for unit in self.get_all():
            if not unit.sticky:
                continue
            body = unit.content
            if count_tokens(body) > POST_COMPACT_MAX_TOKENS_PER_UNIT:
                body = truncate_to_tokens(body, POST_COMPACT_MAX_TOKENS_PER_UNIT)
            cost = count_tokens(body)
            if used + cost > POST_COMPACT_TOKEN_BUDGET:
                # Over budget: drop this and every remaining (older) unit.
                break
            used += cost
            out.append(_project_one(unit, body))
        return out


def _project_one(unit: ResourceUnit, body: str) -> ResourceMessage:
    header = f"# {unit.kind.capitalize()}: {unit.id} (loaded earlier, preserved across compaction)"
    return ResourceMessage(
        f"{header}\n\n{body}",
        resource_id=unit.id,
        resource_kind=unit.kind,
        sticky=True,
    )


__all__ = [
    "ResourceRegistry",
    "POST_COMPACT_MAX_TOKENS_PER_UNIT",
    "POST_COMPACT_TOKEN_BUDGET",
]
