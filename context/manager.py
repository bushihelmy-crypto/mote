"""ContextManager — the facade orchestrating MetaGPT's context-management scopes.

This is the integration point that finally wires the building blocks of this
package into the Role react loop. It plays two roles at once:

1. Message store (replaces the old ``Memory`` object). It owns the conversation
   history, backed by ``RoleState.context`` (``LLMCallContext.messages``) so the
   history survives checkpoint/recovery. It exposes the small slice of the old
   ``Memory`` API the loop / channel / think-engine depend on: ``get`` /
   ``add`` / ``add_batch`` / ``delete`` / ``count``.

2. Context orchestrator. Across the two history-level scopes it runs the cheap
   pass first then the expensive one:
     - ``microcompact`` folds old tool-result bodies in place (no LLM), and
     - ``autocompact`` summarizes+rebuilds when still over the token threshold,
       with the freed-token count threaded between them so the pricey summarize
       only fires when folding wasn't enough.
   The request-level scope (per-call compression) stays inside ``base_llm`` —
   ``ContextManager`` does not duplicate it; it only manages the *stored*
   history.

Why it backs ``RoleState.context`` directly (not a private list): the stored
history IS the data the context manager exists to manage, and it must be
serializable for recovery. Holding it anywhere else would split the source of
truth.

Layering: ``context`` may depend on ``executor`` (downward) but never
the reverse. This facade imports only sibling context modules plus
``schema`` — no Role / loop imports — so the Role depends on the manager, not
the other way around.
"""

from __future__ import annotations

from metagpt.common.schema import AutocompactResult, ContextManagerConfig, MicrocompactResult
from context import token_budget
from metagpt.context.autocompact import autocompact
from metagpt.context.microcompact import COMPACTABLE_TOOLS, microcompact
from metagpt.common.logs import logger
from metagpt.common.schema import LLMCallContext, Message


class ContextManager:
    """Owns the stored conversation and orchestrates its compaction.

    Args:
        context: The ``LLMCallContext`` to back the store with (normally
            ``RoleState.context`` so it is checkpointed). A fresh one is created
            when omitted (standalone / test use).
        llm: Anything with ``async aask(msg, system_msgs=, stream=)`` — used by
            autocompact to summarize. May be None when only the message-store
            API is exercised (compaction then no-ops gracefully).
        config: Tunable knobs; defaults reproduce Claude Code.
        model: Model name for token math. Falls back to ``llm.model`` then a
            generic default.
    """

    def __init__(
        self,
        context: LLMCallContext | None = None,
        *,
        llm=None,
        config: ContextManagerConfig | None = None,
        model: str | None = None,
    ):
        self._context = context if context is not None else LLMCallContext()
        self._llm = llm
        self.config = config or ContextManagerConfig()
        self._model = model
        # Circuit-breaker counter threaded across autocompact attempts.
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Message-store API (the slice of the old Memory the loop depends on)
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        """The live stored history (mutable; backs RoleState.context)."""
        return self._context.messages

    @property
    def model(self) -> str:
        return self._model or getattr(self._llm, "model", None) or "gpt-4"

    def get(self, k: int = 0) -> list[Message]:
        """Return the most-recent ``k`` messages (``k=0`` → all).

        Matches the old ``Memory.get`` contract the react loop and think-engine
        call: ``k`` slices the tail, ``0`` returns the whole history.
        """
        if k <= 0:
            return list(self._context.messages)
        return self._context.messages[-k:]

    def add(self, message: Message) -> None:
        """Append one message to the stored history (old ``Memory.add``)."""
        if message is None:
            return
        self._context.messages.append(message)

    def add_batch(self, messages) -> None:
        """Append several messages, skipping falsy entries (old ``add_batch``)."""
        for m in messages:
            self.add(m)

    def delete(self, message: Message) -> None:
        """Remove a message if present (old ``Memory.delete``; used on recovery).

        No-ops when the message isn't in the store so the recovery path
        (role_raise_decorator deleting the latest observed message) is safe to
        call unconditionally.
        """
        try:
            self._context.messages.remove(message)
        except ValueError:
            pass

    def count(self) -> int:
        return len(self._context.messages)

    def clear(self) -> None:
        self._context.messages.clear()

    # ------------------------------------------------------------------
    # History-level orchestration (microcompact → autocompact)
    # ------------------------------------------------------------------

    async def manage_history(self, *, custom_instructions: str | None = None) -> bool:
        """Run the two history-level passes over the stored history in order.

        Cheap first: ``microcompact`` folds old tool-result bodies in place and
        reports how many tokens that freed. Expensive second: ``autocompact``
        only summarizes+rebuilds when the history is *still* over threshold once
        those freed tokens are subtracted (the bridge between the two scopes).

        Mutates the stored history in place (folding) and/or replaces it with
        ``[summary] + tail`` (summarize). Returns True when either pass changed
        the history.

        Safe to call every turn: each pass is gated and no-ops cheaply when its
        trigger isn't met.
        """
        if not self._context.messages:
            return False

        changed = False
        model = self.model

        # Pass 1 — cheap, no LLM. Fold old compactable tool results in place.
        micro = microcompact(
            self._context.messages,
            self.config,
            model=model,
            compactable=COMPACTABLE_TOOLS,
        )
        if micro.changed:
            changed = True
            logger.info(
                f"context_manager: microcompact folded {len(micro.cleared_tool_use_ids)} "
                f"tool result(s), freed ~{micro.tokens_freed} tokens"
            )

        # Pass 2 — expensive, LLM summarize. Skipped without an llm or when the
        # post-fold history is still under the autocompact threshold.
        if self._llm is None:
            return changed

        result: AutocompactResult = await autocompact(
            self._context.messages,
            self._llm,
            self.config,
            model=model,
            tokens_freed=micro.tokens_freed,
            consecutive_failures=self._consecutive_failures,
            custom_instructions=custom_instructions,
        )
        self._consecutive_failures = result.consecutive_failures
        if result.compacted:
            # autocompact returns a NEW list ([summary] + tail); swap it into the
            # backing context so the rebuilt history is what gets checkpointed.
            self._context.messages[:] = result.messages
            changed = True
            logger.info(
                f"context_manager: autocompact summarized history "
                f"{result.pre_compact_tokens} → {result.post_compact_tokens} tokens"
            )
        elif result.error:
            logger.warning(f"context_manager: autocompact did not run: {result.error}")

        return changed

    def token_state(self):
        """Current token budget snapshot for the stored history (CC TokenState)."""
        return token_budget.evaluate(
            self._context.messages,
            self.model,
            autocompact_enabled=self.config.enable_autocompact,
        )

    # ------------------------------------------------------------------
    # Request assembly (history + the current user prompt)
    # ------------------------------------------------------------------

    async def prepare_request(
        self, user_prompt: str | Message | None = None, *, manage: bool = True
    ) -> list[Message]:
        """Build the ``req`` the think step sends: managed history + user prompt.

        Runs history-level management first (unless ``manage=False``), then
        returns ``stored_history + [user_prompt]``. The returned list is a fresh
        list (the caller may pass it straight to ``llm.aask`` / ``aask_tool``);
        the user prompt is NOT added to the stored history here — only the
        request gets it, matching how the old loop assembled ``req``.
        """
        if manage:
            await self.manage_history()

        req: list[Message] = list(self._context.messages)
        if user_prompt is not None:
            from metagpt.common.schema import UserMessage
            req.append(
                user_prompt if isinstance(user_prompt, Message) else UserMessage(content=user_prompt)
            )
        return req
