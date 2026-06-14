#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BaseTool — base class for all tools.

Tools inherit BaseTool and implement call(**kwargs).
All kwargs are LLM-specified parameters. The only framework context injected
is session_id (via bind(session_id) before call()).

Registration: Use the @register_tool decorator from tool_registry.
Instance management: ToolExecutor creates and caches instances per-Role.
"""
from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from metagpt.common.schema import DEFAULT_MAX_RESULT_SIZE_CHARS
from metagpt.executor.permission.types import PermissionDecision
from metagpt.executor.tool_convert import function_docstring_to_schema


class BaseTool(ABC):
    """Base class for tools. Single call() entry point.

    Subclass contract:
    - Set `name` (primary), optionally `aliases` (alternative names).
    - Decorate with @register_tool to register.
    - Implement call(**kwargs) with type hints and docstring.
    - All call() parameters are LLM-specified. Framework context is self.session_id.
    - If the tool needs Role behavior, list the method names in `requires`;
      bind() injects exactly those (resolved via Role.tool_capabilities()) and
      nothing else.
    - Schema is auto-generated from call() signature — no manual schema needed.
      Scalar params (str/int/float/bool) and pydantic models work out of the box;
      annotate a structured param with a pydantic ``BaseModel`` (or ``list[Model]``)
      to get a correct nested schema automatically. Override custom_schema() /
      get_native_schema() only for dynamic params (e.g. MCP).

    Channel limitation (IMPORTANT):
    - The legacy XML command protocol parses EVERY argument as a string — it does
      not carry parameter types, so list/dict/model params arrive as raw strings
      there. Tools with structured (non-scalar) params therefore work correctly
      ONLY on the native tool-use channel. Keep tool params scalar if the tool
      must run under the XML protocol; otherwise restrict the tool to native.

    Lifecycle:
    - Instances are managed by ToolExecutor (per-Role isolation).
    - bind(session_id) called by ToolExecutor at creation time.
    - cleanup_session() called when a Role exits.
    """

    # --- Identity ---
    name: ClassVar[str] = ""              # Primary tool name
    aliases: ClassVar[list[str]] = []     # Alternative names (LLM can use any)
    description: ClassVar[str] = ""       # Override; if empty, extracted from call() docstring
    # Names of Role capabilities (methods) this tool needs. bind() injects ONLY
    # these, resolved against Role.tool_capabilities() (an explicit allowlist).
    # A name not published there is rejected; the tool never receives RoleState,
    # memory, or the Role object itself.
    requires: ClassVar[tuple[str, ...]] = ()

    # Cap on this tool's result size, in characters. When a single call's text
    # output exceeds this, the framework persists the full result to disk and
    # replaces the inline content with a <persisted-output> preview (see
    # metagpt.executor.tool_result_limit). The effective threshold is
    # this value clamped by the system-wide default; override per tool to allow
    # larger (e.g. Read) or smaller (e.g. Sleep) results. Aligned with CC's
    # per-tool `maxResultSizeChars`.
    max_result_size_chars: ClassVar[int] = DEFAULT_MAX_RESULT_SIZE_CHARS

    # --- Permission metadata (consumed by the PermissionEngine) ---
    # Coarse risk label a tool self-declares (advisory in phase 1). See
    # metagpt.executor.permission.types.RiskLevel.
    risk_level: ClassVar[str] = "low"
    # Whether this tool mutates the filesystem. Drives the ``acceptEdits``
    # permission mode (auto-approve edits). Set True on file-writing tools.
    mutates_filesystem: ClassVar[bool] = False

    # --- Execution ---

    def __init__(self) -> None:
        self._session_id: str = ""

    def bind(self, session_id: str, role=None) -> "BaseTool":
        """Bind context to this tool instance. Returns self for chaining.

        Called by the framework (ToolExecutor) at tool creation time.
        Injects ONLY the narrow Role capabilities named in `requires`, resolved
        against the Role's explicit capability allowlist (Role.tool_capabilities)
        — never via getattr on the Role — so a tool can never reach RoleState,
        memory, or any Role attribute that is not deliberately published.
        """
        self._session_id = session_id
        if role is not None:
            capabilities = role.tool_capabilities()
            for name in self.requires:
                if name not in capabilities:
                    raise AttributeError(
                        f"{type(self).__name__}.requires '{name}', but it is not a "
                        f"capability published by Role.tool_capabilities()"
                    )
                setattr(self, name, capabilities[name])
        return self

    @property
    def session_id(self) -> str:
        """The session_id of the owning Role."""
        return self._session_id

    @abstractmethod
    async def call(self, **kwargs) -> Any:
        """Tool entry point. All kwargs are LLM-specified parameters.

        Schema is auto-generated from this method's signature and docstring.
        """

    def cleanup_session(self, session_id: str) -> None:
        """Clean up per-session resources when a Role exits. Default no-op."""
        pass

    # ------------------------------------------------------------------
    # Permission hooks (consumed by the PermissionEngine before call())
    # ------------------------------------------------------------------

    def permission_target(self, args: dict) -> str:
        """Return the string matched against rule patterns for this call.

        E.g. ``Bash`` returns its ``command``, file tools return the path. The
        default is empty, meaning only whole-tool rules (``Tool`` without a
        pattern) can match. Override to enable ``Tool(pattern)`` rules.
        """
        return ""

    def permission_targets(self, args: dict) -> list[str]:
        """Return *all* permission-target strings this call touches.

        The default wraps :meth:`permission_target` (single target) so existing
        tools are unaffected. A tool that acts on multiple paths in one call
        (e.g. ``ApplyPatch``) overrides this to list every path; the executor
        then evaluates them together via ``PermissionEngine.check_multi``.
        """
        target = self.permission_target(args)
        return [target] if target else []

    def check_permissions(self, args: dict) -> "PermissionDecision | None":
        """Tool-specific permission self-check, run before mode/rule fallback.

        Return a :class:`PermissionDecision` to force ``allow``/``deny``/``ask``
        (deny/ask here are bypass-immune safety checks), or ``None`` to defer to
        rules and the permission mode. Default defers.
        """
        return None

    def tool_schema(self) -> dict:
        """Return this tool's LLM-facing schema (instance-level).

        Default delegates to the class-level get_schema(). Dynamic tools whose
        name/parameters are only known at runtime (e.g. MCP) override this to
        return their own schema.
        """
        return type(self).get_schema()

    @classmethod
    def get_schema(cls) -> dict:
        """Compute this tool's schema. A tool owns its own schema — the registry
        only registers and looks up, it does not describe.

        Uses custom_schema() if overridden, otherwise auto-generates from the
        call() signature and docstring. The first docstring line is the fallback
        description when `description` is not set.
        """
        custom = cls.custom_schema()
        if custom is not None:
            return custom

        docstring = inspect.getdoc(cls.call) or ""
        params = function_docstring_to_schema(cls.call, docstring)

        description = cls.description.strip() or (docstring.strip() if docstring else "")
        return {
            "name": cls.name,
            "description": description,
            "parameters": params,
        }

    @classmethod
    def custom_schema(cls) -> dict | None:
        """Override to provide a custom schema instead of auto-generation.

        Return a dict with "name", "description", "parameters" keys,
        or None to use the default auto-generated schema.
        """
        return None

    def native_schema(self) -> dict:
        """Return this tool's native tool-use schema (instance-level).

        Mirrors tool_schema() but produces a structured JSON Schema for the
        parameters (under "input_schema") instead of the XML protocol's
        free-text "parameters". Default delegates to the class-level
        get_native_schema(); dynamic tools (e.g. MCP) override this to return a
        schema whose params are already JSON Schema.
        """
        return type(self).get_native_schema()

    @classmethod
    def get_native_schema(cls) -> dict:
        """Compute this tool's native tool-use schema.

        Returns {"name", "description", "input_schema"} where input_schema is a
        JSON Schema object built from the call() signature + docstring. Used by
        the native tool-use channel; the XML path keeps using get_schema().
        """
        from metagpt.executor.tool_spec_adapter import build_json_schema

        base = cls.get_schema()
        return {
            "name": base["name"],
            "description": base["description"],
            "input_schema": build_json_schema(cls.call),
        }

