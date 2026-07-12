#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build the LLM recovery-strategy registry consumed by the generic ``RecoveryRunner``.

The recovery *loop* now lives in the leaf layer
(:class:`metagpt.common.exception.RecoveryRunner`): it owns the control flow
(call → dispatch on ``exc.recovery`` → retry → budget) and is shared by every
caller. This module supplies the **LLM-specific strategies** for that loop.

The exception system is a leaf: every typed error carries a ``recovery`` hint
(RETRY / COMPRESS / ROTATE_CREDENTIAL / FALLBACK / SHRINK_IMAGE / … / ABORT) but
can NOT act on it (acting needs business capabilities → ``common → business →
common`` cycle). :func:`build_llm_strategies` lives in the business layer where it
may depend on ``common.exception``; it assembles the injected capabilities
(compress / rotate key / switch provider / repair payload) into a
``{RecoveryAction: strategy}`` registry. Each strategy is an
``async (exc) -> bool`` closure that mutates the request state its caller closed
over and reports whether it recovered.

Division of labour:

- ``RETRY`` / ``ABORT`` — not in the registry; the generic loop re-raises them
  (RETRY is owned by the tenacity ``@retry(is_retryable)`` wrapping the call).
- ``COMPRESS`` — re-compress the context, then retry.
- ``ROTATE_CREDENTIAL`` — advance to the next API key on the active provider.
- ``FALLBACK`` — swap to the provider from ``fallback`` (and notify via
  ``on_fallback`` so the caller can redirect subsequent requests).
- transform-then-retry family (``SHRINK_IMAGE`` / ``DOWNGRADE_TOOL_CONTENT`` /
  ``STRIP_REQUEST_STATE``) — passed through verbatim from ``transformers``.

Any capability left ``None`` (or absent from ``transformers``) simply omits that
action from the registry; the generic loop then re-raises on that hint, so a
capability-less registry is behaviourally equivalent to no recovery loop at all.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, Optional

from metagpt.common.exception import MetaGPTError, RecoveryAction, RecoveryStrategy

if TYPE_CHECKING:
    from metagpt.router.llm.base_llm import BaseLLM

# Injected capabilities. Each already closes over the request state it mutates.
# Compress the current messages in place; return True once compressed.
Compressor = Callable[[], Awaitable[bool]]
# Rotate to the next credential on the active provider; return True on success.
CredentialRotator = Callable[[], bool]
# Supply the next provider to fail over to; return None when no fallback remains.
FallbackSupplier = Callable[[], Optional["BaseLLM"]]
# Called with the new provider after a successful FALLBACK so the caller can
# redirect subsequent requests to it.
FallbackSink = Callable[["BaseLLM"], None]
# Repair the outgoing request for a transform-then-retry action; return True if
# the payload was repaired (False → can't fix → the loop re-raises).
MessageTransformer = Callable[[MetaGPTError], Awaitable[bool]]


def build_llm_strategies(
    *,
    compress: Optional[Compressor] = None,
    rotate: Optional[CredentialRotator] = None,
    fallback: Optional[FallbackSupplier] = None,
    on_fallback: Optional[FallbackSink] = None,
    transformers: Optional[Mapping[RecoveryAction, MessageTransformer]] = None,
) -> dict[RecoveryAction, RecoveryStrategy]:
    """Assemble the ``{RecoveryAction: strategy}`` registry for an LLM call.

    Omits any action whose capability is ``None`` / absent, so the generic
    :class:`RecoveryRunner` re-raises that hint instead of looping.
    """
    strategies: dict[RecoveryAction, RecoveryStrategy] = {}

    if compress is not None:

        async def _compress(_exc: MetaGPTError) -> bool:
            return await compress()

        strategies[RecoveryAction.COMPRESS] = _compress

    if rotate is not None:

        async def _rotate(_exc: MetaGPTError) -> bool:
            return bool(rotate())

        strategies[RecoveryAction.ROTATE_CREDENTIAL] = _rotate

    if fallback is not None:

        async def _fallback(_exc: MetaGPTError) -> bool:
            provider = fallback()
            if provider is None:
                return False
            if on_fallback is not None:
                on_fallback(provider)
            return True

        strategies[RecoveryAction.FALLBACK] = _fallback

    for action, transform in (transformers or {}).items():
        strategies[action] = transform

    return strategies
