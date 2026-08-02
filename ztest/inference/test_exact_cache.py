import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from mote.contracts.model.invocation import (
    CanonicalMessage,
    CanonicalModelResponse,
    CanonicalToolDefinition,
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


def test_every_semantic_prompt_tool_schema_capability_and_policy_field_invalidates():
    invocation = _invocation()
    identity = _identity()
    baseline = exact_cache_key(identity, invocation)
    semantic_inputs = (
        invocation.model_copy(update={"input": invocation.input.model_copy(update={"system_prompt": "changed"})}),
        invocation.model_copy(
            update={
                "input": invocation.input.model_copy(
                    update={
                        "tools": (
                            CanonicalToolDefinition(
                                name="Read", description="read a file", input_schema={"type": "object"}
                            ),
                        )
                    }
                )
            }
        ),
        invocation.model_copy(
            update={"input": invocation.input.model_copy(update={"output_schema": {"type": "string"}})}
        ),
    )
    for changed in semantic_inputs:
        assert exact_cache_key(identity, changed) != baseline
    for changed_identity in (
        replace(identity, generation_revision="next-generation"),
        replace(identity, policy_revision="next-policy"),
        replace(identity, model_capability_identity="next-capabilities"),
    ):
        assert exact_cache_key(changed_identity, invocation) != baseline


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
