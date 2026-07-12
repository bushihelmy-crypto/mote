#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentPath — ported from agent_path.rs ``mod tests``."""

import pytest
from pydantic import BaseModel

from metagpt.environment.agent_path import AgentPath


def test_root_has_expected_name():
    root = AgentPath.root()
    assert root.as_str() == AgentPath.ROOT
    assert root.name() == "root"
    assert root.is_root()


def test_morpheus_has_expected_name():
    morpheus = AgentPath.morpheus()
    assert morpheus.as_str() == AgentPath.MORPHEUS
    assert morpheus.name() == "morpheus"
    assert not morpheus.is_root()


def test_join_builds_child_paths():
    root = AgentPath.root()
    child = root.join("researcher")
    assert child.as_str() == "/root/researcher"
    assert child.name() == "researcher"


def test_resolve_supports_relative_and_absolute_references():
    current = AgentPath.from_string("/root/researcher")
    assert current.resolve("worker") == AgentPath.from_string("/root/researcher/worker")
    assert current.resolve("/root/other") == AgentPath.from_string("/root/other")


def test_invalid_names_and_paths_are_rejected():
    with pytest.raises(ValueError, match="lowercase letters, digits"):
        AgentPath.root().join("BadName")
    with pytest.raises(ValueError, match="must start with `/root`"):
        AgentPath.from_string("/not-root")
    with pytest.raises(ValueError, match="`\\.\\.` is reserved"):
        AgentPath.root().resolve("../sibling")


def test_parent():
    assert AgentPath.from_string("/root/a/b").parent() == AgentPath.from_string("/root/a")
    assert AgentPath.from_string("/root/a").parent() == AgentPath.root()
    assert AgentPath.root().parent() is None


def test_eq_hash_and_ordering():
    a = AgentPath.from_string("/root/a")
    a2 = AgentPath.from_string("/root/a")
    b = AgentPath.from_string("/root/b")
    assert a == a2
    assert hash(a) == hash(a2)
    assert a < b
    assert {a, a2} == {a}


def test_pydantic_roundtrip():
    class M(BaseModel):
        path: AgentPath

    m = M(path="/root/worker")
    assert isinstance(m.path, AgentPath)
    assert m.path == AgentPath.from_string("/root/worker")
    dumped = m.model_dump()
    assert dumped["path"] == "/root/worker"
    assert M.model_validate(dumped).path == m.path
