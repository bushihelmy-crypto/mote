"""``WebSearch`` — provider-native server-side web search (query → hit list).

Answers "what pages exist for this query" — the *discovery* half of web work
that :class:`WebBrowser` (navigate/read a KNOWN url) cannot do. It issues an
isolated secondary LLM call carrying the provider's server-side web-search tool
(Anthropic ``web_search_20250305`` / OpenAI Responses ``web_search``); the API
performs the actual search + crawl and returns structured result blocks, which
we render as a markdown link list.

The secondary call is routed through the ``web_search`` task (a small/fast model
that itself supports server-side search — see ``ModelsConfig``), reached via the
Role's ``web_search`` capability so this tool never touches the router directly.

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

from typing import ClassVar, Optional

from mote.common.exception import ToolNotConfiguredError, ToolValidationError
from mote.executor.base_tool import BaseTool
from mote.executor.capability_types import WebSearch as WebSearchCapability
from mote.executor.tool_registry import register_tool
from mote.executor.tool_result import ToolResult


def _web_search_description() -> str:
    """The WebSearch tool's model-facing description (dynamic current month/year).

    This is the one legitimate exception to docstring-native prose: the
    description carries "use the current year" guidance whose month/year must be
    computed fresh on each call (the process is long-running), so it cannot be a
    static docstring. The first line here MUST match the ``call()`` docstring's
    summary line, since :meth:`summary` reads that for the tool-search menu.

    Aligned verbatim to Claude Code's ``getWebSearchPrompt()``: the bullet
    summary, the CRITICAL "Sources:" requirement, usage notes, and the
    "use the correct year" guidance (mirroring CC's ``getLocalMonthYear()``).
    """
    import datetime

    current_month_year = datetime.datetime.now().strftime("%B %Y")
    return f"""\
Search the web for current information — returns ranked source links.

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
  - The current month is {current_month_year}. You MUST use this year when searching for recent information, documentation, or current events.
  - Example: If the user asks for "latest React docs", search for "React documentation" with the current year, NOT last year
"""


def _unavailable_msg(query: str) -> str:
    """The not-configured notice: name the config gap + steer to WebBrowser.

    The routed ``web_search`` task model cannot drive provider-native server-side
    search, so this is a configuration gap rather than a transient failure —
    point the user at the exact config path AND give the model an immediate
    fallback (navigate a search engine via WebBrowser).
    """
    from urllib.parse import quote_plus

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
    requires = ("web_search",)
    # Aligns with CC's WebSearchTool.maxResultSizeChars = 100_000.
    max_result_size_chars: ClassVar[float] = 100_000
    # A read-only discovery call whose result (a link list) can be re-fetched;
    # folding it away loses nothing the model cannot search for again.
    reconstructable = True

    # Injected from Role by bind():
    web_search: WebSearchCapability

    @classmethod
    def get_schema(cls) -> dict:
        """Auto-generated schema, but with the dynamic (current-month) description.

        The description carries "use the current year" guidance whose month/year
        must be computed fresh (the process is long-running), so it cannot be a
        static docstring — we inject :func:`_web_search_description` here. The XML
        native schema (:meth:`get_native_schema`) builds on this, so both channels
        get the live description.
        """
        schema = super().get_schema()
        schema["description"] = _web_search_description()
        return schema

    async def call(
        self,
        *,
        query: str,
        allowed_domains: Optional[list[str]] = None,
        blocked_domains: Optional[list[str]] = None,
        num_results: Optional[int] = None,
    ) -> ToolResult:
        """Search the web for current information — returns ranked source links.

        Issues a provider-native server-side web search and returns matching
        source pages as a markdown link list. The full operating manual (with the
        mandatory "Sources:" requirement and current-year guidance) is injected
        dynamically at schema-build time; see :func:`_web_search_description`.

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
            hits = await self.web_search(
                query,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
            )
        except NotImplementedError:
            raise ToolNotConfiguredError(_unavailable_msg(query))

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
