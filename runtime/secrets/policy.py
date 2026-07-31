"""Redact known Runtime secret values from text.

No I/O, no crypto, no config knowledge: it takes the *text* and a map of
``{plaintext secret value -> placeholder label}`` and returns the text with every
occurrence of a known value replaced by its label. This is the single algorithm
both ToolResultPolicy and any future scanner
share, so redaction behaviour lives in one testable place.

Two guards keep it from masking noise:

* ``MIN_REDACT_LENGTH`` — a value must be at least this long to be redacted, so
  short config values like ``"true"`` / ``"3000"`` never get masked even if they
  happen to sit under a secret-looking key.
* ``_PLACEHOLDER_VALUES`` — values that mean "no real secret yet" (empty,
  ``"sk-"``, ``"YOUR_API_KEY"``, an already-redacted ``"***"``) are skipped, so a
  config with an unfilled key does not turn every ``sk-`` prefix into a placeholder.

Longest values are replaced first so a secret that contains another (e.g. a
token whose prefix is itself vaulted) is masked whole rather than partially.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Tuple

# A value shorter than this is never redacted (avoids masking "true"/"3000"/etc).
# Mirrors agent-vault's MIN_REDACT_LENGTH.
MIN_REDACT_LENGTH = 8

# Config/env values that mean "no real secret yet" — never redact these. Reuses
# the same placeholder set the config secret-helper recognises
# (mote.product.config.secrets._PLACEHOLDER_KEYS) plus the redaction marker itself.
_PLACEHOLDER_VALUES = frozenset({"", "sk-", "YOUR_API_KEY", "***", "None", "null"})


def redact(text: str, secrets: Mapping[str, str]) -> Tuple[str, List[str]]:
    """Replace each known secret value in ``text`` with its placeholder label.

    Args:
        text: The raw text (a tool's model-facing output).
        secrets: Map of plaintext secret value -> placeholder label.

    Returns:
        ``(redacted_text, hit_labels)`` — the rewritten text and the labels of
        the secrets that were actually found and masked (empty when nothing hit,
        so the caller can skip a no-op rewrite).
    """
    if not text or not secrets:
        return text, []

    hits: List[str] = []
    result = text
    # Longest first: a value containing another is masked whole, not partially.
    for value in sorted(secrets, key=len, reverse=True):
        if len(value) < MIN_REDACT_LENGTH or value in _PLACEHOLDER_VALUES:
            continue
        if value in result:
            label = secrets[value]
            result = result.replace(value, label)
            hits.append(label)
    return result, hits


def build_value_map(*maps: Mapping[str, str]) -> Dict[str, str]:
    """Merge several ``{value: label}`` maps into one (later maps win on clash)."""
    merged: Dict[str, str] = {}
    for m in maps:
        merged.update(m)
    return merged


__all__ = ["redact", "build_value_map", "MIN_REDACT_LENGTH"]
