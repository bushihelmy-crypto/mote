"""Application-scoped durable scan owner for accepted hosted-service calls."""

import asyncio
from dataclasses import dataclass

from mote.contracts.ports.service.call_journal import ServiceCallJournal
from mote.contracts.service import PendingServiceCall


@dataclass(frozen=True, slots=True)
class ServiceReconcileCycle:
    discovered: int
    settled: int
    failed: int


class HostedServiceReconciler:
    def __init__(
        self,
        gateway,
        journal: ServiceCallJournal,
        *,
        scan_interval_seconds: float = 5.0,
        page_size: int = 64,
        concurrency: int = 4,
    ) -> None:
        if scan_interval_seconds <= 0 or not 1 <= page_size <= 256 or concurrency < 1:
            raise ValueError("hosted-service reconciler limits are invalid")
        self._gateway = gateway
        self._journal = journal
        self._scan_interval = scan_interval_seconds
        self._page_size = page_size
        self._concurrency = concurrency
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("hosted-service reconciler is closed")
        if self._task is not None:
            raise RuntimeError("hosted-service reconciler already started")
        self._task = asyncio.create_task(self._run(), name="mote-hosted-service-reconciler")

    def wake(self) -> None:
        if not self._closed:
            self._wake.set()

    async def reconcile_once(self) -> ServiceReconcileCycle:
        cursor: str | None = None
        discovered = settled = failed = 0
        while True:
            page = await self._journal.pending_calls(after=cursor, limit=self._page_size)
            if not page:
                break
            discovered += len(page)
            outcomes = await self._settle_page(page)
            settled += outcomes.count(True)
            failed += outcomes.count(False)
            cursor = page[-1].cursor
            if len(page) < self._page_size:
                break
        return ServiceReconcileCycle(discovered, settled, failed)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wake.set()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None

    async def _settle_page(self, page: tuple[PendingServiceCall, ...]) -> list[bool]:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def settle(item: PendingServiceCall) -> bool:
            async with semaphore:
                try:
                    await self._gateway.reconcile(item.invocation)
                except (Exception, asyncio.CancelledError):
                    if self._closed:
                        raise
                    return False
                return True

        return list(await asyncio.gather(*(settle(item) for item in page)))

    async def _run(self) -> None:
        while not self._closed:
            await self.reconcile_once()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._scan_interval)
            except TimeoutError:
                pass


__all__ = ["HostedServiceReconciler", "ServiceReconcileCycle"]
