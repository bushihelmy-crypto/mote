"""Permission/approval subsystem.

Public surface:
  * :class:`PermissionEngine` — the runtime decision pipeline.
  * :class:`RuleStore` — parsed rules + behavior resolution.
  * Core data types from :mod:`mote.contracts.permissions`
    (re-exported here for convenience).

No module in this package imports tools or the executor, so it can be imported
from ``base_tool``/``tool_executor`` without circular dependencies.
"""
from __future__ import annotations

from mote.contracts.permissions import (
    DecisionReason,
    GrantScope,
    PermissionBehavior,
    PermissionDecision,
    PermissionFacts,
    PermissionMode,
    PermissionRule,
    RiskLevel,
    RuleSource,
)
from mote.runtime.tools.permission.classifier import SafetyAssessment, classify_command
from mote.runtime.tools.permission.engine import PermissionEngine
from mote.runtime.tools.permission.inspector import ToolCallInspector
from mote.runtime.tools.permission.rule_store import RuleStore

__all__ = [
    "PermissionEngine",
    "ToolCallInspector",
    "RuleStore",
    "SafetyAssessment",
    "classify_command",
    "PermissionDecision",
    "PermissionFacts",
    "PermissionRule",
    "DecisionReason",
    "PermissionMode",
    "PermissionBehavior",
    "GrantScope",
    "RuleSource",
    "RiskLevel",
]
