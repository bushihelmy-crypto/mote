"""Stable logical Agent identity used across governance boundaries."""


class AgentId(str):
    def __new__(cls, value: str) -> "AgentId":
        if type(value) is not str or not value:
            raise ValueError("AgentId must be a non-empty string")
        return str.__new__(cls, value)


class _PositiveGeneration(int):
    def __new__(cls, value: int):
        if type(value) is not int or value < 1:
            raise ValueError(f"{cls.__name__} must be a positive integer")
        return int.__new__(cls, value)


class IncarnationGeneration(_PositiveGeneration):
    pass


class LineageRevision(_PositiveGeneration):
    pass


class CancellationEpoch(int):
    def __new__(cls, value: int):
        if type(value) is not int or value < 0:
            raise ValueError("CancellationEpoch must be a non-negative integer")
        return int.__new__(cls, value)


__all__ = ["AgentId", "CancellationEpoch", "IncarnationGeneration", "LineageRevision"]
