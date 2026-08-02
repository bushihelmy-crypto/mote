"""Focused lifecycle tests for Product Application activation."""

from types import SimpleNamespace

import pytest

import mote.product.composition.bootstrap as bootstrap


@pytest.mark.asyncio
async def test_activation_failure_settles_constructed_resources_in_reverse(monkeypatch, tmp_path) -> None:
    events: list[str] = []

    class Routing:
        async def prewarm(self) -> None:
            return None

        async def aclose(self) -> None:
            events.append("routing")

    class Context:
        async def aclose(self) -> None:
            events.append("context")

    container = SimpleNamespace(routing_models=Routing())
    context = Context()
    monkeypatch.setattr(bootstrap.ProductContainer, "standard", lambda *args, **kwargs: container)
    monkeypatch.setattr(bootstrap, "_build_application_context", lambda *args, **kwargs: context)

    async def fail_activation(*args, **kwargs):
        events.append("activate")
        raise RuntimeError("activation failed")

    monkeypatch.setattr(bootstrap, "activate_application_composition", fail_activation)
    request = bootstrap.ApplicationBuildRequest(
        config=SimpleNamespace(tools=SimpleNamespace(durable=SimpleNamespace(enabled=True, backend="jsonl"))),
        paths=SimpleNamespace(workspace_root=tmp_path),
        cwd=SimpleNamespace(),
    )

    with pytest.raises(RuntimeError, match="activation failed"):
        await bootstrap.activate_application(request)

    assert events == ["activate", "context", "routing"]


@pytest.mark.asyncio
async def test_success_publishes_only_the_canonical_application(monkeypatch, tmp_path) -> None:
    class Context:
        async def aclose(self) -> None:
            return None

    class Composition:
        async def aclose(self) -> None:
            return None

    class Routing:
        async def prewarm(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

    container = SimpleNamespace(routing_models=Routing(), agent_factory=object(), agents=object())
    context = Context()
    monkeypatch.setattr(bootstrap.ProductContainer, "standard", lambda *args, **kwargs: container)
    monkeypatch.setattr(bootstrap, "_build_application_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(bootstrap, "lifecycle_resources", lambda runtime: ())

    async def activate(*args, **kwargs):
        return Composition()

    monkeypatch.setattr(bootstrap, "activate_application_composition", activate)
    request = bootstrap.ApplicationBuildRequest(
        config=SimpleNamespace(tools=SimpleNamespace(durable=SimpleNamespace(enabled=True, backend="jsonl"))),
        paths=SimpleNamespace(workspace_root=tmp_path),
        cwd=SimpleNamespace(),
    )

    application = await bootstrap.activate_application(request)

    assert application.container is container
    assert application.services.context is context
    await application.aclose()
