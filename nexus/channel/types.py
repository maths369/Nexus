"""Channel Layer 数据类型定义"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# 消息意图分类
# ---------------------------------------------------------------------------

class MessageIntent(str, enum.Enum):
    """Session Router 分类结果"""
    NEW_TASK = "new_task"           # 全新任务请求
    FOLLOW_UP = "follow_up"        # 对当前活跃 session 的跟进
    RESUME = "resume"              # 对历史 session 的追问
    STATUS_QUERY = "status_query"  # 状态查询（"上次那个怎么样了"）
    COMMAND = "command"            # 控制命令（"停一下" / "重新开始"）
    UNKNOWN = "unknown"            # 无法确定，需要澄清


# ---------------------------------------------------------------------------
# 渠道类型
# ---------------------------------------------------------------------------

class ChannelType(str, enum.Enum):
    FEISHU = "feishu"
    WEB = "web"


# ---------------------------------------------------------------------------
# 入站消息（从渠道进入系统的标准化消息）
# ---------------------------------------------------------------------------

@dataclass
class InboundMessage:
    """从渠道适配器标准化后的消息"""
    message_id: str
    channel: ChannelType
    sender_id: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    # 渠道特有元数据（如飞书的 chat_id、message_type 等）
    metadata: dict[str, Any] = field(default_factory=dict)
    # 附件（音频文件、图片等）
    attachments: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 出站消息（从系统返回给渠道的消息）
# ---------------------------------------------------------------------------

class OutboundMessageType(str, enum.Enum):
    """回复消息类型"""
    ACK = "ack"               # 接收确认
    STATUS = "status"         # 中间状态更新
    BLOCKED = "blocked"       # 阻塞提示（需要审批等）
    RESULT = "result"         # 最终结果
    CLARIFY = "clarify"       # 需要用户澄清
    ERROR = "error"           # 错误通知


@dataclass
class OutboundMessage:
    """返回给渠道的消息"""
    session_id: str
    message_type: OutboundMessageType
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 路由结果
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    """Session Router 的路由决策"""
    intent: MessageIntent
    session_id: str | None = None   # 目标 session（None 表示需要新建）
    confidence: float = 1.0
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
