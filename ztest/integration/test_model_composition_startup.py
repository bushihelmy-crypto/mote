import asyncio

from mote.contracts.config.model.llm import LLMType
from mote.contracts.runtime.application import ExpectedEmpty, SourceRevision
from mote.product.composition.model_application import AtomicApplicationComposition
from mote.product.composition.model_builder import build_application_candidate
from mote.product.composition.model_reload import ApplicationReloadCoordinator
from mote.product.config.model.inputs import ProductEndpointInput, ShortcutModelsConfig
from mote.product.config.schema import Config
from mote.product.models.registry import LLMProviderRegistry


class _Provider:
    pass


def test_initial_generation_is_installed_before_admission(tmp_path) -> None:
    async def scenario() -> None:
        providers = LLMProviderRegistry()
        providers.register(LLMType.OPENAI, _Provider)
        config = Config(
            models=ShortcutModelsConfig(
                default=ProductEndpointInput(
                    provider="openai",
                    api_type="openai",
                    api_key="test-secret",
                    model="gpt-4o",
                )
            )
        )
        composition = AtomicApplicationComposition()
        source_revision = SourceRevision("source-one")
        sequence = composition.accept_reload_request(source_revision)
        candidate = await build_application_candidate(
            config,
            reload_sequence=sequence,
            source_revision=source_revision,
            providers=providers,
            oauth_root=tmp_path,
        )
        await composition.activate(candidate, composition.issue_activation_token(), ExpectedEmpty())
        application_lease = await composition.acquire()
        runtime_lease = await application_lease.acquire_runtime()
        assert runtime_lease.default_model.model == "gpt-4o"
        assert runtime_lease.generation_id
        assert runtime_lease.generation_artifact_digest.startswith("sha256:")
        assert runtime_lease.permit_issuer is not None
        assert runtime_lease.permit_audience.startswith("embedded/")
        assert runtime_lease.command_runtime is not None
        assert runtime_lease.session_runtime is not None
        assert runtime_lease.transfer_runtime is not None
        assert runtime_lease.artifact_store is not None
        assert runtime_lease.artifact_reader is not None
        await runtime_lease.aclose()
        await application_lease.aclose()
        await composition.shutdown()

    asyncio.run(scenario())


def test_reload_reuses_or_rebuilds_model_generation_from_full_reuse_key(tmp_path) -> None:
    async def scenario() -> None:
        providers = LLMProviderRegistry()
        providers.register(LLMType.OPENAI, _Provider)
        current = Config(
            models=ShortcutModelsConfig(
                default=ProductEndpointInput(
                    provider="openai",
                    api_type="openai",
                    api_key="test-secret",
                    model="gpt-4o",
                )
            )
        )
        composition = AtomicApplicationComposition()
        revision = SourceRevision("initial")
        candidate = await build_application_candidate(
            current,
            reload_sequence=composition.accept_reload_request(revision),
            source_revision=revision,
            providers=providers,
            oauth_root=tmp_path,
        )
        initial = await composition.activate(candidate, composition.issue_activation_token(), ExpectedEmpty())
        coordinator = ApplicationReloadCoordinator(
            composition=composition,
            load_config=lambda: current,
            providers=providers,
            oauth_root=tmp_path,
        )

        reused = await coordinator.reload()
        assert reused.runtime_generation_id == initial.runtime_generation_id
        for _ in range(99):
            reused = await coordinator.reload()
            assert reused.runtime_generation_id == initial.runtime_generation_id

        current = Config(
            models=ShortcutModelsConfig(
                default=ProductEndpointInput(
                    provider="openai",
                    api_type="openai",
                    api_key="rotated-secret",
                    model="gpt-4o",
                )
            )
        )
        rebuilt = await coordinator.reload()
        assert rebuilt.runtime_generation_id != initial.runtime_generation_id
        await composition.shutdown()

    asyncio.run(scenario())
