"""
Session Title Generator — 用轻量模型自动生成会话标题

借鉴 Claude Code 的 sessionTitle.ts:
- 从对话尾部提取文本
- 用最便宜的模型生成 3-10 词标题
- 异步执行，不阻塞主流程
- 失败时静默降级
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.provider.gateway import ProviderGateway

logger = logging.getLogger(__name__)

# 从对话尾部提取的最大字符数
_MAX_CONVERSATION_CHARS = 2000

# 标题生成 prompt
_TITLE_PROMPT = (
    "为以下对话生成一个简短的中文标题。\n"
    "要求:\n"
    "- 3 到 10 个词\n"
    "- 概括对话的核心话题\n"
    "- 不要加引号或标点\n"
    "- 不要用「关于」「讨论」等无意义前缀\n"
    "- 好的例子: 飞书消息适配器重构, Vault知识库索引优化, PDF解析能力安装\n"
    "- 不好的例子: 关于代码的讨论, 帮助用户, 对话总结\n\n"
    "只输出标题本身，不要任何解释。\n\n"
    "--- 对话内容 ---\n"
    "{conversation}\n"
    "--- 结束 ---"
)


def _extract_conversation_tail(
    events: list[dict[str, Any]],
    max_chars: int = _MAX_CONVERSATION_CHARS,
) -> str:
    """从对话事件中提取尾部文本用于标题生成。"""
    parts: list[str] = []
    total = 0
    # 从后往前收集
    for event in reversed(events):
        role = event.get("role", "")
        content = event.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "system":
            continue
        # 截断单条消息
        text = content[:500] if len(content) > 500 else content
        parts.append(f"[{role}] {text}")
        total += len(text)
        if total >= max_chars:
            break
    parts.reverse()
    return "\n".join(parts)


async def generate_session_title(
    provider: ProviderGateway,
    events: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> str | None:
    """
    用轻量模型生成会话标题。

    Args:
        provider: LLM provider gateway
        events: 会话事件列表 (role + content dicts)
        model: 指定模型，默认用 provider 的 primary model

    Returns:
        生成的标题字符串，失败时返回 None
    """
    conversation = _extract_conversation_tail(events)
    if not conversation.strip():
        return None

    prompt = _TITLE_PROMPT.format(conversation=conversation)

    try:
        response = await provider.chat_completion(
            model=model or provider.primary_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0.3,
        )
        title = (response.get("message", {}).get("content", "") or "").strip()
        # 清理: 去掉引号、句号等
        title = title.strip("\"'""''《》「」。.!！")
        if not title or len(title) > 50:
            return None
        return title
    except Exception as e:
        logger.debug("Session title generation failed (non-critical): %s", e)
        return None
