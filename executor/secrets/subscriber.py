"""Secret subscribers — the two control-plane choke points of the secret system.

Both drive one shared :class:`~mote.common.secrets.store.SecretStore`, so an
uploaded secret is masked in *both* directions with no per-tool changes:

* :class:`SecretUploadSubscriber` (UserPromptSubmit, **input** side) — the only
  place a raw secret *enters*. It scans the prompt for ``<secret>…</secret>`` upload
  spans (``<secret name="KEY">…</secret>`` to persist under a name), vaults each
  value (named→disk, anonymous→session memory), and substitutes the span with the
  value's placeholder label *before* the prompt reaches the model, history, or
  logs. An explicit ``</secret>`` close means the value may itself contain ``@``,
  ``=``, spaces, or newlines (e.g. an email) with no ambiguity. It then also
  redacts any already-known value the prompt happens to echo. Input is
  **fail-closed**: an unterminated ``<secret>`` tag (we cannot tell where the secret
  ends) stops the turn and the dangling remainder is masked, so a half-typed secret
  can never leak.

* :class:`RedactionSubscriber` (PostToolUse, **output** side) — masks known secret
  values in *every* tool's result at the single PostToolUse emit, so a value-based
  leak through ``cat config.yaml`` is caught the same as one through the Read tool.
  Output is **fail-open**: redaction is best-effort disclosure hygiene, not a
  containment boundary (that is the sandbox); a crash here must never brick a tool
  call.

Both read the store through :meth:`SecretStore.as_map`, which refreshes the vault
by mtime first — so a config edit or an external vault write is picked up without
a restart.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from mote.common.events.outcomes import PromptOutcome, ToolResultOutcome
from mote.common.events.types import POST_TOOL_USE, USER_PROMPT_SUBMIT, PostToolUseEvent, UserPromptSubmitEvent
from mote.common.interface.event_subscriber import FAIL_CLOSED, ControlStage, ControlSubscriber
from mote.common.secrets.policy import redact
from mote.common.secrets.store import SecretStore

#: One complete upload span: ``<secret>value</secret>``, optionally named via a
#: ``name="KEY"`` attribute. An explicit XML-style close makes the delimiter
#: collision-free: the value is non-greedy up to the *next* ``</secret>`` and may
#: contain ``@``, ``=``, spaces, or newlines (``re.DOTALL``) — an email or token
#: is never truncated. The value must be non-empty.
_UPLOAD_RE = re.compile(r'<secret(?:\s+name="([^"]*)")?\s*>(.+?)</secret>', re.DOTALL)
#: A residual ``<secret`` opener after every complete span is consumed → the tag is
#: unterminated. We match it through to end-of-string so the entire half-typed
#: remainder (not just the marker) is masked before we fail closed.
_DANGLING_RE = re.compile(r"<secret\b.*\Z", re.DOTALL)
#: Placeholder for a dangling (unterminated) span we mask before failing closed.
_DANGLING_MASK = "<agent-vault:unterminated>"


class SecretUploadSubscriber(ControlSubscriber):
    """Vaults ``<secret>…</secret>`` spans in the prompt, then redacts known values."""

    handles: tuple[str, ...] = (USER_PROMPT_SUBMIT,)
    stage: ControlStage = ControlStage.REWRITE
    #: Input is fail-closed: if we cannot process the prompt we must not let a
    #: possibly-secret-bearing prompt through — the bus synthesizes ``on_failure``.
    fail_mode = FAIL_CLOSED
    name: str = "secret-upload"

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    async def handle_control(self, event) -> Optional[PromptOutcome]:
        if not isinstance(event, UserPromptSubmitEvent):
            return None
        text = event.prompt
        if not isinstance(text, str) or not text:
            return None

        substituted, uploaded = self._vault_uploads(text)

        # Fail closed on a half-typed secret: we cannot know where the value ends,
        # so mask the dangling remainder and stop the turn rather than risk a leak.
        if _DANGLING_RE.search(substituted):
            masked = _DANGLING_RE.sub(_DANGLING_MASK, substituted)
            return PromptOutcome(
                updated_prompt=masked,
                stop=True,
                stop_reason="Unterminated <secret> tag; aborted to avoid leaking a partial secret.",
            )

        # Also mask any already-known value the prompt echoes verbatim.
        redacted, hits = redact(substituted, self._store.as_map())

        if not uploaded and not hits and redacted == text:
            return None  # nothing changed — no rewrite folded
        return PromptOutcome(updated_prompt=redacted)

    def _vault_uploads(self, text: str) -> Tuple[str, bool]:
        """Replace each ``<secret>…</secret>`` span with its vault label; report if any."""
        uploaded = False

        def _replace(match: "re.Match[str]") -> str:
            nonlocal uploaded
            uploaded = True
            name, value = match.group(1), match.group(2)
            if name:
                return self._store.add_user_secret(name, value)
            return self._store.add_session_secret(value)

        return _UPLOAD_RE.sub(_replace, text), uploaded

    def on_failure(self, reason: str) -> PromptOutcome:
        """Typed fail-closed deny the bus folds if this subscriber itself crashes."""
        return PromptOutcome(stop=True, stop_reason=reason)


class RedactionSubscriber(ControlSubscriber):
    """Rewrites a finished tool's output, masking known secret values (fail-open)."""

    handles: tuple[str, ...] = (POST_TOOL_USE,)
    stage: ControlStage = ControlStage.REWRITE
    name: str = "secret-redaction"

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    async def handle_control(self, event) -> Optional[ToolResultOutcome]:
        if not isinstance(event, PostToolUseEvent):
            return None
        text = event.tool_response
        if not isinstance(text, str) or not text:
            return None
        redacted, hits = redact(text, self._store.as_map())
        if not hits:
            return None
        return ToolResultOutcome(updated_response=redacted)


__all__: List[str] = ["SecretUploadSubscriber", "RedactionSubscriber"]
