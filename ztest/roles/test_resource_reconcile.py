#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Direct history-edit projection + RoleSessionManager.reconcile_resources.

A ``/clear`` or user delete emits :class:`HistoryEditedEvent` carrying the
surviving message list. The reconciler must re-derive the in-memory
ResourceRegistry from those survivors so a body whose message was pruned drops
out, while revealed-tool descriptions re-seed from the durable ``RoleState``
reveal set (history-independent by design). Compaction (``PostCompactEvent``)
must NOT reset the registry — it re-projects sticky bodies through the pull
provider.
"""

from __future__ import annotations

import pytest

from mote.contracts.constants.messages import RESOURCE_ID, RESOURCE_KIND, RESOURCE_STICKY
from mote.contracts.schema import LLMCallContext, Message
from mote.runtime.agent.session_manager import RoleSessionManager
from mote.runtime.context import ContextManager
from mote.runtime.resources.registry import ResourceRegistry


class _FakeExecutor:
    """Stands in for the tool executor's ``describe_deferred_tools``."""

    def __init__(self, descriptions: dict[str, str] | None = None):
        self._descriptions = descriptions or {}

    def describe_deferred_tools(self, names) -> dict[str, str]:
        return {n: self._descriptions[n] for n in names if n in self._descriptions}


class _FakeState:
    def __init__(self, revealed: set[str] | None = None):
        self.revealed_tools = revealed or set()


class _FakeRole:
    """Minimal role surface the manager touches for reconciliation."""

    def __init__(self, *, revealed: set[str] | None = None, descriptions: dict[str, str] | None = None):
        self.resource_registry = ResourceRegistry()
        self.state = _FakeState(revealed)
        self.executor = _FakeExecutor(descriptions)


def _resource_msg(rid: str, kind: str, body: str) -> Message:
    """A history message carrying the sticky-resource markers in metadata."""
    m = Message(role="user", content=body)
    m.metadata[RESOURCE_ID] = rid
    m.metadata[RESOURCE_KIND] = kind
    m.metadata[RESOURCE_STICKY] = True
    return m


def _plain_msg(body: str) -> Message:
    return Message(role="user", content=body)


# ---------------------------------------------------------------------------
# reconcile_resources — the shared seam
# ---------------------------------------------------------------------------
def test_reconcile_reseeds_surviving_sticky_bodies():
    role = _FakeRole()
    mgr = RoleSessionManager(role)
    # Pre-seed a stale unit that is NOT in the surviving history — it must drop.
    role.resource_registry.load(id="gone", kind="skill", content="stale", sticky=True)

    survivors = [_plain_msg("q"), _resource_msg("skill-a", "skill", "BODY A")]
    mgr.reconcile_resources(survivors)

    assert "gone" not in role.resource_registry
    assert "skill-a" in role.resource_registry
    assert len(role.resource_registry) == 1


def test_reconcile_drops_body_whose_message_was_pruned():
    role = _FakeRole()
    mgr = RoleSessionManager(role)
    # First reconcile with the body present.
    mgr.reconcile_resources([_resource_msg("skill-a", "skill", "BODY A")])
    assert "skill-a" in role.resource_registry
    # A delete pruned the message carrying it → next reconcile has no survivor.
    mgr.reconcile_resources([_plain_msg("only chatter")])
    assert "skill-a" not in role.resource_registry
    assert len(role.resource_registry) == 0


def test_reconcile_reseeds_revealed_tools_from_state_not_history():
    # Revealed tools are history-independent: even with an empty surviving
    # history, a revealed tool's description re-seeds from RoleState + catalog.
    role = _FakeRole(revealed={"WebSearch"}, descriptions={"WebSearch": "full web search description"})
    mgr = RoleSessionManager(role)

    mgr.reconcile_resources([_plain_msg("no resource messages at all")])

    assert "WebSearch" in role.resource_registry


def test_reconcile_empty_history_empties_registry():
    role = _FakeRole()
    mgr = RoleSessionManager(role)
    role.resource_registry.load(id="x", kind="skill", content="b", sticky=True)

    mgr.reconcile_resources([])  # /clear survivors == []

    assert len(role.resource_registry) == 0


# ---------------------------------------------------------------------------
# ContextManager — explicit post-commit projection
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_context_clear_reconciles_resources_without_telemetry():
    role = _FakeRole()
    mgr = RoleSessionManager(role)
    role.resource_registry.load(id="stale", kind="skill", content="old", sticky=True)
    survivors = [_resource_msg("skill-a", "skill", "BODY A")]
    context = LLMCallContext(messages=survivors)
    manager = ContextManager(
        context,
        history_edited=lambda event: mgr.reconcile_resources(event.remaining_messages),
    )

    mgr.reconcile_resources(survivors)
    await manager.clear()

    assert "stale" not in role.resource_registry
    assert "skill-a" not in role.resource_registry
    assert len(role.resource_registry) == 0
