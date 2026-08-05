#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Simplified-Chinese human display catalog.

Each entry reproduces the CLI's existing Chinese wording verbatim, so the
default (zh) UX is byte-for-byte unchanged after the i18n migration. Keyed by
the :mod:`mote.product.i18n.keys` constants; Chinese has a single CLDR plural
category (``other``), so counts interpolate directly with no plural branching.
"""

from __future__ import annotations

from typing import Dict

from mote.product.i18n import keys as K

CATALOG: Dict[str, str] = {
    # Status bar
    K.STATUS_IDLE: "就绪",
    K.STATUS_IDLE_HINT: "ctrl+x 可删除聊天",
    K.STATUS_THINKING: "思考中",
    K.STATUS_VERB_THINKING: "思考中",
    K.STATUS_VERB_PROCESSING: "处理中",
    K.STATUS_VERB_WORKING: "工作中",
    K.STATUS_VERB_BUILDING: "构建中",
    K.STATUS_VERB_REASONING: "推理中",
    K.STATUS_VERB_PONDERING: "琢磨中",
    # Retry
    K.RETRY_FAILED: "LLM 请求失败（{error_type}）",
    K.RETRY_ATTEMPT: "第 {attempt}/{total} 次重试",
    K.RETRY_COUNTDOWN: "{secs}s 后重试…",
    # Compaction
    K.COMPACT_COMPACTED: "对话已压缩",
    K.COMPACT_KEPT: "保留 {count} 条消息",
    # Fold / truncation
    K.FOLD_FULL_REF: "输出过大已截断，完整见 {ref}",
    K.FOLD_HIDDEN_LINES: "… +{count} 行已折叠",
    K.FOLD_CONTENT: "… 内容已折叠",
    K.FOLD_MORE_LINES: "… +{count} 行",
    # Collapsed search/read group
    K.GROUP_SEARCH: "搜索 {count} 个模式",
    K.GROUP_READ: "读取 {count} 个文件",
    K.LIST_SEP: "，",
    # Per-tool result summaries
    K.SUMMARY_READ_IMAGE: "读取图片",
    K.SUMMARY_READ_PDF: "读取 PDF",
    K.SUMMARY_READ_LINES: "读取 {count} 行",
    K.SUMMARY_GREP_MATCHES_FILES: "找到 {matches} 处匹配，共 {files} 个文件",
    K.SUMMARY_GREP_MATCHES: "找到 {count} 处匹配",
    K.SUMMARY_FOUND_FILES: "找到 {count} 个文件",
    K.SUMMARY_NO_MATCHES: "无匹配",
    K.SUMMARY_NO_FILES: "无匹配文件",
    K.SUMMARY_CREATED_LINES: "新建 {count} 行",
    K.SUMMARY_UPDATED_LINES: "更新 {count} 行",
    K.SUMMARY_EDIT_ADDED_REMOVED: "更新 +{added} -{removed} 行",
    K.SUMMARY_EDIT_ADDED: "更新 +{count} 行",
    K.SUMMARY_EDIT_REMOVED: "更新 -{count} 行",
    K.SUMMARY_UPDATED: "已更新",
    K.SUMMARY_REPLACED: "替换 {count} 处",
    # Tool outcomes
    K.TOOL_REJECTED: "已被用户拒绝",
    K.RESULT_NO_OUTPUT: "（无输出）",
    K.RESULT_FAILED: "失败",
    K.RESULT_RETRYABLE: "可重试",
    # Driver
    K.DRIVER_TOOLS_LOADED: "可用 {count} 个工具（已加载 {loaded} 个，延迟加载 {deferred} 个）",
    # Prompt input
    K.PROMPT_PLACEHOLDER: "输入消息…（/help 查看命令）",
    # Keybinding + fold hints
    K.KEY_TOGGLE_TOOL: "展开/折叠工具",
    K.KEY_DELETE_MODE: "删除对话",
    K.KEY_EXPAND_HINT: "ctrl+o 展开",
    K.KEY_COLLAPSE_HINT: "ctrl+o 折叠",
    K.KEY_EXIT_HINT: "（再次按 Ctrl+C 退出）",
    # React-unit delete-mode
    K.DELETE_MODE_HINT: "删除模式：点击对话勾选要删除的项，回车确认，Esc 取消",
    K.DELETE_BUSY: "对话进行中，无法进入删除模式",
    K.DELETE_NONE: "未勾选任何对话",
    K.DELETE_DONE: "已删除 {count} 条消息",
    # Approval gate
    K.APPROVAL_REQUIRED: "需要审批",
    K.APPROVAL_PROCEED: "是否继续？",
    K.APPROVAL_ACTION_RUN: "运行：{tool}",
    K.APPROVAL_ACTION_ESCALATE: "越权：{tool}",
    K.APPROVAL_OPT_YES: "是",
    K.APPROVAL_OPT_ALWAYS: "是，且不再询问同类操作",
    K.APPROVAL_OPT_NO: "否，并告诉我该怎么做（esc）",
    K.APPROVAL_OPT_NEVER: "否，且永不允许此操作",
    K.APPROVAL_TYPED_HINT: "[y]是 / [n]否 / [a]总是 / [d]永不？",
    K.APPROVAL_REASON_ASK_RULE: "有 ask 规则要求确认",
    K.APPROVAL_REASON_DEFAULT: "此操作需要你的批准",
    K.APPROVAL_SUGGESTION: "总是允许则本会话放行 {rule}",
    # Interactive select hints
    K.HINT_SELECT_MULTI: "Space 选择 · Enter 确认",
    K.HINT_SELECT_SINGLE: "↑↓ 选择 · Enter 确认",
    K.HINT_SELECT_MULTI_CANCEL: "Space 选择 · Enter 确认 · Esc 取消",
    K.HINT_SELECT_SINGLE_CANCEL: "↑↓ 选择 · Enter 确认 · Esc 取消",
    K.HINT_ESC_CANCEL: "Esc 取消",
    K.SELECT_OTHER: "其他（输入你自己的答案）",
    K.SELECT_FREE_TEXT_PROMPT: "输入你的答案：",
    K.SELECT_ANSWER_PLACEHOLDER: "你的答案…",
    K.SELECT_SUBMIT: "提交",
    K.HANDOFF_TITLE: "接管 · {runtime}",
    K.HANDOFF_MESSAGE_PLACEHOLDER: "给 Agent 的可选留言",
    K.HANDOFF_COMPLETE: "完成",
    K.HANDOFF_CANCEL: "取消",
    K.HANDOFF_TERMINAL_INPUT: "输入终端内容并按 Enter…",
    K.HANDOFF_WINDOW_ACTIVE: "实时工作区已在独立窗口中打开。",
    # /lang command
    K.LANG_CURRENT: "当前语言：{code}",
    K.LANG_AVAILABLE: "可用语言：{codes}",
    K.LANG_SWITCHED: "语言已切换为 {code}",
    K.LANG_UNKNOWN: "未知语言：{code}。可用：{codes}",
}
