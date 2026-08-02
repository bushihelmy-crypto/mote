from __future__ import annotations

import pytest

from mote.contracts.runtime.application import ApplicationState
from mote.product.config.model.inputs import ProductEndpointInput, ShortcutModelsConfig
from mote.product.config.schema import Config
from mote.product.entrypoints.cli.bootstrap import build_engine


def _config(api_key: str | None = "test-secret") -> Config:
    return Config(
        models=ShortcutModelsConfig(
            default=ProductEndpointInput(
                model="gpt-4o",
                provider="openai",
                api_key=api_key,
            )
        )
    )


@pytest.mark.asyncio
async def test_cli_installs_initial_generation_before_engine_is_returned(tmp_path) -> None:
    engine = await build_engine(config=_config(), cwd=str(tmp_path))
    composition = engine.services.application_composition
    assert composition is not None
    assert composition.state is ApplicationState.ACTIVE
    await engine.aclose()


@pytest.mark.asyncio
async def test_cli_initial_generation_failure_does_not_return_engine(tmp_path) -> None:
    with pytest.raises(ValueError, match="has no credential"):
        await build_engine(config=_config(None), cwd=str(tmp_path))
