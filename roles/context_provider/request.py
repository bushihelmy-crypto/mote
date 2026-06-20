"""ThinkRequest — ContextProvider's product, the input set for ThinkEngine.start()."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ThinkRequest:
    """The complete argument set for ThinkEngine.start() — ContextProvider's product.

    Bundles the values the think step feeds into the engine so the loop faces
    a single entry point (context_provider.prepare()) instead of the
    collect → build → prepare_llm_request → tool_specs glue chain.
    """

    req: list
    system_prompt: str
    tool_specs: Any
