"""Reusable Playwright transport for attaching to visible CDP applications."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Pattern

try:
    from playwright.async_api import async_playwright
except ImportError as _playwright_import_error:
    async_playwright = None
else:
    _playwright_import_error = None


class CdpConnectionError(RuntimeError):
    """A CDP application could not be attached or did not expose a target."""


async def focus_chromium_page(page: Any, *, maximize: bool = True) -> None:
    """Raise a native Chromium window and optionally maximize it."""
    await page.bring_to_front()
    if not maximize:
        return
    session = await page.context.new_cdp_session(page)
    try:
        target = await session.send("Target.getTargetInfo")
        target_id = target["targetInfo"]["targetId"]
        window = await session.send("Browser.getWindowForTarget", {"targetId": target_id})
        await session.send(
            "Browser.setWindowBounds",
            {"windowId": window["windowId"], "bounds": {"windowState": "maximized"}},
        )
    except Exception:  # noqa: BLE001 - maximizing is a presentation enhancement
        return
    finally:
        await session.detach()


class CdpBrowserConnection:
    """Attach to an existing Chromium/Electron instance without owning it."""

    def __init__(self) -> None:
        self._manager: Any = None
        self._playwright: Any = None
        self._browser: Any = None

    async def connect(self, endpoint: str, *, timeout_seconds: float = 25.0) -> None:
        if async_playwright is None:
            raise CdpConnectionError("Playwright is required for CDP window presentation") from _playwright_import_error
        if self._browser is not None:
            raise RuntimeError("CDP connection is already open")
        self._manager = async_playwright()
        self._playwright = await self._manager.start()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(endpoint)
                return
            except Exception as exc:  # noqa: BLE001 - external readiness boundary
                last_error = exc
                await asyncio.sleep(0.25)
        await self.aclose()
        raise CdpConnectionError(f"CDP endpoint did not become ready: {endpoint}: {last_error}")

    @property
    def pages(self) -> tuple[Any, ...]:
        if self._browser is None:
            return ()
        return tuple(page for context in self._browser.contexts for page in context.pages)

    async def find_page(
        self,
        pattern: str | Pattern[str],
        *,
        timeout_seconds: float = 25.0,
    ) -> Any:
        matcher = re.compile(pattern, re.IGNORECASE) if isinstance(pattern, str) else pattern
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            for page in self.pages:
                title = ""
                try:
                    title = await page.title()
                except Exception:  # noqa: BLE001 - a target may disappear while enumerating
                    continue
                if matcher.search(f"{title} {page.url}"):
                    return page
            await asyncio.sleep(0.25)
        raise CdpConnectionError(f"CDP target did not appear for pattern {matcher.pattern!r}")

    async def focus(self, page: Any, *, maximize: bool = True) -> None:
        await focus_chromium_page(page, maximize=maximize)

    async def aclose(self) -> None:
        manager = self._manager
        self._browser = None
        self._playwright = None
        self._manager = None
        if manager is not None:
            await manager.__aexit__(None, None, None)


__all__ = ["CdpBrowserConnection", "CdpConnectionError", "focus_chromium_page"]
