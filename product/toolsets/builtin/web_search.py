"""``WebSearch`` — hosted web search (query → hit list).

Answers "what pages exist for this query" — the *discovery* half of web work
that :class:`WebBrowser` (navigate/read a KNOWN url) cannot do. The Tool submits
one PURE logical service call; Product adapters choose provider-native search or
a direct search API while Runtime owns journaling and bounded failover.

Degradation (option 1A — NO third-party scraper fallback): when the routed
model/provider has no server-side search (``NotImplementedError``) the tool
raises :class:`ToolNotConfiguredError` naming the ``models.tasks.web_search``
config path the user must point at a search-capable model, and steering the
model to the WebBrowser tool as an immediate fallback.

The list-shaped ``allowed_domains``/``blocked_domains`` params are native-channel
only (the XML protocol delivers args as strings); ``query`` is a scalar so the
core function works on both channels.
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional
from urllib.parse import quote_plus

from mote.contracts.errors.services import ServiceCallExhaustedError
from mote.contracts.models import WebSearchHit
from mote.contracts.services import ServiceExecutionSemantics
from mote.contracts.tools.effects import ToolEffect
from mote.runtime.errors import ToolNotConfiguredError, ToolValidationError
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.capability_types import InvokeService
from mote.runtime.tools.tool_registry import register_tool
from mote.runtime.tools.tool_result import ToolResult


def _unavailable_msg(query: str) -> str:
    """The not-configured notice: name the config gap + steer to WebBrowser.

    The routed ``web_search`` task model cannot drive provider-native server-side
    search, so this is a configuration gap rather than a transient failure —
    point the user at the exact config path AND give the model an immediate
    fallback (navigate a search engine via WebBrowser).
    """
    return (
        "Server-side web search is unavailable: the model routed for the "
        "'web_search' task does not support provider-native web search. Configure "
        "models.tasks.web_search with a search-capable model (e.g. "
        "claude-haiku-4-5-20251001, or a gpt-4o/gpt-5 model on the OpenAI "
        "Responses API). In the meantime, use the WebBrowser tool to navigate to "
        f"a search engine (e.g. https://duckduckgo.com/?q={quote_plus(query)}) and "
        "read the results instead."
    )


_MSG_NO_RESULTS = "No search results found."
_REMINDER = "\nREMINDER: You MUST include the sources above in your response to the " "user using markdown hyperlinks."


@register_tool
class WebSearch(BaseTool):
    """Search the web for current information, returning a list of source links."""

    name = "WebSearch"
    aliases: list[str] = ["web_search"]
    # Recall synonyms for tool-search: common ways a model asks for web search
    # that the summary ("search the web") does not spell out.
    keywords: ClassVar[list[str]] = [
        "google",
        "internet",
        "online",
        "lookup",
        "news",
        "find information",
        "查资料",
        "搜索",
        "上网",
        "新闻",
        "谷歌",
        "文档",
    ]
    requires: ClassVar[tuple[str, ...]] = ("invoke_service",)
    effect = ToolEffect.EXTERNAL
    # Aligns with CC's WebSearchTool.maxResultSizeChars = 100_000.
    max_result_size_chars: ClassVar[float] = 100_000
    # A read-only discovery call whose result (a link list) can be re-fetched;
    # folding it away loses nothing the model cannot search for again.
    reconstructable = True

    invoke_service: InvokeService

    def __init__(self, config: object) -> None:
        super().__init__()
        self._config = config

    def can_resume_started_call(self, call_id: str) -> bool:
        """Re-enter the gateway, which resumes the same logical search call."""
        return True

    async def call(
        self,
        *,
        query: str,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        num_results: Optional[int] = None,
    ) -> ToolResult:
        """Search the web for current information — returns ranked source links.

        - Allows Claude to search the web and use the results to inform responses
        - Provides up-to-date information for current events and recent data
        - Returns search result information formatted as search result blocks, including links as markdown hyperlinks
        - Use this tool for accessing information beyond Claude's knowledge cutoff
        - Searches are performed automatically within a single API call

        CRITICAL REQUIREMENT - You MUST follow this:
          - After answering the user's question, you MUST include a "Sources:" section at the end of your response
          - In the Sources section, list all relevant URLs from the search results as markdown hyperlinks: [Title](URL)
          - This is MANDATORY - never skip including sources in your response
          - Example format:

            [Your answer here]

            Sources:
            - [Source Title 1](https://example.com/1)
            - [Source Title 2](https://example.com/2)

        Usage notes:
          - Domain filtering is supported to include or block specific websites
          - Web search is only available in the US

        IMPORTANT - Use the correct year in search queries:
          - You MUST use the current year when searching for recent information, documentation, or current events.
          - Example: If the user asks for "latest React docs", search for "React documentation" with the current year, NOT last year

        Args:
            query: The search query to use.
            allowed_domains: Only include search results from these domains
                (native channel only; mutually exclusive with blocked_domains).
            blocked_domains: Never include search results from these domains
                (native channel only; mutually exclusive with allowed_domains).
            num_results: Cap on how many result links to return (default: 8).

        Returns the results as ``Links:`` markdown. Raises
        :class:`ToolNotConfiguredError` (naming the ``models.tasks.web_search``
        config path, steering you to WebBrowser) when the routed model has no
        server-side web search.
        """
        if not query or not query.strip():
            raise ToolValidationError("Missing query.")
        if allowed_domains and blocked_domains:
            raise ToolValidationError("Cannot specify both allowed_domains and blocked_domains in the same request.")

        limit = num_results if isinstance(num_results, int) and num_results > 0 else 8
        try:
            value = await self.invoke_service(
                route_id="web.search",
                capability="web.search",
                operation_key="query",
                payload={
                    "query": query,
                    "allowed_domains": list(allowed_domains or ()),
                    "blocked_domains": list(blocked_domains or ()),
                    "max_uses": 8,
                },
                semantics=ServiceExecutionSemantics.PURE,
            )
        except ServiceCallExhaustedError as exc:
            if getattr(self._config, "backend", "provider") in {"", "provider"}:
                raise ToolNotConfiguredError(_unavailable_msg(query)) from exc
            raise

        hits = _decode_hits(value)

        return ToolResult(output=self._format(query, hits[:limit]))

    @staticmethod
    def _format(query: str, hits: list) -> str:
        """Render hits as CC's ``Web search results for query`` + ``Links:`` block.

        Mirrors CC's ``mapToolResultToToolResultBlockParam``: a header line, a
        ``Links:`` list of ``- [title](url): snippet`` (snippet omitted when
        empty), then the mandatory sources reminder. Empty hits → a
        "no results" line (still with the reminder, matching CC's trim()ed shape).
        """
        parts = [f'Web search results for query: "{query}"', ""]
        if hits:
            parts.append("Links:")
            for hit in hits:
                line = f"  - [{hit.title}]({hit.url})"
                if getattr(hit, "snippet", ""):
                    line += f": {hit.snippet}"
                parts.append(line)
            parts.append("")
        else:
            parts.append(_MSG_NO_RESULTS)
            parts.append("")
        return ("\n".join(parts) + _REMINDER).strip()


def _decode_hits(value: Any) -> list[WebSearchHit]:
    if not isinstance(value, dict) or not isinstance(value.get("hits"), list):
        raise TypeError("web-search service returned an invalid response")
    hits: list[WebSearchHit] = []
    for item in value["hits"]:
        if not isinstance(item, dict):
            raise TypeError("web-search service returned a non-object hit")
        title = item.get("title")
        url = item.get("url")
        snippet = item.get("snippet", "")
        if not isinstance(title, str) or not isinstance(url, str):
            raise TypeError("web-search service returned an invalid hit")
        if not isinstance(snippet, str):
            raise TypeError("web-search service returned an invalid snippet")
        hits.append(WebSearchHit(title=title, url=url, snippet=snippet))
    return hits
