import asyncio

import pytest

from mote.contracts.runtime.application import (
    ApplicationClosedError,
    ApplicationHealth,
    ApplicationNotReadyError,
    ApplicationState,
    ExpectedActive,
    ExpectedEmpty,
    ExpectedStateMismatchError,
    RuntimeGenerationId,
    RuntimeRoleConfigView,
    SourceRevision,
    StaleReloadError,
)
from mote.product.composition.model_application import (
    ActivationDisposition,
    ActivationLedgerCapacityError,
    ApplicationCompositionCandidate,
    AtomicApplicationComposition,
    CandidateState,
)


class _Handle:
    def __init__(self, identity: str) -> None:
        self.runtime_generation_id = RuntimeGenerationId(identity)
        self.topology_revision = f"topology:{identity}"
        self.releases = 0

    def retain(self):
        return self

    async def release(self) -> None:
        self.releases += 1


def _candidate(container, revision: str, identity: str = "runtime"):
    source_revision = SourceRevision(revision)
    sequence = container.accept_reload_request(source_revision)
    handle = _Handle(identity)
    return (
        ApplicationCompositionCandidate(
            source_revision=source_revision,
            reload_sequence=sequence,
            model=handle,
            runtime_role_config=RuntimeRoleConfigView("en"),
        ),
        handle,
    )


def test_empty_first_activation_and_drain() -> None:
    async def scenario() -> None:
        container = AtomicApplicationComposition()
        assert container.health is ApplicationHealth.NOT_READY
        with pytest.raises(ApplicationNotReadyError):
            await container.acquire()

        candidate, first = _candidate(container, "one")
        receipt = await container.activate(candidate, container.issue_activation_token(), ExpectedEmpty())
        assert candidate.state is CandidateState.COMMITTED
        assert container.health is ApplicationHealth.READY
        lease = await container.acquire()
        assert lease.application_generation_id == receipt.application_generation_id

        replacement, second = _candidate(container, "two", "runtime-2")
        await container.activate(
            replacement,
            container.issue_activation_token(),
            ExpectedActive(receipt.application_generation_id),
        )
        assert first.releases == 0
        await lease.aclose()
        assert first.releases == 1
        assert second.releases == 0
        assert await container.shutdown() is None
        assert second.releases == 1
        assert container.state is ApplicationState.CLOSED
        assert container.health is ApplicationHealth.CLOSED

    asyncio.run(scenario())


def test_failed_and_stale_candidates_are_closed() -> None:
    async def scenario() -> None:
        container = AtomicApplicationComposition()
        first, first_handle = _candidate(container, "one")
        first_receipt = await container.activate(first, container.issue_activation_token(), ExpectedEmpty())
        bad, bad_handle = _candidate(container, "two")
        token = container.issue_activation_token()
        with pytest.raises(ExpectedStateMismatchError):
            await container.activate(bad, token, ExpectedEmpty())
        result = await container.activation_result(token)
        assert result.disposition is ActivationDisposition.NOT_COMMITTED
        assert bad_handle.releases == 1

        slow, slow_handle = _candidate(container, "slow")
        _candidate(container, "newer-invalid")
        with pytest.raises(StaleReloadError):
            await container.activate(
                slow,
                container.issue_activation_token(),
                ExpectedActive(first_receipt.application_generation_id),
            )
        assert slow_handle.releases == 1
        assert first_handle.releases == 0

    asyncio.run(scenario())


def test_reload_cannot_expand_capabilities_or_change_trust_identity() -> None:
    async def scenario() -> None:
        container = AtomicApplicationComposition()
        first, _ = _candidate(container, "one")
        first.approved_capabilities = frozenset({"tools.read"})
        first.trust_revision = "trusted-v1"
        await container.activate(first, container.issue_activation_token(), ExpectedEmpty())

        expanded, expanded_handle = _candidate(container, "two", "runtime-2")
        expanded.approved_capabilities = frozenset({"tools.read", "tools.shell"})
        expanded.trust_revision = "trusted-v1"
        with pytest.raises(ApplicationNotReadyError, match="expands"):
            await container.activate(
                expanded,
                container.issue_activation_token(),
                ExpectedActive(container.current_generation_id),
            )
        assert expanded_handle.releases == 1

        changed, changed_handle = _candidate(container, "three", "runtime-3")
        changed.approved_capabilities = frozenset({"tools.read"})
        changed.trust_revision = "unapproved-checkout-change"
        with pytest.raises(ApplicationNotReadyError, match="trusted"):
            await container.activate(
                changed,
                container.issue_activation_token(),
                ExpectedActive(container.current_generation_id),
            )
        assert changed_handle.releases == 1
        await container.aclose()

    asyncio.run(scenario())


def test_empty_shutdown_is_idempotent() -> None:
    async def scenario() -> None:
        container = AtomicApplicationComposition()
        assert await container.shutdown() is None
        assert await container.shutdown() is None
        assert container.state is ApplicationState.CLOSED

    asyncio.run(scenario())


def test_product_config_view_is_published_atomically_and_defensively_copied() -> None:
    async def scenario() -> None:
        container = AtomicApplicationComposition()
        candidate, _handle = _candidate(container, "one")
        candidate.product_config = {"ui": {"language": "en"}}
        await container.activate(candidate, container.issue_activation_token(), ExpectedEmpty())
        candidate.product_config["ui"]["language"] = "mutated"

        lease = await container.acquire()
        view = lease.product_config
        view["ui"]["language"] = "caller-mutated"
        assert lease.product_config == {"ui": {"language": "en"}}
        await lease.aclose()
        await container.shutdown()

    asyncio.run(scenario())


def test_shutdown_finalizes_pending_activation_tokens() -> None:
    async def scenario() -> None:
        container = AtomicApplicationComposition()
        token = container.issue_activation_token()

        await container.shutdown()

        result = await container.activation_result(token)
        assert result.disposition is ActivationDisposition.NOT_COMMITTED
        assert isinstance(result.error, ApplicationClosedError)

    asyncio.run(scenario())


def test_activation_ledger_applies_backpressure_and_reclaims_final_results() -> None:
    async def scenario() -> None:
        now = [0.0]
        container = AtomicApplicationComposition(
            ledger_limit=1,
            ledger_retention_seconds=10.0,
            clock=lambda: now[0],
        )
        candidate, _handle = _candidate(container, "one")
        token = container.issue_activation_token()
        with pytest.raises(ActivationLedgerCapacityError):
            container.issue_activation_token()
        await container.activate(candidate, token, ExpectedEmpty())

        now[0] = 11.0
        replacement_token = container.issue_activation_token()
        assert replacement_token != token
        assert (await container.activation_result(token)).disposition is (
            ActivationDisposition.EXPIRED_CALLER_MUST_NOT_CLOSE
        )
        await container.shutdown()

    asyncio.run(scenario())
