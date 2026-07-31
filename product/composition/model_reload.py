"""Product-owned latest-request-wins application reload coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from mote.contracts.runtime.application import ExpectedActive
from mote.product.composition.model_application import AtomicApplicationComposition, CandidateState
from mote.product.composition.model_builder import build_application_candidate
from mote.product.composition.model_startup import source_revision
from mote.product.config.schema import Config
from mote.product.models.registry import LLMProviderRegistry
from mote.runtime.models.cost import CostTracker
from mote.runtime.resilience.admission import ResourceAdmissionController
from mote.runtime.telemetry.logging import log_class


@log_class(level="DEBUG", exclude={"reload"})
class ApplicationReloadCoordinator:
    """Own the single load/compile/readiness/CAS reload entrypoint."""

    def __init__(
        self,
        *,
        composition: AtomicApplicationComposition,
        load_config: Callable[[], Config],
        providers: LLMProviderRegistry,
        oauth_root: Path,
        cost_tracker: CostTracker | None = None,
        admission_controller: ResourceAdmissionController | None = None,
        model_call_journal=None,
    ) -> None:
        self._composition = composition
        self._load_config = load_config
        self._providers = providers
        self._oauth_root = oauth_root
        self._cost_tracker = cost_tracker
        self._admission_controller = admission_controller
        self._model_call_journal = model_call_journal
        self._load_lock = asyncio.Lock()

    async def reload(self):
        """Build outside the current pointer and atomically install if still latest."""

        # Config loaders are commonly stateful and synchronous. Serialize only that
        # boundary; compilation/readiness remains free to overlap across requests.
        async with self._load_lock:
            config = self._load_config()
        revision = source_revision(config)
        sequence = self._composition.accept_reload_request(revision)
        expected_id = self._composition.current_generation_id
        if expected_id is None:
            raise RuntimeError("reload requires an installed application generation")
        current = await self._composition.retain_current_model()
        candidate = None
        try:
            candidate = await build_application_candidate(
                config,
                reload_sequence=sequence,
                source_revision=revision,
                providers=self._providers,
                oauth_root=self._oauth_root,
                current=current,
                cost_tracker=self._cost_tracker,
                admission_controller=self._admission_controller,
                model_call_journal=self._model_call_journal,
            )
        finally:
            # build_application_candidate retains a second reference on reuse.
            await current.release()
        async with self._load_lock:
            current_revision = source_revision(self._load_config())
            if current_revision != revision:
                self._composition.accept_reload_request(current_revision)
        token = self._composition.issue_activation_token()
        try:
            return await self._composition.activate(
                candidate,
                token,
                ExpectedActive(expected_id),
            )
        finally:
            if candidate.state is CandidateState.NEW:
                await candidate.aclose()


__all__ = ["ApplicationReloadCoordinator"]
