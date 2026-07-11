#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2023/4/29 16:07
@Author  : alexanderwu
@File    : common.py
@Modified By: mashenquan, 2023-11-1. According to Chapter 2.2.2 of RFC 116:
        Add generic class-to-string and object-to-string conversion functionality.
@Modified By: mashenquan, 2023/11/27. Bug fix: `parse_recipient` failed to parse the recipient in certain GPT-3.5
        responses.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import binascii
import contextlib
import functools
import importlib
import inspect
import json
import os
import re
import time
import traceback
from asyncio import iscoroutinefunction
from datetime import datetime
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import aiofiles
import chardet
import fitz
import requests
from mote.common.logs import logger
from mote.common.utils.exceptions import handle_exception
from mote.common.utils.remote import remotable
from PIL import Image
from pydantic_core import to_jsonable_python
from tenacity import RetryCallState, RetryError


class CodeParser:
    @classmethod
    def parse_block(cls, block: str, text: str) -> str:
        blocks = cls.parse_blocks(text)
        for k, v in blocks.items():
            if block in k:
                return v
        return ""

    @classmethod
    def parse_blocks(cls, text: str):
        # 首先根据"##"将文本分割成不同的block
        blocks = text.split("##")

        # 创建一个字典，用于存储每个block的标题和内容
        block_dict = {}

        # 遍历所有的block
        for block in blocks:
            # 如果block不为空，则继续处理
            if block.strip() == "":
                continue
            if "\n" not in block:
                block_title = block
                block_content = ""
            else:
                # 将block的标题和内容分开，并分别去掉前后的空白字符
                block_title, block_content = block.split("\n", 1)
            block_dict[block_title.strip()] = block_content.strip()

        return block_dict

    @classmethod
    def parse_code(cls, text: str, lang: str = "", block: Optional[str] = None) -> str:
        if block:
            text = cls.parse_block(block, text)
        pattern = rf"```{lang}.*?\s+(.*?)\n```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            code = match.group(1)
        else:
            logger.warning(f"{pattern} not match following text:\n{text}")
            # raise Exception
            return text  # just assume original text is code
        return code

    @classmethod
    def parse_multiple_code(cls, text: str, lang: str = "") -> list[str]:
        pattern = rf"```{lang}.*?\s+(.*?)\n```"
        matches = re.findall(pattern, text, re.DOTALL)
        return [match for match in matches]

    @classmethod
    def parse_str(cls, block: str, text: str, lang: str = ""):
        code = cls.parse_code(block=block, text=text, lang=lang)
        code = code.split("=")[-1]
        code = code.strip().strip("'").strip('"')
        return code

    @classmethod
    def parse_file_list(cls, block: str, text: str, lang: str = "") -> list[str]:
        # Regular expression pattern to find the tasks list.
        code = cls.parse_code(block=block, text=text, lang=lang)
        # print(code)
        pattern = r"\s*(.*=.*)?(\[.*\])"

        # Extract tasks list string using regex.
        match = re.search(pattern, code, re.DOTALL)
        if match:
            tasks_list_str = match.group(2)

            # Convert string representation of list to a Python list using ast.literal_eval.
            tasks = ast.literal_eval(tasks_list_str)
        else:
            raise Exception
        return tasks


# MoteError is imported for ``role_raise_decorator`` (preserve typed exceptions).
from mote.common.exception import MoteError  # noqa: E402


def get_class_name(cls) -> str:
    """Return class name"""
    return f"{cls.__module__}.{cls.__name__}"


def any_to_str(val: Any) -> str:
    """Return the class name or the class name of the object, or 'val' if it's a string type."""
    if isinstance(val, str):
        return val
    elif not callable(val):
        return get_class_name(type(val))
    else:
        return get_class_name(val)


def any_to_str_set(val) -> set:
    """Convert any type to string set."""
    res = set()

    # Check if the value is iterable, but not a string (since strings are technically iterable)
    if isinstance(val, (dict, list, set, tuple)):
        # Special handling for dictionaries to iterate over values
        if isinstance(val, dict):
            val = val.values()

        for i in val:
            res.add(any_to_str(i))
    else:
        res.add(any_to_str(val))

    return res


def read_json_file(json_file: str, encoding: str = "utf-8") -> list[Any]:
    if not Path(json_file).exists():
        raise FileNotFoundError(f"json_file: {json_file} not exist, return []")

    with open(json_file, "r", encoding=encoding) as fin:
        try:
            data = json.load(fin)
        except Exception:
            raise ValueError(f"read json file: {json_file} failed")
    return data


