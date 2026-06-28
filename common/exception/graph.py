"""Graph execution tier exceptions (``metagpt.executor.bggraph``).

The background graph engine runs a langgraph-style forward frontier. These are
terminal failures of a graph run — a conditional-edge router blew up or returned
an unknown key, the activation budget was exhausted, or one/more nodes failed at
the terminal step. They are named with a ``Graph`` prefix to disambiguate from
the model-routing :class:`~metagpt.common.exception.router.RouterError`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from metagpt.common.exception.base import MetaGPTError, NonRetryableError, RetryableError
from metagpt.common.exception.codes import ErrorCode
from metagpt.common.exception.report import ErrorReport


class GraphError(MetaGPTError):
    """Base for background-graph execution failures.

    ``run_state`` / ``graph_state`` are declared here (default ``None``) so the
    driver can attach the run snapshot onto a terminal exception without
    monkey-patching arbitrary attributes onto the instance — the pool reads them
    back to capture the snapshot for resume.
    """

    run_state: Any = None
    graph_state: Any = None


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

    def detail(self) -> dict[str, Any]:
        """Expose each failed node as a nested :class:`ErrorReport` dict.

        Per-node failures are normalized through the same contract, so the
        renderer surfaces every node's code + message uniformly (not just the
        joined names in the top-level message).
        """

        return {
            "failures": [
                {"node": node, **ErrorReport.from_exception(exc).as_dict()}
                for node, exc in self.failures
            ]
        }


class GraphNodeTimeoutError(GraphError, RetryableError):
    """Node-level HTTP/network timeout — framework auto-retries, opaque to LLM/user."""

    default_code: ClassVar[ErrorCode] = ErrorCode.GRAPH_NODE_TIMEOUT


class GraphNodeRetryExhaustedError(GraphError, NonRetryableError):
    """Node retry budget exhausted — all auto-retries failed."""

    default_code: ClassVar[ErrorCode] = ErrorCode.GRAPH_NODE_RETRY_EXHAUSTED

    def __init__(self, node_name: str, attempts: int, cause: BaseException):
        self.node_name = node_name
        self.attempts = attempts
        super().__init__(
            f"Node '{node_name}' failed after {attempts} attempts: {cause}",
            cause=cause,
        )

    def detail(self) -> dict[str, Any]:
        return {"node": self.node_name, "attempts": self.attempts}


class GraphParamTypeError(GraphError, NonRetryableError):
    """Node param type mismatch — wiring error, not transient."""

    default_code: ClassVar[ErrorCode] = ErrorCode.GRAPH_PARAM_TYPE_ERROR

    def __init__(self, node: str, param: str, expected: type, got: type, **kw):
        msg = f"Node '{node}' param '{param}': expected {expected.__name__}, got {got.__name__}"
        super().__init__(msg, **kw)
        self.node = node
        self.param = param
        self.expected = expected
        self.got = got

    def detail(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "param": self.param,
            "expected": self.expected.__name__,
            "got": self.got.__name__,
        }
