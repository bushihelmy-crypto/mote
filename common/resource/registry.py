"""ResourceRegistry: process-local side-store of loaded capability bodies.

The registry's ONLY job is post-compaction re-projection. During normal turns a
loaded Skill body already lives in history as its Skill tool-result, and Skill is
not a COMPACTABLE_TOOL, so microcompact never folds it. A body only vanishes when
autocompact discards the head; the registry then re-projects the still-sticky
bodies right after the summary so the model keeps its loaded capabilities.

This handles skill attachment over the invoked-skills side-store:
most-recent-first, per-unit truncation keeping the head, whole-unit drop once
the total budget is exceeded.

Pure in-memory (not persisted to rollout): resume rebuilds it by scanning the
replayed history for RESOURCE_STICKY messages (see Role.resume_session).
"""
from __future__ import annotations

from mote.common.resource.unit import ResourceUnit
from mote.common.schema import ResourceMessage
from mote.common.utils.prompt_sanitizer import count_tokens, truncate_to_tokens

# budget constants: each re-projected unit
# is truncated (head-kept) to at most PER_UNIT tokens; units are added
# most-recent-first until the running total would exceed TOTAL, after which the
# remaining (older) units are dropped whole.
POST_COMPACT_MAX_TOKENS_PER_UNIT = 5_000
POST_COMPACT_TOKEN_BUDGET = 25_000

# Per-kind sub-budgets (tokens) layered *under* the global budget: a kind listed
# here may not consume more than its own cap across a single projection, so a
# flood of one kind (e.g. many background task results) can never starve another
# (e.g. loaded Skill bodies). A kind absent from this map has no sub-cap — it is
# bounded only by the global budget, preserving the original skill behavior.
POST_COMPACT_PER_KIND_BUDGET: dict[str, int] = {"task_result": 8_000}

# Per-kind projection-lifetime caps: a unit of this kind is unloaded once it has
# been re-projected more than ``rounds`` times without being consumed — the
# round-based half of the double-safety recycle (the other half is explicit
# ``unload`` on consume). A kind absent from this map is never reaped by round
# count (a Skill body sticks until explicitly unloaded).
POST_COMPACT_MAX_ROUNDS: dict[str, int] = {"task_result": 6}


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

        Two budget layers apply: a per-kind sub-cap
        (``POST_COMPACT_PER_KIND_BUDGET``) that a kind may not exceed — over it,
        the unit is *skipped* and scanning continues (a per-kind flood cannot
        starve other kinds) — and the global ``POST_COMPACT_TOKEN_BUDGET`` that
        *breaks* the scan (older units dropped whole). Every unit actually
        projected has its ``projection_rounds`` bumped; kinds with a
        ``POST_COMPACT_MAX_ROUNDS`` cap are unloaded once they exceed it, so an
        unconsumed task result recycles itself after a bounded number of turns.
        """
        out: list[ResourceMessage] = []
        used = 0
        per_kind_used: dict[str, int] = {}
        projected_ids: list[str] = []
        for unit in self.get_all():
            if not unit.sticky:
                continue
            body = unit.content
            if count_tokens(body) > POST_COMPACT_MAX_TOKENS_PER_UNIT:
                body = truncate_to_tokens(body, POST_COMPACT_MAX_TOKENS_PER_UNIT)
            cost = count_tokens(body)
            sub_cap = POST_COMPACT_PER_KIND_BUDGET.get(unit.kind)
            if sub_cap is not None and per_kind_used.get(unit.kind, 0) + cost > sub_cap:
                # This kind is over its own sub-budget: skip THIS unit but keep
                # scanning so other kinds still project (no cross-kind starvation).
                continue
            if used + cost > POST_COMPACT_TOKEN_BUDGET:
                # Over the global budget: drop this and every remaining (older) unit.
                break
            used += cost
            if sub_cap is not None:
                per_kind_used[unit.kind] = per_kind_used.get(unit.kind, 0) + cost
            unit.projection_rounds += 1
            projected_ids.append(unit.id)
            out.append(_project_one(unit, body))
        # Round-based reap: a projected unit whose kind has a max-rounds cap and
        # has now exceeded it is unloaded so it recycles itself after a bounded
        # number of unconsumed projections.
        for uid in projected_ids:
            unit = self._units.get(uid)
            if unit is None:
                continue
            cap = POST_COMPACT_MAX_ROUNDS.get(unit.kind)
            if cap is not None and unit.projection_rounds > cap:
                self._units.pop(uid, None)
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
    "POST_COMPACT_PER_KIND_BUDGET",
    "POST_COMPACT_MAX_ROUNDS",
]
