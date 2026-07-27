"""Stable, implementation-free protocols and persisted data contracts.

This package is the future dependency leaf.  Runtime services, provider SDKs,
product tools, and process-local resources must never be introduced here.
"""

from mote.contracts.background_tasks import BackgroundTaskService, BackgroundTaskServiceFactory
from mote.contracts.model_actions import AgentAction, FinalCandidateAction, ModelTurn, TextAction, ToolCallAction
from mote.contracts.models import LLMResponse, LLMToolCall, WebSearchHit
from mote.contracts.output import (
    CommittedOutput,
    OutputContractId,
    OutputEvaluation,
    RunOutcome,
    RunRejected,
    RunRejectionKind,
    RunResult,
    TranscriptRef,
    ValidationIssue,
)
from mote.contracts.tools import ToolEffect

__all__ = [
    "AgentAction",
    "BackgroundTaskService",
    "BackgroundTaskServiceFactory",
    "CommittedOutput",
    "FinalCandidateAction",
    "LLMResponse",
    "LLMToolCall",
    "ModelTurn",
    "OutputContractId",
    "OutputEvaluation",
    "RunOutcome",
    "RunRejectionKind",
    "RunRejected",
    "RunResult",
    "TextAction",
    "ToolCallAction",
    "ToolEffect",
    "TranscriptRef",
    "ValidationIssue",
    "WebSearchHit",
]
