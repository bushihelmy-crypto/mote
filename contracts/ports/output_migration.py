"""Leaf protocol for explicit output-schema migrations."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class OutputMigration(Protocol):
    name: str
    version: str
    source_contract_id: str
    source_schema_fingerprint: str
    target_contract_id: str
    target_schema_fingerprint: str

    def migrate(self, value: Any) -> Any:
        ...
