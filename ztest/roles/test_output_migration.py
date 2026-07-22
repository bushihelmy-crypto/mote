from dataclasses import dataclass

import pytest

from mote.roles.output_migration import OutputMigrationRegistry, ValidatorMigrationRegistry, ValidatorVersionMigration


@dataclass(frozen=True)
class Migration:
    name: str
    version: str
    source_contract_id: str
    source_schema_fingerprint: str
    target_contract_id: str
    target_schema_fingerprint: str
    field: str

    def migrate(self, value):
        return {self.field: value}


def _migration(source, target, source_fp, target_fp, field):
    return Migration(
        name=f"{source}-to-{target}",
        version="1",
        source_contract_id=source,
        source_schema_fingerprint=source_fp,
        target_contract_id=target,
        target_schema_fingerprint=target_fp,
        field=field,
    )


def test_unique_multi_step_path_migrates_in_order():
    registry = OutputMigrationRegistry(
        (
            _migration("test.v1@1", "test.v2@1", "fp1", "fp2", "v2"),
            _migration("test.v2@1", "test.v3@1", "fp2", "fp3", "v3"),
        )
    )

    value, applied = registry.migrate(
        7,
        source_contract_id="test.v1@1",
        source_schema_fingerprint="fp1",
        target_contract_id="test.v3@1",
        target_schema_fingerprint="fp3",
    )

    assert value == {"v3": {"v2": 7}}
    assert [step.name for step in applied] == [
        "test.v1@1-to-test.v2@1",
        "test.v2@1-to-test.v3@1",
    ]


def test_missing_or_ambiguous_paths_fail_closed():
    direct = _migration("a", "c", "a-fp", "c-fp", "direct")
    registry = OutputMigrationRegistry(
        (
            direct,
            _migration("a", "b", "a-fp", "b-fp", "via_b"),
            _migration("b", "c", "b-fp", "c-fp", "to_c"),
        )
    )

    with pytest.raises(ValueError, match="ambiguous"):
        registry.migrate(
            1,
            source_contract_id="a",
            source_schema_fingerprint="a-fp",
            target_contract_id="c",
            target_schema_fingerprint="c-fp",
        )
    with pytest.raises(ValueError, match="no output migration path"):
        registry.migrate(
            1,
            source_contract_id="x",
            source_schema_fingerprint="x-fp",
            target_contract_id="c",
            target_schema_fingerprint="c-fp",
        )


def test_fingerprint_mismatch_fails_before_migration_runs():
    registry = OutputMigrationRegistry((_migration("a", "b", "a-fp", "b-fp", "value"),))

    with pytest.raises(ValueError, match="source fingerprint mismatch"):
        registry.migrate(
            1,
            source_contract_id="a",
            source_schema_fingerprint="wrong",
            target_contract_id="b",
            target_schema_fingerprint="b-fp",
        )


@pytest.mark.asyncio
async def test_output_engine_migrates_then_recommits_current_contract():
    from mote.common.schema import OutputContractId, RunKind
    from mote.roles.output_contract import OutputContract, TypeAdapterOutputDecoder
    from mote.roles.output_engine import OutputEngine

    source_decoder = TypeAdapterOutputDecoder(int)
    target_decoder = TypeAdapterOutputDecoder(dict[str, int])
    migration = Migration(
        name="integer-to-report",
        version="1",
        source_contract_id="test.integer@1",
        source_schema_fingerprint=source_decoder.schema.fingerprint,
        target_contract_id="test.report@2",
        target_schema_fingerprint=target_decoder.schema.fingerprint,
        field="count",
    )
    contract = OutputContract(
        OutputContractId("test", "report", "2"),
        target_decoder,
        migration_registry=OutputMigrationRegistry((migration,)),
    )
    engine = OutputEngine(
        contract,
        restored_state={
            "status": "committed",
            "candidate_id": "candidate-1",
            "contract_id": "test.integer@1",
            "schema_fingerprint": source_decoder.schema.fingerprint,
            "value": 7,
            "run_id": "run-1",
            "run_kind": "agent",
        },
        run_kind=RunKind.AGENT,
    )

    assert engine.state.value == "accepted"
    assert engine.accepted_value == {"count": 7}
    committed = await engine.commit()

    assert committed.contract_id == "test.report@2"
    assert committed.value == {"count": 7}
    assert committed.run_id == "run-1"


def test_validator_identity_migration_preserves_execution_provenance():
    from mote.common.schema import ValidatorProvenance

    registry = ValidatorMigrationRegistry(
        (
            ValidatorVersionMigration("policy", "1", "policy", "2"),
            ValidatorVersionMigration("policy", "2", "policy-v3", "3"),
        )
    )
    recorded = (
        ValidatorProvenance(
            name="policy",
            version="1",
            stage="policy",
            effect="read_external",
            determinism="external_state",
            decision="accept",
        ),
    )

    migrated = registry.migrate(recorded, {("policy-v3", "3")})

    assert (migrated[0].name, migrated[0].version) == ("policy-v3", "3")
    assert migrated[0].stage == "policy"
    assert migrated[0].effect == "read_external"
    assert migrated[0].determinism == "external_state"
    assert migrated[0].decision == "accept"
