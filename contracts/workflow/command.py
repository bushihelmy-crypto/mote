"""Typed caller command vocabulary for durable Workflow runs."""

from enum import Enum


class WorkflowCancelReason(str, Enum):
    USER_REQUEST = "user_request"
    AGENT_REQUEST = "agent_request"
    DEADLINE_POLICY = "deadline_policy"


__all__ = ["WorkflowCancelReason"]
