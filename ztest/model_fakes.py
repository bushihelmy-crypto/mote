"""Small canonical ModelRoute adapters for legacy-shaped unit test doubles."""

from __future__ import annotations

from mote.contracts.models import CanonicalModelResponse, EndpointCapabilities, EndpointDescriptor, GenerateOutput
from mote.contracts.models.profile import profile_for
from mote.contracts.ports import ModelRoute


def offline_config():
    """Return a hermetic Runtime config that never reads the user's config stack."""

    from mote.contracts.config.llm import LLMConfig
    from mote.contracts.config.models import ModelsConfig
    from mote.runtime.config.schema import Config

    return Config(models=ModelsConfig(default=LLMConfig(model="test")))


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
            capabilities=EndpointCapabilities(
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
        return bool(route_id)

    def route_profile(self, route_id):
        return self.profile.model_copy(update={"endpoint_id": route_id})

    def route_profiles(self, route_id):
        return (self.route_profile(route_id),)

    async def execute(self, invocation, **_kwargs):
        self.invocations.append(invocation)
        payload = invocation.input
        messages = [{"role": message.role, "content": message.content} for message in payload.messages]
        reply = await self.llm.aask(
            messages,
            system_msgs=[payload.system_prompt] if payload.system_prompt else None,
            stream=False,
        )
        return CanonicalModelResponse(output=GenerateOutput(content=reply))

    async def resume(self, invocation, **kwargs):
        return await self.execute(invocation, **kwargs)


def model_route(llm, *, route_id: str = "test") -> ModelRoute:
    gateway = FakeModelGateway(llm)
    return ModelRoute(
        gateway=gateway,
        route_id=route_id,
        profile=gateway.profile.model_copy(update={"endpoint_id": route_id}),
    )


__all__ = ["FakeModelGateway", "model_route", "offline_config"]
