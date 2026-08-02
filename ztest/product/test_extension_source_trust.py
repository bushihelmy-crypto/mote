"""Focused trust/provenance tests for Product extension sources."""

from __future__ import annotations

from pathlib import Path

from mote.product.agents.markdown_loader import discover_md_agents
from mote.product.config.adapters.hooks import load_global_hooks
from mote.product.config.adapters.mcp import load_mcp_servers
from mote.product.extensions.sources import (
    ApprovedExtensionSnapshot,
    ExtensionApproval,
    ExtensionKind,
    ExtensionScope,
    ExtensionSourcePolicy,
)
from mote.product.skills.skill_pool import SkillPool


def _policy(tmp_path: Path, *approvals: ExtensionApproval) -> ExtensionSourcePolicy:
    user_root = tmp_path / "user"
    user_root.mkdir(exist_ok=True)
    return ExtensionSourcePolicy(
        user_root=user_root,
        builtin_roots=(tmp_path / "builtin",),
        snapshot=ApprovedExtensionSnapshot(tuple(approvals)),
    )


def _approval(policy: ExtensionSourcePolicy, kind: ExtensionKind, path: Path) -> ExtensionApproval:
    source = policy.inspect(kind, path)
    return ExtensionApproval(
        kind,
        source.canonical_path,
        source.device,
        source.inode,
        source.content_digest,
        "test:principal",
    )


def test_project_source_requires_exact_path_kind_and_digest_approval(tmp_path) -> None:
    first = tmp_path / "checkout" / "mcp.json"
    first.parent.mkdir()
    first.write_text('{"mcpServers": {}}')
    same_name = tmp_path / "other" / "mcp.json"
    same_name.parent.mkdir()
    same_name.write_text(first.read_text())
    baseline = _policy(tmp_path)
    approval = _approval(baseline, ExtensionKind.MCP, first)
    policy = _policy(tmp_path, approval)

    assert policy.inspect(ExtensionKind.MCP, first).approved is True
    assert policy.inspect(ExtensionKind.HOOK, first).approved is False
    assert policy.inspect(ExtensionKind.MCP, same_name).approved is False

    first.write_text('{"mcpServers": {"changed": {}}}')
    assert policy.inspect(ExtensionKind.MCP, first).approved is False


def test_project_approval_is_not_reused_after_same_content_inode_replacement(tmp_path) -> None:
    source_path = tmp_path / "checkout" / "hooks.json"
    source_path.parent.mkdir()
    source_path.write_text('{"hooks": {}}')
    baseline = _policy(tmp_path)
    approval = _approval(baseline, ExtensionKind.HOOK, source_path)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(source_path.read_bytes())
    replacement.replace(source_path)

    assert _policy(tmp_path, approval).inspect(ExtensionKind.HOOK, source_path).approved is False


def test_unapproved_checkout_agent_never_enters_catalog_input(tmp_path) -> None:
    agent = tmp_path / ".mote" / "agents" / "reviewer.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nname: reviewer\ndescription: Review code\n---\nReview carefully.")
    baseline = _policy(tmp_path)

    assert discover_md_agents(tmp_path, source_policy=baseline) == {}

    approved = _policy(tmp_path, _approval(baseline, ExtensionKind.AGENT, agent))
    assert set(discover_md_agents(tmp_path, source_policy=approved)) == {"reviewer"}

    agent.write_text(agent.read_text() + "\nChanged instruction.")
    assert discover_md_agents(tmp_path, source_policy=approved) == {}
    agent.unlink()
    assert discover_md_agents(tmp_path, source_policy=approved) == {}


def test_skill_pool_consumes_only_digest_bound_sources(tmp_path) -> None:
    skill = tmp_path / "checkout" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: Review code\n---\nReview carefully.")
    baseline = _policy(tmp_path)
    denied_pool = SkillPool(source_dirs=[skill.parents[1]], source_policy=baseline)
    denied_pool.load_all()
    assert denied_pool.get_all() == []

    approved = _policy(tmp_path, _approval(baseline, ExtensionKind.SKILL, skill))
    approved_pool = SkillPool(source_dirs=[skill.parents[1]], source_policy=approved)
    approved_pool.load_all()
    assert [item.name for item in approved_pool.get_all()] == ["review"]


def test_user_install_is_distinct_from_project_approval(tmp_path) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    hook = user_root / "hooks.json"
    hook.write_text('{"hooks": {}}')
    policy = ExtensionSourcePolicy(user_root=user_root, builtin_roots=())

    source = policy.inspect(ExtensionKind.HOOK, hook)
    assert source.scope is ExtensionScope.USER
    assert source.approved is True
    assert source.approval_principal == "mote:user-install"


def test_hook_and_mcp_decode_only_approved_snapshot_bytes(tmp_path) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    hook = user_root / "hooks.json"
    hook.write_text('{"hooks": {}}')
    mcp = user_root / "mcp.json"
    mcp.write_text('{"mcpServers": {"local": {"command": "fixed"}}}')
    policy = ExtensionSourcePolicy(user_root=user_root, builtin_roots=())

    hook_source = policy.inspect(ExtensionKind.HOOK, hook)
    mcp_source = policy.inspect(ExtensionKind.MCP, mcp)
    hook.write_text("malformed after snapshot")
    mcp.write_text("malformed after snapshot")

    assert load_global_hooks((hook_source,)) is None
    assert [server.name for server in load_mcp_servers((mcp_source,))] == ["local"]


def test_malformed_approved_extension_fails_closed(tmp_path) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    path = user_root / "mcp.json"
    path.write_text("not json")
    policy = ExtensionSourcePolicy(user_root=user_root, builtin_roots=())

    source = policy.inspect(ExtensionKind.MCP, path)
    try:
        load_mcp_servers((source,))
    except ValueError as error:
        assert "malformed" in str(error)
    else:
        raise AssertionError("malformed approved MCP source must fail closed")
