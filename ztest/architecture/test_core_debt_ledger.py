"""Governance gate for the core architecture debt implementation ledger."""

from ztest.architecture.core_debt_ledger import validate_committed_ledger


def test_core_debt_ledger_is_internally_consistent() -> None:
    result = validate_committed_ledger()
    assert result.work_package_count == 96
    # Milestone blockers (for example ``M0``) are expanded to their concrete
    # work packages, so this count is intentionally larger than the document's
    # 122 explicitly written R-to-R edges.
    assert result.dependency_count > 122
    assert result.production_path_count > 0
