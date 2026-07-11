"""Permission/approval subsystem.

Public surface:
  * :class:`PermissionEngine` — the runtime decision pipeline.
  * :class:`RuleStore` — parsed rules + behavior resolution.
  * Core data types from :mod:`mote.common.schema.permission_types`
    (re-exported here for convenience).

No module in this package imports tools or the executor, so it can be imported
from ``base_tool``/``tool_executor`` without circular dependencies.
"""
from __future__ import annotations

from mote.common.schema.permission_types import (
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
from mote.executor.permission.classifier import SafetyAssessment, classify_command
from mote.executor.permission.engine import PermissionEngine
from mote.executor.permission.inspector import Inspection, ToolCallInspector
from mote.executor.permission.rule_store import RuleStore
from mote.executor.permission.subscriber import PermissionSubscriber

__all__ = [
    "PermissionEngine",
    "PermissionSubscriber",
    "ToolCallInspector",
    "Inspection",
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
