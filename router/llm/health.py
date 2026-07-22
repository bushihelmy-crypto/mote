#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM ↔ resilience glue: resource keys + health-failure classification.

The domain-agnostic :class:`~mote.common.resilience.CircuitBreaker` needs two
LLM-specific decisions the primitive itself cannot make: *which resource* a call
targets (so failures aggregate per provider/model/credential, not globally) and
*which failures reflect the resource's health* (so our-fault errors don't trip a
provider's breaker). Both live here, in the business layer that may depend on
``common.exception``; the primitive stays a leaf.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from mote.common.exception import RecoveryAction, is_retryable
from mote.common.exception.base import MoteError
from mote.common.exception.handlers import classify_llm_error

if TYPE_CHECKING:
    from mote.router.llm.base_llm import BaseLLM


def _action_is_resource_fault(action: RecoveryAction) -> bool:
    """Whether a failure carrying this recovery hint indicts the RESOURCE's health.

    THE welded action↔health map. It is exhaustive *by construction*: every
    :class:`RecoveryAction` is matched below with no catch-all, and the trailing
    :func:`typing.assert_never` makes pyright FAIL THE BUILD if a new action is
    ever added without a verdict here (the residual type would stop narrowing to
    ``Never``). So this classification can never silently drift out of sync with
    the enum — the seam the old ``frozenset`` left unwelded.

    Returns True → the remote resource / credential itself failed: transient
    RETRY (5xx / overload / rate-limit / timeout / connection) or
    ROTATE_CREDENTIAL (the *current credential* is bad — and the resource key
    embeds the key index, so this trips only that credential's breaker, not the
    whole provider). Record these so a sustained outage trips the breaker.

    Returns False → an OUR-fault or provider-deterministic outcome that a
    different provider or a smaller payload would hit identically: context
    overflow (COMPRESS), 400 / parse (ABORT), content-policy (FALLBACK), and the
    image / tool / state transform-then-retry family. Recording these would
    wrongly shed a healthy provider.
    """
    match action:
        case RecoveryAction.RETRY | RecoveryAction.ROTATE_CREDENTIAL:
            return True
        case (
            RecoveryAction.ABORT
            | RecoveryAction.COMPRESS
            | RecoveryAction.FALLBACK
            | RecoveryAction.SHRINK_IMAGE
            | RecoveryAction.DOWNGRADE_TOOL_CONTENT
            | RecoveryAction.STRIP_REQUEST_STATE
        ):
            return False
    assert_never(action)


def resource_key(llm: "BaseLLM") -> str:
    """The breaker key for *llm*: ``{api_type}::{model}::{api_key_index}``.

    Distinguishes the same model reached over different wire protocols and each
    rotatable credential (so a dead key trips its own breaker while a sibling key
    stays healthy). Tolerant of a provider without a rotatable credential list
    (index defaults to 0).
    """
    api_type = getattr(getattr(llm, "config", None), "api_type", "")
    api_val = getattr(api_type, "value", api_type) or "unknown"
    model = getattr(llm, "model", "") or "unknown"
    idx = getattr(llm, "_api_key_index", 0)
    return f"{api_val}::{model}::{idx}"


def counts_as_health_failure(exc: BaseException) -> bool:
    """Whether a failed LLM call reflects RESOURCE health (should trip a breaker).

    Counted: transient (RETRY) and credential (ROTATE_CREDENTIAL) failures — the
    resource/credential itself is unhealthy. NOT counted: our-fault errors that
    a different provider or a smaller payload would hit identically —
    context-overflow (COMPRESS), 400 bad-request / parse (ABORT), content-policy
    (FALLBACK), and the image/tool/state transform family. Recording those as
    provider failures would wrongly shed a healthy provider.
    """
    typed = exc if isinstance(exc, MoteError) else classify_llm_error(exc)
    if typed is None:
        # Untyped vendor error → transient ones count as health failures.
        return is_retryable(exc)
    return _action_is_resource_fault(typed.recovery)


__all__ = ["resource_key", "counts_as_health_failure"]
