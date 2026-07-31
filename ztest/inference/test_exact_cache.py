import asyncio
from datetime import datetime, timedelta, timezone

from mote.contracts.model.invocation import (
    CanonicalMessage,
    CanonicalModelResponse,
    GenerateInput,
    GenerateOutput,
    ModelInvocation,
    ModelOperation,
)
from mote.contracts.model.topology import DefaultRoute
from mote.runtime.inference.cache import ExactCacheIdentity, MemoryExactInferenceCache, exact_cache_key


def _invocation(call_id="call"):
    return ModelInvocation(
        model_call_id=call_id,
        route_id=DefaultRoute(),
        task="cache-test",
        operation=ModelOperation.GENERATE,
        input=GenerateInput(messages=(CanonicalMessage(role="user", content="hello"),)),
    )


def _identity(tenant="tenant", namespace="default"):
    return ExactCacheIdentity(
        tenant_id=tenant,
        namespace=namespace,
        generation_revision="generation",
        policy_revision="policy",
        model_capability_identity="model-capabilities",
    )


def test_exact_cache_key_excludes_call_identity_but_isolates_tenant():
    assert exact_cache_key(_identity(), _invocation("a")) == exact_cache_key(_identity(), _invocation("b"))
    assert exact_cache_key(_identity("other"), _invocation()) != exact_cache_key(_identity(), _invocation())


def test_exact_cache_ttl_and_namespace_deletion():
    async def scenario():
        cache = MemoryExactInferenceCache(maximum_entries=2)
        identity = _identity()
        key = exact_cache_key(identity, _invocation())
        response = CanonicalModelResponse(output=GenerateOutput(content="cached"))
        now = datetime.now(timezone.utc)
        await cache.put(
            key,
            response,
            tenant_id=identity.tenant_id,
            namespace=identity.namespace,
            expires_at=now + timedelta(seconds=1),
        )
        assert await cache.get(key, now=now) == response
        assert await cache.delete_namespace("other", "default") == 0
        assert await cache.delete_namespace("tenant", "default") == 1
        assert await cache.get(key, now=now) is None

    asyncio.run(scenario())
