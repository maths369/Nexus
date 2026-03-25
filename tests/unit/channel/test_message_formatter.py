from nexus.channel.message_formatter import MessageFormatter
from nexus.channel.types import ChannelType, OutboundMessage, OutboundMessageType


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


def test_formatter_renders_markdown_table_for_mobile_channels():
    formatter = MessageFormatter()
    outbound = OutboundMessage(
        session_id="sess-1",
        message_type=OutboundMessageType.RESULT,
        content=(
            "📋 Nexus 支持的命令:\n\n"
            "| 命令 | 中文别名 | 说明 |\n"
            "|---|---|---|\n"
            "| /new | 新对话、新任务、重新开始 | 结束当前对话，开始新任务 |\n"
            "| /help | 帮助、命令 | 显示本帮助信息 |\n"
        ),
    )

    rendered = formatter.render_for_channel(ChannelType.WEIXIN, outbound)

    assert "| 命令 |" not in rendered
    assert "- /new：新对话、新任务、重新开始；结束当前对话，开始新任务" in rendered
    assert "- /help：帮助、命令；显示本帮助信息" in rendered


def test_formatter_strips_internal_hub_prefix():
    formatter = MessageFormatter()
    outbound = OutboundMessage(
        session_id="sess-1",
        message_type=OutboundMessageType.RESULT,
        content="[hub:ubuntu:2352929]\n## 标题\n\n一条结果",
    )

    rendered = formatter.render_for_channel(ChannelType.FEISHU, outbound)

    assert "[hub:ubuntu:2352929]" not in rendered
    assert "【标题】" in rendered
    assert "一条结果" in rendered


def test_formatter_renders_feishu_card():
    formatter = MessageFormatter()
    outbound = OutboundMessage(
        session_id="sess-1",
        message_type=OutboundMessageType.RESULT,
        content=(
            "[hub:ubuntu:2352929]\n"
            "## 执行结果\n\n"
            "| 命令 | 说明 |\n"
            "|---|---|\n"
            "| /help | 显示帮助 |\n"
            "| /status | 查看状态 |\n"
        ),
    )

    card = formatter.render_feishu_card(outbound)

    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "Nexus · 结果"
    assert len(card["body"]["elements"]) >= 1
    assert all(element["tag"] == "markdown" for element in card["body"]["elements"])

    body = "\n".join(element["content"] for element in card["body"]["elements"])
    assert "[hub:ubuntu:2352929]" not in body
    assert "**执行结果**" in body
    assert "| 命令 |" in body


def test_formatter_renders_help_card_without_raw_table():
    formatter = MessageFormatter()
    outbound = OutboundMessage(
        session_id="sess-1",
        message_type=OutboundMessageType.RESULT,
        content=(
            "📋 Nexus 支持的命令:\n\n"
            "| 命令 | 中文别名 | 说明 |\n"
            "|---|---|---|\n"
            "| /new | 新对话、新任务、重新开始 | 结束当前对话，开始新任务 |\n"
            "| /help | 帮助、命令 | 显示本帮助信息 |\n"
        ),
    )

    card = formatter.render_feishu_card(outbound)
    body = "\n".join(element["content"] for element in card["body"]["elements"])

    assert card["header"]["title"]["content"] == "Nexus · 命令帮助"
    assert card["header"]["template"] == "blue"
    assert "| 命令 |" not in body
    assert "- `/new`：结束当前对话，开始新任务" in body
    assert "别名：新对话、新任务、重新开始" in body


def test_formatter_renders_status_card_sections():
    formatter = MessageFormatter()
    outbound = OutboundMessage(
        session_id="sess-1",
        message_type=OutboundMessageType.STATUS,
        content="当前状态：Session [测试任务] 状态: active\n\n1. 收集上下文\n2. 输出结果",
    )

    card = formatter.render_feishu_card(outbound)
    body = "\n".join(element["content"] for element in card["body"]["elements"])

    assert card["header"]["title"]["content"] == "Nexus · 状态更新"
    assert "**当前状态**" in body
    assert "Session [测试任务] 状态: active" in body
    assert "**进度详情**" in body
    assert "1. 收集上下文" in body


def test_formatter_renders_weixin_sources_as_mobile_friendly_block():
    formatter = MessageFormatter()
    outbound = OutboundMessage(
        session_id="sess-1",
        message_type=OutboundMessageType.RESULT,
        content=(
            "西班牙赢得了 2024 欧洲杯。\n\n"
            "Sources:\n"
            "1. Olympics — https://www.olympics.com/en/news/euro-2024-final\n"
            "2. Wikipedia — https://en.wikipedia.org/wiki/UEFA_Euro_2024_final\n"
        ),
    )

    rendered = formatter.render_for_channel(ChannelType.WEIXIN, outbound)

    assert "Sources:" not in rendered
    assert "【来源】" in rendered
    assert "1. Olympics (olympics.com)" in rendered
    assert "https://www.olympics.com/en/news/euro-2024-final" in rendered
    assert "2. Wikipedia (en.wikipedia.org)" in rendered
    assert "https://en.wikipedia.org/wiki/UEFA_Euro_2024_final" in rendered


def test_formatter_renders_feishu_sources_as_clickable_links():
    formatter = MessageFormatter()
    outbound = OutboundMessage(
        session_id="sess-1",
        message_type=OutboundMessageType.RESULT,
        content=(
            "西班牙赢得了 2024 欧洲杯。\n\n"
            "Sources:\n"
            "1. Olympics — https://www.olympics.com/en/news/euro-2024-final\n"
            "2. Wikipedia — https://en.wikipedia.org/wiki/UEFA_Euro_2024_final\n"
        ),
    )

    card = formatter.render_feishu_card(outbound)
    body = "\n".join(element["content"] for element in card["body"]["elements"])

    assert "Sources:" not in body
    assert "**来源**" in body
    assert "[Olympics](https://www.olympics.com/en/news/euro-2024-final) (olympics.com)" in body
    assert "[Wikipedia](https://en.wikipedia.org/wiki/UEFA_Euro_2024_final) (en.wikipedia.org)" in body


def test_formatter_humanizes_provider_quota_errors():
    formatter = MessageFormatter()

    message = formatter.format_error(
        "sess-1",
        "Provider request failed (gemini-2.5-pro): Error code: 429 - {'error': {'message': 'You exceeded your current quota, please check your plan and billing details.', 'status': 'RESOURCEEXHAUSTED'}}",
    )

    assert "当前后端 `gemini-2.5-pro` 已达到额度限制。" in message.content
    assert "发送 `/provider` 切换到其它已配置后端" in message.content
