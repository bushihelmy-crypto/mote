from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from mote.contracts.artifacts import ArtifactRef, ResolvedArtifact
from mote.contracts.config.llm import LLMConfig, LLMType
from mote.contracts.config.model_failover import (
    CredentialPoolConfig,
    CredentialSlotConfig,
    EndpointCapabilitiesConfig,
    FailoverGroupConfig,
    ModelEndpointConfig,
    ModelRoutesConfig,
    RecoveryProfileConfig,
)
from mote.contracts.config.models import ModelsConfig
from mote.contracts.config.oauth import OAuthProviderConfig
from mote.contracts.events.types import LLMStreamDeltaEvent
from mote.contracts.models.failover import EndpointCapabilities, EndpointDescriptor
from mote.contracts.models.invocation import (
    CanonicalMessage,
    CanonicalToolCall,
    CanonicalToolDefinition,
    GenerateInput,
    ImageDescriptionInput,
    ModelInvocation,
    ModelOperation,
    RequestRequirements,
    ResponseMode,
)
from mote.product.integrations.bootstrap import builtin_model_gateway
from mote.product.integrations.models.endpoint_adapter import ProductModelEndpointAdapter
from mote.product.integrations.models.endpoint_resolver import ProductModelEndpointResolver
from mote.runtime.errors import LLMAuthenticationError
from mote.runtime.events.stream import capture_attempt_stream, log_llm_stream
from mote.runtime.models.clients.registry import LLMProviderRegistry


class _FakeProvider:
    created: list["_FakeProvider"] = []
    yield_on_bad = False
    quota_headers: dict[str, str] | None = None
    oauth_refreshes = 0

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.model = config.model
        self.pricing_plan = config.model
        self.cost_manager = None
        self.calls: list[tuple[list[dict], dict]] = []
        self.closed = False
        type(self).created.append(self)

    async def _achat_completion(self, messages, timeout, **kwargs):
        self.calls.append((messages, kwargs))
        if self.config.api_key in {"bad-key", ""}:
            if type(self).yield_on_bad:
                await asyncio.sleep(0)
            raise LLMAuthenticationError("rejected", status_code=401)
        transport = self.config.api_type.value
        if self.quota_headers is not None:
            self.rate_limit_tracker.observe_headers(
                transport,
                self.model,
                self.quota_headers,
            )
        tool = kwargs.get("tools", [{}])[0]
        if transport == LLMType.ANTHROPIC.value:
            name = tool.get("name", "")
        elif transport == LLMType.OPENAI_RESPONSES.value:
            name = tool.get("name", "")
        else:
            name = tool.get("function", {}).get("name", "")
        usage = (
            SimpleNamespace(
                input_tokens=11,
                output_tokens=3,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            )
            if transport == LLMType.ANTHROPIC.value
            else SimpleNamespace(
                prompt_tokens=11,
                completion_tokens=3,
                total_tokens=14,
            )
        )
        return SimpleNamespace(
            id=f"request-{transport}",
            text=('{"answer":"ok"}' if "response_format" in kwargs or "text" in kwargs else f"ok:{transport}"),
            calls=[{"id": "call-1", "name": name, "arguments": {"path": "a"}}] if name else [],
            usage=usage,
        )

    def refresh_oauth_credential(self):
        type(self).oauth_refreshes += 1
        self.config.api_key = "oauth-refreshed"
        return True

    async def _achat_completion_stream_tool(self, messages, timeout, **kwargs):
        log_llm_stream(f"stream:{self.config.api_type.value}")
        return await self._achat_completion(messages, timeout, **kwargs)

    def get_choice_text(self, response):
        return response.text

    def get_choice_tool_calls(self, response):
        return response.calls

    def native_schema_request(self, schema):
        return {"response_format": {"schema": schema}}

    async def aweb_search(self, *args, **kwargs):
        raise AssertionError("unexpected web search")

    async def aclose(self):
        self.closed = True


