"""Small canonical ModelRoute adapters for legacy-shaped unit test doubles."""

from __future__ import annotations

from mote.contracts.model import (
    CanonicalModelResponse,
    EndpointDescriptor,
    GenerateOutput,
    ResolvedEndpointCapabilities,
    ResponseMode,
)
from mote.contracts.model.profile import profile_for
from mote.contracts.model.topology import SemanticRoute, TaskRoute
from mote.contracts.model.topology_codec import encode_route_id
from mote.contracts.ports.model.gateway import ModelRoute
from mote.contracts.runtime.application import ApplicationGenerationId, RuntimeGenerationId, RuntimeRoleConfigView


def offline_config():
    """Return a hermetic Runtime config that never reads the user's config stack."""

    from mote.product.config.model.inputs import ProductEndpointInput, ShortcutModelsConfig
    from mote.product.config.schema import Config

    return Config(models=ShortcutModelsConfig(default=ProductEndpointInput(model="test")))


class FakeModelGateway:
    def __init__(self, llm) -> None:
        self.llm = llm
        self.invocations = []
        model = getattr(llm, "model", "test-model")
        model_profile = profile_for(model)
        self.profile = EndpointDescriptor(
            endpoint_id="test",
            transport="test",
            provider="test",
            model=model,
            base_url_identity="https://test.invalid",
            capabilities=ResolvedEndpointCapabilities(
                supports_native_schema=model_profile.supports_native_structured_output,
                supports_server_web_search=model_profile.supports_web_search,
                supports_vision=model_profile.supports_vision,
                supports_pdf=model_profile.supports_pdf_input,
                supports_native_tool_search=model_profile.supports_native_tool_search,
            ),
            credential_pool_id="test",
            lifecycle_revision="test",
        )

    def supports_route(self, route_id):
        return bool(route_id) and not isinstance(route_id, TaskRoute)

    def route_profile(self, route_id):
        return self.profile.model_copy(update={"endpoint_id": encode_route_id(route_id)})

    def route_profiles(self, route_id):
        return (self.route_profile(route_id),)

    async def execute(self, invocation, **_kwargs):
        self.invocations.append(invocation)
        payload = invocation.input
        messages = [{"role": message.role, "content": message.content} for message in payload.messages]
        prompt = messages[0]["content"] if len(messages) == 1 else messages
        system_msgs = [payload.system_prompt] if payload.system_prompt else None
        if invocation.requirements.response_mode in {
            ResponseMode.NATIVE_TOOLS,
            ResponseMode.NATIVE_SCHEMA,
        }:
            response = await self.llm.aask_tool(
                prompt,
                system_msgs=system_msgs,
                tools=[
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in payload.tools
                ],
                output_schema=payload.output_schema,
            )
            output = GenerateOutput(
                content=response.content or "",
                tool_calls=tuple(
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in response.tool_calls
                ),
            )
        else:
            output = GenerateOutput(
                content=await self.llm.aask(
                    prompt,
                    system_msgs=system_msgs,
                    stream=False,
                )
            )
        return CanonicalModelResponse(output=output).model_copy(update={"model_call_id": invocation.model_call_id})

    async def resume(self, invocation, **kwargs):
        return await self.execute(invocation, **kwargs)


class _FakeRuntimeCompositionLease:
    def __init__(self, gateway) -> None:
        self.gateway = gateway
        self.runtime_generation_id = RuntimeGenerationId("runtime-test")
        self.topology_revision = "topology-test"
        self.default_model = type(
            "DefaultModelView",
            (),
            {"model": gateway.profile.model},
        )()
        self.route_policy = None
        self._released = False

    async def aclose(self) -> None:
        self._released = True


class _FakeApplicationLease:
    application_generation_id = ApplicationGenerationId("application-test")
    runtime_role_config = RuntimeRoleConfigView(response_language="en-US")

    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def acquire_runtime(self):
        return _FakeRuntimeCompositionLease(self._gateway)

    async def aclose(self) -> None:
        return None


class FakeApplicationComposition:
    def __init__(self, gateway) -> None:
        self._gateway = gateway

    async def acquire(self):
        return _FakeApplicationLease(self._gateway)

    def test_lease_pair(self):
        application = _FakeApplicationLease(self._gateway)
        return application, _FakeRuntimeCompositionLease(self._gateway)


def bind_fake_runtime(role, llm) -> None:
    composition = FakeApplicationComposition(FakeModelGateway(llm))
    application, runtime = composition.test_lease_pair()
    role._components._state.application_lease = application
    role._components._state.runtime_composition_lease = runtime


def install_fake_runtime(role, llm) -> None:
    if role.wiring.services is None:
        raise RuntimeError("fake Runtime composition requires EngineServices")
    role.wiring.services.application_composition = FakeApplicationComposition(FakeModelGateway(llm))


def model_route(llm, *, route_id=None) -> ModelRoute:
    route_id = route_id or SemanticRoute(name="test")
    gateway = FakeModelGateway(llm)
    return ModelRoute(
        gateway=gateway,
        route_id=route_id,
        profile=gateway.profile.model_copy(update={"endpoint_id": encode_route_id(route_id)}),
    )


__all__ = [
    "FakeApplicationComposition",
    "FakeModelGateway",
    "install_fake_runtime",
    "bind_fake_runtime",
    "model_route",
    "offline_config",
]
