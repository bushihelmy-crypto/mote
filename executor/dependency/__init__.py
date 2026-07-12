"""Shared dependencies for built-in tools.

Non-tool support modules used by the @register_tool BaseTool subclasses under
``executor/tools/``: shared base classes (FileMutatingTool), engines (terminal
PTY, Jupyter kernel) and document text extraction. These are intentionally NOT
registered tools, so they live outside the registry's package scan of ``tools/``.
"""
