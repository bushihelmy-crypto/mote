"""Rule store — holds parsed rules and resolves the matching behavior.

Phase 1 wires two sources: the Role's declared config (``role``) and in-memory
``session`` rules added when a user picks "always" at an approval prompt. The
``project`` / ``local`` file-backed sources are reserved in the type system for
phase 2; no disk I/O happens here yet.

Resolution precedence is by behavior, not by source: a matching ``deny`` always
wins, then ``ask``, then ``allow``. This makes ``deny`` rules bypass-immune
(the engine consults the store before honoring ``bypass`` mode).
"""
from __future__ import annotations

from typing import Optional

from mote.contracts.permissions import PermissionBehavior, PermissionRule
from mote.contracts.settings.permissions import PermissionConfig
from mote.runtime.tools.permission.rule_matcher import parse_rule, rule_matches


class RuleStore:
    """A flat, ordered collection of permission rules with behavior precedence."""

    def __init__(self, rules: Optional[list[PermissionRule]] = None) -> None:
        self._rules: list[PermissionRule] = list(rules or [])

    @classmethod
    def from_config(cls, config: PermissionConfig) -> "RuleStore":
        """Build a store from a :class:`PermissionConfig`'s allow/deny/ask lists."""
        rules: list[PermissionRule] = []
        for spec in config.deny:
            rules.append(parse_rule(spec, "deny", source="role"))
        for spec in config.ask:
            rules.append(parse_rule(spec, "ask", source="role"))
        for spec in config.allow:
            rules.append(parse_rule(spec, "allow", source="role"))
        return cls(rules)

    def add_session_rule(self, rule: PermissionRule) -> None:
        """Remember a rule for the rest of the session (e.g. user chose 'always')."""
        self._rules.append(rule)

    def _has_match(self, behavior: PermissionBehavior, tool_name: str, target: str) -> bool:
        return any(r.behavior == behavior and rule_matches(r, tool_name, target) for r in self._rules)

    def resolve(self, tool_name: str, target: str) -> Optional[PermissionBehavior]:
        """Return the winning behavior for a call, or ``None`` if no rule matches.

        Precedence: deny > ask > allow.
        """
        if self._has_match("deny", tool_name, target):
            return "deny"
        if self._has_match("ask", tool_name, target):
            return "ask"
        if self._has_match("allow", tool_name, target):
            return "allow"
        return None

    def resolve_segments(self, tool_name: str, segments: list[str]) -> Optional[PermissionBehavior]:
        """Resolve a compound command by folding its segments strictest-wins.

        A command like ``git status && ./deploy.sh`` is split (by the caller)
        into per-segment targets; each is resolved independently and folded:

          * any segment denies                 -> ``deny``
          * else any segment asks              -> ``ask``
          * else every segment allows          -> ``allow``
          * else (a segment is unmatched)      -> ``None`` (defer to mode)

        This makes a ``deny`` rule catch the dangerous half of a compound
        command, and requires *every* segment to be allowed before the whole
        command rides an ``allow`` rule.
        """
        if not segments:
            return None
        behaviors = [self.resolve(tool_name, seg) for seg in segments]
        if any(b == "deny" for b in behaviors):
            return "deny"
        if any(b == "ask" for b in behaviors):
            return "ask"
        if all(b == "allow" for b in behaviors):
            return "allow"
        return None
