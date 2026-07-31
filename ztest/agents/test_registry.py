#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentRegistry — ported from registry_tests.rs."""

import uuid

import pytest

from mote.orchestration.agents.identity.path import AgentPath
from mote.orchestration.agents.identity.registry import (
    AgentMetadata,
    AgentRegistry,
    exceeds_agent_spawn_depth_limit,
    format_agent_nickname,
    next_agent_spawn_depth,
)
from mote.runtime.errors import AgentLimitReached


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


def test_reservation_drop_releases_slot():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_slot(1)
    reservation.rollback()
    reservation = registry.reserve_spawn_slot(1)  # slot released
    reservation.rollback()


def test_commit_holds_slot_until_release():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_slot(1)
    thread_id = new_id()
    reservation.commit(agent_metadata(thread_id))

    with pytest.raises(AgentLimitReached) as exc:
        registry.reserve_spawn_slot(1)
    assert exc.value.max_agents == 1

    registry.release_spawned_agent(thread_id)
    registry.reserve_spawn_slot(1).rollback()  # slot released after agent removal


def test_release_ignores_unknown_thread_id():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_slot(1)
    thread_id = new_id()
    reservation.commit(agent_metadata(thread_id))

    registry.release_spawned_agent(new_id())  # unknown

    with pytest.raises(AgentLimitReached):
        registry.reserve_spawn_slot(1)

    registry.release_spawned_agent(thread_id)
    registry.reserve_spawn_slot(1).rollback()


def test_release_is_idempotent_for_registered_threads():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_slot(1)
    first_id = new_id()
    reservation.commit(agent_metadata(first_id))

    registry.release_spawned_agent(first_id)

    reservation = registry.reserve_spawn_slot(1)
    second_id = new_id()
    reservation.commit(agent_metadata(second_id))

    registry.release_spawned_agent(first_id)  # second release of first is a no-op

    with pytest.raises(AgentLimitReached):
        registry.reserve_spawn_slot(1)

    registry.release_spawned_agent(second_id)
    registry.reserve_spawn_slot(1).rollback()


def test_failed_spawn_keeps_nickname_marked_used():
    registry = AgentRegistry()
    reservation = registry.reserve_spawn_slot(None)
    nickname = reservation.reserve_agent_nickname_with_preference(["alpha"], None)
    assert nickname == "alpha"
    reservation.rollback()

    reservation = registry.reserve_spawn_slot(None)
    nickname = reservation.reserve_agent_nickname_with_preference(["alpha", "beta"], None)
    assert nickname == "beta"  # alpha still marked used


def test_agent_nickname_resets_used_pool_when_exhausted():
    registry = AgentRegistry()
    first = registry.reserve_spawn_slot(None)
    first_name = first.reserve_agent_nickname_with_preference(["alpha"], None)
    first.commit(agent_metadata(new_id()))
    assert first_name == "alpha"

    second = registry.reserve_spawn_slot(None)
    second_name = second.reserve_agent_nickname_with_preference(["alpha"], None)
    assert second_name == "alpha the 2nd"
    assert registry._nickname_reset_count == 1


def test_repeated_resets_advance_the_ordinal_suffix():
    registry = AgentRegistry()
    for expected in ("Plato", "Plato the 2nd", "Plato the 3rd"):
        reservation = registry.reserve_spawn_slot(None)
        name = reservation.reserve_agent_nickname_with_preference(["Plato"], None)
        tid = new_id()
        reservation.commit(agent_metadata(tid))
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
    first = registry.reserve_spawn_slot(None)
    first.reserve_agent_path(AgentPath.from_string("/root/researcher"))
    first.rollback()

    second = registry.reserve_spawn_slot(None)
    second.reserve_agent_path(AgentPath.from_string("/root/researcher"))  # freed


def test_committed_agent_path_is_indexed_until_release():
    registry = AgentRegistry()
    thread_id = new_id()
    reservation = registry.reserve_spawn_slot(None)
    reservation.reserve_agent_path(AgentPath.from_string("/root/researcher"))
    reservation.commit(AgentMetadata(agent_id=thread_id, agent_path=AgentPath.from_string("/root/researcher")))
    assert registry.agent_id_for_path(AgentPath.from_string("/root/researcher")) == thread_id

    registry.release_spawned_agent(thread_id)
    assert registry.agent_id_for_path(AgentPath.from_string("/root/researcher")) is None


def test_live_agents_excludes_root():
    registry = AgentRegistry()
    registry.register_root_agent(new_id())
    res = registry.reserve_spawn_slot(None)
    wid = new_id()
    res.commit(AgentMetadata(agent_id=wid, agent_path=AgentPath.from_string("/root/worker")))
    live = registry.live_agents()
    assert len(live) == 1
    assert live[0].agent_id == wid
