import asyncio

from mote.contracts.inference.identity import TrustedSchedulingClass
from mote.runtime.inference.bulkhead import BulkheadController, BulkheadIdentity
from mote.runtime.inference.dispatcher import Dispatcher
from mote.runtime.inference.fair_queue import FairAdmissionQueue


def test_bulkhead_is_atomic_and_isolates_endpoints():
    async def scenario():
        controller = BulkheadController(global_limit=2, provider_limit=2, endpoint_limit=1)
        loop = asyncio.get_running_loop()
        first = BulkheadIdentity("provider", "one", "http")
        second = BulkheadIdentity("provider", "two", "http")
        permit_one = await controller.acquire(first, deadline=loop.time() + 1)
        permit_two = await controller.acquire(second, deadline=loop.time() + 1)
        assert controller.global_in_flight == 2
        assert controller.in_flight(first) == 1
        await permit_one.release()
        await permit_two.release()
        assert controller.global_in_flight == 0

    asyncio.run(scenario())


def test_dispatcher_uses_one_bulkhead_per_entry_and_drains():
    async def scenario():
        queue = FairAdmissionQueue(capacity=4)
        bulkheads = BulkheadController(global_limit=2, provider_limit=2, endpoint_limit=1)
        seen = []

        async def handler(entry, permit):
            assert permit.identity.endpoint == entry.payload["endpoint"]
            seen.append(entry.payload["value"])

        dispatcher = Dispatcher(
            queue=queue,
            bulkheads=bulkheads,
            identity_resolver=lambda entry: BulkheadIdentity("p", entry.payload["endpoint"], "http"),
            handler=handler,
            timeout_handler=lambda entry: asyncio.sleep(0),
            worker_count=2,
        )
        dispatcher.start()
        loop = asyncio.get_running_loop()
        scheduling = TrustedSchedulingClass()
        for index in range(4):
            await queue.enqueue(
                {"value": index, "endpoint": str(index % 2)},
                tenant_id="tenant",
                project_id="project",
                scheduling=scheduling,
                deadline=loop.time() + 2,
            )
        await dispatcher.drain(timeout_seconds=2)
        await dispatcher.aclose()
        assert sorted(seen) == [0, 1, 2, 3]
        assert bulkheads.global_in_flight == 0

    asyncio.run(scenario())


def test_dispatcher_worker_survives_handler_failure_and_reports_it_on_drain():
    async def scenario():
        queue = FairAdmissionQueue(capacity=2)
        bulkheads = BulkheadController(global_limit=1, provider_limit=1, endpoint_limit=1)
        seen = []

        async def handler(entry, permit):
            seen.append(entry.payload)
            if entry.payload == "bad":
                raise RuntimeError("broken dispatch")

        dispatcher = Dispatcher(
            queue=queue,
            bulkheads=bulkheads,
            identity_resolver=lambda entry: BulkheadIdentity("p", "e", "http"),
            handler=handler,
            timeout_handler=lambda entry: asyncio.sleep(0),
            worker_count=1,
        )
        dispatcher.start()
        loop = asyncio.get_running_loop()
        for value in ("bad", "good"):
            await queue.enqueue(
                value,
                tenant_id="tenant",
                project_id="project",
                scheduling=TrustedSchedulingClass(),
                deadline=loop.time() + 2,
            )
        try:
            await dispatcher.drain(timeout_seconds=2)
        except ExceptionGroup as exc:
            assert "broken dispatch" in str(exc.exceptions[0])
        else:
            raise AssertionError("dispatcher failure was not reported")
        await dispatcher.aclose()
        assert seen == ["bad", "good"]
        assert bulkheads.global_in_flight == 0

    asyncio.run(scenario())
