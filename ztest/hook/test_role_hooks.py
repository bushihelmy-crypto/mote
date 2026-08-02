#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Role-level hook wiring: SessionStart once, UserPromptSubmit context, Stop.

Drives ``Role.run()`` with a stubbed loop so the test stays offline: the loop is
replaced with one that records nothing and returns None, while the hook seams in
run() still fire. ``hooks=None`` + no callbacks => zero hook calls (backward
compat).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from mote.contracts.events.conversation import PromptRejectedEvent, UserPromptSubmitEvent
from mote.contracts.output import RunRejected, RunRejectionKind
from mote.contracts.ports.events.telemetry import TelemetryIdentity, TelemetryOverflow, TelemetrySubscriptionSpec
from mote.runtime.agent import AgentWiring, Role
from mote.runtime.agent.role_schema import RoleSchema
from mote.runtime.events.telemetry import AllTelemetryBinding
from mote.runtime.hook.types import HookOutcome


class _StubFlowEngine:
    """Minimal flow-engine stand-in returning no result."""

    latest_observed_msg = None

    async def run(self):
        return None


class _OfflineLLM:
    """Minimal provider used by Role's lazy compression dependency."""

    def __init__(self, model: str):
        self.model = model
        self.cost_manager = None
        self.rate_limit_tracker = None
        self.context_reducer = None


class _EventCapture:
    def __init__(self):
        self.events: list = []

    async def handle(self, event):
        self.events.append(event)


@pytest_asyncio.fixture
async def role_in_tmp(tmp_path, monkeypatch):
    from mote.kernel.output import text_output_contract
    from mote.product.agents.factory import CodingAgentFactory
    from mote.product.paths import default_runtime_paths
    from mote.runtime.models.clients.context import Context
    from mote.ztest.model_fakes import FakeApplicationComposition, FakeModelGateway, offline_config

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)
    # This suite exercises hook wiring, not the advisory whole-repo cold index.
    monkeypatch.setattr("mote.runtime.code_map.indexer.RepoIndexer.scan_all", lambda self: None)

    async def no_git_snapshot(_cwd):
        return None

    monkeypatch.setattr(
        "mote.runtime.context.turn.sources.git.collect_git_state",
        no_git_snapshot,
    )
    context = Context()
    config = offline_config()
    config = config.model_copy(
        update={
            "secrets": config.secrets.model_copy(
                update={
                    "vault_path": str(tmp_path / "vault.json"),
                    "secrets_config_path": str(tmp_path / "secrets_config.json"),
                }
            )
        }
    )
    composition = FakeApplicationComposition(FakeModelGateway(_OfflineLLM("test")))
    paths = default_runtime_paths(
        user_config_root=tmp_path / "config",
        workspace_root=tmp_path / "workspace",
    )
    role = Role(
        config=config,
        role_schema=RoleSchema(name="Hooked", generate_title=False),
        wiring=AgentWiring.for_context(
            context,
            application_composition=composition,
            dependencies=CodingAgentFactory(paths=paths).dependencies(
                deps=None, output_contract=text_output_contract()
            ),
        ),
    )
    # Replace the loop with a no-op stub so run() exercises only the hook seams.
    # run() builds its flow engine via the component graph; seed that slot with
    # a factory yielding the stub so the test stays independent of the engine.
    role._components._graph.seed(role._components._execution_engine_factory_key, lambda: _StubFlowEngine())
    try:
        yield role
    finally:
        await role.cleanup()
        await context.aclose()


@pytest.mark.asyncio
async def test_no_hooks_is_zero_overhead(role_in_tmp):
    # No HookConfig and no registered callbacks => hook_manager stays None.
    assert role_in_tmp.hook_manager is None
    await role_in_tmp.run(with_message="hello")
    assert role_in_tmp.hook_manager is None


