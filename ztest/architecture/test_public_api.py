"""Stable public facades expose user concepts, not runtime infrastructure."""
from __future__ import annotations

import mote
import mote.output as output
import mote.tools as tools


def test_output_facade_is_small_and_stable() -> None:
    assert output.__all__ == [
        "OutputContract",
        "OutputRetryPolicy",
        "OutputValidator",
        "RunResult",
        "ValidationIssue",
    ]


def test_output_facade_excludes_runtime_infrastructure() -> None:
    for name in (
        "CommitFence",
        "OutputEngine",
        "OutputMigrationRegistry",
        "RunJournal",
        "RunLeaseCoordinator",
    ):
        assert not hasattr(output, name)


def test_tools_facade_is_protocol_explicit() -> None:
    assert tools.__all__ == [
        "CommandProtocol",
        "NativeFunctionToolset",
        "NativeDynamicToolset",
        "NativeApprovalPolicy",
        "NativeToolset",
        "Toolset",
        "ToolsetIdentity",
        "ToolsetProtocolError",
        "XmlFunctionToolset",
        "XmlDynamicToolset",
        "XmlApprovalPolicy",
        "XmlToolset",
    ]


def test_root_facade_is_small_and_stable() -> None:
    assert mote.__all__ == [
        "Agent",
        "AgentRunIncompleteError",
        "AgentRunRejectedError",
        "Engine",
        "Model",
        "ModelMessage",
        "OutputContract",
        "RunContext",
        "RunResult",
        "ToolContext",
        "NativeToolset",
        "Toolset",
        "XmlToolset",
    ]


def test_root_facade_excludes_internal_runtime_concepts() -> None:
    for name in (
        "AgentDependencies",
        "AgentWiring",
        "ComponentGraph",
        "Context",
        "Role",
        "RoleSchema",
        "RoleState",
        "RuntimeModules",
        "ThinkEngine",
        "ToolExecutor",
    ):
        assert not hasattr(mote, name)
