import pytest
from pydantic import BaseModel

from mote.common.schema import FinalCandidateAction, OutputContractId
from mote.roles.output_contract import (
    JsonSchemaOutputDecoder,
    OutputContract,
    OutputRetryPolicy,
    TypeAdapterOutputDecoder,
    text_output_contract,
)
from mote.roles.output_engine import OutputEngine


class Report(BaseModel):
    count: int


def test_schema_fingerprint_is_stable():
    first = TypeAdapterOutputDecoder(Report).schema
    second = TypeAdapterOutputDecoder(Report).schema
    assert first.canonical == second.canonical
    assert first.fingerprint == second.fingerprint


def test_json_schema_decoder_has_canonical_stable_fingerprint():
    first = JsonSchemaOutputDecoder({"required": ["count"], "properties": {"count": {"type": "integer"}}}).schema
    second = JsonSchemaOutputDecoder({"properties": {"count": {"type": "integer"}}, "required": ["count"]}).schema

    assert first.fingerprint == second.fingerprint


def test_json_schema_decoder_normalizes_nested_structural_issues():
    from mote.common.schema import OutputDecodeError

    decoder = JsonSchemaOutputDecoder(
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "integer"},
                }
            },
            "required": ["items"],
        }
    )

    assert decoder.decode({"items": [1, 2]}) == {"items": [1, 2]}
    with pytest.raises(OutputDecodeError) as caught:
        decoder.decode({"items": [1, "bad"]})

    assert caught.value.issues[0].path == ("items", 1)
    assert caught.value.issues[0].code == "type"


def test_public_contract_constructors_hide_decoder_assembly():
    from mote.output import OutputContract as PublicOutputContract

    typed = PublicOutputContract.from_type(
        Report,
        namespace="test",
        name="report",
        version="1",
    )
    schema = PublicOutputContract.from_json_schema(
        {"type": "object", "properties": {"count": {"type": "integer"}}},
        namespace="test",
        name="raw-report",
        version="1",
    )

    assert typed.decoder.decode({"count": 2}) == Report(count=2)
    assert schema.decoder.decode({"count": 2}) == {"count": 2}
    assert PublicOutputContract.text().is_text


@pytest.mark.asyncio
async def test_text_candidate_is_decoded_and_accepted():
    engine = OutputEngine(text_output_contract())
    result = await engine.evaluate(FinalCandidateAction(raw="done", representation="native_text"))
    assert result.accepted is True
    assert result.value == "done"
    assert engine.accepted_value == "done"

    await engine.commit()

    assert engine.committed is True
    assert engine.committed_output is not None
    assert engine.committed_output.run_id == engine.run_id


@pytest.mark.asyncio
async def test_commit_before_acceptance_is_a_typed_non_retryable_error():
    from mote.common.exception import OutputCommitStateError

    engine = OutputEngine(text_output_contract())

    with pytest.raises(OutputCommitStateError) as caught:
        await engine.commit()

    assert caught.value.code.value == "OUTPUT_COMMIT_INVALID_STATE"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_stale_worker_cannot_commit_after_lease_takeover(tmp_path):
    from mote.common.exception import OutputCommitFencedError
    from mote.session.run_lease import RunLeaseStore

    now = [100.0]
    store = RunLeaseStore(tmp_path / "leases.json", clock=lambda: now[0])
    first_lease = store.acquire("run-1", "worker-a", 10)
    first = OutputEngine(
        text_output_contract(),
        run_id="run-1",
        commit_fence=store,
        fencing_token=first_lease.fencing_token,
    )
    await first.evaluate(FinalCandidateAction(raw="stale", representation="test"))

    now[0] = 111
    second_lease = store.acquire("run-1", "worker-b", 10)
    second = OutputEngine(
        text_output_contract(),
        run_id="run-1",
        commit_fence=store,
        fencing_token=second_lease.fencing_token,
    )
    await second.evaluate(FinalCandidateAction(raw="current", representation="test"))

    with pytest.raises(OutputCommitFencedError) as caught:
        await first.commit()
    committed = await second.commit()

    assert caught.value.code.value == "OUTPUT_COMMIT_FENCED"
    assert first.committed is False
    assert committed.value == "current"
    assert committed.fencing_token == second_lease.fencing_token


