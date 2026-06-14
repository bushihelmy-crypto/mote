"""Rule parsing and matching.

Parses ``Tool(pattern)`` rule specs into :class:`PermissionRule` and decides
whether a parsed rule matches a concrete tool call. Pattern matching uses
``fnmatch`` so the familiar glob syntax (``*`` / ``?``) works out of the box —
e.g. ``Bash(git*)``, ``Bash(npm install)``, ``Write(/tmp/*)``.

Tool-name matching supports three forms:
  * exact:        ``Bash``                  matches the ``Bash`` tool
  * MCP namespace ``mcp__server``           matches every ``mcp__server__tool``
  * glob:         ``mcp__server__*``        matches via fnmatch
"""
from __future__ import annotations

from fnmatch import fnmatch

from metagpt.executor.permission.types import PermissionBehavior, PermissionRule, RuleSource

# Sentinel separating an MCP server from its tool name, e.g. ``mcp__github__search``.
_MCP_PREFIX = "mcp__"


def parse_rule(spec: str, behavior: PermissionBehavior, source: RuleSource = "session") -> PermissionRule:
    """Parse a single rule spec like ``Bash(git commit)`` or ``Read``.

    The pattern is whatever sits inside the outermost parentheses. A spec with
    no parentheses is a whole-tool rule (``pattern is None``). Parentheses
    inside the pattern itself are preserved (we split on the FIRST ``(`` and the
    LAST ``)``), so ``Bash(echo (hi))`` yields pattern ``echo (hi)``.
    """
    spec = spec.strip()
    open_idx = spec.find("(")
    if open_idx == -1 or not spec.endswith(")"):
        return PermissionRule(tool_name=spec, pattern=None, behavior=behavior, source=source)
    tool_name = spec[:open_idx].strip()
    pattern = spec[open_idx + 1 : -1].strip()
    # An empty pattern "Tool()" is treated as a whole-tool rule.
    return PermissionRule(tool_name=tool_name, pattern=pattern or None, behavior=behavior, source=source)


def _tool_name_matches(rule_tool: str, tool_name: str) -> bool:
    """Return True if ``rule_tool`` applies to the call's ``tool_name``."""
    if rule_tool == tool_name:
        return True
    # MCP namespace rule: "mcp__server" covers every "mcp__server__<tool>".
    if rule_tool.startswith(_MCP_PREFIX) and "__" not in rule_tool[len(_MCP_PREFIX):]:
        return tool_name.startswith(rule_tool + "__")
    # Glob form, e.g. "mcp__server__*" or "Bash*".
    if any(ch in rule_tool for ch in "*?[") and fnmatch(tool_name, rule_tool):
        return True
    return False


def rule_matches(rule: PermissionRule, tool_name: str, target: str) -> bool:
    """Return True if ``rule`` matches a call to ``tool_name`` with ``target``.

    ``target`` is the tool's permission-target string (command, path, ...). It
    is only consulted when the rule carries a ``pattern``; a whole-tool rule
    (``pattern is None``) matches on the tool name alone.
    """
    if not _tool_name_matches(rule.tool_name, tool_name):
        return False
    if rule.pattern is None:
        return True
    return fnmatch(target or "", rule.pattern)
