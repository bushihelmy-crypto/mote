#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for control-plane exceptions."""

import pytest

from metagpt.environment.exceptions import (
    AgentControlError,
    AgentLimitReached,
    AgentNotFound,
    AgentNotKnown,
    AgentPathExists,
)


def test_all_are_agent_control_errors():
    for exc in (AgentLimitReached, AgentNotFound, AgentNotKnown, AgentPathExists):
        assert issubclass(exc, AgentControlError)


def test_agent_limit_reached_carries_max_agents():
    err = AgentLimitReached(3)
    assert err.max_agents == 3
    assert "3" in str(err)
    with pytest.raises(AgentLimitReached):
        raise AgentLimitReached(1)


def test_agent_not_found_carries_id():
    err = AgentNotFound("abc")
    assert err.agent_id == "abc"
    assert "abc" in str(err)


def test_agent_path_exists():
    err = AgentPathExists("/root/x")
    assert err.agent_path == "/root/x"
    assert "/root/x" in str(err)


def test_agent_not_known():
    err = AgentNotKnown("worker")
    assert err.reference == "worker"
