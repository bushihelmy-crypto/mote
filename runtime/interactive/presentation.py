"""Capability-based dispatch for host live-surface presenters."""
from __future__ import annotations

from mote.contracts.ports.surface_presenter import LiveSurfacePresenter, SurfacePresentationSession
from mote.contracts.surfaces import LiveSurfaceSession, SurfacePresentationMode


class SurfacePresenterUnavailableError(RuntimeError):
    """No presenter is registered for the requested surface placement."""


class SurfacePresenterRegistry:
    """Resolve and retain presentation attachments by stable surface identity."""

    def __init__(self, presenters: tuple[LiveSurfacePresenter, ...] = ()) -> None:
        self._presenters: dict[tuple[SurfacePresentationMode, str], LiveSurfacePresenter] = {}
        self._sessions: dict[
            tuple[SurfacePresentationMode, str, str],
            SurfacePresentationSession,
        ] = {}
        for presenter in presenters:
            self.register(presenter)

    def register(self, presenter: LiveSurfacePresenter) -> None:
        for surface_kind in presenter.surface_kinds:
            key = (presenter.presentation, surface_kind)
            if key in self._presenters:
                raise ValueError(f"surface presenter already registered: {presenter.presentation.value}:{surface_kind}")
            self._presenters[key] = presenter

    async def present(self, surface: LiveSurfaceSession) -> SurfacePresentationSession:
        descriptor = surface.descriptor
        presenter = self._presenters.get((descriptor.presentation, descriptor.kind))
        if presenter is None:
            raise SurfacePresenterUnavailableError(
                f"no {descriptor.presentation.value} presenter for surface kind {descriptor.kind!r}"
            )
        key = (descriptor.presentation, descriptor.kind, descriptor.ref)
        existing = self._sessions.get(key)
        if existing is not None and not existing.closed:
            try:
                await existing.attach(surface)
            except Exception:
                if not existing.closed:
                    raise
            else:
                return existing
        if existing is not None:
            await existing.aclose()
        session = await presenter.present(surface)
        self._sessions[key] = session
        return session

    async def aclose(self) -> None:
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        for session in reversed(sessions):
            await session.aclose()


__all__ = ["SurfacePresenterRegistry", "SurfacePresenterUnavailableError"]
