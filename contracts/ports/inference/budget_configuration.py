"""Narrow Product configuration Port for canonical UsageLedger scopes."""

from typing import Protocol


class BudgetLimitConfigurator(Protocol):
    async def configure_budget(self, tenant_id: str, project_id: str, limit_units: int) -> None: ...


__all__ = ["BudgetLimitConfigurator"]
