#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Request-level context compression for LLM calls.

Owns the per-call message compression that runs immediately before the wire
request: keep the system preamble, then trim/sketch user+assistant turns so the
payload fits the model's window. The strategies (POST/PRE/BALANCED cut by
token/msg) are unchanged.

This module lives in ``router.llm`` because it operates on the token-budgeting
surface of an LLM. It depends on the narrow :class:`MessageBudgeter` protocol
(not the full ``BaseLLM``), so the contract it needs is explicit and it has no
dependency on the ``context`` package.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from metagpt.common.config.config.compress_msg_config import CompressType
from metagpt.common.utils.token_counter import TOKEN_MAX
from metagpt.router.llm.editor_read_parser import find_editor_read_segments


@runtime_checkable
class MessageBudgeter(Protocol):
    """The token-budgeting surface :class:`RequestContextBuilder` needs from an LLM.

    Declaring it explicitly (instead of taking the whole ``BaseLLM``) documents
    the actual coupling and lets the builder be tested with a lightweight stub.
    """

    model: str

    def count_tokens(self, messages: list[dict]) -> int:
        ...

    def system_role(self) -> str:
        ...

    def get_content_under_limit_token(
        self, msg: dict, target_token_count: int, from_end: bool = True, delta: int = 2
    ) -> dict:
        ...

    def get_content_under_limit_token_balanced(
        self, msg: dict, target_token_count: int, delta: int = 8, head_ratio: float = 0.5
    ) -> dict:
        ...