def _endpoint(transport: str, endpoint_id: str = "endpoint") -> EndpointDescriptor:
    return EndpointDescriptor(
        endpoint_id=endpoint_id,
        transport=transport,
        provider=transport,
        model="model",
        base_url_identity="https://models.example.test/v1",
        capabilities=EndpointCapabilities(supports_tools=True),
        credential_pool_id="pool",
        lifecycle_revision="revision",
    )


def _invocation(
    *,
    model_call_id: str = "call-1",
    route_id: str = "default",
    mode: ResponseMode = ResponseMode.NATIVE_TOOLS,
) -> ModelInvocation:
    return ModelInvocation(
        model_call_id=model_call_id,
        route_id=route_id,
        task="agent",
        operation=ModelOperation.GENERATE,
        input=GenerateInput(
            system_prompt="system",
            messages=(CanonicalMessage(role="user", content="hello"),),
            tools=(
                CanonicalToolDefinition(
                    name="Read",
                    description="Read a file",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                ),
            ),
            output_schema=(
                {"type": "object", "properties": {"answer": {"type": "string"}}}
                if mode is ResponseMode.NATIVE_SCHEMA
                else None
            ),
        ),
        requirements=RequestRequirements(
            response_mode=mode,
            needs_tools=True,
            needs_native_schema=mode is ResponseMode.NATIVE_SCHEMA,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "expected_tool"),
    [
        (
            LLMType.OPENAI.value,
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            },
        ),
        (
            LLMType.DEEPSEEK.value,
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            },
        ),
        (
            LLMType.OPENAI_RESPONSES.value,
            {
                "type": "function",
                "name": "Read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        ),
        (
            LLMType.ANTHROPIC.value,
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        ),
    ],
)
async def test_each_transport_reprojects_canonical_generate_once(
    transport: str,
    expected_tool: dict,
) -> None:
    provider = _FakeProvider(
        LLMConfig(
            api_key="key",
            api_type=LLMType(transport),
            base_url="https://models.example.test/v1",
            model="model",
        )
    )
    adapter = ProductModelEndpointAdapter(
        endpoint_id="endpoint",
        credential_slot_id="slot",
        tenant_fingerprint="tenant",
        transport=transport,
        llm=provider,
    )

    response = await adapter.execute_once(
        _invocation(),
        _endpoint(transport),
        timeout_seconds=12.5,
    )

    assert len(provider.calls) == 1
    messages, kwargs = provider.calls[0]
    assert messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    assert kwargs["tools"] == [expected_tool]
    assert response.output.tool_calls[0].name == "Read"
    assert response.usage.total_tokens == 14
    assert response.provider_request_id == f"request-{transport}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport",
    [
        LLMType.OPENAI.value,
        LLMType.DEEPSEEK.value,
        LLMType.OPENAI_RESPONSES.value,
        LLMType.ANTHROPIC.value,
    ],
)
async def test_each_transport_uses_attempt_scoped_stream_path(transport: str) -> None:
    provider = _FakeProvider(
        LLMConfig(
            api_key="key",
            api_type=LLMType(transport),
            base_url="https://models.example.test/v1",
            model="model",
        )
    )
    adapter = ProductModelEndpointAdapter(
        endpoint_id="endpoint",
        credential_slot_id="slot",
        tenant_fingerprint="tenant",
        transport=transport,
        llm=provider,
    )

    with capture_attempt_stream(True) as buffer:
        response = await adapter.execute_once(
            _invocation(),
            _endpoint(transport),
            timeout_seconds=12.5,
            stream=True,
        )

    assert buffer is not None
    assert buffer.chunks == [f"stream:{transport}"]
    assert response.output.tool_calls[0].name == "Read"


