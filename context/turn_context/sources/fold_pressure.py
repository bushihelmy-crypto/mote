"""FoldPressureContextSource — warn before old tool results get folded away.

The count-based sibling of :class:`TokenPressureContextSource`. That source
watches the *token* budget and warns before the autocompact summarize/rebuild;
this one watches the *count* of live reconstructable tool results and warns
before the FREE microcompact fold clears the oldest of them.

The two triggers are independent: fold fires on the NUMBER of reconstructable
results (regardless of token budget), so a run of many small reads hits the fold
long before token pressure ever registers. Without this feed that clearing has
no pre-warning at all — the model only learns its old reads are gone after the
fact. This closes that blind spot: once the live count reaches ~80% of the fold
trigger (the last window before the oldest bodies are cleared), nudge the model
to write down anything it still needs.

Silent until near the trigger, so it costs nothing on a short conversation.

Duck-typed exactly like the token source: holds any object exposing
``fold_state()`` (the ``ContextManager``), supplied either directly or as a
zero-arg getter resolved lazily per render — the latter lets the Role inject
``lambda: self.context_manager`` without forcing that collaborator to exist at
roster-assembly time.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, Union

from mote.common.interface import TurnContextPriority


class _FoldStateProvider(Protocol):
    """The context-manager slice this source reads (duck-typed).

    Structural only — reading through ``fold_state()`` (not importing the
    ``ContextManager``) keeps the source trivially fakeable in tests.
    """

    def fold_state(self) -> object:
        ...


class FoldPressureContextSource:
    """Emits a pre-fold reminder when the reconstructable-result count nears the
    microcompact trigger."""

    name = "fold"
    priority = TurnContextPriority.FOLD
    # Ephemeral (request-only): a "results about to be folded" nudge is a
    # transient state signal, only meaningful on the turn it fires. Re-evaluated
    # every cycle from the live fold state, so persisting it would just leave
    # stale warnings in history (and add to the very count it warns about).
    save_to_context = False

    def __init__(
        self,
        provider: Union[
            _FoldStateProvider,
            Callable[[], Optional[_FoldStateProvider]],
            None,
        ],
    ) -> None:
        # `provider` is anything with a `fold_state()` -> FoldState method, or a
        # zero-arg callable returning one (resolved lazily per render), or None.
        self._provider = provider
        # Edge-trigger latch: the ``near_fold`` value at the last render. The
        # warning fires only on the rising edge (``near_fold`` False -> True), so
        # sitting inside the warn window turn after turn stays silent. A real fold
        # drops the count back below the window (resetting this to False), so the
        # next approach re-arms and fires once more — one nudge per approach, not
        # per turn.
        self._was_near = False

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        provider = self._provider
        # A getter callable (e.g. ``lambda: self.context_manager``) is resolved
        # on demand; a bare provider object has no ``__call__`` so passes through.
        if callable(provider):
            provider = provider()
        if provider is None:
            return None
        state = provider.fold_state()
        near = bool(state is not None and getattr(state, "near_fold", False))
        # Rising edge only: emit when we just entered the window, then latch so
        # later turns still inside it stay quiet. Update the latch every render
        # (including to False when the window is left) so the next entry re-fires.
        fire, self._was_near = (near and not self._was_near), near
        if not fire:
            return None
        keep_recent = getattr(state, "keep_recent", 0)
        return (
            "# Tool-result clearing imminent\n"
            f"Old tool results are about to be cleared — only the {keep_recent} most "
            "recent survive. Now, note down any earlier findings, paths, or values "
            "you still need; unwritten ones are lost."
        )


__all__ = ["FoldPressureContextSource"]