def handle_unknown_serialization(x: Any) -> str:
    """For `to_jsonable_python` debug, get more detail about the x."""

    if inspect.ismethod(x):
        tip = f"Cannot serialize method '{x.__func__.__name__}' of class '{x.__self__.__class__.__name__}'"
    elif inspect.isfunction(x):
        tip = f"Cannot serialize function '{x.__name__}'"
    elif hasattr(x, "__class__"):
        tip = f"Cannot serialize instance of '{x.__class__.__name__}'"
    elif hasattr(x, "__name__"):
        tip = f"Cannot serialize class or module '{x.__name__}'"
    else:
        tip = f"Cannot serialize object of type '{type(x).__name__}'"

    raise TypeError(tip)


def write_json_file(json_file: str, data: Any, encoding: str = "utf-8", indent: int = 4, use_fallback: bool = False):
    folder_path = Path(json_file).parent
    if not folder_path.exists():
        folder_path.mkdir(parents=True, exist_ok=True)

    custom_default = partial(to_jsonable_python, fallback=handle_unknown_serialization if use_fallback else None)

    with open(json_file, "w", encoding=encoding) as fout:
        json.dump(data, fout, ensure_ascii=False, indent=indent, default=custom_default)


def import_class(class_name: str, module_name: str) -> type:
    module = importlib.import_module(module_name)
    a_class = getattr(module, class_name)
    return a_class


def format_trackback_info(limit: Optional[int] = 2):
    return traceback.format_exc(limit=limit)