@pytest.mark.asyncio
async def test_adapter_projects_wire_quota_headers_to_canonical_observation() -> None:
    provider = _FakeProvider(
        LLMConfig(
            api_key="key",
            api_type=LLMType.OPENAI,
            base_url="https://models.example.test/v1",
            model="model",
        )
    )
    adapter = ProductModelEndpointAdapter(
        endpoint_id="endpoint",
        credential_slot_id="slot",
        tenant_fingerprint="tenant",
        transport=LLMType.OPENAI.value,
        llm=provider,
    )
    _FakeProvider.quota_headers = {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "1",
        "x-ratelimit-reset-requests": "5s",
        "x-ratelimit-remaining-tokens": "900",
    }
    try:
        response = await adapter.execute_once(
            _invocation(),
            _endpoint(LLMType.OPENAI.value),
            timeout_seconds=5,
        )
    finally:
        _FakeProvider.quota_headers = None

    assert response.quota is not None
    assert response.quota.limit_requests == 100
    assert response.quota.remaining_requests == 1
    assert response.quota.reset_requests_after_seconds == 5.0
    assert response.quota.remaining_tokens == 900


@pytest.mark.asyncio
async def test_canonical_tool_history_projects_without_provider_state() -> None:
    provider = _FakeProvider(LLMConfig(api_key="key", model="model", base_url="https://example.test/v1"))
    adapter = ProductModelEndpointAdapter(
        endpoint_id="endpoint",
        credential_slot_id="slot",
        tenant_fingerprint="tenant",
        transport=LLMType.OPENAI.value,
        llm=provider,
    )
    invocation = _invocation(mode=ResponseMode.TEXT).model_copy(
        update={
            "input": GenerateInput(
                messages=(
                    CanonicalMessage(
                        role="assistant",
                        content="",
                        tool_calls=(
                            CanonicalToolCall(
                                id="call-read",
                                name="Read",
                                arguments={"path": "a"},
                            ),
                        ),
                    ),
                    CanonicalMessage(
                        role="tool",
                        content="contents",
                        tool_call_id="call-read",
                    ),
                )
            )
        }
    )

    await adapter.execute_once(
        invocation,
        _endpoint(LLMType.OPENAI.value),
        timeout_seconds=5,
    )

    assert provider.calls[0][0] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": '{"path":"a"}',
                    },
                }
            ],
        },
        {"role": "tool", "content": "contents", "tool_call_id": "call-read"},
    ]


@pytest.mark.asyncio
async def test_native_schema_attempt_keeps_canonical_tools_on_same_wire_call() -> None:
    provider = _FakeProvider(LLMConfig(api_key="key", model="model", base_url="https://example.test/v1"))
    adapter = ProductModelEndpointAdapter(
        endpoint_id="endpoint",
        credential_slot_id="slot",
        tenant_fingerprint="tenant",
        transport=LLMType.OPENAI.value,
        llm=provider,
    )

    await adapter.execute_once(
        _invocation(mode=ResponseMode.NATIVE_SCHEMA),
        _endpoint(LLMType.OPENAI.value),
        timeout_seconds=5,
    )

    assert len(provider.calls) == 1
    _, kwargs = provider.calls[0]
    assert kwargs["tools"][0]["function"]["name"] == "Read"
    assert kwargs["response_format"]["schema"]["type"] == "object"


