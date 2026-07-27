from pathlib import Path

import mote.contracts.output as output
from mote import contracts


def test_output_contract_module_has_explicit_stable_surface():
    assert "OutputContractId" in output.__all__
    assert "OutputLifecycleState" in output.__all__
    assert "RunResult" in output.__all__
    assert "RunRejected" in output.__all__
    assert "RunRejectionKind" in output.__all__
    assert "RunOutcome" in output.__all__
    assert "ValidationIssue" in output.__all__


def test_high_frequency_output_contracts_are_available_at_contracts_root():
    assert contracts.OutputContractId is output.OutputContractId
    assert contracts.OutputEvaluation is output.OutputEvaluation
    assert contracts.RunResult is output.RunResult
    assert contracts.RunRejected is output.RunRejected
    assert contracts.RunRejectionKind is output.RunRejectionKind
    assert contracts.RunOutcome is output.RunOutcome


def test_common_schema_no_longer_owns_output_contracts():
    import mote.contracts.schema as schema

    assert not hasattr(schema, "OutputContractId")
    assert not hasattr(schema, "RunResult")
    assert not (Path(schema.__file__).parent / "output.py").exists()
