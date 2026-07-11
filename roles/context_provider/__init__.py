"""ContextProvider package — assembles everything one think() cycle needs.

Split from the former ``mote/roles/context_provider.py`` module into:
  - ``request.py``  — ``ThinkRequest`` (the pure-data product)
  - ``base.py``     — ``BaseContextProvider`` (the narrow ABC the loop depends on)
  - ``provider.py`` — ``ContextProvider`` (the concrete Role-reading assembler)

Re-exported here so existing imports ``from mote.roles.context_provider
import ...`` keep working.
"""

from mote.roles.context_provider.base import BaseContextProvider
from mote.roles.context_provider.provider import ContextProvider
from mote.roles.context_provider.request import ThinkRequest

__all__ = ["BaseContextProvider", "ContextProvider", "ThinkRequest"]
