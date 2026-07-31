import pytest
from pydantic import ValidationError

from mote.contracts.config.inference import DeploymentMode, InferenceConfig, PrivateNetworkPolicy


def _persistence():
    return {"encryption_key_ref": "env://INFERENCE_STORAGE_KEY"}


def test_embedded_defaults_are_bounded_and_private_network_is_denied():
    config = InferenceConfig(persistence=_persistence())
    assert config.deployment is DeploymentMode.EMBEDDED
    assert config.capacity.queue_capacity == 5000
    assert config.network.allow_private_network is False
    assert config.network.block_link_local is True
    assert config.network.block_metadata_endpoints is True


def test_shared_process_requires_explicit_socket_contract():
    with pytest.raises(ValidationError, match="requires shared_process config"):
        InferenceConfig(deployment="shared_process", persistence=_persistence())
    config = InferenceConfig(
        deployment="shared_process",
        persistence=_persistence(),
        shared_process={"runtime_directory": "runtime/inference/shared"},
    )
    assert config.shared_process is not None
    assert config.shared_process.peer_credentials_required is True


def test_private_network_requires_explicit_opt_in_and_canonical_cidr():
    with pytest.raises(ValidationError, match="require allow_private_network"):
        PrivateNetworkPolicy(allowed_cidrs=("10.0.0.0/8",))
    with pytest.raises(ValidationError):
        PrivateNetworkPolicy(allow_private_network=True, allowed_cidrs=("10.0.0.1/8",))
    policy = PrivateNetworkPolicy(
        allow_private_network=True,
        allowed_cidrs=("10.0.0.0/8",),
        allowed_dns_suffixes=("INTERNAL.EXAMPLE.",),
    )
    assert policy.allowed_dns_suffixes == ("internal.example",)


def test_cluster_is_not_an_accepted_current_deployment_mode():
    with pytest.raises(ValidationError):
        InferenceConfig(deployment="cluster", persistence=_persistence())


def test_shared_sqlite_requires_full_durability_and_ordered_watermarks():
    config = InferenceConfig(persistence=_persistence())
    sqlite = config.persistence.shared_sqlite
    assert sqlite.wal_enabled and sqlite.foreign_keys
    assert sqlite.synchronous == "FULL"
    assert sqlite.hard_disk_free_bytes < sqlite.soft_disk_free_bytes
    with pytest.raises(ValidationError, match="requires WAL"):
        InferenceConfig(persistence={**_persistence(), "shared_sqlite": {"wal_enabled": False}})


def test_response_caches_are_opt_in_and_semantic_backend_is_explicit():
    config = InferenceConfig(persistence=_persistence())
    assert config.cache.exact.enabled is False
    assert config.cache.semantic.enabled is False
    with pytest.raises(ValidationError, match="explicit backend"):
        InferenceConfig(persistence=_persistence(), cache={"semantic": {"enabled": True}})