@pytest.mark.asyncio
async def test_invalid_structured_candidate_returns_typed_issues():
    contract = OutputContract(OutputContractId("test", "report", "1"), TypeAdapterOutputDecoder(Report))
    engine = OutputEngine(contract)
    result = await engine.evaluate(FinalCandidateAction(raw={"count": "bad"}, representation="test"))
    assert result.accepted is False
    assert result.issues[0].path == ("count",)
    assert result.issues[0].code == "int_parsing"


@pytest.mark.asyncio
async def test_structured_candidate_decodes_prompted_json_text():
    contract = OutputContract(OutputContractId("test", "report", "1"), TypeAdapterOutputDecoder(Report))
    engine = OutputEngine(contract)

    result = await engine.evaluate(FinalCandidateAction(raw='{"count": 7}', representation="native_text"))

    assert result.accepted is True
    assert result.value == Report(count=7)


@pytest.mark.asyncio
async def test_rejection_consumes_only_the_independent_correction_budget():
    contract = OutputContract(
        OutputContractId("test", "report", "1"),
        TypeAdapterOutputDecoder(Report),
        OutputRetryPolicy(max_corrections=2),
    )
    engine = OutputEngine(contract)

    first = await engine.evaluate(FinalCandidateAction(raw={"count": "bad"}, representation="test"))
    accepted = await engine.evaluate(FinalCandidateAction(raw={"count": 1}, representation="test"))

    assert first.correction_allowed is True
    assert first.correction_attempt == 1
    assert first.corrections_remaining == 1
    assert accepted.accepted is True
    assert engine.correction_attempts == 1


@pytest.mark.asyncio
async def test_zero_budget_allows_initial_candidate_but_no_correction():
    contract = OutputContract(
        OutputContractId("test", "report", "1"),
        TypeAdapterOutputDecoder(Report),
        OutputRetryPolicy(max_corrections=0),
    )
    engine = OutputEngine(contract)

    result = await engine.evaluate(FinalCandidateAction(raw={"count": "bad"}, representation="test"))

    assert result.accepted is False
    assert result.correction_allowed is False
    assert result.correction_attempt == 0
    assert result.corrections_remaining == 0
    assert engine.correction_attempts == 0


def test_negative_correction_budget_is_rejected_at_contract_boundary():
    with pytest.raises(ValueError, match="non-negative"):
        OutputRetryPolicy(max_corrections=-1)


def test_restore_preserves_correction_budget():
    contract = OutputContract(
        OutputContractId("test", "report", "1"),
        TypeAdapterOutputDecoder(Report),
        OutputRetryPolicy(max_corrections=2),
    )
    engine = OutputEngine(
        contract,
        restored_state={
            "status": "awaiting_correction",
            "contract_id": "test.report@1",
            "schema_fingerprint": contract.decoder.schema.fingerprint,
            "correction_attempts": 1,
        },
    )

    assert engine.correction_attempts == 1


def test_restore_refuses_contract_or_schema_drift():
    from mote.common.exception import OutputResumeContractMismatchError

    contract = OutputContract(OutputContractId("test", "report", "1"), TypeAdapterOutputDecoder(Report))

    with pytest.raises(OutputResumeContractMismatchError) as caught:
        OutputEngine(
            contract,
            restored_state={
                "status": "awaiting_correction",
                "contract_id": "test.report@2",
                "schema_fingerprint": contract.decoder.schema.fingerprint,
            },
        )

    assert caught.value.code.value == "OUTPUT_RESUME_CONTRACT_MISMATCH"


def test_restore_decodes_accepted_value_without_revalidation():
    contract = OutputContract(OutputContractId("test", "report", "1"), TypeAdapterOutputDecoder(Report))
    engine = OutputEngine(
        contract,
        restored_state={
            "status": "commit_started",
            "candidate_id": "candidate-1",
            "contract_id": "test.report@1",
            "schema_fingerprint": contract.decoder.schema.fingerprint,
            "value": {"count": 8},
            "correction_attempts": 1,
        },
    )

    assert engine.accepted is True
    assert engine.accepted_value == Report(count=8)
    assert engine.correction_attempts == 1


