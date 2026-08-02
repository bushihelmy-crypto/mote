"""Product credential-source catalog for static, environment and OAuth inputs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from secrets import token_bytes
from typing import Mapping

from mote.contracts.config.model.oauth import OAuthProviderConfig
from mote.product.config.model.inputs import (
    ExplicitModelsConfig,
    ProductEndpointInput,
    ProductModelsConfig,
    ShortcutModelsConfig,
)
from mote.product.models.compiler import CredentialSourceDescriptor
from mote.product.models.secrets import (
    CredentialEpoch,
    CredentialMaterial,
    InMemorySecretHandle,
    SecretHandle,
    SecretIdentity,
)
from mote.runtime.models.auth.oauth.manager import OAuthManager
from mote.runtime.process import FixedExecutableBinding, ProcessDisposition, run_verified_fixed_argv
from mote.runtime.telemetry.logging import logger

_EPOCH_KEY = token_bytes(32)


def _epoch(value: str) -> CredentialEpoch:
    return CredentialEpoch(hmac.digest(_EPOCH_KEY, value.encode(), "sha256").hex())


def _shortcut_endpoint(default: ProductEndpointInput, endpoint: ProductEndpointInput) -> ProductEndpointInput:
    values = endpoint.model_dump()
    for field in ("provider", "api_key", "api_type", "base_url", "oauth"):
        if values[field] is None:
            values[field] = getattr(default, field)
    return ProductEndpointInput.model_validate(values)


@dataclass(frozen=True, slots=True)
class _StaticSource:
    value: str


@dataclass(frozen=True, slots=True)
class _EnvironmentSource:
    variable: str


@dataclass(frozen=True, slots=True)
class _OAuthSource:
    config: OAuthProviderConfig
    provider: str


@dataclass(frozen=True, slots=True)
class _HelperSource:
    argv: tuple[str, ...]
    executable_device: int
    executable_inode: int


_HELPER_TIMEOUT_SECONDS = 30.0
_HELPER_MAX_OUTPUT_BYTES = 65_536


class ProductCredentialSourceCatalog:
    def __init__(
        self,
        source: ProductModelsConfig,
        *,
        oauth_root: Path,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._oauth_root = oauth_root
        self._environ = environ if environ is not None else os.environ
        self._sources: dict[str, _StaticSource | _EnvironmentSource | _OAuthSource | _HelperSource] = {}
        self._index(source)

    def _index(self, source: ProductModelsConfig) -> None:
        if isinstance(source, ShortcutModelsConfig):
            endpoints = {
                "endpoint:default": source.default,
                **{
                    f"endpoint:task:{name}": _shortcut_endpoint(source.default, endpoint)
                    for name, endpoint in source.tasks.items()
                },
            }
            for endpoint_id, endpoint in endpoints.items():
                self._index_direct(
                    endpoint_id,
                    endpoint,
                    helper=(source.api_key_helper.argv if source.api_key_helper else None),
                )
            return
        assert isinstance(source, ExplicitModelsConfig)
        for endpoint_id, endpoint in source.endpoints.items():
            if endpoint.credential_pool is None:
                self._index_direct(endpoint_id, endpoint)
                continue
            for slot in source.credential_pools[endpoint.credential_pool].slots:
                if not slot.secret_ref.startswith("env://"):
                    raise ValueError(f"credential slot {slot.id!r} requires an env:// reference")
                variable = slot.secret_ref.removeprefix("env://")
                if not variable:
                    raise ValueError(f"credential slot {slot.id!r} has an empty env ref")
                self._sources[slot.secret_ref] = _EnvironmentSource(variable)

    def _index_direct(
        self,
        endpoint_id: str,
        endpoint: ProductEndpointInput,
        *,
        helper: tuple[str, ...] | None = None,
    ) -> None:
        if endpoint.oauth is not None:
            oauth = OAuthProviderConfig.model_validate(endpoint.oauth.model_dump() | {"storage_root": self._oauth_root})
            self._sources[f"{endpoint_id}:oauth"] = _OAuthSource(oauth, endpoint.provider or endpoint_id)
            return
        keys = endpoint.api_key if isinstance(endpoint.api_key, list) else [endpoint.api_key]
        for index, value in enumerate(keys):
            if not value:
                if helper is None:
                    raise ValueError(f"endpoint {endpoint_id!r} has no credential")
                executable = Path(helper[0]).resolve(strict=True)
                metadata = executable.stat()
                if not stat.S_ISREG(metadata.st_mode) or not os.access(executable, os.X_OK):
                    raise ValueError("api_key_helper executable must be an executable regular file")
                self._sources[f"{endpoint_id}:key:{index}"] = _HelperSource(
                    (str(executable), *helper[1:]),
                    metadata.st_dev,
                    metadata.st_ino,
                )
                continue
            self._sources[f"{endpoint_id}:key:{index}"] = _StaticSource(value)

    def describe(self, source_ids: tuple[str, ...]) -> tuple[CredentialSourceDescriptor, ...]:
        descriptors: list[CredentialSourceDescriptor] = []
        for source_id in source_ids:
            source = self._sources[source_id]
            if isinstance(source, _StaticSource):
                epoch = _epoch(source.value)
            elif isinstance(source, _EnvironmentSource):
                value = self._environ.get(source.variable)
                if not value:
                    raise ValueError(f"credential environment variable {source.variable!r} is unset")
                epoch = _epoch(value)
            elif isinstance(source, _OAuthSource):
                public = source.config.model_dump_json(exclude={"storage_root"})
                epoch = _epoch(public)
            else:
                epoch = _epoch(
                    f"helper-v1\0{source.executable_device}\0{source.executable_inode}\0" + "\0".join(source.argv)
                )
            descriptors.append(CredentialSourceDescriptor(source_id, epoch))
        return tuple(descriptors)

    async def create_handle(self, slot_id: str, endpoint_id: str, source_id: str) -> SecretHandle:
        source = self._sources[source_id]
        identity = SecretIdentity(hashlib.sha256(f"{endpoint_id}\0{slot_id}".encode()).hexdigest()[:24])
        if isinstance(source, _OAuthSource):
            return OAuthSecretHandle(
                endpoint_id=endpoint_id,
                slot_id=slot_id,
                identity=identity,
                epoch=self.describe((source_id,))[0].epoch,
                manager=OAuthManager(source.config, provider=source.provider),
                force_refresh=slot_id.endswith("oauth-refresh"),
            )
        if isinstance(source, _HelperSource):
            command_identity = hashlib.sha256("\0".join(source.argv).encode()).hexdigest()
            logger.info(
                f"api_key_helper activation source={source_id} command_sha256={command_identity} "
                f"executable_device={source.executable_device} executable_inode={source.executable_inode}"
            )
            result = await run_verified_fixed_argv(
                FixedExecutableBinding(
                    source.argv[0],
                    source.executable_device,
                    source.executable_inode,
                ),
                source.argv[1:],
                working_dir=str(self._oauth_root.parent),
                env={},
                timeout=_HELPER_TIMEOUT_SECONDS,
                max_output_bytes=_HELPER_MAX_OUTPUT_BYTES,
            )
            logger.info(
                f"api_key_helper settlement source={source_id} command_sha256={command_identity} "
                f"disposition={result.disposition.value} exit_code={result.exit_code}"
            )
            if result.disposition is not ProcessDisposition.EXITED or result.exit_code != 0:
                raise RuntimeError(f"api_key_helper failed: {result.disposition.value}")
            value = result.stdout
            if not value or "\n" in value or "\x00" in value:
                raise RuntimeError("api_key_helper returned an invalid credential payload")
            return InMemorySecretHandle(
                endpoint_id=endpoint_id,
                slot_id=slot_id,
                identity=identity,
                epoch=self.describe((source_id,))[0].epoch,
                value=value,
            )
        value = source.value if isinstance(source, _StaticSource) else self._environ.get(source.variable)
        if not value:
            raise ValueError(f"credential source {source_id!r} is unavailable")
        return InMemorySecretHandle(
            endpoint_id=endpoint_id,
            slot_id=slot_id,
            identity=identity,
            epoch=_epoch(value),
            value=value,
        )


class _OAuthCredentialLease:
    def __init__(self, handle: "OAuthSecretHandle") -> None:
        self._handle = handle
        self._released = False

    async def resolve(self) -> CredentialMaterial:
        if self._released:
            raise RuntimeError("credential lease is released")
        token = await asyncio.to_thread(self._handle._resolve)
        return CredentialMaterial(self._handle._endpoint_id, self._handle._slot_id, token)

    async def refresh(self) -> CredentialMaterial:
        return await self.resolve()

    async def release(self) -> None:
        self._released = True


class OAuthSecretHandle:
    def __init__(
        self,
        *,
        endpoint_id: str,
        slot_id: str,
        identity: SecretIdentity,
        epoch: CredentialEpoch,
        manager: OAuthManager,
        force_refresh: bool,
    ) -> None:
        self._endpoint_id = endpoint_id
        self._slot_id = slot_id
        self._identity = identity
        self._epoch = epoch
        self._manager = manager
        self._force_refresh = force_refresh
        self._closed = False

    def __repr__(self) -> str:
        return "OAuthSecretHandle(<redacted>)"

    @property
    def volatile(self) -> bool:
        return True

    @property
    def identity(self) -> SecretIdentity:
        return self._identity

    @property
    def epoch(self) -> CredentialEpoch:
        return self._epoch

    def _resolve(self) -> str:
        if self._force_refresh:
            token = self._manager.force_refresh()
            if token is None:
                raise RuntimeError("OAuth credential refresh failed")
            return token.access_token
        return self._manager.get_valid_token()

    async def acquire(self) -> _OAuthCredentialLease:
        if self._closed:
            raise RuntimeError("secret handle is closed")
        return _OAuthCredentialLease(self)

    async def aclose(self) -> None:
        self._closed = True


__all__ = ["OAuthSecretHandle", "ProductCredentialSourceCatalog"]
