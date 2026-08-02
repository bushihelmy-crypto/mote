#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentRegistry — ported from registry_tests.rs."""

import uuid

import pytest

from mote.contracts.agent.errors import AgentPathExists
from mote.orchestration.agents.identity.path import AgentPath
from mote.orchestration.agents.identity.registry import (
    AgentMetadata,
    AgentRegistry,
    exceeds_agent_spawn_depth_limit,
    format_agent_nickname,
    next_agent_spawn_depth,
)


def new_id() -> str:
    return uuid.uuid4().hex


def agent_metadata(thread_id: str) -> AgentMetadata:
    return AgentMetadata(agent_id=thread_id)


def test_format_agent_nickname_adds_ordinals_after_reset():
    assert format_agent_nickname("Plato", 0) == "Plato"
    assert format_agent_nickname("Plato", 1) == "Plato the 2nd"
    assert format_agent_nickname("Plato", 2) == "Plato the 3rd"
    assert format_agent_nickname("Plato", 10) == "Plato the 11th"
    assert format_agent_nickname("Plato", 20) == "Plato the 21st"


def test_agent_spawn_depth_increments_and_enforces_limit():
    child_depth = next_agent_spawn_depth(1)
    assert child_depth == 2
    assert exceeds_agent_spawn_depth_limit(child_depth, 1)
    assert not exceeds_agent_spawn_depth_limit(1, 1)


def test_identity_reservation_rollback_allows_another_transaction():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    reservation.rollback()
    reservation = registry.reserve_spawn_identity()
    reservation.rollback()


def test_committed_identity_can_be_terminally_released():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    thread_id = new_id()
    reservation.commit(agent_metadata(thread_id))
    registry.release_spawned_agent(thread_id)
    assert registry.agent_metadata_for_id(thread_id) is None


def test_release_ignores_unknown_thread_id():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    thread_id = new_id()
    reservation.commit(agent_metadata(thread_id))

    registry.release_spawned_agent(new_id())  # unknown

    registry.release_spawned_agent(thread_id)


def test_release_is_idempotent_for_registered_threads():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    first_id = new_id()
    reservation.commit(agent_metadata(first_id))

    registry.release_spawned_agent(first_id)

    reservation = registry.reserve_spawn_identity()
    second_id = new_id()
    reservation.commit(agent_metadata(second_id))

    registry.release_spawned_agent(first_id)  # second release of first is a no-op

    registry.release_spawned_agent(second_id)


def test_failed_unpublished_spawn_releases_only_its_nickname_claim():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_identity()
    nickname = reservation.reserve_agent_nickname_with_preference(["alpha"], None)
    assert nickname == "alpha"
    reservation.rollback()

    reservation = registry.reserve_spawn_identity()
    nickname = reservation.reserve_agent_nickname_with_preference(["alpha", "beta"], None)
    assert nickname == "alpha"
    reservation.rollback()


def test_agent_nickname_resets_used_pool_when_exhausted():
    registry = AgentRegistry()
    first = registry.reserve_spawn_identity()
    first_name = first.reserve_agent_nickname_with_preference(["alpha"], None)
    first_id = new_id()
    first.commit(AgentMetadata(agent_id=first_id, agent_nickname=first_name))
    assert first_name == "alpha"

    second = registry.reserve_spawn_identity()
    second_name = second.reserve_agent_nickname_with_preference(["alpha"], None)
    assert second_name == "alpha the 2nd"
    assert registry._nickname_reset_count == 1


def test_repeated_resets_advance_the_ordinal_suffix():
    registry = AgentRegistry()
    for expected in ("Plato", "Plato the 2nd", "Plato the 3rd"):
        reservation = registry.reserve_spawn_identity()
        name = reservation.reserve_agent_nickname_with_preference(["Plato"], None)
        tid = new_id()
        reservation.commit(AgentMetadata(agent_id=tid, agent_nickname=name))
        assert name == expected
        registry.release_spawned_agent(tid)
    assert registry._nickname_reset_count == 2


def test_register_root_agent_indexes_root_path():
    registry = AgentRegistry()
    root_id = new_id()
    registry.register_root_agent(root_id)
    assert registry.agent_id_for_path(AgentPath.root()) == root_id


def test_reserved_agent_path_is_released_when_spawn_fails():
    registry = AgentRegistry()
    first = registry.reserve_spawn_identity()
    first.reserve_agent_path(AgentPath.from_string("/root/researcher"))
    first.rollback()

    second = registry.reserve_spawn_identity()
    second.reserve_agent_path(AgentPath.from_string("/root/researcher"))  # freed


def test_committed_agent_path_is_indexed_until_release():
    registry = AgentRegistry()
    thread_id = new_id()
    reservation = registry.reserve_spawn_identity()
    reservation.reserve_agent_path(AgentPath.from_string("/root/researcher"))
    reservation.commit(AgentMetadata(agent_id=thread_id, agent_path=AgentPath.from_string("/root/researcher")))
    assert registry.agent_id_for_path(AgentPath.from_string("/root/researcher")) == thread_id

    registry.release_spawned_agent(thread_id)
    assert registry.agent_id_for_path(AgentPath.from_string("/root/researcher")) is None


def test_live_agents_excludes_root():
    registry = AgentRegistry()
    registry.register_root_agent(new_id())
    res = registry.reserve_spawn_identity()
    wid = new_id()
    res.reserve_agent_path(AgentPath.from_string("/root/worker"))
    res.commit(AgentMetadata(agent_id=wid, agent_path=AgentPath.from_string("/root/worker")))
    live = registry.live_agents()
    assert len(live) == 1
    assert live[0].agent_id == wid


def test_terminal_release_tombstones_indices_and_prevents_aba_reuse():
    registry = AgentRegistry()
    path = AgentPath.from_string("/root/worker")
    reservation = registry.reserve_spawn_identity()
    nickname = reservation.reserve_agent_nickname_with_preference(["worker"], None)
    reservation.reserve_agent_path(path)
    agent_id = new_id()
    reservation.commit(AgentMetadata(agent_id=agent_id, agent_path=path, agent_nickname=nickname))
    path_revision = registry._path_claims[path.as_str()].revision
    nickname_revision = registry._nickname_claims[nickname].revision

    registry.release_spawned_agent(agent_id)

    assert registry.agent_id_for_path(path) is None
    assert registry._path_claims[path.as_str()].tombstoned
    assert registry._nickname_claims[nickname].tombstoned
    assert registry._path_claims[path.as_str()].revision > path_revision
    assert registry._nickname_claims[nickname].revision > nickname_revision
    replacement = registry.reserve_spawn_identity()
    with pytest.raises(AgentPathExists):
        replacement.reserve_agent_path(path)
    replacement.rollback()


def test_rollback_cannot_release_another_reservations_claim():
    registry = AgentRegistry()
    first = registry.reserve_spawn_identity()
    name = first.reserve_agent_nickname_with_preference(["worker"], None)
    second = registry.reserve_spawn_identity()

    registry._release_reserved_claim(registry._nickname_claims, name, second.reservation_id)

    assert registry._nickname_claims[name].reservation_id == first.reservation_id
    first.rollback()
    second.rollback()
