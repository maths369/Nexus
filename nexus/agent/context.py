"""
Context Builder — 上下文构建

职责:
1. 加载系统提示模板
2. 注入 bootstrap 文件
3. 注入记忆（Episodic Memory）
4. 注入语义检索结果（Retrieval Index）
5. 管理上下文大小，防止溢出
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 粗估 token 计数（中文约 1.5 token/字，英文约 1.3 token/word）
CHARS_PER_TOKEN_ESTIMATE = 2.0


def estimate_tokens(text: str) -> int:
    """粗估文本的 token 数"""
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """粗估消息列表的 token 数"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        total += 4  # role + formatting overhead
    return total


def truncate_messages(
    messages: list[dict[str, Any]],
    max_tokens: int,
    preserve_system: bool = True,
    preserve_last_n: int = 5,
) -> list[dict[str, Any]]:
    """
    截断消息列表以适应 token 限制。

    策略:
    1. 总是保留 system 消息（如果有）
    2. 总是保留最后 N 条消息
    3. 从中间开始删除最老的消息
    """
    if not messages:
        return messages

    current_tokens = estimate_messages_tokens(messages)
    if current_tokens <= max_tokens:
        return messages

    # 分离 system 消息和其他消息
    system_msgs = []
    other_msgs = []
    for msg in messages:
        if preserve_system and msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            other_msgs.append(msg)

    # 保留最后 N 条
    if len(other_msgs) <= preserve_last_n:
        return system_msgs + other_msgs

    # 保留首条（任务描述）和最后 N 条
    preserved = [other_msgs[0]] + other_msgs[-preserve_last_n:]

    # 检查是否在限制内
    result = system_msgs + preserved
    if estimate_messages_tokens(result) <= max_tokens:
        logger.info(
            f"Truncated {len(messages)} messages to {len(result)} "
            f"(~{estimate_messages_tokens(result)} tokens)"
        )
        return result

    # 如果仍然超限，只保留 system + 最后 N 条
    result = system_msgs + other_msgs[-preserve_last_n:]
    logger.warning(
        f"Aggressive truncation: {len(messages)} → {len(result)} messages"
    )
    return result


def load_prompt_template(
    template_name: str,
    prompts_dir: Path | None = None,
) -> str:
    """加载提示模板文件"""
    if prompts_dir is None:
        prompts_dir = Path(__file__).resolve().parents[2] / "config" / "prompts"

    template_path = prompts_dir / template_name
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    logger.warning(f"Prompt template not found: {template_path}")
    return ""
