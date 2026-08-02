from __future__ import annotations

import pytest

from mote.contracts.model.topology import (
    DefaultRoute,
    EndpointCapabilityDeclaration,
    FailoverGroupTopology,
    ModelEndpointTopology,
    ModelTopology,
    RecoveryProfileTopology,
    RouteBinding,
    SemanticRoute,
    TaskRoute,
)
from mote.contracts.model.topology_codec import (
    NonAsciiHostnameError,
    canonical_base_url,
    canonical_topology_bytes,
    decode_route_id,
    encode_route_id,
    topology_revision,
)

_CANONICAL = b'{"endpoints":[{"base_url":"https://api.example.com/v1","capabilities":{"context_tokens":128000,"supported_operations":["generate"],"supports_native_schema":false,"supports_native_tool_search":false,"supports_pdf":false,"supports_server_web_search":false,"supports_tools":true,"supports_vision":false},"credential_slots":["s"],"endpoint_id":"e","governance_domain":"default","model":"gpt","pricing_class":"default","provider":"openai","region":"global","transport":"openai"}],"failover_groups":[{"endpoint_ids":["e"],"group_id":"g","recovery_profile_id":"p"}],"recovery_profiles":[{"max_attempts_per_endpoint":1,"max_backoff_ms":0,"max_credential_rotations":0,"max_endpoint_switches":0,"max_request_transforms":0,"max_wire_attempts":1,"profile_id":"p","single_attempt_timeout_ms":500,"total_deadline_ms":1000}],"routes":[{"group_id":"g","route_id":{"kind":"default"}}],"schema":"mote.model-topology/v2"}'


def _topology() -> ModelTopology:
    return ModelTopology(
        endpoints=(
            ModelEndpointTopology(
                endpoint_id="e",
                transport="openai",
                provider="openai",
                model="gpt",
                base_url="HTTPS://API.EXAMPLE.COM:443/v1/../v1",
                capabilities=EndpointCapabilityDeclaration(
                    supports_tools=True,
                    supports_native_schema=False,
                    supports_server_web_search=False,
                    supports_vision=False,
                    supports_pdf=False,
                    supports_native_tool_search=False,
                    context_tokens=128000,
                ),
                governance_domain="default",
                region="global",
                pricing_class="default",
                credential_slots=("s",),
            ),
        ),
        recovery_profiles=(
            RecoveryProfileTopology(
                profile_id="p",
                max_wire_attempts=1,
                max_attempts_per_endpoint=1,
                max_endpoint_switches=0,
                max_credential_rotations=0,
                max_request_transforms=0,
                total_deadline_ms=1000,
                single_attempt_timeout_ms=500,
                max_backoff_ms=0,
            ),
        ),
        failover_groups=(FailoverGroupTopology(group_id="g", endpoint_ids=("e",), recovery_profile_id="p"),),
        routes=(RouteBinding(route_id=DefaultRoute(), group_id="g"),),
    )


def test_v2_golden_bytes_and_revision() -> None:
    topology = _topology()
    assert canonical_topology_bytes(topology) == _CANONICAL
    assert topology_revision(topology) == "0efd25e730520205d0943a63251f5adc3805460e52d061b026e648e0de7f1750"


def test_url_policy_is_stable_and_rejects_unicode() -> None:
    assert canonical_base_url("https://XN--BCHER-KVA.example:443/a/../b") == "https://xn--bcher-kva.example/b"
    assert canonical_base_url("http://[2001:0db8::1]:80") == "http://[2001:db8::1]/"
    with pytest.raises(NonAsciiHostnameError):
        canonical_base_url("https://b\xfccher.example")


def test_route_values_are_typed_and_hashable() -> None:
    routes = {DefaultRoute(), TaskRoute(name="compression"), SemanticRoute(name="fast")}
    assert len(routes) == 3
    with pytest.raises(Exception):
        TaskRoute(name="semantic:fast")


@pytest.mark.parametrize(
    ("route", "wire"),
    [
        (DefaultRoute(), "default"),
        (TaskRoute(name="compression"), "task:compression"),
        (SemanticRoute(name="fast"), "semantic:fast"),
    ],
)
def test_route_wire_codec_is_stable(route, wire) -> None:
    assert encode_route_id(route) == wire
    assert decode_route_id(wire) == route
