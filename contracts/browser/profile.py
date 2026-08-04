"""Canonical browser profile identity and strict storage-state DTOs."""

from __future__ import annotations

import math
from dataclasses import dataclass

from mote.contracts.events.envelope import JsonValue


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str
    expires: float
    http_only: bool
    secure: bool
    same_site: str

    def __post_init__(self) -> None:
        for name in ("name", "domain", "path"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"browser cookie {name} must be a non-empty string")
        if type(self.value) is not str:
            raise ValueError("browser cookie value must be a string")
        if type(self.expires) not in (int, float) or not math.isfinite(self.expires):
            raise ValueError("browser cookie expires must be finite")
        if type(self.http_only) is not bool or type(self.secure) is not bool:
            raise ValueError("browser cookie flags must be booleans")
        if self.same_site not in {"Strict", "Lax", "None"}:
            raise ValueError("browser cookie same_site is invalid")

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "expires": self.expires,
            "httpOnly": self.http_only,
            "secure": self.secure,
            "sameSite": self.same_site,
        }


@dataclass(frozen=True)
class BrowserLocalStorageEntry:
    name: str
    value: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("browser localStorage name must be a non-empty string")
        if type(self.value) is not str:
            raise ValueError("browser localStorage value must be a string")


@dataclass(frozen=True)
class BrowserOriginState:
    origin: str
    local_storage: tuple[BrowserLocalStorageEntry, ...]

    def __post_init__(self) -> None:
        if type(self.origin) is not str or not self.origin:
            raise ValueError("browser origin must be a non-empty string")
        if type(self.local_storage) is not tuple or any(
            not isinstance(entry, BrowserLocalStorageEntry) for entry in self.local_storage
        ):
            raise ValueError("browser origin local_storage must be a canonical tuple")


@dataclass(frozen=True)
class BrowserStorageState:
    cookies: tuple[BrowserCookie, ...]
    origins: tuple[BrowserOriginState, ...]

    def __post_init__(self) -> None:
        if type(self.cookies) is not tuple or any(not isinstance(cookie, BrowserCookie) for cookie in self.cookies):
            raise ValueError("browser storage cookies must be a canonical tuple")
        if type(self.origins) is not tuple or any(
            not isinstance(origin, BrowserOriginState) for origin in self.origins
        ):
            raise ValueError("browser storage origins must be a canonical tuple")

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "cookies": [cookie.to_payload() for cookie in self.cookies],
            "origins": [
                {
                    "origin": origin.origin,
                    "localStorage": [{"name": entry.name, "value": entry.value} for entry in origin.local_storage],
                }
                for origin in self.origins
            ],
        }


def decode_browser_storage_state(value: object) -> BrowserStorageState:
    if type(value) is not dict or set(value) != {"cookies", "origins"}:
        raise ValueError("storage state must contain exactly cookies and origins")
    cookies_raw, origins_raw = value["cookies"], value["origins"]
    if type(cookies_raw) is not list or type(origins_raw) is not list:
        raise ValueError("cookies and origins must be arrays")
    cookies: list[BrowserCookie] = []
    cookie_fields = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    for raw in cookies_raw:
        if type(raw) is not dict or set(raw) != cookie_fields:
            raise ValueError("cookie has an invalid shape")
        cookies.append(
            BrowserCookie(
                name=raw["name"],
                value=raw["value"],
                domain=raw["domain"],
                path=raw["path"],
                expires=raw["expires"],
                http_only=raw["httpOnly"],
                secure=raw["secure"],
                same_site=raw["sameSite"],
            )
        )
    origins: list[BrowserOriginState] = []
    for raw in origins_raw:
        if type(raw) is not dict or set(raw) != {"origin", "localStorage"}:
            raise ValueError("origin has an invalid shape")
        entries_raw = raw["localStorage"]
        if type(entries_raw) is not list:
            raise ValueError("origin localStorage must be an array")
        entries: list[BrowserLocalStorageEntry] = []
        for entry in entries_raw:
            if type(entry) is not dict or set(entry) != {"name", "value"}:
                raise ValueError("localStorage entry has an invalid shape")
            entries.append(BrowserLocalStorageEntry(entry["name"], entry["value"]))
        origins.append(BrowserOriginState(raw["origin"], tuple(entries)))
    return BrowserStorageState(tuple(cookies), tuple(origins))


@dataclass(frozen=True)
class BrowserProfileSnapshot:
    subject_id: str
    display_name: str
    revision: int
    content_digest: str
    storage_state: BrowserStorageState


@dataclass(frozen=True)
class BrowserProfileCommitReceipt:
    subject_id: str
    revision: int
    content_digest: str


class BrowserProfileError(RuntimeError):
    pass


class BrowserProfileNotFoundError(BrowserProfileError):
    pass


class BrowserProfileConflictError(BrowserProfileError):
    pass


__all__ = [
    "BrowserCookie",
    "BrowserLocalStorageEntry",
    "BrowserOriginState",
    "BrowserStorageState",
    "BrowserProfileSnapshot",
    "BrowserProfileCommitReceipt",
    "BrowserProfileError",
    "BrowserProfileNotFoundError",
    "BrowserProfileConflictError",
    "decode_browser_storage_state",
]
