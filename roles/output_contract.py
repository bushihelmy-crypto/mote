"""Trusted assembly of immutable typed output contracts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Generic, Self, TypeVar

from jsonschema.validators import validator_for
from pydantic import TypeAdapter, ValidationError

from mote.common.interface import OutputDecoder, OutputValidator
from mote.common.schema.output import (
    Determinism,
    OutputContractId,
    OutputDecodeError,
    SchemaDocument,
    ValidationIssue,
    ValidationStage,
    ValidatorEffect,
)
from mote.roles.output_migration import OutputMigrationRegistry, ValidatorMigrationRegistry

OutputT = TypeVar("OutputT")


class TypeAdapterOutputDecoder(Generic[OutputT]):
    """Pydantic TypeAdapter behind mote's provider-independent codec seam."""

    def __init__(self, output_type: Any) -> None:
        self._adapter = TypeAdapter(output_type)
        canonical = self._adapter.json_schema()
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._schema = SchemaDocument(
            dialect="https://json-schema.org/draft/2020-12/schema",
            canonical=canonical,
            fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )

    @property
    def schema(self) -> SchemaDocument:
        return self._schema

    def decode(self, raw: Any) -> OutputT:
        try:
            return self._adapter.validate_python(raw)
        except ValidationError:
            if isinstance(raw, str):
                return self._adapter.validate_json(raw)
            raise

    def encode(self, value: Any) -> Any:
        return self._adapter.dump_python(value, mode="json")


class JsonSchemaOutputDecoder:
    """JSON Schema decoder for model-authored RunGraph contracts."""

    def __init__(self, schema: dict[str, Any]) -> None:
        validator_cls = validator_for(schema)
        validator_cls.check_schema(schema)
        self._validator = validator_cls(schema)
        payload = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._schema = SchemaDocument(
            dialect=schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
            canonical=schema,
            fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )

    @property
    def schema(self) -> SchemaDocument:
        return self._schema

    def decode(self, raw: Any) -> Any:
        errors = sorted(self._validator.iter_errors(raw), key=lambda item: list(item.path))
        if errors:
            raise OutputDecodeError(
                tuple(
                    ValidationIssue(
                        path=tuple(error.path),
                        code=str(error.validator or "json_schema"),
                        message=error.message,
                    )
                    for error in errors
                )
            )
        return raw

    def encode(self, value: Any) -> Any:
        return value


@dataclass(frozen=True)
class OutputRetryPolicy:
    """Bounded model-correction policy for one output contract."""

    max_corrections: int = 2

    def __post_init__(self) -> None:
        if self.max_corrections < 0:
            raise ValueError("max_corrections must be non-negative")


@dataclass(frozen=True)
class OutputContract(Generic[OutputT]):
    contract_id: OutputContractId
    decoder: OutputDecoder
    retry_policy: OutputRetryPolicy = OutputRetryPolicy()
    validators: tuple[OutputValidator, ...] = ()
    migration_registry: OutputMigrationRegistry | None = None
    validator_migration_registry: ValidatorMigrationRegistry | None = None

    def __post_init__(self) -> None:
        identities: set[tuple[str, str]] = set()
        for validator in self.validators:
            identity = (validator.name, validator.version)
            if not all(identity):
                raise ValueError("output validator name and version are required")
            if identity in identities:
                raise ValueError(f"duplicate output validator {validator.name}@{validator.version}")
            identities.add(identity)
            if not isinstance(validator.stage, ValidationStage):
                raise ValueError(f"invalid validation stage for {validator.name}")
            if not isinstance(validator.determinism, Determinism):
                raise ValueError(f"invalid determinism for {validator.name}")
            if not isinstance(validator.effect, ValidatorEffect):
                raise ValueError(f"invalid validator effect for {validator.name}")

    @classmethod
    def from_type(
        cls,
        output_type: Any,
        *,
        namespace: str,
        name: str,
        version: str,
        retry_policy: OutputRetryPolicy = OutputRetryPolicy(),
        validators: tuple[OutputValidator, ...] = (),
        migration_registry: OutputMigrationRegistry | None = None,
        validator_migration_registry: ValidatorMigrationRegistry | None = None,
    ) -> Self:
        """Build a typed contract without exposing decoder assembly."""
        return cls(
            contract_id=OutputContractId(namespace, name, version),
            decoder=TypeAdapterOutputDecoder(output_type),
            retry_policy=retry_policy,
            validators=validators,
            migration_registry=migration_registry,
            validator_migration_registry=validator_migration_registry,
        )

    @classmethod
    def from_json_schema(
        cls,
        schema: dict[str, Any],
        *,
        namespace: str,
        name: str,
        version: str,
        retry_policy: OutputRetryPolicy = OutputRetryPolicy(),
        validators: tuple[OutputValidator, ...] = (),
        migration_registry: OutputMigrationRegistry | None = None,
        validator_migration_registry: ValidatorMigrationRegistry | None = None,
    ) -> Self:
        """Build a contract from a trusted JSON Schema document."""
        return cls(
            contract_id=OutputContractId(namespace, name, version),
            decoder=JsonSchemaOutputDecoder(schema),
            retry_policy=retry_policy,
            validators=validators,
            migration_registry=migration_registry,
            validator_migration_registry=validator_migration_registry,
        )

    @classmethod
    def text(cls) -> "OutputContract[str]":
        """Return the framework's stable plain-text output contract."""
        return OutputContract(
            contract_id=OutputContractId("mote", "text", "1"),
            decoder=TypeAdapterOutputDecoder(str),
        )

    @property
    def is_text(self) -> bool:
        return self.contract_id == OutputContractId("mote", "text", "1")


def text_output_contract() -> OutputContract[str]:
    return OutputContract.text()
