"""Stable Session identity contract."""


class SessionId(str):
    def __new__(cls, value: str) -> "SessionId":
        if type(value) is not str or not value:
            raise ValueError("SessionId must be a non-empty string")
        return str.__new__(cls, value)


__all__ = ["SessionId"]
