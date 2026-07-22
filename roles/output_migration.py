"""Deployment-scoped registry for explicit output contract migrations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mote.common.interface import OutputMigration
from mote.common.schema import ValidatorProvenance


@dataclass(frozen=True)
class AppliedOutputMigration:
    name: str
    version: str
    source_contract_id: str
    target_contract_id: str


class OutputMigrationRegistry:
    def __init__(self, migrations: tuple[OutputMigration, ...] = ()) -> None:
        self._edges: dict[str, list[OutputMigration]] = {}
        identities: set[tuple[str, str]] = set()
        edge_keys: set[tuple[str, str]] = set()
        for migration in migrations:
            identity = (migration.name, migration.version)
            edge = (migration.source_contract_id, migration.target_contract_id)
            if not all(identity) or not all(edge):
                raise ValueError("migration identity and contract IDs are required")
            if not migration.source_schema_fingerprint or not migration.target_schema_fingerprint:
                raise ValueError("migration schema fingerprints are required")
            if identity in identities:
                raise ValueError(f"duplicate output migration {migration.name}@{migration.version}")
            if edge in edge_keys:
                raise ValueError(f"duplicate output migration edge {edge[0]} -> {edge[1]}")
            identities.add(identity)
            edge_keys.add(edge)
            self._edges.setdefault(edge[0], []).append(migration)

    def migrate(
        self,
        value: Any,
        *,
        source_contract_id: str,
        source_schema_fingerprint: str,
        target_contract_id: str,
        target_schema_fingerprint: str,
    ) -> tuple[Any, tuple[AppliedOutputMigration, ...]]:
        paths = self._paths(source_contract_id, target_contract_id)
        if len(paths) != 1:
            reason = "no" if not paths else "ambiguous"
            raise ValueError(f"{reason} output migration path")
        current_value = value
        current_fingerprint = source_schema_fingerprint
        applied: list[AppliedOutputMigration] = []
        for migration in paths[0]:
            if migration.source_schema_fingerprint != current_fingerprint:
                raise ValueError("output migration source fingerprint mismatch")
            current_value = migration.migrate(current_value)
            current_fingerprint = migration.target_schema_fingerprint
            applied.append(
                AppliedOutputMigration(
                    name=migration.name,
                    version=migration.version,
                    source_contract_id=migration.source_contract_id,
                    target_contract_id=migration.target_contract_id,
                )
            )
        if current_fingerprint != target_schema_fingerprint:
            raise ValueError("output migration target fingerprint mismatch")
        return current_value, tuple(applied)

    def _paths(self, source: str, target: str) -> list[list[OutputMigration]]:
        found: list[list[OutputMigration]] = []

        def visit(node: str, path: list[OutputMigration], seen: set[str]) -> None:
            if len(found) > 1:
                return
            if node == target:
                found.append(path)
                return
            for migration in self._edges.get(node, ()):
                if migration.target_contract_id in seen:
                    continue
                visit(
                    migration.target_contract_id,
                    [*path, migration],
                    {*seen, migration.target_contract_id},
                )

        visit(source, [], {source})
        return found


@dataclass(frozen=True)
class ValidatorVersionMigration:
    source_name: str
    source_version: str
    target_name: str
    target_version: str


class ValidatorMigrationRegistry:
    def __init__(self, migrations: tuple[ValidatorVersionMigration, ...] = ()) -> None:
        self._edges: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for migration in migrations:
            source = (migration.source_name, migration.source_version)
            target = (migration.target_name, migration.target_version)
            if not all((*source, *target)):
                raise ValueError("validator migration identities are required")
            targets = self._edges.setdefault(source, [])
            if target in targets:
                raise ValueError("duplicate validator migration edge")
            targets.append(target)

    def migrate(
        self,
        provenance: tuple[ValidatorProvenance, ...],
        target_identities: set[tuple[str, str]],
    ) -> tuple[ValidatorProvenance, ...]:
        migrated: list[ValidatorProvenance] = []
        for item in provenance:
            source = (item.name, item.version)
            if source in target_identities:
                target = source
            else:
                paths = self._identity_paths(source, target_identities)
                if len(paths) != 1:
                    reason = "no" if not paths else "ambiguous"
                    raise ValueError(f"{reason} validator migration path")
                target = paths[0][-1]
            migrated.append(
                ValidatorProvenance(
                    name=target[0],
                    version=target[1],
                    stage=item.stage,
                    effect=item.effect,
                    determinism=item.determinism,
                    decision=item.decision,
                )
            )
        return tuple(migrated)

    def _identity_paths(
        self,
        source: tuple[str, str],
        targets: set[tuple[str, str]],
    ) -> list[list[tuple[str, str]]]:
        found: list[list[tuple[str, str]]] = []

        def visit(node, path, seen):
            if len(found) > 1:
                return
            if node in targets:
                found.append(path)
                return
            for target in self._edges.get(node, ()):
                if target not in seen:
                    visit(target, [*path, target], {*seen, target})

        visit(source, [source], {source})
        return found
