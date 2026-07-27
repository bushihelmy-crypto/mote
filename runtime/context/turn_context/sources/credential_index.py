"""CredentialIndexContextSource — the login-fill menu the model may use.

The discovery answer to "how does the model know a login credential lives in my
vault?". Without it the model learns the placeholder *syntax* (from the WebBrowser
tool docs) but never *which keys exist* — so it cannot autonomously fill a login
form. This source renders, per turn, a ``name: value-to-type`` menu the model
copies verbatim into a field. Two kinds of entry converge here (the caller merges
them into one map; the source stays kind-agnostic):

- **secret placeholders** — ``name: <agent-vault:key>`` / ``<secret:dotted.path>``.
  The model types the placeholder string; the tool expands it from the vault at
  fill time, so the value never enters the conversation.
- **inline non-secret values** — ``name: literal`` for non-sensitive fields (a
  username, a display name) the user put in plaintext config. The model types the
  literal directly (there is nothing to expand).

**Opt-in + gated on ACTUAL USE.** Secret names are already broadcast to the model
as redaction labels, so listing them adds no disclosure — but it is still off by
default (``context.turn_context.credential_index: true`` to enable). Construction
requires secrets on + the role equipped with WebBrowser (its sole consumer); those
cheap gates live in ``role_components``. The RENDER gate, however, is dynamic: the
menu appears only on a turn where WebBrowser was *recently used* (a WebBrowser tool
call sits in the recent history). A role merely equipped with — but not currently
driving — a browser sees nothing, so the login menu surfaces exactly when a login
form is plausibly in play and is silent the rest of the time.

Ephemeral (``save_to_context=False``) + sorted → byte-stable across turns: it
rides the reminder tail after the cache breakpoint, re-injected each turn, never
persisted into history nor in the cached prefix (no prompt-cache churn). Duck-typed
(mirrors :class:`DeferredToolIndexContextSource`): holds callables so the low
``context`` layer never imports ``roles`` / secrets.
"""

from __future__ import annotations

from typing import Callable, Optional

from mote.contracts.ports import TurnContextPriority


class CredentialIndexContextSource:
    """Emits the stable menu of referenceable secret names, per turn."""

    name = "credential_index"
    # Right after the deferred-tool menu: another "what can I use" surface, but
    # login-specific, so it trails the general tool discovery.
    priority = TurnContextPriority.CREDENTIAL_INDEX
    # Ephemeral: byte-stable and re-injected each turn; persisting it would just
    # accumulate duplicates in history.
    save_to_context = False

    def __init__(
        self,
        get_labels: Callable[[], dict[str, str]],
        browser_recently_used: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._get_labels = get_labels
        # Dynamic render gate: True only on a turn where WebBrowser was recently
        # used. None (unset) means "always render" — a role_components caller
        # always injects it, but a bare test may omit it to inspect labels alone.
        self._browser_recently_used = browser_recently_used

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        # Suppress unless a browser workflow is actually in play this turn — the
        # login menu is only useful when a login form is plausibly on screen.
        if self._browser_recently_used is not None and not self._browser_recently_used():
            return None
        labels = self._get_labels() if self._get_labels else {}
        if not labels:
            return None
        lines = [
            "# Configured credentials (type the value shown into the login form)",
            "To fill a login field, type the exact value shown for a name into the "
            "field (e.g. via WebBrowser type/fill_form). A <agent-vault:KEY> / "
            "<secret:KEY> value is a placeholder — type it verbatim and the tool "
            "expands it from the vault at fill time, so the secret never enters the "
            "conversation. Any other value is a plain non-secret literal — type it "
            "as-is. For a seed-based 2FA code (Google Authenticator style), write "
            "<totp:KEY> to type the current 6-digit code. An SMS / email code is NOT "
            "available here — ask the user for it (assist) when prompted.",
        ]
        # Sorted → byte-stable across turns.
        for name in sorted(labels):
            lines.append(f"- {name}: {labels[name]}")
        return "\n".join(lines)


__all__ = ["CredentialIndexContextSource"]
