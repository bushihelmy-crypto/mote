"""Runtime tool execution and immutable catalog adapters.

Product bootstrap imports decorated capability declarations, then freezes them
into an Application-owned ``ToolCatalog`` before any Agent is constructed.
"""