@pytest.mark.asyncio
async def test_session_start_fired_once(role_in_tmp):
    events: list[str] = []
    role_in_tmp.register_hook("SessionStart", lambda hi: events.append(hi.payload.source))
    await role_in_tmp.run(with_message="one")
    await role_in_tmp.run(with_message="two")
    # SessionStart fires exactly once across multiple run() calls.
    assert events == ["startup"]


@pytest.mark.asyncio
async def test_user_prompt_submit_injects_context(role_in_tmp):
    seen_prompts: list[str] = []

    role_in_tmp.register_hook("UserPromptSubmit", lambda hi: {"additionalContext": "PROJECT RULES"})

    # Capture what actually got pushed into the buffer.
    pushed: list = []
    orig_put = role_in_tmp.put_message
    role_in_tmp.put_message = lambda m: (pushed.append(m), orig_put(m))[1]

    await role_in_tmp.run(with_message="do the thing")
    assert pushed, "a message should have been queued"
    assert pushed[0].content.startswith("PROJECT RULES")
    assert "do the thing" in pushed[0].content


@pytest.mark.asyncio
async def test_prompt_rejection_never_enters_history_or_starts_flow(role_in_tmp):
    raw_secret = "deny-boundary-secret"
    capture = _EventCapture()
    handle = await role_in_tmp.telemetry.subscribe_all(
        TelemetrySubscriptionSpec(
            identity=TelemetryIdentity("mote.test.role_hook_prompt_rejection"),
            capacity=16,
            overflow=TelemetryOverflow.DROP_NEWEST,
        ),
        capture,
    )
    role_in_tmp.register_hook(
        "UserPromptSubmit",
        lambda _hi: {
            "decision": "block",
            "systemMessage": f"policy denied {raw_secret}",
        },
    )

    def forbidden_flow():
        pytest.fail("PromptPolicy rejection must not construct a flow engine")

    role_in_tmp._components._graph.seed(role_in_tmp._components._execution_engine_factory_key, forbidden_flow)
    history_before = list(role_in_tmp.state.context.messages)

    result = await role_in_tmp.run(with_message=(f'use <secret name="denied_token">{raw_secret}</secret> now'))
    await role_in_tmp.telemetry.drain()

    assert isinstance(result, RunRejected)
    assert result.kind is RunRejectionKind.PROMPT_ADMISSION
    assert raw_secret not in repr(result)
    assert role_in_tmp.state.context.messages == history_before
    rejected = [e for e in capture.events if isinstance(e, PromptRejectedEvent)]
    assert len(rejected) == 1
    assert not any(isinstance(e, UserPromptSubmitEvent) for e in capture.events)
    assert raw_secret not in repr(rejected[0])
    assert raw_secret not in role_in_tmp._components.session_log.path.read_text(encoding="utf-8")
    await handle.aclose()


@pytest.mark.asyncio
async def test_secret_policy_failure_withholds_prompt_before_role_boundary(role_in_tmp, monkeypatch):
    raw_secret = "vault-failure-secret"
    capture = _EventCapture()
    handle = await role_in_tmp.telemetry.subscribe_all(
        TelemetrySubscriptionSpec(
            identity=TelemetryIdentity("mote.test.role_hook_secret_failure"),
            capacity=16,
            overflow=TelemetryOverflow.DROP_NEWEST,
        ),
        capture,
    )
    store = role_in_tmp.prompt_policy._secret_store
    assert store is not None

    def fail_capture(_value):
        raise OSError("vault unavailable")

    def forbidden_flow():
        pytest.fail("secret-policy failure must not construct a flow engine")

    monkeypatch.setattr(store, "add_session_secret", fail_capture)
    role_in_tmp._components._graph.seed(role_in_tmp._components._execution_engine_factory_key, forbidden_flow)
    history_before = list(role_in_tmp.state.context.messages)

    result = await role_in_tmp.run(with_message=f"use <secret>{raw_secret}</secret> now")
    await role_in_tmp.telemetry.drain()

    assert isinstance(result, RunRejected)
    assert result.terminate is True
    assert raw_secret not in repr(result)
    assert role_in_tmp.state.context.messages == history_before
    rejected = [e for e in capture.events if isinstance(e, PromptRejectedEvent)]
    assert len(rejected) == 1
    assert rejected[0].redacted_excerpt.startswith("[prompt withheld")
    assert rejected[0].prompt_digest.startswith("sha256:")
    assert raw_secret not in repr(capture.events)
    assert raw_secret not in role_in_tmp._components.session_log.path.read_text(encoding="utf-8")
    await handle.aclose()


