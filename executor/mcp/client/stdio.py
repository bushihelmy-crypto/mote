from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncGenerator

from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from metagpt.executor.mcp.client.base import EnhancedClientSession, MCPBaseClient


class MCPStdioClient(MCPBaseClient):
    def __init__(self, server_config):
        super().__init__(server_config)
        self._session = None
        self._exit_stack = None

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[EnhancedClientSession, None]:
        if self._session is None:
            self._exit_stack = AsyncExitStack()
            server_params = StdioServerParameters(
                command=self.server_config.command, args=self.server_config.args, env=self.server_config.env
            )

            stdio_transport = await self._exit_stack.enter_async_context(stdio_client(server_params))
            self._session = await self._exit_stack.enter_async_context(EnhancedClientSession(*stdio_transport))
            await self._session.initialize()

        yield self._session

    async def cleanup(self):
        if not self._exit_stack:
            return

        try:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None
        finally:
            pass
