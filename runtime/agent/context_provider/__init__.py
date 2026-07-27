"""ContextProvider package — assembles everything one think() cycle needs.

Split from the former ``mote/roles/context_provider.py`` module into:
  - ``request.py``  — ``ThinkRequest`` (the pure-data product)
  - ``base.py``     — ``BaseContextProvider`` (the narrow ABC the flow depends on)
  - ``provider.py`` — ``ContextProvider`` (the concrete Role-reading assembler)

Re-exported here so existing imports ``from mote.runtime.agent.context_provider
import ...`` keep working.
"""

from mote.runtime.agent.context_provider.base import BaseContextProvider
from mote.runtime.agent.context_provider.provider import ContextProvider
from mote.runtime.agent.context_provider.request import ThinkRequest

__all__ = ["BaseContextProvider", "ContextProvider", "ThinkRequest"]
