"""
Message Formatter — 回复格式化

职责:
1. 将 Agent 的输出格式化为渠道友好的消息
2. 生成接收确认、状态更新、阻塞提示、最终结果
3. 适配不同渠道的格式要求（飞书卡片 vs Web HTML）
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable
from urllib.parse import urlparse

from .types import ChannelType, OutboundMessage, OutboundMessageType

logger = logging.getLogger(__name__)

_INTERNAL_PREFIX_RE = re.compile(r"^\[hub:[^\]]+\]\s*")
_TABLE_SEPARATOR_RE = re.compile(r"^\|?[\s:\-|\u2500\u2501]+\|?\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_ORDERED_LIST_RE = re.compile(r"^\s*(\d+)\.\s+")
_UNORDERED_LIST_RE = re.compile(r"^\s*[-*+]\s+")
_SOURCE_HEADING_RE = re.compile(r"^\s*(sources|来源)\s*:?\s*$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+")
_PROVIDER_ERROR_RE = re.compile(r"Provider (?:request failed|quota exhausted) \(([^)]+)\):\s*(.*)", re.IGNORECASE | re.DOTALL)
_FEISHU_CARD_MAX_CHARS = 3500
_FEISHU_HELP_BATCH_SIZE = 4
_FEISHU_TITLE_BY_TYPE = {
    OutboundMessageType.ACK: "Nexus · 已接收",
    OutboundMessageType.STATUS: "Nexus · 状态更新",
    OutboundMessageType.BLOCKED: "Nexus · 需要确认",
    OutboundMessageType.RESULT: "Nexus · 结果",
    OutboundMessageType.CLARIFY: "Nexus · 需要澄清",
    OutboundMessageType.ERROR: "Nexus · 错误",
}
_FEISHU_TEMPLATE_BY_TYPE = {
    OutboundMessageType.ACK: "blue",
    OutboundMessageType.STATUS: "blue",
    OutboundMessageType.BLOCKED: "orange",
    OutboundMessageType.RESULT: "green",
    OutboundMessageType.CLARIFY: "orange",
    OutboundMessageType.ERROR: "red",
}


class MessageFormatter:
    """
    消息格式化器。

    设计原则:
    1. 默认自然语言优先
    2. 内部治理状态不直接外泄给用户
    3. 只在必要时展示技术细节
    """

    def format_ack(self, session_id: str, task_summary: str) -> OutboundMessage:
        """生成接收确认消息"""
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.ACK,
            content=f"收到，正在处理：{task_summary}",
        )

    def format_queued(
        self,
        session_id: str,
        position: int = 1,
    ) -> OutboundMessage:
        """生成排队通知 — 告知用户消息已排队等待处理。"""
        content = (
            "当前任务仍在执行中，你的消息已排队，完成后将自动处理。\n"
            "如需开始全新话题，请先发送 /new 再发消息。"
        )
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.STATUS,
            content=content,
        )

    def format_status(
        self, session_id: str, status: str, progress: str = ""
    ) -> OutboundMessage:
        """生成状态更新消息"""
        content = f"当前状态：{status}"
        if progress:
            content += f"\n{progress}"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.STATUS,
            content=content,
        )

    def format_blocked(
        self, session_id: str, reason: str, options: list[str] | None = None
    ) -> OutboundMessage:
        """生成阻塞提示消息"""
        content = f"需要你的确认：{reason}"
        if options:
            content += "\n\n选项：\n"
            for i, opt in enumerate(options, 1):
                content += f"  {i}. {opt}\n"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.BLOCKED,
            content=content,
        )

    def format_result(
        self,
        session_id: str,
        result: str,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> OutboundMessage:
        """生成最终结果消息"""
        content = result
        if artifacts:
            content += "\n\n📎 相关文件：\n"
            for artifact in artifacts:
                name = artifact.get("name", "未知文件")
                path = artifact.get("path", "")
                content += f"  • {name}: {path}\n"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.RESULT,
            content=content,
        )

    def format_clarify(
        self,
        session_id: str,
        question: str,
        options: list[str] | None = None,
    ) -> OutboundMessage:
        """生成澄清请求消息"""
        content = question.strip()
        if options:
            content += "\n\n可选项：\n"
            for idx, option in enumerate(options, start=1):
                content += f"{idx}. {option}\n"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.CLARIFY,
            content=content.rstrip(),
        )

    def format_error(
        self, session_id: str, error: str, user_friendly: bool = True
    ) -> OutboundMessage:
        """生成错误通知消息"""
        if user_friendly:
            content = (
                f"处理过程中遇到了问题：{self._humanize_error(str(error or ''))}\n\n"
                "你可以重试，或发送 `/provider` 切换到其它已配置后端。"
            )
        else:
            content = f"错误：{error}"
        return OutboundMessage(
            session_id=session_id,
            message_type=OutboundMessageType.ERROR,
            content=content,
        )

    def render_for_channel(self, channel: ChannelType, message: OutboundMessage) -> str:
        """Render an outbound message into channel-friendly plain text."""
        content = self._strip_internal_prefix(message.content)
        if channel == ChannelType.FEISHU:
            return self._render_feishu_plain_text(content).strip()
        if channel == ChannelType.WEIXIN:
            return self._render_weixin_plain_text(content).strip()
        return content.strip()

    def render_feishu_card(self, message: OutboundMessage) -> dict[str, Any]:
        content = self._strip_internal_prefix(message.content)
        if self._looks_like_help_payload(content):
            return self._build_feishu_help_card(content)
        if message.message_type in {
            OutboundMessageType.ACK,
            OutboundMessageType.STATUS,
            OutboundMessageType.BLOCKED,
            OutboundMessageType.CLARIFY,
        }:
            return self._build_feishu_status_card(message, content)
        return self._build_feishu_result_card(message, content)

    @staticmethod
    def _strip_internal_prefix(text: str) -> str:
        lines = [line for line in str(text or "").splitlines()]
        if not lines:
            return ""
        if lines and _INTERNAL_PREFIX_RE.match(lines[0].strip()):
            lines = lines[1:]
        return "\n".join(lines).strip()

    def _render_mobile_plain_text(self, text: str) -> str:
        result = str(text or "").replace("\r\n", "\n").strip()
        if not result:
            return ""

        result = self._render_markdown_tables(result)
        result = self._render_code_blocks(result)
        result = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1", result)
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)
        result, preserved_urls = self._preserve_urls(result)
        result = re.sub(r"`([^`]+)`", r"\1", result)
        result = re.sub(r"(?<!\*)\*\*([^*]+)\*\*(?!\*)", r"\1", result)
        result = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", result)
        result = re.sub(r"(?<!_)__([^_]+)__(?!_)", r"\1", result)
        result = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"\1", result)
        result = re.sub(r"^\s*>\s?", "", result, flags=re.MULTILINE)
        result = re.sub(r"^\s*---+\s*$", "", result, flags=re.MULTILINE)
        result = re.sub(r"^\s*___+\s*$", "", result, flags=re.MULTILINE)
        result = self._restore_urls(result, preserved_urls)

        normalized_lines: list[str] = []
        for raw_line in result.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                normalized_lines.append("")
                continue
            heading_match = _HEADING_RE.match(line)
            if heading_match:
                title = heading_match.group(1).strip()
                normalized_lines.append(f"【{title}】" if title else "")
                continue
            if _UNORDERED_LIST_RE.match(line):
                normalized_lines.append(f"- {_UNORDERED_LIST_RE.sub('', line).strip()}")
                continue
            ordered_match = _ORDERED_LIST_RE.match(line)
            if ordered_match:
                number = ordered_match.group(1)
                body = _ORDERED_LIST_RE.sub("", line).strip()
                normalized_lines.append(f"{number}. {body}")
                continue
            normalized_lines.append(stripped)

        result = "\n".join(normalized_lines)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    def _render_feishu_plain_text(self, text: str) -> str:
        result = self._render_mobile_plain_text(text)
        if not result:
            return ""
        return self._format_source_sections(
            result,
            formatter=self._render_feishu_source_entries_as_text,
        ).strip()

    def _render_weixin_plain_text(self, text: str) -> str:
        result = self._render_mobile_plain_text(text)
        if not result:
            return ""
        result = self._format_source_sections(
            result,
            formatter=self._render_weixin_source_entries,
        ).strip()
        # 增强微信结构化排版
        result = self._enhance_weixin_structure(result)
        return result

    @staticmethod
    def _enhance_weixin_structure(text: str) -> str:
        """为微信纯文本消息添加结构化排版元素。

        使用 Unicode 分隔符和状态前缀增强可读性，
        同时保持纯文本兼容（iLinkai 仅支持文本消息）。
        """
        lines = text.splitlines()
        out: list[str] = []
        prev_was_heading = False
        for line in lines:
            stripped = line.strip()
            # 【标题】样式 → 添加分隔线
            if stripped.startswith("【") and stripped.endswith("】"):
                if out and out[-1].strip():
                    out.append("")
                out.append(f"{'─' * 18}")
                out.append(f"  {stripped}")
                out.append(f"{'─' * 18}")
                prev_was_heading = True
                continue
            # 成功/失败/警告状态行前缀
            if stripped.startswith("- "):
                body = stripped[2:].strip()
                if any(body.startswith(kw) for kw in ("成功", "已完成", "已保存", "已创建", "已更新")):
                    out.append(f"  {body}")
                elif any(body.startswith(kw) for kw in ("失败", "错误", "异常", "无法")):
                    out.append(f"  {body}")
                elif any(body.startswith(kw) for kw in ("注意", "警告", "提醒")):
                    out.append(f"  {body}")
                else:
                    out.append(f"  {stripped}")
                prev_was_heading = False
                continue
            if prev_was_heading and not stripped:
                prev_was_heading = False
                continue
            prev_was_heading = False
            out.append(line)
        result = "\n".join(out)
        # 压缩连续空行
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    def _render_feishu_card_body(self, text: str) -> str:
        result = str(text or "").replace("\r\n", "\n").strip()
        if not result:
            return ""
        result = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1", result)
        result = re.sub(r"^\s*>\s?", "", result, flags=re.MULTILINE)
        result = re.sub(r"^\s*---+\s*$", "", result, flags=re.MULTILINE)
        result = re.sub(r"^\s*___+\s*$", "", result, flags=re.MULTILINE)

        lines: list[str] = []
        for raw_line in result.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                lines.append("")
                continue
            heading_match = _HEADING_RE.match(line)
            if heading_match:
                title = heading_match.group(1).strip()
                lines.append(f"**{title}**" if title else "")
                continue
            if _UNORDERED_LIST_RE.match(line):
                lines.append(f"- {_UNORDERED_LIST_RE.sub('', line).strip()}")
                continue
            ordered_match = _ORDERED_LIST_RE.match(line)
            if ordered_match:
                number = ordered_match.group(1)
                body = _ORDERED_LIST_RE.sub("", line).strip()
                lines.append(f"{number}. {body}")
                continue
            lines.append(stripped)
        result = "\n".join(lines)
        result = self._format_source_sections(
            result,
            formatter=self._render_feishu_source_entries_as_markdown,
        )
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    def _build_feishu_card(
        self,
        *,
        title: str,
        template: str,
        sections: list[str],
    ) -> dict[str, Any]:
        elements: list[dict[str, Any]] = []
        for section in sections:
            for chunk in self._chunk_for_feishu_card(section):
                elements.append({"tag": "markdown", "content": chunk})
        if not elements:
            elements.append({"tag": "markdown", "content": " "})
        return {
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title,
                },
                "template": template,
            },
            "body": {
                "elements": elements,
            },
        }

    @staticmethod
    def _looks_like_help_payload(text: str) -> bool:
        normalized = str(text or "")
        return "Nexus 支持的命令" in normalized or (
            "| 命令 |" in normalized
            and "中文别名" in normalized
            and "/help" in normalized
            and "/status" in normalized
        )

    def _build_feishu_help_card(self, text: str) -> dict[str, Any]:
        entries = self._parse_help_entries(text)
        intro = self._extract_text_before_first_table(text)
        sections: list[str] = []
        if intro:
            sections.append(self._render_feishu_card_body(intro))
        sections.append("**可用命令**\n\n直接发送以下命令即可控制 Nexus。")
        for start in range(0, len(entries), _FEISHU_HELP_BATCH_SIZE):
            batch = entries[start:start + _FEISHU_HELP_BATCH_SIZE]
            lines: list[str] = []
            for entry in batch:
                command = entry.get("command", "")
                alias = entry.get("alias", "")
                description = entry.get("description", "")
                row = f"- `{command}`"
                if description:
                    row += f"：{description}"
                if alias:
                    row += f"\n  别名：{alias}"
                lines.append(row)
            if lines:
                sections.append("\n".join(lines))
        sections.append("**提示**\n\n- 发送 `/status` 查看当前任务状态\n- 发送 `/new` 立即开始新任务")
        return self._build_feishu_card(
            title="Nexus · 命令帮助",
            template="blue",
            sections=sections,
        )

    def _build_feishu_status_card(
        self,
        message: OutboundMessage,
        text: str,
    ) -> dict[str, Any]:
        body = self._render_feishu_card_body(text)
        summary, detail = self._split_summary_and_detail(body)
        summary = self._strip_message_type_prefix(message.message_type, summary)
        lead_map = {
            OutboundMessageType.ACK: "已收到请求",
            OutboundMessageType.STATUS: "当前状态",
            OutboundMessageType.BLOCKED: "需要你的确认",
            OutboundMessageType.CLARIFY: "还需要补充信息",
        }
        detail_map = {
            OutboundMessageType.ACK: "处理内容",
            OutboundMessageType.STATUS: "进度详情",
            OutboundMessageType.BLOCKED: "可直接回复",
            OutboundMessageType.CLARIFY: "可直接回复",
        }
        sections: list[str] = []
        if summary:
            sections.append(
                f"**{lead_map.get(message.message_type, '状态')}**\n\n{summary}"
            )
        if detail:
            sections.append(
                f"**{detail_map.get(message.message_type, '详细信息')}**\n\n{detail}"
            )
        return self._build_feishu_card(
            title=_FEISHU_TITLE_BY_TYPE.get(message.message_type, "Nexus"),
            template=_FEISHU_TEMPLATE_BY_TYPE.get(message.message_type, "blue"),
            sections=sections,
        )

    def _build_feishu_result_card(
        self,
        message: OutboundMessage,
        text: str,
    ) -> dict[str, Any]:
        body = self._render_feishu_card_body(text)
        summary, detail = self._split_summary_and_detail(body)
        sections: list[str] = []
        if summary:
            lead = "执行结果" if message.message_type == OutboundMessageType.RESULT else "处理遇到问题"
            sections.append(f"**{lead}**\n\n{summary}")
        if detail:
            sections.append(f"**详细内容**\n\n{detail}")
        if message.message_type == OutboundMessageType.ERROR and not detail:
            sections.append("**建议**\n\n- 可以直接重试一次\n- 或换一种方式重新描述需求")
        return self._build_feishu_card(
            title=_FEISHU_TITLE_BY_TYPE.get(message.message_type, "Nexus"),
            template=_FEISHU_TEMPLATE_BY_TYPE.get(message.message_type, "blue"),
            sections=sections,
        )

    @staticmethod
    def _split_summary_and_detail(text: str) -> tuple[str, str]:
        sections = [section.strip() for section in str(text or "").split("\n\n") if section.strip()]
        if not sections:
            return "", ""
        if len(sections) == 1:
            return sections[0], ""
        return sections[0], "\n\n".join(sections[1:])

    @staticmethod
    def _strip_message_type_prefix(message_type: OutboundMessageType, text: str) -> str:
        normalized = str(text or "").strip()
        prefixes = {
            OutboundMessageType.ACK: ["收到，正在处理："],
            OutboundMessageType.STATUS: ["当前状态："],
            OutboundMessageType.BLOCKED: ["需要你的确认："],
        }
        for prefix in prefixes.get(message_type, []):
            if normalized.startswith(prefix):
                return normalized[len(prefix):].strip()
        return normalized

    def _parse_help_entries(self, text: str) -> list[dict[str, str]]:
        lines = str(text or "").replace("\r\n", "\n").splitlines()
        block = self._extract_first_table_block(lines)
        if not block:
            return []
        rows = [self._split_table_row(line) for line in block if self._is_table_row(line)]
        if len(rows) <= 2:
            return []
        entries: list[dict[str, str]] = []
        for row in rows[2:]:
            if not row:
                continue
            command = row[0].strip() if len(row) >= 1 else ""
            alias = row[1].strip() if len(row) >= 2 else ""
            description = row[2].strip() if len(row) >= 3 else ""
            if command:
                entries.append(
                    {
                        "command": command,
                        "alias": alias,
                        "description": description,
                    }
                )
        return entries

    def _extract_text_before_first_table(self, text: str) -> str:
        lines = str(text or "").replace("\r\n", "\n").splitlines()
        for index in range(len(lines)):
            if self._looks_like_table_header(lines, index):
                return "\n".join(lines[:index]).strip()
        return str(text or "").strip()

    def _extract_first_table_block(self, lines: list[str]) -> list[str]:
        for index in range(len(lines)):
            if self._looks_like_table_header(lines, index):
                block: list[str] = [lines[index], lines[index + 1]]
                cursor = index + 2
                while cursor < len(lines) and self._is_table_row(lines[cursor]):
                    block.append(lines[cursor])
                    cursor += 1
                return block
        return []

    def _chunk_for_feishu_card(self, text: str) -> list[str]:
        normalized = text.strip()
        if not normalized:
            return []
        if len(normalized) <= _FEISHU_CARD_MAX_CHARS:
            return [normalized]

        chunks: list[str] = []
        remaining = normalized
        while len(remaining) > _FEISHU_CARD_MAX_CHARS:
            split_at = remaining.rfind("\n\n", 0, _FEISHU_CARD_MAX_CHARS)
            if split_at <= 0:
                split_at = remaining.rfind("\n", 0, _FEISHU_CARD_MAX_CHARS)
            if split_at <= 0:
                split_at = _FEISHU_CARD_MAX_CHARS
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].lstrip()
        if remaining:
            chunks.append(remaining)
        return [chunk for chunk in chunks if chunk]

    def _render_code_blocks(self, text: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            code = (match.group(2) or "").strip("\n")
            if not code:
                return ""
            indented = "\n".join(
                f"  {line.rstrip()}" if line.strip() else ""
                for line in code.splitlines()
            ).rstrip()
            return f"代码:\n{indented}" if indented else ""

        return re.sub(r"```([^\n`]*)\n?([\s\S]*?)```", _replace, text)

    def _render_markdown_tables(self, text: str) -> str:
        lines = text.splitlines()
        rendered: list[str] = []
        i = 0
        while i < len(lines):
            if self._looks_like_table_header(lines, i):
                block: list[str] = [lines[i], lines[i + 1]]
                i += 2
                while i < len(lines) and self._is_table_row(lines[i]):
                    block.append(lines[i])
                    i += 1
                rendered.extend(self._convert_table_block(block))
                continue
            rendered.append(lines[i])
            i += 1
        return "\n".join(rendered)

    def _looks_like_table_header(self, lines: list[str], index: int) -> bool:
        return (
            index + 1 < len(lines)
            and self._is_table_row(lines[index])
            and _TABLE_SEPARATOR_RE.match(lines[index + 1].strip()) is not None
        )

    @staticmethod
    def _is_table_row(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2

    @staticmethod
    def _split_table_row(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    def _convert_table_block(self, block: list[str]) -> list[str]:
        rows = [self._split_table_row(line) for line in block if self._is_table_row(line)]
        if len(rows) <= 2:
            return [row for row in block if row.strip()]
        data_rows = rows[2:]
        converted: list[str] = []
        for row in data_rows:
            cells = [cell for cell in row if cell]
            if not cells:
                continue
            first, *rest = cells
            if not rest:
                converted.append(f"- {first}")
                continue
            body = "；".join(rest)
            converted.append(f"- {first}：{body}")
        return converted or [row for row in block if row.strip()]

    def _format_source_sections(
        self,
        text: str,
        *,
        formatter: Callable[[list[dict[str, str]]], list[str]],
    ) -> str:
        lines = str(text or "").splitlines()
        if not lines:
            return ""

        rendered: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if self._is_source_heading(line):
                entries, next_index = self._collect_source_entries(lines, index + 1)
                if entries:
                    rendered.extend(formatter(entries))
                    index = next_index
                    continue
            rendered.append(line)
            index += 1

        result = "\n".join(rendered)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()

    @staticmethod
    def _is_source_heading(line: str) -> bool:
        return _SOURCE_HEADING_RE.match(str(line or "").strip()) is not None

    def _collect_source_entries(
        self,
        lines: list[str],
        start_index: int,
    ) -> tuple[list[dict[str, str]], int]:
        entries: list[dict[str, str]] = []
        index = start_index
        while index < len(lines):
            stripped = lines[index].strip()
            if not stripped:
                if entries:
                    index += 1
                    break
                index += 1
                continue
            entry = self._parse_source_entry(stripped)
            if entry is None:
                break
            entries.append(entry)
            index += 1
        return entries, index

    def _parse_source_entry(self, line: str) -> dict[str, str] | None:
        url_match = _URL_RE.search(line)
        if url_match is None:
            return None
        url = url_match.group(0).rstrip(".,);]")
        prefix = line[:url_match.start()].strip()
        rank_match = re.match(r"^\s*(\d+)[\.\)]\s+(.*)$", prefix)
        rank = rank_match.group(1) if rank_match else ""
        title = rank_match.group(2).strip() if rank_match else prefix
        title = re.sub(r"\s*[-—–:：]\s*$", "", title).strip()
        domain = self._extract_domain(url)
        if not title:
            title = domain or url
        return {
            "rank": rank,
            "title": title,
            "url": url,
            "domain": domain,
        }

    @staticmethod
    def _extract_domain(url: str) -> str:
        hostname = urlparse(str(url or "")).hostname or ""
        return hostname.removeprefix("www.")

    @staticmethod
    def _preserve_urls(text: str) -> tuple[str, list[str]]:
        preserved: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            preserved.append(match.group(0))
            return f"@@URL{len(preserved) - 1}@@"

        return _URL_RE.sub(_replace, str(text or "")), preserved

    @staticmethod
    def _restore_urls(text: str, preserved: list[str]) -> str:
        restored = str(text or "")
        for index, url in enumerate(preserved):
            restored = restored.replace(f"@@URL{index}@@", url)
        return restored

    @staticmethod
    def _render_feishu_source_entries_as_markdown(
        entries: list[dict[str, str]],
    ) -> list[str]:
        lines = ["**来源**"]
        for index, entry in enumerate(entries, start=1):
            rank = entry.get("rank") or str(index)
            title = entry.get("title") or entry.get("domain") or entry.get("url") or f"来源 {index}"
            url = entry.get("url") or ""
            domain = entry.get("domain") or ""
            suffix = f" ({domain})" if domain else ""
            lines.append(f"{rank}. [{title}]({url}){suffix}")
        return lines

    @staticmethod
    def _render_feishu_source_entries_as_text(
        entries: list[dict[str, str]],
    ) -> list[str]:
        lines = ["来源："]
        for index, entry in enumerate(entries, start=1):
            rank = entry.get("rank") or str(index)
            title = entry.get("title") or entry.get("domain") or entry.get("url") or f"来源 {index}"
            url = entry.get("url") or ""
            domain = entry.get("domain") or ""
            label = f"{title} ({domain})" if domain else title
            lines.append(f"{rank}. {label}")
            if url:
                lines.append(url)
        return lines

    @staticmethod
    def _render_weixin_source_entries(entries: list[dict[str, str]]) -> list[str]:
        lines = ["【来源】"]
        for index, entry in enumerate(entries, start=1):
            rank = entry.get("rank") or str(index)
            title = entry.get("title") or entry.get("domain") or entry.get("url") or f"来源 {index}"
            url = entry.get("url") or ""
            domain = entry.get("domain") or ""
            label = f"{title} ({domain})" if domain else title
            lines.append(f"{rank}. {label}")
            if url:
                lines.append(url)
        return lines

    @staticmethod
    def _humanize_error(error: str) -> str:
        normalized = str(error or "").strip()
        if not normalized:
            return "执行失败，请稍后重试。"

        match = _PROVIDER_ERROR_RE.match(normalized)
        if match:
            provider_name = match.group(1).strip()
            details = match.group(2).strip().lower()
            if any(
                token in details
                for token in [
                    "quota exceeded",
                    "resourceexhausted",
                    "resource_exhausted",
                    "insufficient_quota",
                    "current quota",
                    "billing details",
                    "free tier",
                    "freetier",
                    "当前额度已用尽",
                    "达到额度限制",
                ]
            ):
                return f"当前后端 `{provider_name}` 已达到额度限制。"
            if any(token in details for token in ["rate limit", "too many requests", "429"]):
                return f"当前后端 `{provider_name}` 请求过于频繁，请稍后再试。"
            return f"当前后端 `{provider_name}` 请求失败。"

        return normalized
