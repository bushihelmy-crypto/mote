#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Simplified-Chinese human display catalog.

Each entry reproduces the CLI's existing Chinese wording verbatim, so the
default (zh) UX is byte-for-byte unchanged after the i18n migration. Keyed by
the :mod:`mote.common.i18n.keys` constants; Chinese has a single CLDR plural
category (``other``), so counts interpolate directly with no plural branching.
"""
from __future__ import annotations

from typing import Dict

from mote.common.i18n import keys as K

CATALOG: Dict[str, str] = {
    # Status bar
    K.STATUS_IDLE: "就绪",
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
    # Driver
    K.DRIVER_TOOLS_LOADED: "已加载 {count} 个工具",
    # Prompt input
    K.PROMPT_PLACEHOLDER: "输入消息…（/help 查看命令）",
    # Keybinding + fold hints
    K.KEY_TOGGLE_TOOL: "展开/折叠工具",
    K.KEY_EXPAND_HINT: "ctrl+o 展开",
    K.KEY_COLLAPSE_HINT: "ctrl+o 折叠",
    K.KEY_EXIT_HINT: "（再次按 Ctrl+C 退出）",
    # Interactive select hints
    K.HINT_SELECT_MULTI: "Space 选择 · Enter 确认",
    K.HINT_SELECT_SINGLE: "↑↓ 选择 · Enter 确认",
    K.HINT_SELECT_MULTI_CANCEL: "Space 选择 · Enter 确认 · Esc 取消",
    K.HINT_SELECT_SINGLE_CANCEL: "↑↓ 选择 · Enter 确认 · Esc 取消",
    K.HINT_ESC_CANCEL: "Esc 取消",
    # /lang command
    K.LANG_CURRENT: "当前语言：{code}",
    K.LANG_AVAILABLE: "可用语言：{codes}",
    K.LANG_SWITCHED: "语言已切换为 {code}",
    K.LANG_UNKNOWN: "未知语言：{code}。可用：{codes}",
}