def test_restore_refuses_validator_version_drift():
    from mote.common.exception import OutputResumeContractMismatchError
    from mote.common.schema import Accept, ValidationStage

    contract = OutputContract(
        OutputContractId("test", "report", "1"),
        TypeAdapterOutputDecoder(Report),
        validators=(_Validator("policy", ValidationStage.POLICY, Accept(Report(count=1))),),
    )

    with pytest.raises(OutputResumeContractMismatchError):
        OutputEngine(
            contract,
            restored_state={
                "status": "committed",
                "candidate_id": "candidate-1",
                "contract_id": "test.report@1",
                "schema_fingerprint": contract.decoder.schema.fingerprint,
                "value": {"count": 1},
                "validator_provenance": [
                    {
                        "name": "policy",
                        "version": "0",
                        "stage": "policy",
                        "effect": "pure",
                        "determinism": "deterministic",
                        "decision": "accept",
                    }
                ],
            },
        )


class _Validator:
    version = "1"

    def __init__(self, name, stage, decision):
        from mote.common.schema import Determinism, ValidatorEffect

        self.name = name
        self.stage = stage
        self.determinism = Determinism.DETERMINISTIC
        self.effect = ValidatorEffect.PURE
        self._decision = decision

    async def validate(self, value, context):
        return self._decision(value) if callable(self._decision) else self._decision


@pytest.mark.asyncio
async def test_validator_pipeline_orders_stages_and_applies_correction():
    from mote.common.schema import Accept, Corrected, ValidationStage

    calls = []

    def correct(value):
        calls.append("semantic")
        return Corrected(Report(count=value.count + 1), note="canonicalized")

    def accept(value):
        calls.append("policy")
        return Accept(value)

    contract = OutputContract(
        OutputContractId("test", "report", "1"),
        TypeAdapterOutputDecoder(Report),
        validators=(
            _Validator("policy", ValidationStage.POLICY, accept),
            _Validator("semantic", ValidationStage.SEMANTIC, correct),
        ),
    )
    engine = OutputEngine(contract)

    result = await engine.evaluate(FinalCandidateAction(raw={"count": 1}, representation="test"))

    assert calls == ["semantic", "policy"]
    assert result.accepted is True
    assert result.value == Report(count=2)
    assert [item.decision for item in engine.validator_provenance] == [
        "corrected",
        "accept",
    ]
    assert [item.name for item in engine.validator_provenance] == [
        "semantic",
        "policy",
    ]

    committed = await engine.commit()
    assert committed.validator_provenance == engine.validator_provenance


@pytest.mark.asyncio
async def test_validator_reject_is_model_correction_not_exception():
    from mote.common.schema import Reject, ValidationIssue, ValidationStage

    contract = OutputContract(
        OutputContractId("test", "report", "1"),
        TypeAdapterOutputDecoder(Report),
        validators=(
            _Validator(
                "positive",
                ValidationStage.SEMANTIC,
                Reject((ValidationIssue(("count",), "positive", "Must be positive"),)),
            ),
        ),
    )

    result = await OutputEngine(contract).evaluate(FinalCandidateAction(raw={"count": -1}, representation="test"))

    assert result.accepted is False
    assert result.issues[0].code == "positive"


@pytest.mark.asyncio
async def test_validator_retry_later_is_typed_operational_error():
    from mote.common.exception import OutputValidatorUnavailableError
    from mote.common.schema import RetryLater, ValidationStage

    contract = OutputContract(
        OutputContractId("test", "report", "1"),
        TypeAdapterOutputDecoder(Report),
        validators=(
            _Validator(
                "remote-policy",
                ValidationStage.POLICY,
                RetryLater("policy service unavailable", 1.5),
            ),
        ),
    )

    engine = OutputEngine(contract)
    with pytest.raises(OutputValidatorUnavailableError) as caught:
        await engine.evaluate(FinalCandidateAction(raw={"count": 1}, representation="test"))

    assert caught.value.retryable is True
    assert caught.value.context["retry_after_seconds"] == 1.5
    assert engine.correction_attempts == 0
