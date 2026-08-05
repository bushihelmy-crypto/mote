from pathlib import Path

import mote.contracts.output as output
from mote import contracts


def test_output_contract_module_has_explicit_stable_surface():
    assert "OutputContractId" in output.__all__
    assert "ValidatedCandidate" in output.__all__
    assert "RunResult" in output.__all__
    assert "RunRejected" in output.__all__
    assert "RunRejectionKind" in output.__all__
    assert "RunOutcome" in output.__all__
    assert "ValidationIssue" in output.__all__


def test_output_contracts_are_only_available_from_output_domain():
    assert not hasattr(contracts, "OutputContractId")
    assert not hasattr(contracts, "OutputEvaluation")
    assert not hasattr(contracts, "RunResult")
    assert not hasattr(contracts, "RunRejected")
    assert not hasattr(contracts, "RunRejectionKind")
    assert not hasattr(contracts, "RunOutcome")


def test_common_schema_no_longer_owns_output_contracts():
    import mote.contracts.config.tool as schema

    assert not hasattr(schema, "OutputContractId")
    assert not hasattr(schema, "RunResult")
    assert not (Path(schema.__file__).parent / "output.py").exists()
