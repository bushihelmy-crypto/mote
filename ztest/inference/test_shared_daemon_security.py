import os
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.shared import ProtocolNegotiation, SharedHandshake
from mote.product.inference.daemon.security import (
    SharedAuthenticationError,
    SharedHandshakeAuthority,
    current_incarnation,
    sign_handshake,
)

DIGEST = "sha256:" + "4" * 64


def _handshake(now):
    return SharedHandshake(
        protocol_versions=(3, 2),
        application_id="application",
        caller=current_incarnation(os.getpid()),
        socket_generation="generation",
        tenant_id="tenant",
        project_id="project",
        subject_id="subject",
        policy_revision="1",
        delegation_digest=DIGEST,
        nonce="0123456789abcdef",
        issued_at=now,
        expires_at=now + timedelta(seconds=30),
        key_id="application-key",
        signature="unsigned",
    )


def test_shared_handshake_binds_peer_process_tenant_and_n_minus_one():
    now = datetime.now(timezone.utc)
    authority = SharedHandshakeAuthority(
        socket_generation="generation",
        application_keys={"application": ("application-key", b"application-secret")},
        session_key_id="daemon-key",
        session_key=b"daemon-session-secret",
        current_protocol_version=3,
    )
    assert authority.negotiate(ProtocolNegotiation(supported_versions=(2,))).protocol_version == 2
    signed = sign_handshake(_handshake(now), b"application-secret")
    credential = authority.authenticate(signed, peer_uid=os.getuid(), now=now)
    assert credential.principal.tenant_id == "tenant"
    assert authority.verify_session(credential, peer_uid=os.getuid(), now=now)
    authority.revoke_session(credential.session_id)
    assert not authority.verify_session(credential, peer_uid=os.getuid(), now=now)
    with pytest.raises(SharedAuthenticationError, match="replayed"):
        authority.authenticate(signed, peer_uid=os.getuid(), now=now)
    with pytest.raises(SharedAuthenticationError, match="UID"):
        SharedHandshakeAuthority(
            socket_generation="generation",
            application_keys={"application": ("application-key", b"application-secret")},
            session_key_id="daemon-key",
            session_key=b"daemon-session-secret",
            current_protocol_version=3,
        ).authenticate(signed, peer_uid=os.getuid() + 1, now=now)


def test_shared_negotiation_rejects_unsupported_versions():
    authority = SharedHandshakeAuthority(
        socket_generation="generation",
        application_keys={},
        session_key_id="daemon-key",
        session_key=b"daemon-session-secret",
        current_protocol_version=3,
    )
    with pytest.raises(SharedAuthenticationError, match="compatible"):
        authority.negotiate(ProtocolNegotiation(supported_versions=(1,)))
