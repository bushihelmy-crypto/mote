#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Recovery / failover loop that consumes the pre-embedded ``RecoveryAction`` hints.

The exception system (``metagpt.common.exception``) is a leaf module: every typed
error carries a ``recovery`` hint (RETRY / COMPRESS / ROTATE_CREDENTIAL / FALLBACK /
ABORT) but, being the lowest layer, it can NOT act on them (acting would require
importing business modules → ``common → business → common`` cycle).

``RecoveryRunner`` lives in the business layer (``metagpt.router.llm``) where it may
legally depend on ``common.exception``. It reads ``exc.recovery`` and runs the matching
recovery strategy, then retries the call. All business capabilities (compress / rotate
key / switch provider) are *injected as callbacks* rather than imported, so this module
never depends on ``router.py`` / ``context_manager`` and forms no cycle.

Division of labour:

- ``RETRY``  — left to the tenacity ``@retry(is_retryable)`` already wrapping
  ``acompletion_text``; by the time a retryable error bubbles up here, tenacity is
  exhausted, so the runner re-raises instead of looping forever.
- ``COMPRESS`` — calls the injected ``compressor`` (e.g. ``BaseLLM.compress_messages``)
  then retries with the smaller messages.
- ``ROTATE_CREDENTIAL`` — calls the injected ``credential_rotator`` (rebuild client
  with the next key) then retries.
- ``FALLBACK`` — calls the injected ``fallback_supplier`` to obtain the next provider
  then retries.
- "transform-then-retry" family (``SHRINK_IMAGE`` / ``DOWNGRADE_TOOL_CONTENT`` /
  ``STRIP_REQUEST_STATE``) — dispatched through the injected ``message_transformers``
  registry: a transformer rewrites the outgoing messages (shrink an oversized image,
  downgrade list-type tool content to text, strip an unparseable request blob) and the
  runner retries the *same* provider with the repaired payload. The default
  transformers (``router.llm.transformers.DEFAULT_MESSAGE_TRANSFORMERS``) are wired
  in via ``BaseLLM._message_transformers``; a transformer that returns ``None``
  (can't repair the payload) leaves that recovery a re-raise.
- ``ABORT`` — re-raise.

Every callback / transformer defaults to ``None`` (or absent from the registry) → that
strategy degrades to "skip" (re-raise), making a callback-less runner behaviourally
equivalent to having no loop at all.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, Optional

from metagpt.common.exception import MetaGPTError, RecoveryAction
from metagpt.common.logs import logger

if TYPE_CHECKING:
    from metagpt.router.llm.base_llm import BaseLLM

# A coroutine factory: each invocation issues one LLM request and returns its result.
Call = Callable[[], Awaitable]
# Compress the given messages and return the (smaller) replacement.
Compressor = Callable[[list[dict]], Awaitable[list[dict]]]
# Rotate to the next credential; return True if a fresh credential was applied.
CredentialRotator = Callable[[], bool]
# Supply the next provider to fail over to; return None when no fallback remains.
FallbackSupplier = Callable[[], Optional["BaseLLM"]]
# Repair the outgoing messages for a "transform-then-retry" recovery, given the
# triggering error. Return the rewritten messages, or None if it can't fix them
# (→ the runner re-raises). Keyed by the ``RecoveryAction`` it serves.
MessageTransformer = Callable[[list[dict], MetaGPTError], Awaitable[Optional[list[dict]]]]

# The reserved "repair the request, then retry the same provider" actions served
# by the ``message_transformers`` registry (not by the discrete callbacks).
_TRANSFORM_ACTIONS = frozenset(
    {
        RecoveryAction.SHRINK_IMAGE,
        RecoveryAction.DOWNGRADE_TOOL_CONTENT,
        RecoveryAction.STRIP_REQUEST_STATE,
    }
)


class RecoveryRunner:
    """Consume ``exc.recovery`` hints, run the matching recovery, then retry the call.

    Depends only on ``common.exception``. All business capabilities are injected; a
    missing callback (``None``) makes that strategy a no-op (re-raise), so the default
    runner behaves exactly like the un-wrapped call.
    """

    def __init__(
        self,
        *,
        compressor: Optional[Compressor] = None,
        credential_rotator: Optional[CredentialRotator] = None,
        fallback_supplier: Optional[FallbackSupplier] = None,
        message_transformers: Optional[Mapping[RecoveryAction, MessageTransformer]] = None,
        max_recoveries: int = 3,
    ) -> None:
        self._compressor = compressor
        self._credential_rotator = credential_rotator
        self._fallback_supplier = fallback_supplier
        self._message_transformers = dict(message_transformers or {})
        self.max_recoveries = max_recoveries
        # Set when a FALLBACK recovery swaps in a new provider, so the caller can
        # redirect subsequent work (P3 wiring); None until a fallback fires.
        self._fallback_llm: Optional["BaseLLM"] = None

    @property
    def fallback_llm(self) -> Optional["BaseLLM"]:
        """The provider produced by the most recent FALLBACK recovery, if any."""
        return self._fallback_llm

    async def run(self, call: Call, *, messages: Optional[list[dict]] = None):
        """Run ``call``; on a typed error, apply the recovery hint and retry.

        Args:
            call: A no-arg coroutine factory; each call issues one LLM request.
            messages: The current request messages, fed to ``compressor`` on COMPRESS.

        Only ``MetaGPTError`` is handled (the provider errors ``classify_llm_error``
        already typed); any other exception propagates unchanged. RETRY/ABORT re-raise;
        a missing callback or an exhausted recovery budget also re-raises.
        """
        recoveries = 0
        while True:
            try:
                return await call()
            except MetaGPTError as exc:
                action = exc.recovery
                if action in (RecoveryAction.RETRY, RecoveryAction.ABORT):
                    # RETRY is owned by the lower-layer tenacity loop; ABORT gives up.
                    raise
                if recoveries >= self.max_recoveries:
                    logger.warning(
                        f"RecoveryRunner exhausted {self.max_recoveries} recoveries; "
                        f"re-raising {type(exc).__name__}"
                    )
                    raise
                handled, messages = await self._recover(action, exc, messages)
                if not handled:
                    raise
                recoveries += 1

    async def _recover(
        self, action: RecoveryAction, exc: MetaGPTError, messages: Optional[list[dict]]
    ) -> tuple[bool, Optional[list[dict]]]:
        """Execute the recovery for ``action``; return (handled, updated_messages)."""
        if action == RecoveryAction.COMPRESS:
            if self._compressor is None:
                return False, messages
            logger.info(f"RecoveryRunner: COMPRESS after {type(exc).__name__}")
            messages = await self._compressor(messages)
            return True, messages
        if action == RecoveryAction.ROTATE_CREDENTIAL:
            if self._credential_rotator is None:
                return False, messages
            logger.info(f"RecoveryRunner: ROTATE_CREDENTIAL after {type(exc).__name__}")
            return bool(self._credential_rotator()), messages
        if action == RecoveryAction.FALLBACK:
            if self._fallback_supplier is None:
                return False, messages
            provider = self._fallback_supplier()
            if provider is None:
                return False, messages
            logger.info(f"RecoveryRunner: FALLBACK after {type(exc).__name__}")
            self._fallback_llm = provider
            return True, messages
        if action in _TRANSFORM_ACTIONS:
            transformer = self._message_transformers.get(action)
            if transformer is None:
                return False, messages
            repaired = await transformer(messages, exc)
            if repaired is None:
                return False, messages
            logger.info(f"RecoveryRunner: {action.value} after {type(exc).__name__}")
            return True, repaired
        return False, messages
