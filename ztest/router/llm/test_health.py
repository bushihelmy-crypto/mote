#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the LLM ↔ resilience glue (``router.llm.health``).

The build-time weld (``match`` + ``typing.assert_never`` over every
:class:`RecoveryAction`) is enforced by pyright; these tests are the runtime
belt that (a) proves the classification is TOTAL — every enum member returns a
verdict rather than falling through the ``match`` to ``assert_never`` — and (b)
pins the specific resource-fault verdicts and the typed/untyped dispatch in
``counts_as_health_failure``.
"""
from __future__ import annotations

from mote.common.exception import (
    ContextWindowExceededError,
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConnectionError,
    RecoveryAction,
)
from mote.router.llm.health import _action_is_resource_fault, counts_as_health_failure


class TestActionIsResourceFault:
    def test_classification_is_total_over_every_action(self):
        # No RecoveryAction may fall through the match to assert_never at runtime
        # (the runtime belt behind the pyright build-time weld). If a new action
        # is added without a verdict, this raises AssertionError here — and
        # pyright would already have failed the build.
        for action in RecoveryAction:
            assert isinstance(_action_is_resource_fault(action), bool)

    def test_resource_side_actions_count(self):
        assert _action_is_resource_fault(RecoveryAction.RETRY) is True
        assert _action_is_resource_fault(RecoveryAction.ROTATE_CREDENTIAL) is True

    def test_our_fault_and_deterministic_actions_do_not_count(self):
        for action in (
            RecoveryAction.ABORT,
            RecoveryAction.COMPRESS,
            RecoveryAction.FALLBACK,
            RecoveryAction.SHRINK_IMAGE,
            RecoveryAction.DOWNGRADE_TOOL_CONTENT,
            RecoveryAction.STRIP_REQUEST_STATE,
        ):
            assert _action_is_resource_fault(action) is False


class TestCountsAsHealthFailure:
    def test_typed_transient_counts(self):
        assert counts_as_health_failure(LLMConnectionError("boom")) is True

    def test_typed_credential_counts(self):
        # auth → ROTATE_CREDENTIAL → resource(credential) fault.
        assert counts_as_health_failure(LLMAuthenticationError("nope")) is True

    def test_typed_our_fault_does_not_count(self):
        # 400 bad-request (ABORT) and context overflow (COMPRESS) are our fault.
        assert counts_as_health_failure(LLMBadRequestError("malformed")) is False
        assert counts_as_health_failure(ContextWindowExceededError("too big")) is False

    def test_untyped_transient_counts(self):
        # An untyped vendor/transport error falls back to is_retryable.
        assert counts_as_health_failure(ConnectionError("reset")) is True

    def test_untyped_permanent_does_not_count(self):
        assert counts_as_health_failure(ValueError("bad")) is False
