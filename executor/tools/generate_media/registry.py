"""MediaProvider registry — pluggable media-generation backends per kind.

The generation backend for each media *kind* (image / audio / music / video) is
a swappable strategy, selected by config, in the same shape as the LLM provider
registry (:mod:`mote.router.llm.llm_provider_registry`): a
``@register_media_provider(kind, name)`` decorator populates a singleton keyed by
``(kind, name)``, and :func:`create_media_provider` resolves the active provider
for a kind from ``config.multimodal.<kind>_generation.provider``.

Today every kind ships exactly one built-in provider named ``"openai"`` (the
OpenAI-compatible async-task endpoints in :mod:`creators`). Adding a second
vendor for a kind (e.g. a Flux image backend, an ElevenLabs audio backend) is a
new file + a ``@register_media_provider(kind, "flux")`` decorator + pointing the
kind's config ``provider`` at it — zero changes here or at the call site. This
is the "leave the vendor entry-point, config-select it" seam, aligned with how
LLM providers are added.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional

from mote.common.base.singleton import Singleton
from mote.common.config.loader import load_config
from mote.common.exception import ToolNotConfiguredError


class MediaProvider(ABC):
    """A media-generation backend for ONE kind (image / audio / music / video).

    A provider is constructed with its kind's resolved sub-config (e.g.
    ``config.multimodal.image_generation``) plus an optional ``output_dir`` to
    download finished assets into, and exposes a single uniform :meth:`generate`
    that submits every requested asset and blocks until each resolves to its
    final URL. ``kind`` / ``provider`` are stamped by
    :func:`register_media_provider` (single source = the decorator args).
    """

    kind: ClassVar[str] = ""
    provider: ClassVar[str] = ""

    def __init__(self, config: Any, output_dir: Optional[str] = None) -> None:
        self._config = config
        self._output_dir_arg = output_dir

    @abstractmethod
    async def generate(self, items: list[dict], /) -> dict:
        """Generate every asset in *items* and return the poll-summary dict.

        ``items`` is positional-only so each kind's provider may name it after its
        payload (``images`` / ``audios`` / ``tracks`` / ``videos``).
        """
        raise NotImplementedError


class MediaProviderRegistry(metaclass=Singleton):
    """Singleton map ``(kind, name) -> MediaProvider subclass``."""

    def __init__(self) -> None:
        self.providers: dict[tuple[str, str], type[MediaProvider]] = {}

    def register(self, kind: str, name: str, provider_cls: type[MediaProvider]) -> None:
        self.providers[(kind, name)] = provider_cls

    def get_provider(self, kind: str, name: str) -> type[MediaProvider]:
        try:
            return self.providers[(kind, name)]
        except KeyError as e:
            available = sorted(n for (k, n) in self.providers if k == kind)
            raise ToolNotConfiguredError(
                f"No media provider {name!r} registered for kind {kind!r}. "
                f"Set multimodal.{kind}_generation.provider to one of: {available}."
            ) from e


# Registry instance.
MEDIA_REGISTRY = MediaProviderRegistry()


def register_media_provider(kind: str, name: str):
    """Register a :class:`MediaProvider` subclass under ``(kind, name)``.

    Also stamps ``cls.kind`` / ``cls.provider`` so the decorator args are the
    single source of truth (the class need not restate them).
    """

    def decorator(cls: type[MediaProvider]) -> type[MediaProvider]:
        cls.kind = kind
        cls.provider = name
        MEDIA_REGISTRY.register(kind, name, cls)
        return cls

    return decorator


def create_media_provider(kind: str, output_dir: Optional[str] = None) -> MediaProvider:
    """Construct the active provider for *kind* from config.

    Reads ``config.multimodal.<kind>_generation``, selects the provider named by
    its ``provider`` field (default ``"openai"``), and constructs it with that
    sub-config + ``output_dir``. Raises :class:`ToolNotConfiguredError` naming
    the config path when the selected provider is not registered.
    """
    cfg = getattr(load_config().multimodal, f"{kind}_generation")
    name = getattr(cfg, "provider", "openai") or "openai"
    cls = MEDIA_REGISTRY.get_provider(kind, name)
    return cls(cfg, output_dir)


__all__ = [
    "MediaProvider",
    "MediaProviderRegistry",
    "MEDIA_REGISTRY",
    "register_media_provider",
    "create_media_provider",
]
