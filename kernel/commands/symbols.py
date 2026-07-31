"""Command-protocol prompt symbols and lowering pass.

THE PROBLEM THIS SOLVES
=======================
Shared prompt prose (SYSTEM_PROMPT, AGENT_TASK_PROMPT, ...) is sent to BOTH the
XML text protocol and the provider-native tool-use protocol. Whenever a sentence
hard-codes one protocol's surface syntax — e.g. "emit <end></end>" or "in its own
command block" — the other protocol's model receives instructions for a mechanic
it does not have, and (for native models) echoes ``<end></end>`` back as literal
text. This class of leak recurred repeatedly because nothing structurally stopped
an author from typing the literal mechanic into shared prose.

THE FIX: SYMBOLS + LOWERING (compiler analogy)
==============================================
Shared prose may not name protocol mechanics directly. Instead it writes a
*symbol* — a protocol-agnostic reference to a control-flow INTENT or a CAPABILITY:

    "Only ``CTL_FINISH`` after you have observed all outputs."   (in the prose)

Each CommandChannel owns a *vocabulary*: a mapping from symbol -> the surface
syntax for THAT protocol. At the very end of prompt assembly the active channel
*lowers* the prose, substituting every symbol with its protocol-specific surface:

    XML     CTL_FINISH -> "emit <end></end>"
    native  CTL_FINISH -> "stop calling tools and reply with a plain text message"

So a native render can never contain ``<end></end>``: the prose never held that
literal, only the symbol, and the native vocabulary renders it as plain English.

WHY THIS IS ROOT-CAUSE, NOT A PATCH
===================================
- The only natural way to express "finish the task" in shared prose is the symbol.
  The literal mechanic is simply not available to write.
- ``lower`` raises ``UnknownSymbolError`` on any leftover ``⟦...⟧`` (a typo'd or
  unregistered symbol), so a mistake dies at build time instead of leaking.
- A CI invariant test renders every registered prompt under every protocol and
  asserts no residual symbols + zero cross-protocol tokens.

Symbols use guillemet-style brackets ``⟦ … ⟧`` (U+27E6 / U+27E7) — characters
that never appear in normal prose or code, so the parse is unambiguous and a
stray symbol is visually obvious.
"""
from __future__ import annotations

import re
from enum import Enum

# Bracket delimiters for a symbol token: ⟦ name ⟧. Chosen because U+27E6/U+27E7
# never occur in prose, JSON, code, or either protocol's syntax.
_OPEN = "\u27e6"
_CLOSE = "\u27e7"


class Sym(str, Enum):
    """The closed set of protocol-agnostic prompt symbols.

    A ``str`` Enum so a member renders as its own ``⟦...⟧`` token when dropped
    into an f-string or template. Two families:

    - ``CTL_*`` — turn/task CONTROL-FLOW intents (the leak-prone ones): how to
      finish, how many action blocks per turn, step separation. These map to
      ``<end></end>`` / "command block" mechanics under XML and to tool-call
      mechanics under native.
    - ``CAP_*`` — CAPABILITY references: how to name a built-in action in prose.
      ``Editor.read`` under XML, "the read tool" under native.

    Adding a symbol here is the ONLY way to extend the vocabulary; both channels
    must then provide a surface for it (enforced by the invariant test).
    """

    # -- control-flow intents -------------------------------------------------
    CTL_FINISH = "ctl:finish"
    CTL_ONE_BLOCK = "ctl:one_block"
    CTL_SEPARATE_STEPS = "ctl:separate_steps"

    # -- capability references ------------------------------------------------
    CAP_READ = "cap:read"
    CAP_WRITE = "cap:write"
    CAP_REPLY = "cap:reply"

    def __str__(self) -> str:  # so f"{Sym.CTL_FINISH}" == "⟦ctl:finish⟧"
        return f"{_OPEN}{self.value}{_CLOSE}"


# Public token strings, for use in prose templates: f"... {CTL_FINISH} ...".
CTL_FINISH = str(Sym.CTL_FINISH)
CTL_ONE_BLOCK = str(Sym.CTL_ONE_BLOCK)
CTL_SEPARATE_STEPS = str(Sym.CTL_SEPARATE_STEPS)
CAP_READ = str(Sym.CAP_READ)
CAP_WRITE = str(Sym.CAP_WRITE)
CAP_REPLY = str(Sym.CAP_REPLY)


# Matches any ⟦...⟧ token; the inner text is captured for lookup / error report.
_TOKEN_RE = re.compile(re.escape(_OPEN) + r"(?P<name>.*?)" + re.escape(_CLOSE), re.DOTALL)


class UnknownSymbolError(ValueError):
    """Raised when lowering encounters a ``⟦...⟧`` token not in the vocabulary.

    A build-time failure by design: a leftover symbol means a typo or an
    unregistered symbol would otherwise leak to the model verbatim.
    """


def find_symbols(text: str) -> list[str]:
    """Return every ``⟦...⟧`` symbol token found in ``text`` (inner names)."""
    if not text:
        return []
    return [m.group("name") for m in _TOKEN_RE.finditer(text)]


def lower(text: str, vocabulary: dict[str, str]) -> str:
    """Substitute every ``⟦symbol⟧`` in ``text`` with its surface from ``vocabulary``.

    ``vocabulary`` maps a symbol VALUE (e.g. ``"ctl:finish"``, the ``Sym`` value
    or ``Sym`` member — both hash-compatible as ``str`` keys) to the surface
    string for the target protocol.

    Raises ``UnknownSymbolError`` if any token has no entry, so an unregistered
    or mistyped symbol fails the build instead of leaking. Returns ``text``
    unchanged when it holds no symbols.
    """
    if not text or _OPEN not in text:
        return text

    def _replace(m: re.Match) -> str:
        name = m.group("name")
        if name not in vocabulary:
            raise UnknownSymbolError(
                f"prompt symbol {_OPEN}{name}{_CLOSE} has no surface in the active "
                f"channel vocabulary; known symbols: {sorted(vocabulary)}"
            )
        return vocabulary[name]

    return _TOKEN_RE.sub(_replace, text)


def assert_no_symbols(text: str, *, where: str = "") -> None:
    """Raise ``UnknownSymbolError`` if any ``⟦...⟧`` token remains in ``text``.

    The build-end-stage guard: after the active channel has lowered a fully
    assembled prompt, no symbol may survive. ``where`` is an optional label for
    the error message (which prompt was being assembled).
    """
    residue = find_symbols(text)
    if residue:
        loc = f" in {where}" if where else ""
        raise UnknownSymbolError(
            f"unlowered prompt symbol(s){loc}: {residue} — the active channel "
            f"vocabulary is missing a surface, or a symbol was typed wrong"
        )


def normalize_vocabulary(vocabulary: dict) -> dict[str, str]:
    """Coerce a vocabulary's keys to plain symbol-value strings.

    Channels may key their vocabulary with ``Sym`` members for readability;
    this flattens them to the ``str`` values ``lower`` looks up. Plain-string
    keys pass through unchanged.
    """
    out: dict[str, str] = {}
    for key, surface in vocabulary.items():
        out[key.value if isinstance(key, Sym) else str(key)] = surface
    return out
