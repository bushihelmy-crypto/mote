"""Ed25519 wire-permit signing with versioned issuer trust."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mote.contracts.inference.wire_permit import WirePermit


def canonical_wire_permit_payload(permit: WirePermit) -> bytes:
    payload = permit.model_dump(mode="json", exclude={"signature"})
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class Ed25519WirePermitSigner:
    def __init__(
        self,
        *,
        issuer_key_id: str,
        trust_revision: int,
        private_key: Ed25519PrivateKey,
    ) -> None:
        if not issuer_key_id or trust_revision <= 0:
            raise ValueError("permit signer identity is invalid")
        self._issuer_key_id = issuer_key_id
        self._trust_revision = trust_revision
        self._private_key = private_key

    def sign(self, permit: WirePermit) -> WirePermit:
        if permit.issuer_key_id != self._issuer_key_id or permit.trust_revision != self._trust_revision:
            raise ValueError("permit claims do not match signer identity")
        unsigned = permit.model_copy(update={"signature": "unsigned"})
        signature = self._private_key.sign(canonical_wire_permit_payload(unsigned))
        return unsigned.model_copy(update={"signature": _encode_signature(signature)})


class Ed25519WirePermitVerifier:
    def __init__(
        self,
        trusted_keys: Mapping[tuple[str, int], Ed25519PublicKey],
        *,
        revoked_keys: frozenset[tuple[str, int]] = frozenset(),
    ) -> None:
        self._trusted_keys = dict(trusted_keys)
        self._revoked_keys = revoked_keys

    def register(
        self,
        issuer_key_id: str,
        trust_revision: int,
        public_key: Ed25519PublicKey,
    ) -> None:
        identity = (issuer_key_id, trust_revision)
        if identity in self._trusted_keys:
            raise ValueError("wire-permit issuer identity is already registered")
        self._trusted_keys[identity] = public_key

    def revoke(self, issuer_key_id: str, trust_revision: int) -> None:
        self._revoked_keys = self._revoked_keys.union({(issuer_key_id, trust_revision)})

    async def verify(self, permit: WirePermit) -> bool:
        identity = (permit.issuer_key_id, permit.trust_revision)
        if identity in self._revoked_keys:
            return False
        key = self._trusted_keys.get(identity)
        if key is None:
            return False
        try:
            signature = _decode_signature(permit.signature)
            key.verify(signature, canonical_wire_permit_payload(permit))
        except (InvalidSignature, ValueError):
            return False
        return True


def _encode_signature(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def _decode_signature(signature: str) -> bytes:
    padding = "=" * (-len(signature) % 4)
    decoded = base64.b64decode(signature + padding, altchars=b"-_", validate=True)
    if len(decoded) != 64:
        raise ValueError("Ed25519 signature has invalid length")
    return decoded
