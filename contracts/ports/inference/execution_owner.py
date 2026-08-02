"""Durable Shared execution owner-record store."""

from typing import Protocol

from mote.contracts.inference.execution_owner import ExecutionId, ExecutionOwnerRecord


class ExecutionOwnerRecordStore(Protocol):
    async def put_owner_record(self, record: ExecutionOwnerRecord) -> ExecutionOwnerRecord: ...

    async def get_owner_record(self, execution_id: ExecutionId) -> ExecutionOwnerRecord | None: ...


__all__ = ["ExecutionOwnerRecordStore"]
