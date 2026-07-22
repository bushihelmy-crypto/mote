"""Provider-wire transforms for native structured output schemas."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return an OpenAI-strict wire copy without changing contract identity."""
    result = deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if isinstance(node.get("additionalProperties"), dict):
            raise ValueError("OpenAI strict output cannot represent an open-ended object map")
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["additionalProperties"] = False
            node["required"] = list(properties)
        for value in node.values():
            visit(value)

    visit(result)
    return result
