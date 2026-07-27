from __future__ import annotations

from mote.product.toolsets import BUILTIN_TOOL_GROUPS, builtin_toolsets


def test_builtin_toolsets_have_unique_stable_ownership() -> None:
    owners: dict[str, str] = {}
    for toolset_id, names in BUILTIN_TOOL_GROUPS.items():
        assert toolset_id.startswith("mote.") and toolset_id.endswith(".v1")
        for name in names:
            assert name not in owners, f"{name} is owned by both {owners[name]} and {toolset_id}"
            owners[name] = toolset_id


def test_retired_task_tools_are_not_product_toolset_members() -> None:
    names = set().union(*BUILTIN_TOOL_GROUPS.values())
    assert "ResumeTasks" not in names
    assert "GetNodeStates" not in names


def test_toolsets_resolve_only_their_owned_tools() -> None:
    toolsets = {toolset.id: toolset for toolset in builtin_toolsets()}
    workspace = toolsets["mote.workspace.v1"]
    execution = toolsets["mote.execution.v1"]

    workspace.prepare()
    assert workspace.get("Read") is not None
    assert workspace.get("Bash") is None
    assert execution.get("Bash") is not None
    assert execution.get("Read") is None
