from __future__ import annotations

import json
from datetime import datetime
from typing import Optional, Tuple

import pytz

from metagpt.common.const import IMAGES, PDFS, USE_ENCODED_MEDIA
from metagpt.prompts.role import (
    ASK_HUMAN_COMMAND,
    ASK_HUMAN_GUIDANCE_FORMAT,
    SUMMARIZE_PROBLEM_WHEN_DUPLICATE,
)
from metagpt.common.schema import Message, UserMessage
from metagpt.common.utils.common import (
    extract_and_encode_images,
    extract_and_encode_pdfs,
)
from metagpt.common.utils.stream_xml import PythonObjectParser


def attach_media(memory: list[Message], k: int = 3) -> list[Message]:
    for message in memory:
        if USE_ENCODED_MEDIA in message.metadata and message.metadata[USE_ENCODED_MEDIA]:
            # backward compatibility: check if message.metadata[USE_ENCODED_MEDIA] is True
            images = extract_and_encode_images(message.content)
            if images:
                message.add_metadata(IMAGES, images[:k])
            pdfs = extract_and_encode_pdfs(message.content)
            if pdfs:
                message.add_metadata(PDFS, pdfs[:k])
    return memory


def detach_media(memory: list[Message]) -> list[Message]:
    for message in memory:
        if USE_ENCODED_MEDIA in message.metadata and message.metadata[USE_ENCODED_MEDIA]:
            # backward compatibility: check if message.metadata[USE_ENCODED_MEDIA] is True
            if IMAGES in message.metadata:
                del message.metadata[IMAGES]
            if PDFS in message.metadata:
                del message.metadata[PDFS]
    return memory


async def check_duplicates(req: list[dict], command_rsp: str, rsp_hist: list[str], llm, check_window: int = 10) -> str:
    past_rsp = rsp_hist[-check_window:]
    if command_rsp in past_rsp and '"command_name": "end"' not in command_rsp:
        # Normal response with thought contents are highly unlikely to reproduce
        # If an identical response is detected, it is a bad response, mostly due to LLM repeating generated content
        # In this case, ask human for help and regenerate

        # A special rule to skip checking
        # Terminal commands such as pnpm * can be repeated for continuous deployment; detect commands that contain Terminal only without risky tools such as Editor
        if "Terminal" in command_rsp and "Editor" not in command_rsp:
            return command_rsp

        #  Hard rule to ask human for help
        if past_rsp.count(command_rsp) >= 3:
            context = llm.format_msg(req + [UserMessage(content=SUMMARIZE_PROBLEM_WHEN_DUPLICATE)])
            problem = await llm.aask(context)
            ASK_HUMAN_COMMAND[0]["args"]["question"] = ASK_HUMAN_GUIDANCE_FORMAT.format(problem=problem).strip()
            ask_human_command = "```json\n" + json.dumps(ASK_HUMAN_COMMAND, indent=4, ensure_ascii=False) + "\n```"
            return ask_human_command
    return command_rsp


def call_signature(command_calls: Optional[list[dict]]) -> str:
    """Stable, order-insensitive signature string for a turn's structured calls.

    Accepts both call shapes used in this codebase: the live IR from ThinkEngine
    (``command_name``) and the recorded TOOL_CALLS metadata (``name``).
    """
    return json.dumps(
        [
            {"name": c.get("command_name") or c.get("name"), "args": c.get("args") or {}}
            for c in (command_calls or [])
        ],
        sort_keys=True,
        ensure_ascii=False,
    )


async def check_duplicate_calls(
    req: list[dict],
    command_calls: list[dict],
    sig_hist: list[str],
    llm,
    check_window: int = 10,
) -> Optional[list[dict]]:
    """Native counterpart to check_duplicates: dedup by structured-call signature.

    Native tool-use messages carry no rich thought text, so the text guard in
    check_duplicates does not apply. Instead we compare a stable signature of this
    turn's calls (name + args, order-insensitive) against recent turns' signatures.
    On a hard 3x repeat (excluding terminal-only / end turns) we ask the human for
    help by returning a synthesized ask_human call list; otherwise return None to
    signal "no override — keep the original calls".
    """
    if not command_calls:
        return None
    names = {c.get("command_name") for c in command_calls}
    # end and pure-terminal repeats are legitimate (e.g. deploy loops); skip them.
    if "end" in names:
        return None
    if "Terminal" in names and "Editor" not in names:
        return None
    signature = call_signature(command_calls)
    past = sig_hist[-check_window:]
    if past.count(signature) >= 3:
        context = llm.format_msg(req + [UserMessage(content=SUMMARIZE_PROBLEM_WHEN_DUPLICATE)])
        problem = await llm.aask(context)
        question = ASK_HUMAN_GUIDANCE_FORMAT.format(problem=problem).strip()
        return [{"id": None, "command_name": "ask_human", "args": {"question": question}}]
    return None


async def parse_commands2(command_rsp, valid_names: set[str]) -> Tuple[list[dict], str]:
    """Parse commands from XML-like tagged response, filtering by valid names.

    Args:
        command_rsp: Response string with XML-like command tags
        valid_names: Set of valid command names for tag filtering.

    Returns:
        (parsed commands, error message). If valid, error message is "".
    """
    if not command_rsp:
        return [], "Empty command response"
    try:
        command_list, _ = await loads_xml(data=command_rsp, valid_names=valid_names)
        if not command_list:
            return [], "No valid commands found"
        return command_list, ""
    except Exception as e:
        return [], f"Error parsing commands: {str(e)}"


def get_time_info():
    time_zone = pytz.timezone("America/Los_Angeles")
    current_time = datetime.now(time_zone)
    formatted_time = current_time.strftime("%Y-%m-%d")
    return f"Current date in Los Angeles is {formatted_time}."


async def loads_xml(data, valid_names: set[str]) -> Tuple[list[dict], str]:
    lexer = PythonObjectParser(ignore_text=True, valid_names=valid_names)
    try:
        await lexer.loads_xml(xml=data)
        return lexer.get_commands(), ""
    except ValueError as e:
        return lexer.get_commands(), str(e)
