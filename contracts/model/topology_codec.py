"""Versioned canonical codec for public model topology."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from mote.contracts.model.execution_policy import EndpointExecutionPolicy
from mote.contracts.model.topology import DefaultRoute, ModelTopology, RouteId, SemanticRoute, TaskRoute

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"


class NonAsciiHostnameError(ValueError):
    """A v1 topology URL contained a Unicode hostname."""


def encode_route_id(route_id: RouteId) -> str:
    if isinstance(route_id, DefaultRoute):
        return "default"
    if isinstance(route_id, TaskRoute):
        return f"task:{route_id.name}"
    if isinstance(route_id, SemanticRoute):
        return f"semantic:{route_id.name}"
    raise TypeError(f"unsupported route id: {type(route_id).__name__}")


def decode_route_id(value: str) -> RouteId:
    if value == "default":
        return DefaultRoute()
    kind, separator, name = value.partition(":")
    if not separator or not name:
        raise ValueError(f"invalid route id wire value: {value!r}")
    if kind == "task":
        return TaskRoute(name=name)
    if kind == "semantic":
        return SemanticRoute(name=name)
    raise ValueError(f"invalid route id namespace: {kind!r}")


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if any(0xD800 <= ord(char) <= 0xDFFF for char in normalized):
        raise ValueError("canonical text contains a surrogate code point")
    return normalized


def canonical_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or parsed.hostname is None:
        raise ValueError("model endpoint URL must be absolute")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("model endpoint URL cannot contain userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("model endpoint URL cannot contain query or fragment")
    host = parsed.hostname
    if not host.isascii():
        raise NonAsciiHostnameError("v1 topology requires an ASCII hostname")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        host = host.lower()
        labels = host.rstrip(".").split(".")
        if not labels or any(not _DNS_LABEL.fullmatch(label) for label in labels):
            raise ValueError("model endpoint URL contains an invalid ASCII hostname")
        host = ".".join(labels)
    else:
        host = address.compressed.lower()
    if ":" in host:
        host = f"[{host}]"
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    decoded_path = unquote(parsed.path or "/")
    segments: list[str] = []
    for segment in decoded_path.split("/"):
        if segment == "..":
            if segments:
                segments.pop()
        elif segment not in ("", "."):
            segments.append(segment)
    path = "/" + "/".join(quote(segment, safe=_UNRESERVED) for segment in segments)
    if decoded_path.endswith("/") and path != "/":
        path += "/"
    return urlunsplit((scheme, host, path, "", ""))


def _normalize(value: Any) -> Any:
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, float):
        raise TypeError("canonical topology v1 forbids float")
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _normalize_text(key)
            if normalized_key in normalized:
                raise ValueError("canonical object keys collide after NFC normalization")
            normalized[normalized_key] = _normalize(item)
        return {key: normalized[key] for key in sorted(normalized, key=lambda item: item.encode("utf-8"))}
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_topology_bytes(topology: ModelTopology) -> bytes:
    payload = topology.model_dump(mode="json", by_alias=True)
    default_policy = EndpointExecutionPolicy().model_dump(mode="json")
    for endpoint in payload["endpoints"]:
        endpoint["base_url"] = canonical_base_url(endpoint["base_url"])
        if endpoint.get("execution_policy") == default_policy:
            del endpoint["execution_policy"]
    normalized = _normalize(payload)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=False).encode("utf-8")


def topology_revision(topology: ModelTopology) -> str:
    return hashlib.sha256(b"mote-model-topology-v1\0" + canonical_topology_bytes(topology)).hexdigest()


__all__ = [
    "NonAsciiHostnameError",
    "canonical_base_url",
    "canonical_topology_bytes",
    "decode_route_id",
    "encode_route_id",
    "topology_revision",
]
