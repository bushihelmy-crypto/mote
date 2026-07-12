"""DSML tool-call decoder — salvage DeepSeek's leaked tool calls from text.

DeepSeek models occasionally "fall out" of the structured tool-call channel and
emit their internal DSML tool-call markup as plain assistant ``content`` instead
of the gateway-translated ``tool_calls`` field. The gateway only translates the
structured channel, so a leaked block reaches us as text and is never executed —
it pollutes history, the model imitates its own bad output, and the loop spins.

This module reverse-parses that leaked DSML back into the agnostic tool-call
shape (``[{"id", "name", "arguments"}]``) so the DeepSeek provider can recover
it. Pure functions, zero side effects, never raises — on any malformed input it
returns ``([], original_text)`` so the caller falls back to plain text handling.

The DSML wire format (observed from real gateway leaks)::

    <｜｜DSML｜｜tool_calls>
    <｜｜DSML｜｜invoke name="ToolName">
    <｜｜DSML｜｜parameter name="arg" string="true">value</｜｜DSML｜｜parameter>
    </｜｜DSML｜｜invoke>
    </｜｜DSML｜｜tool_calls>

Notes on the format:
  * The separator is the FULLWIDTH VERTICAL LINE U+FF5C, doubled (``｜｜``), not
    the ASCII ``|``.
  * ``string="true"`` => the value is a literal string; ``string="false"`` => the
    value is a raw literal (number / bool / null) parsed via ``json.loads``,
    falling back to the raw string if that fails.
  * A single ``tool_calls`` block may contain multiple ``invoke`` elements.
"""
from __future__ import annotations

import json
import re
from typing import Tuple

# Fullwidth vertical line (U+FF5C), doubled. Kept as an explicit escape so the
# marker is unambiguous in source and greppable.
_BAR = "\uff5c\uff5c"

# Tag delimiters. The opening tag carries a leading "<", the closing tag "</".
_OPEN = re.escape(f"<{_BAR}DSML{_BAR}")
_CLOSE = re.escape(f"</{_BAR}DSML{_BAR}")

# Outer block: <｜｜DSML｜｜tool_calls> ... </｜｜DSML｜｜tool_calls>
_BLOCK_RE = re.compile(
    rf"{_OPEN}tool_calls>(?P<body>.*?){_CLOSE}tool_calls>",
    re.DOTALL,
)
_INVOKE_RE = re.compile(
    rf'{_OPEN}invoke\s+name="(?P<name>[^"]*)"\s*>(?P<body>.*?){_CLOSE}invoke>',
    re.DOTALL,
)
_PARAM_RE = re.compile(
    rf'{_OPEN}parameter\s+name="(?P<name>[^"]*)"'
    rf'(?:\s+string="(?P<string>true|false)")?\s*>'
    rf"(?P<value>.*?){_CLOSE}parameter>",
    re.DOTALL,
)


def contains_dsml(text: str) -> bool:
    """Cheap pre-check: does *text* hold a DSML tool_calls opening marker?"""
    return bool(text) and f"<{_BAR}DSML{_BAR}tool_calls>" in text


def _coerce_value(raw: str, is_string: str | None) -> object:
    """Map a parameter's raw inner text to a Python value.

    ``string="true"`` (or missing) keeps the raw string. ``string="false"``
    means a literal — try ``json.loads`` (handles numbers, bools, null), and
    fall back to the raw string when that fails.
    """
    if is_string == "false":
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def parse_dsml_tool_calls(content: str) -> Tuple[list[dict], str]:
    """Extract leaked DSML tool calls from *content*.

    Returns ``(tool_calls, remaining_text)`` where ``tool_calls`` is a list of
    ``{"id", "name", "arguments"}`` (``id`` is ``None`` — DSML carries none, the
    caller mints one) and ``remaining_text`` is *content* with every parsed DSML
    block stripped. On no match or any parse failure returns ``([], content)``.
    """
    if not contains_dsml(content):
        return [], content

    tool_calls: list[dict] = []
    try:
        for block in _BLOCK_RE.finditer(content):
            for invoke in _INVOKE_RE.finditer(block.group("body")):
                name = invoke.group("name")
                args: dict = {}
                for param in _PARAM_RE.finditer(invoke.group("body")):
                    args[param.group("name")] = _coerce_value(
                        param.group("value"), param.group("string")
                    )
                tool_calls.append({"id": None, "name": name, "arguments": args})
    except Exception:  # noqa: BLE001 — never let salvage crash the turn
        return [], content

    if not tool_calls:
        return [], content

    remaining = _BLOCK_RE.sub("", content).strip()
    return tool_calls, remaining
