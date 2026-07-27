"""Static contract checks for the public generic output pipeline."""

from __future__ import annotations

from typing import assert_type

from pydantic import BaseModel

from mote.contracts.output import (
    Accept,
    Determinism,
    ValidationContext,
    ValidationStage,
    ValidatorDecision,
    ValidatorEffect,
)
from mote.output import OutputContract, OutputValidator


class Report(BaseModel):
    count: int


class ReportValidator:
    name = "report-policy"
    version = "1"
    stage = ValidationStage.POLICY
    determinism = Determinism.DETERMINISTIC
    effect = ValidatorEffect.PURE

    async def validate(
        self,
        value: Report,
        context: ValidationContext,
    ) -> ValidatorDecision[Report]:
        del context
        return Accept(value)


validator: OutputValidator[Report] = ReportValidator()
contract: OutputContract[Report] = OutputContract.from_type(
    Report,
    namespace="typecheck",
    name="report",
    version="1",
    validators=(validator,),
)
assert_type(contract.decoder.decode({"count": 1}), Report)
