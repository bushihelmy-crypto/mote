"""Executable assertions over the canonical static-governance scanner."""

from __future__ import annotations

import pytest

from ztest.architecture.static_governance import CHECKS, _references_evidence


def test_evidence_linkage_rejects_unrelated_source() -> None:
    assert not _references_evidence(
        "from mote.runtime.unrelated import OtherService",
        {"CircuitBreaker", "breaker"},
    )


def test_evidence_linkage_accepts_registered_identity() -> None:
    assert _references_evidence(
        "from mote.runtime.resilience.breaker import CircuitBreaker",
        {"CircuitBreaker", "breaker"},
    )


@pytest.mark.parametrize("check_name", tuple(CHECKS))
def test_dynamic_boundary_governance(check_name: str) -> None:
    violations = CHECKS[check_name]()
    assert not violations, f"{check_name} violations:\n" + "\n".join(sorted(violations))
