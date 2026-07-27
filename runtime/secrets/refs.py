"""Secret-reference expansion — turn ``<secret:KEY>`` placeholders into values.

The autonomous login-fill path (Login Ladder L1) lets the model type a
credential into a page **by reference, never by value**: it writes a placeholder
like ``<agent-vault:xhs_password>`` into a form field and the tool expands it to
the real secret at fill time. The plaintext therefore lives only in a local
variable inside the tool for the microsecond before Playwright types it — it
never enters the model's context, the tool-call arguments recorded to history,
or the rollout.

Three placeholder forms, matching the labels
:class:`~mote.runtime.secrets.store.SecretStore` already emits when it *masks* a
value in output (so a redacted value round-trips straight back as a fill token):

* ``<secret:dotted.path>`` — a config-tier secret (e.g. ``<secret:llm.api_key>``).
* ``<agent-vault:KEY>`` — a user/file/session named secret.
* ``<totp:KEY>`` — read ``KEY``'s base32 seed from the vault and substitute the
  *current* RFC 6238 code (the seed itself is never emitted).

The first two are equivalent at resolve time — both hand ``KEY`` to
``get_secret`` (which itself resolves a bare name or a trailing dotted segment) —
they differ only in the label a human/model reads. Resolution is **fail-closed**:
an unknown key or an empty value raises :class:`SecretRefError` with an
actionable message, so a mistyped placeholder never silently types the literal
placeholder text into a login form.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from mote.runtime.secrets.totp import totp_now

# KEY charset covers bare names, dotted config paths, and hyphen/underscore
# vault keys. The three prefixes map to the SecretStore labels above.
_REF_RE = re.compile(r"<(secret|agent-vault|totp):([A-Za-z0-9_.\-]+)>")


class SecretRefError(ValueError):
    """A ``<secret:…>`` / ``<agent-vault:…>`` / ``<totp:…>`` ref could not resolve."""


def has_secret_refs(text: str) -> bool:
    """Return whether ``text`` contains any secret placeholder (cheap pre-check)."""
    return bool(text) and _REF_RE.search(text) is not None


def expand_secret_refs(text: str, *, get_secret: Callable[[str], Optional[str]]) -> str:
    """Replace every secret placeholder in ``text`` with its resolved value.

    Args:
        text: Model-authored text that may embed secret placeholders.
        get_secret: The by-key resolver (``Role.get_secret`` / ``SecretStore.get``);
            returns the plaintext value for a key or ``None`` if unknown/disabled.

    Returns:
        ``text`` with each placeholder replaced by the resolved secret (or, for
        ``<totp:…>``, the current one-time code). Text with no placeholder is
        returned unchanged.

    Raises:
        SecretRefError: A referenced key resolves to nothing (unknown key or
            empty value), or a ``<totp:…>`` seed is not valid base32. Fail-closed
            so a bad reference never types placeholder text.
    """
    if not text:
        return text

    def _replace(match: "re.Match[str]") -> str:
        kind, key = match.group(1), match.group(2)
        value = get_secret(key)
        if not value:
            raise SecretRefError(
                f"secret reference <{kind}:{key}> did not resolve — no secret named "
                f"{key!r} is available (check the vault / secrets_config.json)."
            )
        if kind == "totp":
            try:
                return totp_now(value)
            except ValueError as exc:
                raise SecretRefError(f"secret reference <totp:{key}> could not produce a code: {exc}") from exc
        return value

    return _REF_RE.sub(_replace, text)


__all__ = ["expand_secret_refs", "has_secret_refs", "SecretRefError"]
