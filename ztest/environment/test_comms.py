#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CommGraph — address routing + named channels + subtree queries."""

from mote.contracts.constants.messages import MESSAGE_ROUTE_TO_ALL
from mote.orchestration.environment.agent_path import AgentPath
from mote.orchestration.environment.comms import CommGraph, CommKind


def test_register_and_addresses_for():
    g = CommGraph()
    g.register("s1", addresses={"alice", "team"}, agent_path=AgentPath.from_string("/root/alice"))
    assert g.addresses_for("s1") == {"alice", "team"}
    assert g.path_for("s1") == AgentPath.from_string("/root/alice")


def test_set_addresses_replaces():
    g = CommGraph()
    g.register("s1", addresses={"alice"})
    g.set_addresses("s1", {"alice", "captain"})
    assert g.addresses_for("s1") == {"alice", "captain"}
    g.set_addresses("s1", None)
    assert g.addresses_for("s1") == set()


def test_resolve_recipients_by_address():
    g = CommGraph()
    g.register("s1", addresses={"alice"})
    g.register("s2", addresses={"bob"})
    g.register("s3", addresses={"bob", "team"})
    assert set(g.resolve_recipients({"bob"})) == {"s2", "s3"}
    assert g.resolve_recipients({"alice"}) == ["s1"]
    assert g.resolve_recipients({"ghost"}) == []


def test_resolve_recipients_broadcast_uses_all_ids():
    g = CommGraph()
    g.register("s1", addresses={"alice"})
    g.register("s2", addresses={"bob"})
    # broadcast resolves to the caller-supplied live id set, not the address map
    assert set(g.resolve_recipients({MESSAGE_ROUTE_TO_ALL}, all_ids=["s1", "s2", "s9"])) == {
        "s1",
        "s2",
        "s9",
    }
    assert g.resolve_recipients({MESSAGE_ROUTE_TO_ALL}) == []


def test_channels_join_leave_members():
    g = CommGraph()
    g.join_channel("alerts", "s1")
    g.join_channel("alerts", "s2")
    g.join_channel("alerts", "s1")  # idempotent
    assert g.channel_members("alerts") == ["s1", "s2"]
    assert g.channels() == ["alerts"]
    g.leave_channel("alerts", "s1")
    assert g.channel_members("alerts") == ["s2"]
    g.leave_channel("alerts", "s2")
    # channel pruned when empty
    assert g.channels() == []
    assert g.channel_members("alerts") == []


def test_subtree_members_includes_and_excludes_root():
    g = CommGraph()
    g.register("root", agent_path=AgentPath.root())
    g.register("a", agent_path=AgentPath.from_string("/root/a"))
    g.register("ab", agent_path=AgentPath.from_string("/root/a/b"))
    g.register("c", agent_path=AgentPath.from_string("/root/c"))
    sub = g.subtree_members(AgentPath.from_string("/root/a"), include_root=True)
    assert set(sub) == {"a", "ab"}
    sub_no_root = g.subtree_members(AgentPath.from_string("/root/a"), include_root=False)
    assert set(sub_no_root) == {"ab"}
    whole = g.subtree_members(AgentPath.root(), include_root=True)
    assert set(whole) == {"root", "a", "ab", "c"}


def test_subtree_no_prefix_false_positive():
    g = CommGraph()
    # "/root/ab" must NOT be considered under "/root/a"
    g.register("a", agent_path=AgentPath.from_string("/root/a"))
    g.register("ab", agent_path=AgentPath.from_string("/root/ab"))
    sub = g.subtree_members(AgentPath.from_string("/root/a"), include_root=True)
    assert set(sub) == {"a"}


def test_remove_clears_all_facets():
    g = CommGraph()
    g.register("s1", addresses={"alice"}, agent_path=AgentPath.from_string("/root/alice"))
    g.join_channel("alerts", "s1")
    g.remove("s1")
    assert g.addresses_for("s1") == set()
    assert g.path_for("s1") is None
    assert g.channel_members("alerts") == []


def test_comm_kind_values():
    assert CommKind.TASK.value == "task"
    assert CommKind.NOTIFICATION.value == "notification"
    assert CommKind.RESULT.value == "result"
    assert CommKind.QUERY.value == "query"
    assert CommKind.BROADCAST.value == "broadcast"