def role_raise_decorator(func):
    async def wrapper(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        except KeyboardInterrupt as kbi:
            logger.error(f"KeyboardInterrupt: {kbi} occurs, start to serialize the project")
            if self.state.latest_observed_msg:
                self.context_manager.delete(self.state.latest_observed_msg)
            # raise again to make it captured outside
            raise Exception(format_trackback_info(limit=None))
        except Exception as e:
            if self.state.latest_observed_msg:
                logger.exception(
                    "There is a exception in role's execution, in order to resume, "
                    "we delete the newest role communication message in the role's memory."
                )
                # remove role newest observed msg to make it observed again
                self.context_manager.delete(self.state.latest_observed_msg)
            # raise again to make it captured outside
            if isinstance(e, MoteError):
                # Preserve typed exceptions instead of wrapping into a bare Exception.
                raise
            if isinstance(e, RetryError):
                last_error = e.last_attempt._exception
                name = any_to_str(last_error)
                if re.match(r"^openai\.", name) or re.match(r"^httpx\.", name):
                    raise last_error

            raise Exception(format_trackback_info(limit=None)) from e

    return wrapper


@handle_exception
async def aread(filename: str | Path, encoding="utf-8") -> str:
    """Read file asynchronously."""
    if not filename or not Path(filename).exists():
        return ""
    try:
        async with aiofiles.open(str(filename), mode="r", encoding=encoding) as reader:
            content = await reader.read()
    except UnicodeDecodeError:
        async with aiofiles.open(str(filename), mode="rb") as reader:
            raw = await reader.read()
            result = chardet.detect(raw) or {}
            detected_encoding = result.get("encoding") or "utf-8"
            content = raw.decode(detected_encoding)
    return content


def encode_image(
    image_path_or_pil: Union[Path, "Image.Image", str], encoding: str = "utf-8", resize: int = 1568
) -> str:
    """encode image from file or PIL.Image into base64 with optional resize"""
    # Load image to PIL if it's not already a PIL Image
    if isinstance(image_path_or_pil, Image.Image):
        image_pil = image_path_or_pil
    else:
        if isinstance(image_path_or_pil, str):
            image_path_or_pil = Path(image_path_or_pil)
        if not image_path_or_pil.exists():
            raise FileNotFoundError(f"{image_path_or_pil} not exists")
        with open(str(image_path_or_pil), "rb") as image_file:
            bytes_data = image_file.read()
        image_pil = Image.open(BytesIO(bytes_data))

    # Check image size and resize if needed
    width, height = image_pil.size
    max_size = max(width, height)
    if max_size > resize:
        # Calculate new dimensions maintaining aspect ratio
        new_width = round(width * resize / max_size)
        new_height = round(height * resize / max_size)
        image_pil = image_pil.resize((new_width, new_height), Image.Resampling.LANCZOS)

    # Save to buffer and encode
    buffer = BytesIO()
    # convert to WebP
    if image_pil.mode == "P":
        image_pil = image_pil.convert("RGBA")  # Convert palette mode images to RGBA format

    elif image_pil.mode == "CMYK":
        image_pil = image_pil.convert("RGB")  # Convert CMYK mode images to RGB format

    # Ensure compatible mode for JPEG
    if image_pil.mode not in ("RGB", "L"):
        image_pil = image_pil.convert("RGB")

    image_pil.save(buffer, format="JPEG", quality=90)
    bytes_data = buffer.getvalue()

    return base64.b64encode(bytes_data).decode(encoding)


def decode_image(img_url_or_b64: str) -> "Image.Image":
    """decode image from url or base64 into PIL.Image"""
    if img_url_or_b64.startswith("http"):
        # image http(s) url
        resp = requests.get(img_url_or_b64)
        img = Image.open(BytesIO(resp.content))
    else:
        # image b64_json
        b64_data = re.sub("^data:image/.+;base64,", "", img_url_or_b64)
        img_data = BytesIO(base64.b64decode(b64_data))
        img = Image.open(img_data)
    return img


def extract_image_paths(content: str) -> list[str]:
    # We require that the path must have a space preceding it, like "xxx /an/absolute/path.jpg xxx"
    pattern = r"[^\s]+\.(?:png|jpe?g|gif|bmp|tiff|PNG|JPE?G|GIF|BMP|TIFF)"
    image_paths = re.findall(pattern, content)
    return image_paths


def extract_and_encode_images(content: str) -> list[str]:
    images = []
    for path in extract_image_paths(content):
        if os.path.exists(path):
            images.append(encode_image(path))
    return images


def extract_pdf_paths(content: str) -> list[str]:
    # Require a non-whitespace path ending with .pdf or .PDF
    pattern = r"[^\s]+\.(?:pdf|PDF)"
    return re.findall(pattern, content)


def extract_and_encode_pdfs(content: str) -> list[str]:
    """Extract local PDF file paths from content and encode as base64 strings.

    Note: we only encode if the path exists locally to avoid network fetch.
    """
    pdfs: list[str] = []
    for path in extract_pdf_paths(content):
        if os.path.exists(path):
            with open(path, "rb") as f:
                pdfs.append(base64.b64encode(f.read()).decode("utf-8"))
    return pdfs


def sniff_image_media_type(b64_data: str) -> Optional[str]:
    """Detect an image's media type from its leading bytes (magic numbers).

    Providers like Bedrock/Anthropic reject a base64 image whose declared media
    type disagrees with the actual bytes (e.g. a PNG labelled as JPEG). Sniffing
    the real type lets callers send the correct ``media_type``.

    Returns ``None`` when the data can't be decoded or the format isn't
    recognised, leaving the declared media type untouched.
    """
    try:
        header = base64.b64decode(b64_data[:64], validate=False)
    except (binascii.Error, ValueError):
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


# -- data-URL codec ---------------------------------------------------------
# The single authority for the ``data:<media_type>;base64,<data>`` wire shape
# and the "which media type do we trust" policy, shared by every LLM provider
# (base_llm assembly, anthropic block conversion, transformers image shrink).
# Previously each site re-implemented the split/partition/regex and its own
# sniff-vs-declared rule, and the rules had already drifted apart.

_DATA_URL_DEFAULT_MEDIA_TYPE = "image/jpeg"


def resolve_image_media_type(b64_data: str, declared: Optional[str] = None) -> str:
    """Decide an image's media type: sniffed bytes win, else declared, else JPEG.

    The one place the sniff-vs-declared precedence lives. A declared media type
    is often wrong (e.g. a PNG labelled JPEG), and providers like Bedrock /
    Anthropic reject the mismatch, so a successful magic-number sniff always
    overrides the declaration; only when sniffing fails do we fall back to the
    declared type, and finally to ``image/jpeg``.
    """
    return sniff_image_media_type(b64_data) or declared or _DATA_URL_DEFAULT_MEDIA_TYPE


def build_data_url(b64_data: str, declared: Optional[str] = None) -> str:
    """Wrap raw base64 image bytes into a ``data:...;base64,...`` URL.

    The media type is resolved via :func:`resolve_image_media_type` (sniff wins),
    so callers hand over the raw base64 and get a correctly-typed data URL.
    """
    media_type = resolve_image_media_type(b64_data, declared)
    return f"data:{media_type};base64,{b64_data}"


def parse_data_url(url: str) -> Optional[Tuple[str, str]]:
    """Split a ``data:<media_type>;base64,<data>`` URL into (media_type, data).

    Returns ``None`` when *url* is not a string, lacks the ``data:`` scheme, or
    has no comma separating the header from the payload (malformed). The returned
    media type is the *declared* one (stripped, may be ``""``); apply
    :func:`resolve_image_media_type` on the data if a sniff-corrected type is
    wanted. Does not decode the payload.
    """
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, sep, data = url.partition(",")
    if not sep:
        return None
    media_type = header[len("data:") :].split(";", 1)[0].strip()
    return media_type, data


def pdfs_within_limits(
    pdfs: list[str],
    max_total_pdf_bytes: int = 15 * 1024 * 1024,
    max_total_pdf_pages: int = 80,
) -> tuple[bool, int, int]:
    """Check whether given base64-encoded PDFs are within size/page limits.

    Args:
        pdfs (list[str]): Base64-encoded PDF strings. Accepts optional data URL prefix.
        max_total_pdf_bytes (int): Max total bytes across PDFs. Default is 15MB.
        max_total_pdf_pages (int): Max total pages across PDFs. Default is 80.

    Returns:
        tuple[bool, int, int]: (ok_to_attach, total_bytes, total_pages)
    """

    total_pdf_bytes = 0
    total_pdf_pages = 0

    for raw_b64 in pdfs:
        if not raw_b64:
            continue
        # Remove potential data URL prefix
        pdf_b64 = re.sub(r"^data:application/pdf;base64,", "", raw_b64)

        decoded = b""
        try:
            decoded = base64.b64decode(pdf_b64)
        except Exception as e:
            logger.warning(f"Decode base64 PDF failed, using length estimate for size. exp: {e}")
            # Estimate size from base64 length if decode fails
            total_pdf_bytes += int(len(pdf_b64) * 3 / 4)
            # Unable to count pages without bytes
            continue

        total_pdf_bytes += len(decoded)

        try:
            doc = fitz.open(stream=decoded, filetype="pdf")
            total_pdf_pages += doc.page_count
            doc.close()
        except Exception as e:
            logger.warning(f"PyMuPDF failed to read pages, default to 0. exp: {e}")

    ok = total_pdf_bytes <= max_total_pdf_bytes and total_pdf_pages <= max_total_pdf_pages
    return ok, total_pdf_bytes, total_pdf_pages


def log_and_reraise(retry_state: RetryCallState):
    # tenacity only invokes this callback after an attempt has completed, so the
    # outcome future is always present here; narrow away the Optional.
    outcome = retry_state.outcome
    assert outcome is not None, "log_and_reraise called before any attempt completed"
    logger.error(f"Retry attempts exhausted. Last exception: {outcome.exception()}")
    logger.warning(
        """
Recommend going to https://deepwisdom.feishu.cn/wiki/MsGnwQBjiif9c3koSJNcYaoSnu4#part-XdatdVlhEojeAfxaaEZcMV3ZniQ
See FAQ 5.8
"""
    )
    exc = outcome.exception()
    assert exc is not None, "log_and_reraise invoked on a successful outcome"
    raise exc


def log_time(method):
    """A time-consuming decorator for printing execution duration."""

    def before_call():
        start_time, cpu_start_time = time.perf_counter(), time.process_time()
        logger.debug(f"[{method.__name__}] started at: " f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return start_time, cpu_start_time

    def after_call(start_time, cpu_start_time):
        end_time, cpu_end_time = time.perf_counter(), time.process_time()
        logger.debug(
            f"[{method.__name__}] ended. "
            f"Time elapsed: {end_time - start_time:.4} sec, CPU elapsed: {cpu_end_time - cpu_start_time:.4} sec"
        )

    @functools.wraps(method)
    def timeit_wrapper(*args, **kwargs):
        start_time, cpu_start_time = before_call()
        result = method(*args, **kwargs)
        after_call(start_time, cpu_start_time)
        return result

    @functools.wraps(method)
    async def timeit_wrapper_async(*args, **kwargs):
        start_time, cpu_start_time = before_call()
        result = await method(*args, **kwargs)
        after_call(start_time, cpu_start_time)
        return result

    return timeit_wrapper_async if iscoroutinefunction(method) else timeit_wrapper


# Conventional exit code reported for a command killed by timeout (aligns with
# codex's EXEC_TIMEOUT_EXIT_CODE and the `timeout(1)` utility convention).
EXEC_TIMEOUT_EXIT_CODE = 124


@remotable
async def aexecute(
    cmd: str,
    working_dir: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    shell: bool = True,
    timeout: Optional[float] = None,
    check: bool = False,
    wait: bool = False,
    return_partial_on_timeout: bool = False,
    sandbox_runtime: Optional[Any] = None,
) -> Union[None, Tuple[int, str, str], Tuple[int, str, str, bool]]:
    """
    Generic async function to execute shell commands

    Args:
        cmd: Command to execute
        working_dir: Working directory for command execution (default: current directory)
        env: Dictionary of environment variables (default: None, uses current environment)
        shell: Whether to execute command through shell (default: True)
        timeout: Command execution timeout in seconds (default: None, no timeout)
        check: If True and return code is non-zero, raise exception (default: False)
        wait: If True, wait for process to complete and return results (default: False)
             If False, return immediately without waiting
        return_partial_on_timeout: If True, drain stdout/stderr incrementally so a
             timeout returns the output captured so far instead of discarding it.
             In this mode aexecute NEVER raises on timeout and ALWAYS returns the
             4-tuple ``(return_code, stdout, stderr, timed_out)``; on timeout the
             return code is ``EXEC_TIMEOUT_EXIT_CODE`` (124).
        sandbox_runtime: Optional OS-level sandbox runtime (a
             :class:`mote.sandbox.SandboxRuntime`). When supplied, the command
             is wrapped (bwrap + process hardening) and the env is amended with
             the network-proxy policy *before* spawning. None => no OS-level
             isolation (the historical behavior).

    Returns:
        If wait=False: None
        If wait=True and return_partial_on_timeout=False: (return_code, stdout, stderr)
        If wait=True and return_partial_on_timeout=True: (return_code, stdout, stderr, timed_out)

    Raises:
        asyncio.TimeoutError: If command execution times out (only when
            return_partial_on_timeout=False)
        RuntimeError: If check=True and command returns non-zero status code
    """
    if sandbox_runtime is not None:
        # Wrap the command + amend the env for OS-level isolation. wrap_command
        # returns a single shell-quoted string still meant for the shell, so the
        # ``shell=True`` spawn below is unchanged.
        cmd, env = await sandbox_runtime.wrap_command(cmd, cwd=working_dir, env=env)

    process = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=working_dir
    )

    # If not waiting, return immediately
    if not wait:
        return

    if return_partial_on_timeout:
        return await _aexecute_capture_partial(process, cmd, timeout, check)

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        stdout_str = stdout.decode(errors="replace").strip() if stdout else ""
        stderr_str = stderr.decode(errors="replace").strip() if stderr else ""

        # Check return code
        if check and process.returncode != 0:
            raise RuntimeError(
                f"Command '{cmd}' failed with return code {process.returncode}\n"
                f"STDOUT: {stdout_str}\nSTDERR: {stderr_str}"
            )

        return process.returncode or 0, stdout_str, stderr_str

    except asyncio.TimeoutError:
        # Try to terminate process on timeout, and force kill process if termination fails
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()

        raise asyncio.TimeoutError(f"Command '{cmd}' timed out after {timeout} seconds")


async def _aexecute_capture_partial(
    process: "asyncio.subprocess.Process",
    cmd: str,
    timeout: Optional[float],
    check: bool,
) -> Tuple[int, str, str, bool]:
    """Run ``process`` draining its pipes into external buffers.

    Reading into buffers we own means that when ``wait_for`` cancels the
    collector on timeout, whatever was already read is still available — that is
    how the partial output survives. On timeout the child is terminated (then
    killed) and any output produced before the kill is returned with
    ``EXEC_TIMEOUT_EXIT_CODE``.
    """
    stdout_buf = bytearray()
    stderr_buf = bytearray()

    async def _drain(stream, buf: bytearray) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            buf.extend(chunk)

    async def _collect() -> None:
        await asyncio.gather(_drain(process.stdout, stdout_buf), _drain(process.stderr, stderr_buf))
        await process.wait()

    timed_out = False
    try:
        await asyncio.wait_for(_collect(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        # _collect (and its drain tasks) are cancelled by wait_for, but the
        # buffers above already hold the bytes read so far.
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    stdout_str = bytes(stdout_buf).decode(errors="replace").strip()
    stderr_str = bytes(stderr_buf).decode(errors="replace").strip()
    rc = EXEC_TIMEOUT_EXIT_CODE if timed_out else (process.returncode if process.returncode is not None else -1)

    if check and not timed_out and rc != 0:
        raise RuntimeError(f"Command '{cmd}' failed with return code {rc}\nSTDOUT: {stdout_str}\nSTDERR: {stderr_str}")

    return rc, stdout_str, stderr_str, timed_out
