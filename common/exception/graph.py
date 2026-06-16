"""Graph execution tier exceptions (``metagpt.executor.bggraph``).

The background graph engine runs a langgraph-style forward frontier. These are
terminal failures of a graph run — a conditional-edge router blew up or returned
an unknown key, the activation budget was exhausted, or one/more nodes failed at
the terminal step. They are named with a ``Graph`` prefix to disambiguate from
the model-routing :class:`~metagpt.common.exception.router.RouterError`.
"""

from __future__ import annotations

from typing import ClassVar

from metagpt.common.exception.base import MetaGPTError, NonRetryableError
from metagpt.common.exception.codes import ErrorCode


class GraphError(MetaGPTError):
    """Base for background-graph execution failures."""


class GraphRouterError(GraphError, NonRetryableError):
    """A conditional-edge router failed or returned an unknown key."""

    default_code: ClassVar[ErrorCode] = ErrorCode.GRAPH_ROUTER


class GraphRecursionError(GraphError, NonRetryableError):
    """Total node activations exceeded ``recursion_limit``."""

    default_code: ClassVar[ErrorCode] = ErrorCode.GRAPH_RECURSION


class GraphBatchFailureError(GraphError):
    """Raised at the terminal step when one or more nodes failed."""

    default_code: ClassVar[ErrorCode] = ErrorCode.GRAPH_BATCH_FAILURE

    def __init__(self, failures: list[tuple[str, BaseException]]):
        self.failures = failures
        names = ", ".join(n for n, _ in failures)
        super().__init__(f"Nodes failed: {names}")
