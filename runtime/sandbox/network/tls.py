"""MITM certificate authority — the TLS-interception half of credential brokering.

Credential brokering (see :mod:`.credentials`) injects a per-domain auth header at
the egress proxy so a sandboxed tool reaches an authenticated endpoint without ever
holding the secret. Over **plaintext HTTP** the proxy just splices the header into
the forwarded request. Over **HTTPS** the request is inside an end-to-end TLS tunnel
the proxy only raw-splices — so to inject a header the proxy must terminate that TLS,
read the plaintext, inject, and re-originate a fresh TLS connection to the origin.
That is a man-in-the-middle, and it only works if the sandboxed client *trusts* the
certificate the proxy presents. Hence a local CA:

  * :class:`MitmCa` — a load-or-generate root CA under ``~/.mote/sandbox_ca/``
    (``ca.key`` locked ``0600`` + ``ca.pem``). :meth:`leaf_context` mints (and
    LRU-caches) a per-host leaf certificate signed by that CA and returns a
    *server-side* :class:`ssl.SSLContext` the proxy uses to speak TLS to the client.
  * :meth:`combined_bundle_path` — writes ``certifi``'s real root bundle **plus** our
    ``ca.pem`` to ``~/.mote/sandbox_ca/ca-bundle.pem``. Sandboxed tools are pointed
    at this bundle (``SSL_CERT_FILE`` et al.) so a MITM'd (credentialed) host
    validates against our CA while **every other** TLS connection still validates
    against the real public roots — interception is scoped to exactly the domains
    that have a configured credential.

Trust-boundary contract (enforced elsewhere, relied on here):

  * The CA **private key** (``ca.key``) must be *masked* from the sandbox (the
    adapter binds ``/dev/null`` over it) — a sandboxed process that could read it
    could forge certs for any host. Only the *public* bundle is visible inside.
  * ``~/.mote/`` is already visible read-only inside the sandbox via bwrap's
    ``--ro-bind / /``, so the public bundle needs no extra bind; the mask on the
    private key is what makes the "secret never in the sandbox" guarantee real.

Leaf module: imports only stdlib + ``cryptography`` (already a dependency via
``common/secrets/cipher.py``) + the optional ``certifi`` for the real-root bundle.
No new dependency, no knowledge of the proxy, configuration, or event infrastructure.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

try:
    import certifi
except Exception:  # noqa: BLE001 — optional public-root bundle
    certifi = None

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mote.runtime.logging import logger
from mote.runtime.paths import CONFIG_ROOT

#: Directory holding the CA material + the combined bundle (sibling of the vault).
_CA_DIRNAME = "sandbox_ca"
_CA_KEY_FILE = "ca.key"
_CA_CERT_FILE = "ca.pem"
_BUNDLE_FILE = "ca-bundle.pem"

#: How many per-host leaf contexts to keep minted+cached before evicting the LRU.
_LEAF_CACHE_MAX = 128
#: Root CA validity window (long — regenerated only if the file is removed).
_CA_VALID_DAYS = 3650
#: Per-host leaf validity window (short-ish; re-minted freely on cache miss).
_LEAF_VALID_DAYS = 90
#: Clock-skew backdating so a freshly minted cert is not "not yet valid".
_BACKDATE = datetime.timedelta(minutes=5)
_RSA_BITS = 2048


def _ca_dir() -> Path:
    """The ``~/.mote/sandbox_ca/`` directory (not created here)."""
    return CONFIG_ROOT / _CA_DIRNAME


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class MitmCa:
    """A local root CA that mints per-host leaf certs for HTTPS interception.

    Loads an existing CA from ``~/.mote/sandbox_ca/`` or generates one on first
    use (private key written ``0600``). Thread-safe: leaf minting and the LRU
    cache are guarded by a lock so the asyncio proxy can call from any task.
    """

    def __init__(self, ca_dir: Optional[Path] = None) -> None:
        self._dir = Path(ca_dir) if ca_dir is not None else _ca_dir()
        self._key_path = self._dir / _CA_KEY_FILE
        self._cert_path = self._dir / _CA_CERT_FILE
        self._bundle_path = self._dir / _BUNDLE_FILE
        self._lock = threading.Lock()
        self._leaf_cache: "OrderedDict[str, ssl.SSLContext]" = OrderedDict()
        self._ca_key: Optional[rsa.RSAPrivateKey] = None
        self._ca_cert: Optional[x509.Certificate] = None
        self._load_or_generate()

    # --- CA material -------------------------------------------------------

    @property
    def cert_path(self) -> Path:
        """Path to the public CA certificate (``ca.pem``)."""
        return self._cert_path

    @property
    def key_path(self) -> Path:
        """Path to the CA **private** key (``ca.key``) — must be masked in-sandbox."""
        return self._key_path

    def _load_or_generate(self) -> None:
        """Populate ``_ca_key`` / ``_ca_cert`` from disk, generating if absent."""
        try:
            key_bytes = self._key_path.read_bytes()
            cert_bytes = self._cert_path.read_bytes()
            self._ca_key = serialization.load_pem_private_key(key_bytes, password=None)  # type: ignore[assignment]
            self._ca_cert = x509.load_pem_x509_certificate(cert_bytes)
            return
        except (OSError, ValueError) as exc:
            logger.debug(f"MitmCa: no usable CA on disk ({exc}); generating a fresh one")
        self._generate_ca()

    def _generate_ca(self) -> None:
        """Create a new root CA and persist it (private key locked ``0600``)."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_BITS)
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "mote sandbox"),
                x509.NameAttribute(NameOID.COMMON_NAME, "mote sandbox local CA"),
            ]
        )
        now = _utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _BACKDATE)
            .not_valid_after(now + datetime.timedelta(days=_CA_VALID_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        self._ca_key = key
        self._ca_cert = cert
        self._persist_ca(key, cert)

    def _persist_ca(self, key: rsa.RSAPrivateKey, cert: x509.Certificate) -> None:
        """Write ``ca.key`` (0600) + ``ca.pem``; best-effort (in-memory CA still works)."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            key_pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
            # Open 0600 from the start so the CA key is never briefly world-readable.
            fd = os.open(str(self._key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, key_pem)
            finally:
                os.close(fd)
            os.chmod(self._key_path, 0o600)
            self._cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        except OSError as exc:
            logger.warning(f"MitmCa: could not persist CA material to {self._dir}: {exc}")

    # --- leaf minting ------------------------------------------------------

    def leaf_context(self, host: str) -> ssl.SSLContext:
        """Return a server-side :class:`ssl.SSLContext` presenting a leaf for *host*.

        The leaf is signed by this CA, carries *host* as its SAN, and is cached
        (LRU) so repeated CONNECTs to the same host reuse one context. Thread-safe.
        """
        with self._lock:
            ctx = self._leaf_cache.get(host)
            if ctx is not None:
                self._leaf_cache.move_to_end(host)
                return ctx
            ctx = self._mint_leaf_context(host)
            self._leaf_cache[host] = ctx
            self._leaf_cache.move_to_end(host)
            while len(self._leaf_cache) > _LEAF_CACHE_MAX:
                self._leaf_cache.popitem(last=False)
            return ctx

    def _mint_leaf_context(self, host: str) -> ssl.SSLContext:
        """Mint a fresh per-host leaf cert+key and load it into a server SSLContext."""
        assert self._ca_key is not None and self._ca_cert is not None
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_BITS)
        san = _san_for(host)
        now = _utcnow()
        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self._ca_cert.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _BACKDATE)
            .not_valid_after(now + datetime.timedelta(days=_LEAF_VALID_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(self._ca_key, hashes.SHA256())
        )
        # Load the leaf into a server-side context. certfile/keyfile take paths, so
        # we route the PEM bytes through a temp pair via load_cert_chain's file API.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
        key_pem = leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        _load_cert_chain_from_bytes(ctx, cert_pem, key_pem)
        return ctx

    # --- trust bundle ------------------------------------------------------

    def combined_bundle_path(self) -> str:
        """Path to a PEM bundle of the real roots **plus** this CA (written on call).

        Concatenates ``certifi``'s bundle (or a system fallback) with our ``ca.pem``
        so a sandboxed tool pointed at this file trusts the MITM leaf for
        credentialed hosts while still validating every other origin against the
        real public roots. Idempotent; best-effort (returns the path regardless).
        """
        assert self._ca_cert is not None
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            roots = _real_root_bundle_bytes()
            ca_pem = self._ca_cert.public_bytes(serialization.Encoding.PEM)
            blob = roots + (b"\n" if roots and not roots.endswith(b"\n") else b"") + ca_pem
            self._bundle_path.write_bytes(blob)
        except OSError as exc:
            logger.warning(f"MitmCa: could not write combined CA bundle: {exc}")
        return str(self._bundle_path)


def _san_for(host: str) -> x509.GeneralName:
    """A SAN entry for *host* — an ``IPAddress`` for IP literals, else a ``DNSName``."""
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def _load_cert_chain_from_bytes(ctx: ssl.SSLContext, cert_pem: bytes, key_pem: bytes) -> None:
    """Load an in-memory cert+key into *ctx* via a transient 0600 temp file pair.

    ``SSLContext.load_cert_chain`` only accepts filesystem paths, so the leaf PEM
    is written to a short-lived temp file (0600, unlinked immediately after load).
    """
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        os.write(fd, cert_pem + b"\n" + key_pem)
        os.close(fd)
        ctx.load_cert_chain(certfile=path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _real_root_bundle_bytes() -> bytes:
    """Return the real public-root CA bundle bytes (``certifi``, else system path)."""
    try:
        if certifi is None:
            raise RuntimeError("certifi is not installed")
        return Path(certifi.where()).read_bytes()
    except Exception as exc:  # noqa: BLE001 — certifi missing / unreadable → system fallback
        logger.debug(f"MitmCa: certifi bundle unavailable ({exc}); trying system roots")
    for candidate in (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/cert.pem",
    ):
        try:
            return Path(candidate).read_bytes()
        except OSError:
            continue
    logger.warning("MitmCa: no real root bundle found; MITM'd tools may fail non-MITM TLS")
    return b""


__all__ = ["MitmCa"]
