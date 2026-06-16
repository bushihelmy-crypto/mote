"""Permission/approval subsystem.

Public surface:
  * :class:`PermissionEngine` — the runtime decision pipeline.
  * :class:`RuleStore` — parsed rules + behavior resolution.
  * Core data types from :mod:`metagpt.common.schema.permission_types`
    (re-exported here for convenience).

No module in this package imports tools or the executor, so it can be imported
from ``base_tool``/``tool_executor`` without circular dependencies.
"""
from __future__ import annotations

from metagpt.executor.permission.classifier import SafetyAssessment, classify_command
from metagpt.executor.permission.engine import PermissionEngine
from metagpt.executor.permission.rule_store import RuleStore
from metagpt.common.schema.permission_types import (
    DecisionReason,
    GrantScope,
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
    RiskLevel,
    RuleSource,
)

__all__ = [
    "PermissionEngine",
    "RuleStore",
    "SafetyAssessment",
    "classify_command",
    "PermissionDecision",
    "PermissionRule",
    "DecisionReason",
    "PermissionMode",
    "PermissionBehavior",
    "GrantScope",
    "RuleSource",
    "RiskLevel",
]
