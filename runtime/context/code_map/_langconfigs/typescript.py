"""TypeScript + TSX language configs — JavaScript plus the type-level declarations.

TypeScript is JavaScript with a type layer, so these configs reuse the JS import
extractor (:func:`~mote.runtime.context.code_map._langconfigs.javascript.js_esm` — ``import
type`` is the same ``import_statement`` shape) and the JS path-stem resolver
(:class:`JsModuleResolver` already unions ``.ts``/``.tsx``), adding the def rules
for the constructs JS lacks: ``interface`` / ``enum`` / ``type`` aliases,
``abstract class`` bodies, and the body-less method *signatures* inside an
interface. ``.ts`` and ``.tsx`` are two configs, not one, because they bind
different tree-sitter grammars (``typescript`` vs ``tsx``); everything else is
shared through :func:`_ts_def_rules`.
"""

from __future__ import annotations

from mote.runtime.context.code_map._langconfigs.javascript import js_esm
from mote.runtime.context.code_map.providers.config import DefRule, DocRule, FieldAccess, LangConfig
from mote.runtime.context.code_map.providers.resolvers.javascript import JsModuleResolver


def _ts_def_rules() -> tuple[DefRule, ...]:
    """The def rules shared by ``.ts`` and ``.tsx`` (JS constructs + the type layer)."""
    return (
        DefRule(
            node_types=("function_declaration", "generator_function_declaration"),
            def_kind="function",
            symbol_kind="function",
            opens_scope=True,
            scope_kind="function",
            params_field="parameters",
            return_field="return_type",
        ),
        DefRule(
            node_types=("class_declaration", "abstract_class_declaration"),
            def_kind="class",
            symbol_kind="class",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("interface_declaration",),
            def_kind="class",
            symbol_kind="interface",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("method_definition",),
            def_kind="function",
            symbol_kind="method",
            opens_scope=True,
            scope_kind="function",
            is_method_context=True,
            params_field="parameters",
            return_field="return_type",
        ),
        # Body-less signatures inside an interface / abstract class: a method
        # symbol (for presentation), but no scope and no call graph — there is no
        # body to walk.
        DefRule(
            node_types=("method_signature", "abstract_method_signature"),
            def_kind="function",
            symbol_kind="method",
            is_method_context=True,
            params_field="parameters",
            return_field="return_type",
        ),
        DefRule(
            node_types=("enum_declaration",),
            def_kind="class",
            symbol_kind="enum",
        ),
        DefRule(
            node_types=("type_alias_declaration",),
            def_kind="class",
            symbol_kind="type",
        ),
        DefRule(
            node_types=("variable_declarator",),
            def_kind="variable",
            symbol_kind="variable",
            emit_symbol=False,
        ),
    )


def _ts_config(language: str, ts_name: str, extensions: tuple[str, ...]) -> LangConfig:
    return LangConfig(
        language=language,
        ts_name=ts_name,
        extensions=extensions,
        def_rules=_ts_def_rules(),
        call_node_types=("call_expression",),
        call_func_field="function",
        identifier_node="identifier",
        field_access=FieldAccess(node_types=("member_expression",), object_field="object", member_field="property"),
        self_receivers=frozenset({"this"}),
        block_node_types=("statement_block",),
        skip_class_scope=False,
        doc_comment=DocRule(comment_types=("comment",)),
        import_extractor=js_esm,
        module_resolver_factory=JsModuleResolver,
    )


TYPESCRIPT = _ts_config("typescript", "typescript", (".ts",))
TSX = _ts_config("tsx", "tsx", (".tsx",))


__all__ = ["TYPESCRIPT", "TSX"]
