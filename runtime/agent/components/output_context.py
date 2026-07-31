"""Per-turn guidance for a Role's non-text output contract."""
from __future__ import annotations

import json
from typing import Optional

from mote.contracts.ports.conversation.turn_context import TurnContextPriority
from mote.kernel.output.contract import OutputContract


class OutputContractContextSource:
    """Render provider-neutral structured-output guidance below the cache boundary."""

    name = "output_contract"
    priority = TurnContextPriority.OUTPUT_CONTRACT
    save_to_context = False

    def __init__(self, contract: OutputContract) -> None:
        self._contract = contract

    async def render(self, *, cwd: Optional[str] = None) -> Optional[str]:
        contract_id = self._contract.contract_id
        if self._contract.is_text:
            return None
        schema = json.dumps(
            self._contract.decoder.schema.canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            "Your final answer must be only one JSON value matching the output "
            f"contract {contract_id}. Do not wrap it in Markdown fences or add prose.\n"
            f"JSON Schema: {schema}"
        )


__all__ = ["OutputContractContextSource"]
