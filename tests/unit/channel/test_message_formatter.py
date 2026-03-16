from nexus.channel.message_formatter import MessageFormatter
from nexus.channel.types import OutboundMessageType


def test_formatter_can_render_clarify_options():
    formatter = MessageFormatter()

    message = formatter.format_clarify(
        "sess-1",
        "你是想继续哪个任务？",
        options=["OpenClaw 架构分析", "今天的会议纪要"],
    )

    assert message.message_type == OutboundMessageType.CLARIFY
    assert "1. OpenClaw 架构分析" in message.content
    assert "2. 今天的会议纪要" in message.content
