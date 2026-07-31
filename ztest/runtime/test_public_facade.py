from __future__ import annotations

import pytest

from mote import Agent, AgentRunIncompleteError, AgentRunRejectedError, Engine, Model


class _IncompleteDriver:
    name = "incomplete"
    deps = object()

    async def run(self, with_message: str):
        return None


class _RejectedDriver:
    name = "rejected"
    deps = object()

    async def run(self, with_message: str):
        from mote.contracts.output import RunRejected, RunRejectionKind, TranscriptRef

        return RunRejected(
            kind=RunRejectionKind.PROMPT_ADMISSION,
            reason="organization policy denied the prompt",
            transcript=TranscriptRef(session_id="session"),
        )


@pytest.mark.asyncio
async def test_public_agent_rejects_a_run_without_committed_output() -> None:
    async def release() -> None:
        return None

    agent = Agent._create(
        driver=_IncompleteDriver(),
        release=release,
        is_open=lambda: True,
    )

    with pytest.raises(AgentRunIncompleteError, match="without a committed output"):
        await agent.run("hello")


@pytest.mark.asyncio
async def test_public_agent_surfaces_typed_admission_rejection() -> None:
    async def release() -> None:
        return None

    agent = Agent._create(
        driver=_RejectedDriver(),
        release=release,
        is_open=lambda: True,
    )

    with pytest.raises(AgentRunRejectedError, match="organization policy") as exc:
        await agent.run("hello")
    assert exc.value.rejection.kind.value == "prompt_admission"


@pytest.mark.asyncio
async def test_public_engine_mints_typed_handle_and_owns_its_lifecycle(tmp_path, monkeypatch) -> None:
    from mote.product.config.model.inputs import ProductEndpointInput, ShortcutModelsConfig
    from mote.product.config.schema import Config

    monkeypatch.setattr(
        "mote.engine.load_config",
        lambda *_args, **_kwargs: Config(
            models=ShortcutModelsConfig(
                default=ProductEndpointInput(model="gpt-4o", provider="openai", api_key="test-secret")
            )
        ),
    )
    dependencies = object()

    async with Engine(Model("gpt-4o", provider="openai"), cwd=tmp_path) as engine:
        agent = engine.agent(name="Worker", deps=dependencies, tools=[])
        assert agent.name == "Worker"
        assert agent.deps is dependencies
        assert engine.model == Model("gpt-4o", provider="openai")

    with pytest.raises(RuntimeError, match="closed"):
        await agent.run("too late")