class RequestContextBuilder:
    """Build request messages for LLM completion without changing public BaseLLM behavior."""

    ARTIFACT_SKETCH_CHAR_LIMIT = 3000
    COMPRESSED_SECTION_MARKER = "[COMPRESSED_EDITOR_READ]"
    COMPRESSED_SKETCH_MARKER = "\n[CONTENT SKETCH]\n"
    # Phase 1 compresses every editor.read message to this fixed fraction of its original
    # token count. Using a fixed ratio (rather than one derived from tokens_to_reduce) keeps
    # each compressed message's bytes stable across turns — so appending new messages does
    # not invalidate prompt-cache prefixes on earlier editor.read messages.
    PHASE1_KEEP_RATIO = 0.5

    def __init__(self, llm: MessageBudgeter):
        self.llm = llm

    def build(
        self,
        messages: list[dict],
        compress_type: CompressType = CompressType.NO_COMPRESS,
        max_token: int = 128000,
        threshold: float = 0.8,
    ) -> list[dict]:
        if compress_type == CompressType.NO_COMPRESS:
            return messages

        max_token = TOKEN_MAX.get(self.llm.model, max_token)
        keep_token = int(max_token * threshold)
        if self.llm.count_tokens(messages) <= keep_token:
            return messages

        return self._compress_messages(messages, compress_type=compress_type, keep_token=keep_token)

    def _compress_messages(self, messages: list[dict], compress_type: CompressType, keep_token: int) -> list[dict]:
        compressed = []

        system_msg_val = self.llm.system_role()
        system_msgs = []
        user_assistant_msgs = []
        for i, msg in enumerate(messages):
            if msg["role"] == system_msg_val:
                system_msgs.append(msg)
            else:
                user_assistant_msgs = messages[i:]
                break

        compressed.extend(system_msgs)

        if compress_type == CompressType.BALANCED_CUT_BY_TOKEN:
            return self._balanced_compress_messages(system_msgs, user_assistant_msgs, keep_token)

        current_token_count = self.llm.count_tokens(system_msgs)

        if compress_type in [
            CompressType.POST_CUT_BY_TOKEN,
            CompressType.POST_CUT_BY_MSG,
        ]:
            for i, msg in enumerate(reversed(user_assistant_msgs)):
                token_count = self.llm.count_tokens([msg])
                if current_token_count + token_count <= keep_token:
                    compressed.insert(len(system_msgs), msg)
                    current_token_count += token_count
                else:
                    if compress_type == CompressType.POST_CUT_BY_TOKEN or len(compressed) == len(system_msgs):
                        truncated_token = keep_token - current_token_count
                        truncated_msg = self._compact_message(
                            msg,
                            truncated_token,
                            from_end=True,
                            balanced=False,
                        )
                        compressed.insert(len(system_msgs), truncated_msg)
                        token_count = self.llm.count_tokens([truncated_msg])
                        current_token_count += token_count
                    break

        elif compress_type in [CompressType.PRE_CUT_BY_TOKEN, CompressType.PRE_CUT_BY_MSG]:
            for i, msg in enumerate(user_assistant_msgs):
                token_count = self.llm.count_tokens([msg])
                if current_token_count + token_count <= keep_token:
                    compressed.append(msg)
                    current_token_count += token_count
                else:
                    if compress_type == CompressType.PRE_CUT_BY_TOKEN or len(compressed) == len(system_msgs):
                        truncated_token = keep_token - current_token_count
                        truncated_msg = self._compact_message(msg, truncated_token, from_end=False, balanced=False)
                        compressed.append(truncated_msg)
                        token_count = self.llm.count_tokens([truncated_msg])
                        current_token_count += token_count
                    break

        return compressed

    def _balanced_compress_messages(
        self, system_msgs: list[dict], user_assistant_msgs: list[dict], keep_token: int
    ) -> list[dict]:
        """Balanced compression: keep all messages, compress editor.read first, then compress large messages."""
        compressed = list(system_msgs)
        system_tokens = self.llm.count_tokens(system_msgs)
        available_tokens = keep_token - system_tokens

        # Calculate token count for each message
        msg_tokens = []
        for msg in user_assistant_msgs:
            tokens = self.llm.count_tokens([msg])
            msg_tokens.append(tokens)

        total_tokens = sum(msg_tokens)
        if total_tokens <= available_tokens:
            compressed.extend(user_assistant_msgs)
            return compressed

        # Phase 1: compress every editor.read message to a fixed fraction of its original
        # size. Using a constant ratio (not one derived from tokens_to_reduce) keeps each
        # compressed message's bytes stable across turns — so appending new messages does
        # not invalidate prompt-cache prefixes on earlier editor.read messages.
        compressed_msgs = []

        for i, msg in enumerate(user_assistant_msgs):
            content = msg.get("content", "")
            if not isinstance(content, str):
                compressed_msgs.append(msg)
                continue

            editor_reads = find_editor_read_segments(content)
            if not editor_reads:
                compressed_msgs.append(msg)
                continue

            target_tokens = int(msg_tokens[i] * self.PHASE1_KEEP_RATIO)
            compacted = self._compact_message(msg, target_tokens, from_end=True, balanced=True)
            compressed_msgs.append(compacted)

            actual_tokens = self.llm.count_tokens([compacted])
            msg_tokens[i] = actual_tokens

        # Phase 2: If still over limit, compress messages above average proportionally
        total_tokens = sum(msg_tokens)

        def _return_phase1_only() -> list[dict]:
            compressed.extend(compressed_msgs)
            return compressed

        if total_tokens <= available_tokens:
            return _return_phase1_only()

        tokens_to_reduce = total_tokens - available_tokens
        avg_tokens = total_tokens / len(compressed_msgs) if compressed_msgs else 0

        # Only compress above-average messages. Touching short messages here would destroy
        # conversational context (user prompts, brief tool outputs) for little token savings.
        above_avg_indices = [i for i, t in enumerate(msg_tokens) if t > avg_tokens]
        above_avg_total = sum(msg_tokens[i] for i in above_avg_indices)

        final_msgs = []
        for i, msg in enumerate(compressed_msgs):
            if i not in above_avg_indices or above_avg_total <= 0:
                final_msgs.append(msg)
                continue

            reduction = int(tokens_to_reduce * (msg_tokens[i] / above_avg_total))
            target_tokens = max(msg_tokens[i] - reduction, msg_tokens[i] // 2)

            compacted = self._compact_message(msg, target_tokens, from_end=True, balanced=True)
            final_msgs.append(compacted)
            actual_tokens = self.llm.count_tokens([compacted])
            msg_tokens[i] = actual_tokens

        # Phase 3 fallback: keep newest messages, drop oldest, balanced-truncate the
        # boundary message.
        total_after_phase2 = system_tokens + sum(msg_tokens)
        if total_after_phase2 > keep_token:
            kept_reverse = []
            kept_tokens = system_tokens
            for i in range(len(final_msgs) - 1, -1, -1):
                msg = final_msgs[i]
                tokens = msg_tokens[i]
                if kept_tokens + tokens <= keep_token:
                    kept_reverse.append(msg)
                    kept_tokens += tokens
                    continue
                remaining = keep_token - kept_tokens
                if remaining > 0:
                    truncated = self._compact_message(msg, remaining, from_end=True, balanced=True)
                    truncated_tokens = self.llm.count_tokens([truncated])
                    if kept_tokens + truncated_tokens <= keep_token:
                        kept_reverse.append(truncated)
                        break
                break
            final_msgs = list(reversed(kept_reverse))

        compressed.extend(final_msgs)
        return compressed

    def _compact_message(
        self, msg: dict, target_token_count: int, from_end: bool, balanced: bool = False
    ) -> dict:
        if target_token_count <= 0:
            return {"role": msg["role"], "content": "" if not isinstance(msg["content"], list) else []}

        if isinstance(msg["content"], list):
            text_item_tokens = []
            for item in msg["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_item_tokens.append(
                        self.llm.count_tokens([{"role": msg["role"], "content": item.get("text", "")}])
                    )
                else:
                    text_item_tokens.append(0)

            original_text_total = sum(text_item_tokens)
            compacted_content = []
            for idx, item in enumerate(msg["content"]):
                if not (isinstance(item, dict) and item.get("type") == "text"):
                    compacted_content.append(item)
                    continue

                if original_text_total <= 0:
                    per_item_budget = 0
                else:
                    per_item_budget = max(1, int(target_token_count * text_item_tokens[idx] / original_text_total))

                compacted_text = self._compact_text(
                    item.get("text", ""),
                    msg["role"],
                    per_item_budget,
                    from_end=from_end,
                    balanced=balanced,
                )
                compacted_content.append({"type": "text", "text": compacted_text})
            return {"role": msg["role"], "content": compacted_content}

        return {
            "role": msg["role"],
            "content": self._compact_text(msg["content"], msg["role"], target_token_count, from_end=from_end, balanced=balanced),
        }

    def _compact_text(self, text: str, role: str, target_token_count: int, from_end: bool, balanced: bool = False) -> str:
        if not text:
            return ""

        original = {"role": role, "content": text}
        if self.llm.count_tokens([original]) <= target_token_count:
            return text

        if not balanced:
            return self.llm.get_content_under_limit_token(original, target_token_count, from_end=from_end)["content"]

        editor_reads = find_editor_read_segments(text)
        if not editor_reads:
            if self.COMPRESSED_SECTION_MARKER in text:
                refit = self._refit_compressed_sections(text, role, target_token_count)
                if refit is not None:
                    return refit
            return self.llm.get_content_under_limit_token_balanced(original, target_token_count)["content"]

        sketch_sections = []
        for editor_read in editor_reads:
            sketch = self._build_preview_sketch(editor_read["block_content"])
            if not sketch:
                continue
            sketch_sections.append(
                "\n".join(
                    [
                        "[COMPRESSED_EDITOR_READ]",
                        f"file_path={editor_read['file_path']}",
                        "status=partial",
                        "instruction=Use Terminal to run 'grep -n pattern file_path' to search specific content.",
                        "",
                        "[CONTENT SKETCH]",
                        sketch,
                    ]
                )
            )

        if not sketch_sections:
            return self.llm.get_content_under_limit_token_balanced(original, target_token_count)["content"]
        raw_header = "\n\n[RAW EXCERPT]\n"
        hint = (
            "\n\n[HINT] File content was truncated. "
            "To access specific parts, use Terminal to run: "
            "grep -n 'pattern' file_path"
        )
        sketch_text = "\n\n---\n\n".join(sketch_sections)
        sketch_tokens = self.llm.count_tokens([{"role": role, "content": sketch_text}])
        overhead_tokens = self.llm.count_tokens(
            [{"role": role, "content": raw_header + hint}]
        )

        if sketch_tokens >= target_token_count:
            refit = self._refit_compressed_sections(sketch_text, role, target_token_count)
            if refit is not None:
                return refit
            return self.llm.get_content_under_limit_token_balanced(
                {"role": role, "content": sketch_text}, target_token_count
            )["content"]

        excerpt_budget = target_token_count - sketch_tokens - overhead_tokens
        if excerpt_budget <= 0:
            return sketch_text

        excerpt_parts = []
        cursor = 0
        for segment in editor_reads:
            excerpt_parts.append(text[cursor:segment["start"]])
            cursor = segment["end"]
        excerpt_parts.append(text[cursor:])
        text_without_reads = "".join(excerpt_parts)

        excerpt = self.llm.get_content_under_limit_token_balanced(
            {"role": role, "content": text_without_reads}, excerpt_budget
        )["content"]

        candidate = sketch_text + raw_header + excerpt + hint
        candidate_msg = {"role": role, "content": candidate}
        if self.llm.count_tokens([candidate_msg]) <= target_token_count:
            return candidate
        refit = self._refit_compressed_sections(candidate, role, target_token_count)
        if refit is not None:
            return refit
        return self.llm.get_content_under_limit_token_balanced(candidate_msg, target_token_count)["content"]

    def _build_preview_sketch(self, text: str) -> str:
        preview = text[: self.ARTIFACT_SKETCH_CHAR_LIMIT]
        omitted_chars = max(0, len(text) - len(preview))
        if omitted_chars <= 0:
            return preview
        return f"{preview}\n\n[... TRUNCATED {omitted_chars} CHARS ...]"

    def _refit_compressed_sections(self, text: str, role: str, target_token_count: int) -> str | None:
        """Shrink each [COMPRESSED_EDITOR_READ] section's body so every marker
        survives under ``target_token_count``.

        Returns None if the section headers alone exceed the budget — the caller
        should fall back to plain balanced truncation in that case.
        """
        positions = []
        cursor = 0
        while True:
            idx = text.find(self.COMPRESSED_SECTION_MARKER, cursor)
            if idx == -1:
                break
            positions.append(idx)
            cursor = idx + len(self.COMPRESSED_SECTION_MARKER)
        if not positions:
            return None

        preamble = text[: positions[0]]
        sections: list[tuple[str, str]] = []
        for k, start in enumerate(positions):
            end = positions[k + 1] if k + 1 < len(positions) else len(text)
            segment = text[start:end]
            sketch_idx = segment.find(self.COMPRESSED_SKETCH_MARKER)
            if sketch_idx == -1:
                sections.append((segment, ""))
            else:
                header_end = sketch_idx + len(self.COMPRESSED_SKETCH_MARKER)
                sections.append((segment[:header_end], segment[header_end:]))

        headers_only = preamble + "".join(header for header, _ in sections)
        header_tokens = self.llm.count_tokens([{"role": role, "content": headers_only}])
        if header_tokens >= target_token_count:
            return None

        body_budget = target_token_count - header_tokens
        per_body_budget = max(1, body_budget // len(sections))

        new_bodies = []
        for _, body in sections:
            if not body:
                new_bodies.append(body)
                continue
            body_msg = {"role": role, "content": body}
            if self.llm.count_tokens([body_msg]) <= per_body_budget:
                new_bodies.append(body)
                continue
            new_bodies.append(
                self.llm.get_content_under_limit_token_balanced(body_msg, per_body_budget)["content"]
            )

        parts = [preamble]
        for (header, _), new_body in zip(sections, new_bodies):
            parts.append(header + new_body)
        result = "".join(parts)
        if self.llm.count_tokens([{"role": role, "content": result}]) <= target_token_count:
            return result
        return None
