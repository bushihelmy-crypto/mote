"""CostNode — a per-agent node in the fleet cost mirror tree.

Each spawned agent gets one :class:`CostNode` that wraps the agent's *own*
:class:`CostTracker` (the node's self-bucket) and a parent pointer mirroring the
spawn lineage. A tracker keeps recording only its own usage (exactly the fresh-
tracker behavior), so per-node attribution is preserved; subtree aggregates are
computed on demand by walking the children.

Pointing the child's tracker at a parent's shared tracker would lose per-node
attribution; the mirror tree avoids that. It is naturally mode-safe: a FIREWORKS
/ FREE child builds its own differently-priced tracker in
``Context._select_cost_manager`` and that tracker is adopted as the node bucket
unchanged — the parent's standard tracker is never polluted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from mote.router.cost.tracker import CostTracker
from mote.router.cost.usage import TokenUsage


@dataclass
class CostNode:
    """One agent's cost node: its own tracker + lineage pointers."""

    tracker: CostTracker
    parent: Optional["CostNode"] = None
    children: List["CostNode"] = field(default_factory=list)
    agent_path: Optional[str] = None
    agent_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Subtree aggregates (computed on demand by walking children)
    # ------------------------------------------------------------------
    def subtree_cost(self) -> float:
        """Total USD cost of this node plus every descendant."""
        return self.tracker.total_cost + sum(child.subtree_cost() for child in self.children)

    def subtree_usage(self) -> TokenUsage:
        """Element-wise token-usage sum over this node and all descendants."""
        total = TokenUsage()
        total.add(self.tracker.total_token_usage())
        for child in self.children:
            total.add(child.subtree_usage())
        return total

    def subtree_has_estimated(self) -> bool:
        """True if any node in the subtree priced an unknown model (low confidence)."""
        if self.tracker.has_unknown_model_cost:
            return True
        return any(child.subtree_has_estimated() for child in self.children)

    def walk(self) -> Iterator["CostNode"]:
        """Pre-order traversal of this node and its descendants (for reporting)."""
        yield self
        for child in self.children:
            yield from child.walk()


__all__ = ["CostNode"]