@pytest.mark.asyncio
async def test_image_description_projects_resolved_artifact_on_one_wire() -> None:
    provider = _FakeProvider(LLMConfig(api_key="key", model="vision-model", base_url="https://example.test/v1"))
    adapter = ProductModelEndpointAdapter(
        endpoint_id="endpoint",
        credential_slot_id="slot",
        tenant_fingerprint="tenant",
        transport=LLMType.OPENAI.value,
        llm=provider,
    )
    content = b"png"
    ref = ArtifactRef(
        artifact_id="image-1",
        revision=1,
        representation="png",
        kind="image",
        mime_type="image/png",
        content_ref="cas:image-1",
        digest=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )
    invocation = ModelInvocation(
        model_call_id="image-call",
        route_id="image_description",
        task="image_description",
        operation=ModelOperation.IMAGE_DESCRIPTION,
        input=ImageDescriptionInput(artifact=ref, prompt="read it"),
        requirements=RequestRequirements(needs_vision=True),
    )

    response = await adapter.execute_once(
        invocation,
        _endpoint(LLMType.OPENAI.value),
        timeout_seconds=5,
        artifact=ResolvedArtifact(ref=ref, content=content),
    )

    assert response.output.text == "ok:openai"
    assert len(provider.calls) == 1
    content = provider.calls[0][0][0]["content"]
    assert content[0] == {"type": "text", "text": "read it"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def _models(
    *,
    primary_slots: tuple[tuple[str, str], ...] = (("primary", "PRIMARY"),),
) -> ModelsConfig:
    return ModelsConfig(
        default=LLMConfig(api_key="default", model="default-model"),
        tasks={},
        credential_pools={
            "primary-pool": CredentialPoolConfig(
                slots=[
                    CredentialSlotConfig(id=slot_id, secret_ref=f"env://{env_name}")
                    for slot_id, env_name in primary_slots
                ]
            ),
            "backup-pool": CredentialPoolConfig(slots=[CredentialSlotConfig(id="backup", secret_ref="env://BACKUP")]),
        },
        endpoints={
            "primary": ModelEndpointConfig(
                api_key="placeholder",
                api_type=LLMType.OPENAI,
                base_url="https://gateway.example.test/v1",
                model="openai-model",
                credential_pool="primary-pool",
                capabilities=EndpointCapabilitiesConfig(supports_tools=True),
            ),
            "backup": ModelEndpointConfig(
                api_key="placeholder",
                api_type=LLMType.ANTHROPIC,
                base_url="https://api.anthropic.com",
                model="claude-model",
                credential_pool="backup-pool",
                capabilities=EndpointCapabilitiesConfig(supports_tools=True),
            ),
        },
        failover_groups={
            "main": FailoverGroupConfig(
                endpoints=["primary", "backup"],
                recovery_profile="fast",
            )
        },
        routes=ModelRoutesConfig(default="main"),
        recovery_profiles={
            "fast": RecoveryProfileConfig(
                max_wire_attempts=2,
                max_attempts_per_endpoint=min(2, len(primary_slots)),
                max_endpoint_switches=1,
                max_credential_rotations=1,
                max_request_transforms=0,
                total_deadline_seconds=10,
                single_attempt_timeout_seconds=5,
                max_backoff_seconds=0,
            )
        },
    )


def _fake_registry() -> LLMProviderRegistry:
    registry = LLMProviderRegistry()
    registry.register(LLMType.OPENAI, _FakeProvider)
    registry.register(LLMType.ANTHROPIC, _FakeProvider)
    return registry


def test_resolver_binds_one_secret_per_fresh_adapter_without_secret_repr() -> None:
    _FakeProvider.created.clear()
    models = _models()
    resolver = ProductModelEndpointResolver(
        models,
        _fake_registry(),
        environ={"PRIMARY": "primary-secret", "BACKUP": "backup-secret"},
    )
    endpoint = _endpoint(LLMType.OPENAI.value, "primary")

    first = resolver.resolve(endpoint, "primary")
    second = resolver.resolve(endpoint, "primary")

    assert first is not None and second is not None and first is not second
    assert _FakeProvider.created[-2].config.api_key == "primary-secret"
    assert _FakeProvider.created[-1].config.api_key == "primary-secret"
    assert _FakeProvider.created[-2] is not _FakeProvider.created[-1]
    assert "primary-secret" not in repr(resolver)
    assert "primary-secret" not in repr(first)


@pytest.mark.asyncio
async def test_oauth_refresh_is_a_lazy_gateway_selected_credential_slot() -> None:
    _FakeProvider.created.clear()
    _FakeProvider.oauth_refreshes = 0
    models = ModelsConfig(
        default=LLMConfig(
            api_key="",
            api_type=LLMType.OPENAI,
            model="oauth-model",
            oauth=OAuthProviderConfig(
                token_url="https://issuer.example.test/token",
                client_id="client",
            ),
        ),
        tasks={},
    )
    gateway = builtin_model_gateway(models, providers=_fake_registry())
    invocation = _invocation(mode=ResponseMode.TEXT).model_copy(
        update={
            "input": GenerateInput(messages=(CanonicalMessage(role="user", content="hello"),)),
            "requirements": RequestRequirements(),
        }
    )

    response = await gateway.execute(invocation)

    assert response.credential_slot_id == "default:oauth-refresh"
    assert _FakeProvider.oauth_refreshes == 1
    current, refreshed = _FakeProvider.created
    assert len(current.calls) == len(refreshed.calls) == 1


@pytest.mark.asyncio
async def test_cross_transport_fallback_reprojects_from_canonical_input() -> None:
    _FakeProvider.created.clear()
    gateway = builtin_model_gateway(
        _models(),
        providers=_fake_registry(),
        environ={"PRIMARY": "bad-key", "BACKUP": "good-key"},
    )

    response = await gateway.execute(_invocation())

    assert response.endpoint_id == "backup"
    assert response.credential_slot_id == "backup"
    primary, backup = _FakeProvider.created
    assert len(primary.calls) == len(backup.calls) == 1
    assert primary.calls[0][1]["tools"][0]["function"]["name"] == "Read"
    assert backup.calls[0][1]["tools"][0]["name"] == "Read"
    assert "function" not in backup.calls[0][1]["tools"][0]
    assert primary.closed and backup.closed


@pytest.mark.asyncio
async def test_cross_transport_stream_discards_failed_primary_at_composition_root(
    monkeypatch,
) -> None:
    emitted = []
    monkeypatch.setattr(
        "mote.runtime.events.stream.observe_event_sync",
        emitted.append,
    )
    _FakeProvider.created.clear()
    gateway = builtin_model_gateway(
        _models(),
        providers=_fake_registry(),
        environ={"PRIMARY": "bad-key", "BACKUP": "good-key"},
    )

    response = await gateway.execute(_invocation(), stream=True)

    assert response.endpoint_id == "backup"
    deltas = [event for event in emitted if isinstance(event, LLMStreamDeltaEvent)]
    assert [event.token for event in deltas] == [
        "stream:openai",
        "stream:anthropic",
    ]
    assert [event.attempt_id for event in deltas] == ["call-1:1", "call-1:2"]


@pytest.mark.asyncio
async def test_concurrent_gateway_calls_have_independent_credential_cursors() -> None:
    _FakeProvider.created.clear()
    _FakeProvider.yield_on_bad = True
    models = _models(primary_slots=(("primary-0", "PRIMARY"), ("primary-1", "PRIMARY_SECOND")))
    gateway = builtin_model_gateway(
        models,
        providers=_fake_registry(),
        environ={
            "PRIMARY": "bad-key",
            "PRIMARY_SECOND": "good-key",
            "BACKUP": "backup-key",
        },
    )

    try:
        first, second = await asyncio.gather(
            gateway.execute(_invocation(model_call_id="agent-a")),
            gateway.execute(_invocation(model_call_id="agent-b")),
        )
    finally:
        _FakeProvider.yield_on_bad = False

    assert first.credential_slot_id == second.credential_slot_id == "primary-1"
    assert len(_FakeProvider.created) == 6
    for offset in (0, 3):
        bad, good, unused_backup = _FakeProvider.created[offset : offset + 3]
        assert bad.config.api_key == "bad-key" and len(bad.calls) == 1
        assert good.config.api_key == "good-key" and len(good.calls) == 1
        assert unused_backup.calls == []
    assert all(provider.closed for provider in _FakeProvider.created)
