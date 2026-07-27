#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentExecutionLimiter — ported from execution_tests.rs."""

import pytest

from mote.orchestration.environment.exceptions import AgentLimitReached
from mote.orchestration.environment.limiter import AgentExecutionLimiter


def test_execution_guards_count_active_turns():
    limiter = AgentExecutionLimiter()
    limiter.initialize(1)
    limiter.initialize(2)  # second init ignored — ceiling stays 1

    limiter.ensure_capacity()  # first turn fits
    guard = limiter.guard()
    assert limiter.active == 1

    with pytest.raises(AgentLimitReached) as exc:
        limiter.ensure_capacity()
    assert exc.value.max_agents == 1

    guard.release()
    limiter.ensure_capacity()  # capacity released
    assert limiter.active == 0


def test_uninitialized_limiter_is_unbounded():
    limiter = AgentExecutionLimiter()
    guards = [limiter.guard() for _ in range(5)]
    limiter.ensure_capacity()  # never raises
    assert limiter.active == 5
    for g in guards:
        g.release()


def test_guard_context_manager_releases():
    limiter = AgentExecutionLimiter()
    limiter.initialize(1)
    with limiter.guard():
        assert limiter.active == 1
        with pytest.raises(AgentLimitReached):
            limiter.ensure_capacity()
    assert limiter.active == 0
    limiter.ensure_capacity()


def test_double_release_is_safe():
    limiter = AgentExecutionLimiter()
    limiter.initialize(1)
    guard = limiter.guard()
    guard.release()
    guard.release()
    assert limiter.active == 0
