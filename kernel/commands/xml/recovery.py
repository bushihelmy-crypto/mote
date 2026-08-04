"""Parsing and recovery for XML command responses."""

from __future__ import annotations

from typing import Optional, Tuple

from mote.kernel.commands.xml.stream import Command, LexerState, PythonObjectParser


async def parse_commands(command_rsp: str, valid_names: Optional[set[str]]) -> Tuple[list[Command], str]:
    if not command_rsp:
        return [], "Empty command response"
    try:
        commands, _ = await loads_xml(command_rsp, valid_names)
        return (commands, "") if commands else ([], "No valid commands found")
    except Exception as exc:
        return [], f"Error parsing commands: {exc}"


async def loads_xml(data: str, valid_names: Optional[set[str]]) -> Tuple[list[Command], str]:
    lexer = PythonObjectParser(ignore_text=True, valid_names=valid_names)
    try:
        await lexer.loads_xml(xml=data)
        return lexer.get_commands(), ""
    except ValueError as exc:
        recovered = await _repair_unclosed_xml(data, lexer, valid_names)
        return (recovered, "") if recovered else (lexer.get_commands(), str(exc))


def _missing_closers(lexer: PythonObjectParser) -> str:
    if not lexer.functions:
        return ""
    parts: list[str] = []
    if lexer.state == LexerState.PARSE_ARG_VALUE and lexer.functions[-1].args:
        parts.append(lexer.functions[-1].args[-1].end_variable_name)
    if lexer.state in (LexerState.PARSE_ARG_NAME, LexerState.PARSE_ARG_VALUE):
        parts.append(lexer.functions[-1].end_function_name)
    return "".join(parts)


async def _repair_unclosed_xml(
    data: str, failed_lexer: PythonObjectParser, valid_names: Optional[set[str]]
) -> Optional[list[Command]]:
    closers = _missing_closers(failed_lexer)
    if not closers:
        return None
    lexer = PythonObjectParser(ignore_text=True, valid_names=valid_names)
    try:
        await lexer.loads_xml(xml=data + closers)
    except ValueError:
        return None
    return lexer.get_commands() or None
