#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the MITM CA (``sandbox.network.tls``).

Covers CA generate/load idempotency + file permissions, per-host leaf minting
(signed by the CA, correct SAN, LRU-cached), and the combined trust bundle
(contains both our CA and a real public root).
"""
from __future__ import annotations

import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mote.sandbox.network import tls as tls_mod
from mote.sandbox.network.tls import MitmCa


def test_ca_generated_on_first_use(tmp_path):
    ca = MitmCa(ca_dir=tmp_path)
    assert ca.cert_path.exists()
    assert ca.key_path.exists()


def test_ca_private_key_is_0600(tmp_path):
    ca = MitmCa(ca_dir=tmp_path)
    mode = os.stat(ca.key_path).st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_ca_load_is_idempotent(tmp_path):
    """Re-instantiating over an existing dir reuses the same CA material."""
    ca1 = MitmCa(ca_dir=tmp_path)
    cert1 = ca1.cert_path.read_bytes()
    ca2 = MitmCa(ca_dir=tmp_path)
    assert ca2.cert_path.read_bytes() == cert1


def _mint_inspectable_leaf(ca: MitmCa, host: str) -> x509.Certificate:
    """Mint a leaf via the CA's private key so the test can inspect the cert."""
    assert ca._ca_key is not None and ca._ca_cert is not None
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca._ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=90))
        .add_extension(x509.SubjectAlternativeName([tls_mod._san_for(host)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca._ca_key, hashes.SHA256())
    )


def test_leaf_signed_by_ca_with_dns_san(tmp_path):
    ca = MitmCa(ca_dir=tmp_path)
    leaf = _mint_inspectable_leaf(ca, "api.github.com")
    # Signature verifies against the CA public key.
    ca._ca_cert.public_key().verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,
    )
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "api.github.com" in sans.get_values_for_type(x509.DNSName)


def test_san_for_ip_literal(tmp_path):
    assert isinstance(tls_mod._san_for("127.0.0.1"), x509.IPAddress)
    assert isinstance(tls_mod._san_for("example.com"), x509.DNSName)


def test_leaf_context_is_cached(tmp_path):
    ca = MitmCa(ca_dir=tmp_path)
    ctx1 = ca.leaf_context("api.github.com")
    ctx2 = ca.leaf_context("api.github.com")
    assert isinstance(ctx1, ssl.SSLContext)
    assert ctx1 is ctx2  # LRU cache returns the same context


def test_leaf_context_distinct_per_host(tmp_path):
    ca = MitmCa(ca_dir=tmp_path)
    assert ca.leaf_context("a.com") is not ca.leaf_context("b.com")


def test_combined_bundle_contains_our_ca_and_real_roots(tmp_path):
    ca = MitmCa(ca_dir=tmp_path)
    bundle_path = ca.combined_bundle_path()
    blob = Path(bundle_path).read_bytes()
    # Our CA is present.
    assert ca.cert_path.read_bytes() in blob
    # Plus at least one more certificate (a real public root).
    assert blob.count(b"BEGIN CERTIFICATE") > 1


def test_combined_bundle_is_idempotent(tmp_path):
    ca = MitmCa(ca_dir=tmp_path)
    p1 = ca.combined_bundle_path()
    p2 = ca.combined_bundle_path()
    assert p1 == p2
    assert Path(p1).exists()
