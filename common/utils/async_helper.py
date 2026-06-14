import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


def run_coroutine_sync(coroutine: Coroutine[Any, Any, T]) -> T:
    """Execute an asynchronous coroutine synchronously.

    Chooses the appropriate method to run the coroutine based on the current execution context
    (event loop availability, thread, and loop running state) to avoid deadlocks and runtime errors.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    if threading.current_thread() is not threading.main_thread():
        # run_coroutine_threadsafe cannot be executed in the main thread, otherwise it will cause a deadlock, because:
        # a. run_coroutine_threadsafe submits the coroutine to the event loop.
        # b. The .result() method is called, which blocks the current thread, waiting for the coroutine to complete.
        # c. However, since the current thread (main thread) is blocked, the event loop cannot continue running.
        # d. As a result, the submitted coroutine will never be executed because the event loop that should execute it is blocked.
        # e. This creates a deadlock: the main thread is waiting for the coroutine to complete, while the coroutine is waiting for the event loop in the main thread to continue running.
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    if not loop.is_running():
        # run_until_complete cannot contain another run_until_complete,
        # Otherwise it will raise an error: "RuntimeError: This event loop is already running".
        return loop.run_until_complete(coroutine)

    # Each thread can only have one loop.
    # If the loop is already running, use another thread to start a new loop to execute the coroutine.
    with ThreadPoolExecutor() as pool:
        return pool.submit(lambda: asyncio.run(coroutine)).result()
