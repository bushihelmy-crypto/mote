"""The reactive context-reduction capability an LLM can be given for recovery.

When a wire request overflows the model window (a context/413 error that
survives the transient-retry budget), the recovery loop needs to *shrink the
outgoing payload* and re-issue it. The reduction logic lives in the ``context``
package (the fold + head-drop reducers over a boundary-safe ``Transcript``), but
``BaseLLM`` sits in the ``router`` layer and must not import ``context``.

This narrow Protocol is the injection seam (mirroring ``sticky_provider`` /
``lsp_notifier``): the upper layer hands ``BaseLLM`` something shaped like this,
and the recovery loop calls it. A ``None`` slot means "no reducer wired" and the
COMPRESS strategy simply degrades to a re-raise.

Contract: given the current wire messages (already ``Message.to_dict()`` shaped)
and a token target, return a reduced message list, or ``None`` when nothing
could be freed (so the loop does not re-issue an identical payload and spin).
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class ContextReducer(Protocol):
    async def reduce(self, messages: list[dict], *, target_tokens: int) -> Optional[list[dict]]:
        """Shrink ``messages`` to fit ``target_tokens``; ``None`` if nothing freed."""
        ...
