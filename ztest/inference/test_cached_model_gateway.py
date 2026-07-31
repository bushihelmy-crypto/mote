import asyncio
from decimal import Decimal

from mote.contracts.model.invocation import (
    CanonicalMessage,
    CanonicalToolDefinition,
    GenerateInput,
    GenerateOutput,
    ModelInvocation,
    ModelOperation,
    ModelUsage,
    RequestRequirements,
    ResolvedModelResponse,
    ResponseMode,
)
from mote.contracts.model.topology import DefaultRoute
from mote.runtime.inference.cache import ExactCacheIdentity, MemoryExactInferenceCache
from mote.runtime.models.cached_gateway import ExactCachedModelGateway


class Gateway:
    def __init__(self):
        self.calls = 0

    def supports_route(self, route):
        return True

    def route_profile(self, route):
        return None

    def route_profiles(self, route):
        return ()

    async def execute(self, invocation, **kwargs):
        self.calls += 1
        return ResolvedModelResponse(
            output=GenerateOutput(content="wire"),
            usage=ModelUsage(total_tokens=1),
            cost_usd=Decimal("0.1"),
            endpoint_id="endpoint",
            endpoint_fingerprint="fingerprint",
            model_or_deployment="model",
            tenant_fingerprint="tenant",
            credential_slot_id="slot",
            provider="provider",
            transport="http",
            model_call_id=invocation.model_call_id,
            successful_attempt_id="attempt",
        )

    async def resume(self, invocation, **kwargs):
        raise AssertionError("not used")


def _invocation(identifier, *, tools=(), classification="default"):
    return ModelInvocation(
        model_call_id=identifier,
        route_id=DefaultRoute(),
        task="cache",
        operation=ModelOperation.GENERATE,
        input=GenerateInput(
            messages=(CanonicalMessage(role="user", content="same"),),
            tools=tools,
        ),
        requirements=RequestRequirements(
            needs_tools=bool(tools),
            response_mode=ResponseMode.NATIVE_TOOLS if tools else ResponseMode.TEXT,
            data_classification=classification,
        ),
    )


def test_cache_hit_skips_second_wire_owner_call():
    async def scenario():
        wire = Gateway()
        gateway = ExactCachedModelGateway(
            wire,
            MemoryExactInferenceCache(),
            identity=ExactCacheIdentity("tenant", "default", "generation", "policy", "model"),
            ttl_seconds=60,
        )
        first = await gateway.execute(_invocation("call-1"))
        second = await gateway.execute(_invocation("call-2"))
        assert first.provider == "provider"
        assert second.provider == "cache"
        assert second.model_call_id == "call-2"
        assert second.successful_attempt_id == ""
        assert second.summary["provider_request_id"] is None
        assert wire.calls == 1

    asyncio.run(scenario())


def test_cache_failure_degrades_to_provider_without_retrying_cache():
    class BrokenCache(MemoryExactInferenceCache):
        async def get(self, key, *, now=None):
            raise RuntimeError("cache unavailable")

        async def put(self, *args, **kwargs):
            raise RuntimeError("cache unavailable")

    async def scenario():
        wire = Gateway()
        gateway = ExactCachedModelGateway(
            wire,
            BrokenCache(),
            identity=ExactCacheIdentity("tenant", "default", "generation", "policy", "model"),
            ttl_seconds=60,
        )
        assert (await gateway.execute(_invocation("call"))).provider == "provider"
        assert wire.calls == 1

    asyncio.run(scenario())


def test_tools_sensitive_stream_and_resume_bypass_cache():
    async def scenario():
        wire = Gateway()
        gateway = ExactCachedModelGateway(
            wire,
            MemoryExactInferenceCache(),
            identity=ExactCacheIdentity("tenant", "default", "generation", "policy", "model"),
            ttl_seconds=60,
        )
        tool = CanonicalToolDefinition(name="read")
        await gateway.execute(_invocation("tool-1", tools=(tool,)))
        await gateway.execute(_invocation("tool-2", tools=(tool,)))
        await gateway.execute(_invocation("secret-1", classification="secret"))
        await gateway.execute(_invocation("secret-2", classification="secret"))
        await gateway.execute(_invocation("stream-1"), stream=True)
        await gateway.execute(_invocation("stream-2"), stream=True)
        assert wire.calls == 6

        try:
            await gateway.resume(_invocation("resume"))
        except AssertionError as exc:
            assert str(exc) == "not used"
        else:
            raise AssertionError("resume did not reach the wrapped gateway")

    asyncio.run(scenario())
