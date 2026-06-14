from __future__ import annotations

from typing import AsyncGenerator, Union

from mcp import ClientSession, types
from mcp.types import CallToolResult, EmbeddedResource, ImageContent, TextContent
from mcp.types import Tool as MCPTool
from tenacity import after_log, retry, stop_after_delay, wait_random_exponential

from metagpt.common.config.mcp_config import MCPServerConfig
from metagpt.common.logs import logger
from metagpt.executor.mcp.client.exceptions import (
    NonRetryableToolError,
    handle_exception_group,
    retry_if_retryable_error,
)
from metagpt.executor.mcp.client.utils import format_method_name
from metagpt.common.utils.async_helper import run_coroutine_sync
from metagpt.common.utils.common import log_time
from metagpt.common.utils.sentry import capture_errors


class EnhancedClientSession(ClientSession):
    async def call_tool(self, name: str, arguments: dict | None = None, **kwargs) -> types.CallToolResult:
        """Send a tools/call request with context."""
        return await self.send_request(
            types.ClientRequest(
                types.CallToolRequest(
                    method="tools/call",
                    params=types.CallToolRequestParams(name=name, arguments=arguments, **kwargs),
                )
            ),
            types.CallToolResult,
        )


class MCPBaseClient:
    def __init__(self, server_config: MCPServerConfig):
        self.server_config = server_config

    @log_time
    @capture_errors
    @retry(
        wait=wait_random_exponential(min=0.5, max=5),
        stop=stop_after_delay(600),
        after=after_log(logger, logger.level("WARNING").name),
        retry=retry_if_retryable_error,
    )
    @handle_exception_group
    async def list_tools(self) -> list[MCPTool]:
        async with self.get_session() as session:
            session: EnhancedClientSession
            response = await session.list_tools()
            return response.tools

    @log_time
    @capture_errors
    @retry(
        wait=wait_random_exponential(min=0.5, max=5),
        stop=stop_after_delay(600),
        after=after_log(logger, logger.level("WARNING").name),
        retry=retry_if_retryable_error,
    )
    @handle_exception_group
    async def call_tool(self, name: str, arguments: dict | None = None, meta: dict | None = None) -> any:
        async with self.get_session() as session:
            session: EnhancedClientSession
            result = await session.call_tool(name, arguments, _meta=meta)
            return self._process_tool_result(result, name, arguments)

    def call_tool_sync(self, name: str, arguments: dict | None = None, meta: dict | None = None) -> str:
        return run_coroutine_sync(self.call_tool(name, arguments, meta))

    async def get_session(self) -> AsyncGenerator[EnhancedClientSession, None]:
        """Get a session for interacting with the MCP service.

        This method should be implemented by derived classes to provide the specific session.
        Typically, implementations will use asynccontextmanager to allow for use with 'async with'.

        Example implementation:
            @asynccontextmanager
            async def get_session(self) -> AsyncGenerator[EnhancedClientSession, None]:
                session = EnhancedClientSession(...)
                yield session
        """
        raise NotImplementedError("Subclasses must implement get_session")

    async def cleanup(self):
        """Implement this method to cleanup resources."""
        ...

    def _process_tool_result(self, result: CallToolResult, name: str, arguments: dict) -> any:
        """Process tool call result and extract appropriate data

        Args:
            result: The result returned from tool call
            name: Tool name (for logging)
            arguments: Tool arguments (for logging)

        Returns:
            any: Processed content which could be str, int, bool, list, etc.
        """
        # Handle error case
        if result.isError:
            error_msg = "Unknown error"
            if result.content and hasattr(result.content[0], "text"):
                error_msg = result.content[0].text

            # If one tool fails, it should stop early, especially when multiple tools need to be run.
            error_msg = format_method_name(error_msg)
            raise NonRetryableToolError(error_msg)

        # Handle empty content
        if not result.content:
            return ""

        # Handle multiple content items
        if len(result.content) > 1:
            processed_items = []
            for content in result.content:
                processed_items.append(self._extract_content_value(content))
            return processed_items

        # Handle single content item
        return self._extract_content_value(result.content[0])

    def _extract_content_value(self, content: Union[TextContent, ImageContent, EmbeddedResource]) -> any:
        """Extract and convert content value based on its type"""
        if content.type == "text":
            text_value = content.text

            # Attempt to convert to boolean
            if text_value.lower() == "true":
                return True
            if text_value.lower() == "false":
                return False

            # Keep as string
            return text_value

        if content.type == "image":
            return content.data

        if content.type == "resource":
            resource = content.resource
            if hasattr(resource, "text"):
                return resource.text
            if hasattr(resource, "blob"):
                return resource.blob

        return "Unparseable content"
