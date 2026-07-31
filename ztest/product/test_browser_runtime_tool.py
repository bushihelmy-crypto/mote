from __future__ import annotations

import pytest

from mote.contracts.runtime import CheckpointFidelity, DriverStartResult, RuntimeCapabilities, RuntimeHealth
from mote.contracts.runtime.errors import ManagedRuntimeNotFoundError
from mote.product.toolsets.builtin import web_browser as web_browser_module
from mote.product.toolsets.builtin.web_browser import WebBrowser
from mote.ztest.executor.tools.conftest import CapRole, bind


class _FakeSession:
    async def read(self, *, extract_links=False, extract_images=False):
        return "managed browser content"

    async def capture_state(self):
        return (["data:text/html,managed"], 0, None)


class _FakeBrowserRuntimeDriver:
    kind = "browser"
    capabilities = RuntimeCapabilities(checkpoint_fidelity=CheckpointFidelity.LOGICAL)

    def __init__(self, **kwargs) -> None:
        self.session = _FakeSession()
        self.surface_changes = 0

    async def start(self, checkpoint=None):
        return DriverStartResult()

    async def health(self):
        return RuntimeHealth(healthy=True)

    async def checkpoint(self, reason):
        raise RuntimeError

    def surface_changed(self):
        self.surface_changes += 1

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_web_browser_uses_runtime_host_instead_of_tool_session(monkeypatch):
    monkeypatch.setattr(web_browser_module, "BrowserRuntimeDriver", _FakeBrowserRuntimeDriver)
    role = CapRole()
    tool = bind(WebBrowser(), role=role)

    assert await tool.call(action="read") == "managed browser content"
    descriptor = tool.get_runtime_host().descriptor("browser:default")
    assert descriptor.revision == 1

    assert await tool.call(action="close") == "[browser closed]"
    with pytest.raises(ManagedRuntimeNotFoundError):
        tool.get_runtime_host().descriptor("browser:default")
