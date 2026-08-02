"""Same-host Shared Process handshake and protocol negotiation authority."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mote.contracts.inference.identity import InferencePrincipal
from mote.contracts.inference.shared import (
    CallerIncarnation,
    ProtocolNegotiation,
    ProtocolNegotiationResult,
    SharedHandshake,
    SharedSessionCredential,
)
from mote.product.inference.security.wire_permit import Ed25519WirePermitVerifier


class SharedAuthenticationError(PermissionError):
    pass


class SharedHandshakeAuthority:
    def __init__(
        self,
        *,
        socket_generation: str,
        application_keys: Mapping[str, tuple[str, bytes]],
        session_key_id: str,
        session_key: bytes,
        current_protocol_version: int,
        session_ttl_seconds: float = 300.0,
        max_clock_skew_seconds: float = 5.0,
        nonce_capacity: int = 100_000,
        permit_verifier: Ed25519WirePermitVerifier | None = None,
    ) -> None:
        if (
            not socket_generation
            or current_protocol_version < 2
            or session_ttl_seconds <= 0
            or max_clock_skew_seconds < 0
            or nonce_capacity <= 0
        ):
            raise ValueError("invalid Shared handshake authority configuration")
        self._socket_generation = socket_generation
        self._application_keys = dict(application_keys)
        self._session_key_id = session_key_id
        self._session_key = session_key
        self._current_protocol_version = current_protocol_version
        self._session_ttl_seconds = session_ttl_seconds
        self._max_clock_skew_seconds = max_clock_skew_seconds
        self._nonce_capacity = nonce_capacity
        self._seen_nonces: OrderedDict[tuple[str, str], datetime] = OrderedDict()
        self._sessions: OrderedDict[str, tuple[datetime, str, int]] = OrderedDict()
        self._permit_verifier = permit_verifier

    def negotiate(self, request: ProtocolNegotiation) -> ProtocolNegotiationResult:
        accepted = {
            self._current_protocol_version,
            self._current_protocol_version - 1,
        }.intersection(request.supported_versions)
        if not accepted:
            raise SharedAuthenticationError("no compatible Shared protocol version")
        return ProtocolNegotiationResult(
            protocol_version=max(accepted),
            capabilities=tuple(sorted(set(request.capabilities))),
            socket_generation=self._socket_generation,
        )

    def authenticate(
        self,
        handshake: SharedHandshake,
        *,
        peer_uid: int,
        now: datetime | None = None,
    ) -> SharedSessionCredential:
        current = now or datetime.now(timezone.utc)
        if peer_uid != os.getuid():
            raise SharedAuthenticationError("Shared peer UID rejected")
        if handshake.socket_generation != self._socket_generation:
            raise SharedAuthenticationError("daemon socket generation mismatch")
        if (
            handshake.issued_at - timedelta(seconds=self._max_clock_skew_seconds) > current
            or current >= handshake.expires_at
        ):
            raise SharedAuthenticationError("handshake validity window rejected")
        key_record = self._application_keys.get(handshake.application_id)
        if key_record is None or key_record[0] != handshake.key_id:
            raise SharedAuthenticationError("application identity is unknown")
        if not hmac.compare_digest(
            _decode_mac(handshake.signature),
            _mac(key_record[1], _canonical(handshake, exclude_signature=True)),
        ):
            raise SharedAuthenticationError("application handshake signature rejected")
        observed = current_incarnation(handshake.caller.pid)
        if observed != handshake.caller:
            raise SharedAuthenticationError("caller process incarnation rejected")
        nonce_key = (handshake.application_id, handshake.nonce)
        self._purge_nonces(current)
        if nonce_key in self._seen_nonces:
            raise SharedAuthenticationError("handshake nonce replayed")
        self._seen_nonces[nonce_key] = handshake.expires_at
        self._seen_nonces.move_to_end(nonce_key)
        while len(self._seen_nonces) > self._nonce_capacity:
            self._seen_nonces.popitem(last=False)
        negotiation = self.negotiate(ProtocolNegotiation(supported_versions=handshake.protocol_versions))
        permit_private_key = Ed25519PrivateKey.generate()
        permit_issuer_key_id = f"shared-session:{secrets.token_urlsafe(18)}"
        if self._permit_verifier is not None:
            self._permit_verifier.register(
                permit_issuer_key_id,
                1,
                permit_private_key.public_key(),
            )
        encoded_private_key = (
            base64.urlsafe_b64encode(
                permit_private_key.private_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PrivateFormat.Raw,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        unsigned = SharedSessionCredential(
            session_id=secrets.token_urlsafe(24),
            protocol_version=negotiation.protocol_version,
            socket_generation=self._socket_generation,
            application_id=handshake.application_id,
            caller=handshake.caller,
            principal=InferencePrincipal(
                tenant_id=handshake.tenant_id,
                project_id=handshake.project_id,
                subject_id=handshake.subject_id,
                policy_revision=handshake.policy_revision,
                delegation_digest=handshake.delegation_digest,
            ),
            issued_at=current,
            expires_at=current + timedelta(seconds=self._session_ttl_seconds),
            key_id=self._session_key_id,
            permit_issuer_key_id=permit_issuer_key_id,
            permit_trust_revision=1,
            permit_private_key=encoded_private_key,
            signature="unsigned",
        )
        credential = unsigned.model_copy(
            update={
                "signature": _encode_mac(
                    _mac(
                        self._session_key,
                        _canonical(unsigned, exclude_signature=True),
                    )
                )
            }
        )
        self._sessions[credential.session_id] = (
            credential.expires_at,
            credential.permit_issuer_key_id,
            credential.permit_trust_revision,
        )
        self._sessions.move_to_end(credential.session_id)
        while len(self._sessions) > self._nonce_capacity:
            _session_id, (_expiry, key_id, revision) = self._sessions.popitem(last=False)
            if self._permit_verifier is not None:
                self._permit_verifier.revoke(key_id, revision)
        return credential

    def verify_session(
        self,
        credential: SharedSessionCredential,
        *,
        peer_uid: int,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        self._purge_sessions(current)
        registered = self._sessions.get(credential.session_id)
        if (
            peer_uid != os.getuid()
            or credential.key_id != self._session_key_id
            or credential.socket_generation != self._socket_generation
            or current >= credential.expires_at
            or current_incarnation(credential.caller.pid) != credential.caller
            or registered
            != (
                credential.expires_at,
                credential.permit_issuer_key_id,
                credential.permit_trust_revision,
            )
        ):
            return False
        try:
            supplied = _decode_mac(credential.signature)
        except ValueError:
            return False
        return hmac.compare_digest(
            supplied,
            _mac(
                self._session_key,
                _canonical(credential, exclude_signature=True),
            ),
        )

    def revoke_session(self, session_id: str) -> None:
        record = self._sessions.pop(session_id, None)
        if record is None:
            return
        _expiry, key_id, revision = record
        if self._permit_verifier is not None:
            self._permit_verifier.revoke(key_id, revision)

    def _purge_nonces(self, now: datetime) -> None:
        expired = [key for key, expiry in self._seen_nonces.items() if expiry <= now]
        for key in expired:
            del self._seen_nonces[key]

    def _purge_sessions(self, now: datetime) -> None:
        expired = [session_id for session_id, (expiry, _key_id, _revision) in self._sessions.items() if expiry <= now]
        for session_id in expired:
            self.revoke_session(session_id)


def current_incarnation(pid: int) -> CallerIncarnation:
    stat_path = Path("/proc") / str(pid) / "stat"
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        stat = stat_path.read_text(encoding="utf-8")
        boot_id = boot_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SharedAuthenticationError("caller process is unavailable") from exc
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    if close < 0 or len(fields) <= 19:
        raise SharedAuthenticationError("caller process identity is malformed")
    return CallerIncarnation(
        pid=pid,
        process_start_ticks=int(fields[19]),
        boot_id=boot_id,
    )


def sign_handshake(handshake: SharedHandshake, key: bytes) -> SharedHandshake:
    unsigned = handshake.model_copy(update={"signature": "unsigned"})
    return unsigned.model_copy(
        update={"signature": _encode_mac(_mac(key, _canonical(unsigned, exclude_signature=True)))}
    )


def _canonical(value, *, exclude_signature: bool) -> bytes:
    excluded = {"signature"} if exclude_signature else set()
    return json.dumps(
        value.model_dump(mode="json", exclude=excluded),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _mac(key: bytes, payload: bytes) -> bytes:
    return hmac.new(key, payload, hashlib.sha256).digest()


def _encode_mac(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_mac(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if len(decoded) != 32:
        raise ValueError("HMAC has invalid length")
    return decoded