@pytest.mark.asyncio
async def test_stop_fired_in_finally(role_in_tmp):
    fired: list[str] = []
    role_in_tmp.register_hook("Stop", lambda _hi: fired.append("Stop"))
    await role_in_tmp.run(with_message="x")
    assert fired == ["Stop"]


@pytest.mark.asyncio
async def test_hook_config_engages_manager(tmp_path, monkeypatch):
    from mote.runtime.config.hook import HookConfig
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)
    role = Role(
        role_schema=RoleSchema(name="Cfg", hooks=HookConfig()),
        wiring=AgentWiring.for_context(Context()),
    )
    # A declared HookConfig (even empty events) engages the manager.
    assert role.hook_manager is not None


@pytest.mark.asyncio
async def test_global_hooks_json_engages_manager(tmp_path, monkeypatch):
    """A global ``~/.mote/hooks.json`` engages the hook layer for a Role that
    declares no per-Role ``HookConfig`` and registers no callbacks."""
    import json
    from dataclasses import replace

    from mote.contracts.tool.identity import (
        ToolAttemptOrdinal,
        ToolInvocationId,
        ToolInvocationIdentity,
        tool_arguments_digest,
    )
    from mote.product.config.adapters.hooks import load_global_hooks
    from mote.product.extensions.sources import ExtensionKind, ExtensionSourcePolicy
    from mote.product.paths import default_runtime_paths, mote_layered_files
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "handlers": [{"type": "command", "id": "deny", "argv": ["/bin/sh", "-c", "exit 2"]}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    paths = default_runtime_paths(user_config_root=home)
    source_policy = ExtensionSourcePolicy(
        user_root=home,
        builtin_roots=(tmp_path,),
    )
    from mote.contracts.agent import ApprovedDeclaration
    from mote.kernel.output import text_output_contract
    from mote.product.agents.factory import CodingAgentFactory

    hook_sources = source_policy.admitted_files(
        ExtensionKind.HOOK,
        mote_layered_files(
            "hooks.json",
            tmp_path,
            user_config_root=paths.user_config_root,
        ),
    )
    hook_config = load_global_hooks(hook_sources)
    assert hook_config is not None
    dependencies = CodingAgentFactory(
        paths=paths,
        hooks=ApprovedDeclaration(
            hook_config,
            tuple(source.approved_identity() for source in hook_sources),
        ),
    ).dependencies(deps=None, output_contract=text_output_contract())

    role = Role(
        role_schema=RoleSchema(name="Global"),
        wiring=AgentWiring.for_context(Context(), dependencies=dependencies),
    )
    # hooks=None + no callbacks, yet the global file engages the layer.
    assert role.role_schema.hooks is None
    assert role.hook_manager is not None

    # And the loaded command hook actually fires: a matched Bash tool blocks
    # (the `exit 2` handler signals deny).
    arguments: dict[str, object] = {}
    identity = ToolInvocationIdentity(
        ToolInvocationId("role-hook-test"),
        ToolAttemptOrdinal(1),
        "role-hook-definition",
        1,
        tool_arguments_digest(arguments),
        "role-hook-owner",
        "role-hook-run",
    )
    outcome = await role.hook_manager.fire(
        "PreToolUse",
        {"identity": identity, "tool_name": "Bash", "tool_input": arguments},
    )
    assert outcome.behavior == "deny"

    # An unmatched tool selects no handler → EMPTY (no block).
    outcome_read = await role.hook_manager.fire("PreToolUse", {"tool_name": "Read"})
    assert outcome_read.behavior != "deny"
