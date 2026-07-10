#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the L1 :class:`ReductionRequest` — the uniform way to *ask*.

The request carries only intent (target + urgency + reason); the one behavioral
bit it exposes is ``allow_destructive``, which gates the lossy head-drop reducer
to ``HARD`` urgency only.
"""
from __future__ import annotations

from metagpt.context.compaction.request import (
    ReductionReason,
    ReductionRequest,
    Urgency,
)


def test_defaults_are_soft_threshold():
    req = ReductionRequest(target_tokens=1000)
    assert req.urgency is Urgency.SOFT
    assert req.reason is ReductionReason.THRESHOLD


def test_soft_forbids_destructive():
    req = ReductionRequest(target_tokens=1000, urgency=Urgency.SOFT)
    assert req.allow_destructive is False


def test_hard_allows_destructive():
    req = ReductionRequest(target_tokens=1000, urgency=Urgency.HARD, reason=ReductionReason.REACTIVE)
    assert req.allow_destructive is True


def test_request_is_frozen():
    req = ReductionRequest(target_tokens=1000)
    try:
        req.target_tokens = 2000  # type: ignore[misc]
    except Exception as e:  # frozen dataclass raises FrozenInstanceError
        assert "cannot assign" in str(e) or "frozen" in str(e).lower()
    else:
        raise AssertionError("expected ReductionRequest to be immutable")
