from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError
from mote.runtime.control.leases import FileLeaseCoordinator


def _canonical(path: Path) -> dict:
    coordinator = FileLeaseCoordinator(path, clock=lambda: 10.0)
    coordinator.acquire("session/one", "owner-a", 5.0)
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_rejected(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LeaseCoordinatorUnavailableError):
        FileLeaseCoordinator(path, clock=lambda: 11.0).get("session/one")


def test_file_lease_state_uses_versioned_canonical_instant(tmp_path: Path) -> None:
    payload = _canonical(tmp_path / "leases.json")
    assert payload["schema"] == "mote.file-lease-coordinator/v1"
    assert payload["schema_version"] == 1
    record = payload["leases"]["session/one"]
    assert record["subject"] == "session/one"
    assert record["expires_at"]["schema"] == "mote.absolute-instant/v1"
    assert record["expires_at"]["clock"]["value"] == "unix-utc"


def test_unrelated_write_preserves_exact_nanosecond_expiry(tmp_path: Path) -> None:
    path = tmp_path / "leases.json"
    payload = _canonical(path)
    exact = 15_000_000_001
    payload["leases"]["session/one"]["expires_at"]["epoch_nanoseconds"] = exact
    path.write_text(json.dumps(payload), encoding="utf-8")

    coordinator = FileLeaseCoordinator(path, clock=lambda: 11.0)
    coordinator.acquire("session/two", "owner-b", 5.0)

    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["leases"]["session/one"]["expires_at"]["epoch_nanoseconds"] == exact


@pytest.mark.parametrize("token", (True, "1", -1, 0))
def test_file_lease_decoder_rejects_noncanonical_fencing_tokens(tmp_path: Path, token: object) -> None:
    path = tmp_path / "leases.json"
    payload = _canonical(path)
    payload["leases"]["session/one"]["fencing_token"] = token
    _assert_rejected(path, payload)


def test_file_lease_decoder_rejects_unknown_version_subject_mismatch_and_extra_fields(tmp_path: Path) -> None:
    path = tmp_path / "leases.json"
    canonical = _canonical(path)
    future = deepcopy(canonical)
    future["schema_version"] = 2
    _assert_rejected(path, future)
    mismatch = deepcopy(canonical)
    mismatch["leases"]["session/one"]["subject"] = "session/two"
    _assert_rejected(path, mismatch)
    extra = deepcopy(canonical)
    extra["leases"]["session/one"]["extra"] = True
    _assert_rejected(path, extra)


def test_file_lease_decoder_rejects_invalid_instant_and_truncated_json(tmp_path: Path) -> None:
    path = tmp_path / "leases.json"
    canonical = _canonical(path)
    invalid_epoch = deepcopy(canonical)
    invalid_epoch["leases"]["session/one"]["expires_at"]["epoch_nanoseconds"] = float("nan")
    _assert_rejected(path, invalid_epoch)
    invalid_clock = deepcopy(canonical)
    invalid_clock["leases"]["session/one"]["expires_at"]["clock"]["value"] = "future-clock"
    _assert_rejected(path, invalid_clock)
    path.write_text('{"schema":', encoding="utf-8")
    with pytest.raises(LeaseCoordinatorUnavailableError):
        FileLeaseCoordinator(path).get("session/one")


def test_noncanonical_lease_mapping_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "leases.json"
    path.write_text(
        json.dumps(
            {
                "session/one": {
                    "owner_id": "",
                    "fencing_token": 7,
                    "expires_at": 10.0,
                }
            }
        ),
        encoding="utf-8",
    )
    coordinator = FileLeaseCoordinator(path, clock=lambda: 20.0)
    with pytest.raises(LeaseCoordinatorUnavailableError):
        coordinator.get("session/one")
    with pytest.raises(LeaseCoordinatorUnavailableError):
        coordinator.acquire("session/one", "owner-b", 5.0)
