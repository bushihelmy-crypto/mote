import asyncio

from mote.contracts.model.invocation import (
    CanonicalMessage,
    CanonicalModelResponse,
    GenerateInput,
    GenerateOutput,
    ModelInvocation,
    ModelOperation,
)
from mote.contracts.model.topology import DefaultRoute
from mote.contracts.ports.inference.cache import SemanticCacheCandidate
from mote.runtime.inference.cache import ExactCacheIdentity
from mote.runtime.inference.semantic_cache import SemanticCachePlanner


def test_semantic_lookup_embedding_is_privileged_bypass_and_tenant_scoped():
    class Backend:
        def __init__(self):
            self.lookup_args = None

        async def lookup(self, embedding, **kwargs):
            self.lookup_args = (embedding, kwargs)
            return (
                SemanticCacheCandidate(
                    "candidate",
                    0.98,
                    CanonicalModelResponse(output=GenerateOutput(content="hit")),
                ),
            )

    async def scenario():
        backend = Backend()
        requests = []

        async def embed(request):
            requests.append(request)
            return (0.1, 0.2)

        planner = SemanticCachePlanner(
            backend,
            identity=ExactCacheIdentity("tenant", "namespace", "generation", "policy", "model"),
            threshold=0.95,
            embed=embed,
        )
        invocation = ModelInvocation(
            model_call_id="call",
            route_id=DefaultRoute(),
            task="semantic",
            operation=ModelOperation.GENERATE,
            input=GenerateInput(messages=(CanonicalMessage(role="user", content="hello"),)),
        )
        candidate = await planner.lookup(invocation)
        assert candidate is not None
        assert candidate.response.output.content == "hit"
        assert requests[0].origin == "semantic_cache_lookup"
        assert requests[0].cache_mode == "bypass"
        assert backend.lookup_args[1] == {
            "tenant_id": "tenant",
            "namespace": "namespace",
            "generation_revision": "generation",
            "policy_revision": "policy",
            "limit": 1,
        }

    asyncio.run(scenario())
