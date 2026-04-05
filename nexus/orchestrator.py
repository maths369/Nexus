"""
Orchestrator — Channel → Session → Run → Response 调度核心

串联 Channel Layer 和 Agent Core 的中枢。
负责:
1. 接收 InboundMessage → Session Router 路由
2. 根据路由决策创建/恢复 session、启动 run
3. 将 run 结果通过 MessageFormatter 回复给渠道
4. 并发管理：一个 session 内串行执行 run
"""

from __future__ import annotations

import asyncio
import logging
import uuid
import re
from pathlib import Path
from typing import Any, Callable, Awaitable
from urllib.parse import quote

from nexus.channel.types import (
    InboundMessage,
    MessageIntent,
    OutboundMessage,
    OutboundMessageType,
    RoutingDecision,
)
from nexus.channel.session_router import SessionRouter
from nexus.channel.session_store import SessionStore, SessionStatus
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.message_formatter import MessageFormatter
from nexus.agent.run import RunManager
from nexus.agent.session_manager import EnqueueResult, SessionManager
from nexus.agent.types import RunStatus
from nexus.agent.tool_profiles import ToolProfile
from nexus.provider.gateway import ProviderGateway, ProviderGatewayError
from nexus.shared.config import switch_primary_provider, switch_search_provider

logger = logging.getLogger(__name__)

# 回复发送回调类型: (OutboundMessage) -> None
ReplyFn = Callable[[OutboundMessage], Awaitable[None]]


class Orchestrator:
    """
    Channel → Agent 调度器。

    使用方式:
        orchestrator = Orchestrator(...)
        await orchestrator.handle_message(inbound_msg, reply_fn)
    """

    def __init__(
        self,
        session_router: SessionRouter,
        session_store: SessionStore,
        context_window: ContextWindowManager,
        run_manager: RunManager,
        formatter: MessageFormatter,
        provider_gateway: ProviderGateway | None = None,
        search_config: dict[str, Any] | None = None,
        config_path: Path | None = None,
        available_tools: list[Any] | None = None,
        skill_manager: Any | None = None,
        capability_manager: Any | None = None,
        task_router: Any | None = None,
        mesh_registry: Any | None = None,
        session_manager: SessionManager | None = None,
        memory_manager: Any | None = None,
        external_base_url: str | None = None,
    ):
        self._router = session_router
        self._sessions = session_store
        self._context = context_window
        self._run_manager = run_manager
        self._formatter = formatter
        self._provider_gateway = provider_gateway or getattr(run_manager, "_provider", None)
        self._search_config = search_config or {}
        self._config_path = Path(config_path).resolve() if config_path is not None else None
        self._available_tools = available_tools or []
        self._skill_manager = skill_manager
        self._capability_manager = capability_manager
        self._task_router = task_router
        self._mesh_registry = mesh_registry
        self._external_base_url = str(external_base_url or "").rstrip("/")
        self._memory_manager = memory_manager
        self._session_manager = session_manager or SessionManager(
            session_store=session_store,
            session_router=session_router,
        )

    async def handle_message(
        self,
        message: InboundMessage,
        reply: ReplyFn,
    ) -> None:
        """
        处理入站消息的完整流程。

        Channel Adapter 调用此方法即可，不用关心内部路由逻辑。
        """
        logger.info(
            "Orchestrator inbound: channel=%s sender=%s message_id=%s preview=%s",
            message.channel.value,
            message.sender_id,
            message.message_id,
            message.content[:200],
        )

        # Step 1: 路由决策
        decision = await self._router.route(message)
        logger.info(
            f"Routing decision: intent={decision.intent.value} "
            f"session={decision.session_id} confidence={decision.confidence:.2f}"
        )

        # Step 2: 根据意图分发
        handler = self._intent_handlers.get(decision.intent)
        if handler:
            await handler(self, message, decision, reply)
        else:
            await self._handle_new_task(message, decision, reply)

    # _handle_runtime_fact_query / _handle_control_plane_action 已移除。
    # 所有消息统一交给 LLM 理解意图，不再做关键词硬编码拦截。

    # ------------------------------------------------------------------
    # 意图处理器
    # ------------------------------------------------------------------

    async def _handle_new_task(
        self,
        message: InboundMessage,
        decision: RoutingDecision,
        reply: ReplyFn,
    ) -> None:
        """处理新任务意图 — 开启新 session，将旧的 active session 标记为 completed。"""
        channel_key = message.metadata.get("channel_key") or message.channel.value
        self._sessions.close_other_active_sessions(
            sender_id=message.sender_id,
            new_status=SessionStatus.COMPLETED,
        )
        session = self._sessions.create_session(
            sender_id=message.sender_id,
            channel=channel_key,
            summary=message.content[:100],
        )
        # 记录用户消息
        self._record_inbound_message(session.session_id, message)
        # 发送 ACK
        await reply(self._formatter.format_ack(
            session.session_id, message.content[:50]
        ))

        # 启动 run
        channel_name, group_id = self._resolve_policy_scope(message)
        await self._start_run(
            session.session_id,
            message.content,
            reply,
            route_hint=str(message.metadata.get("route_mode", "auto")),
            channel=channel_name,
            group_id=group_id,
        )

    async def _handle_follow_up(
        self,
        message: InboundMessage,
        decision: RoutingDecision,
        reply: ReplyFn,
    ) -> None:
        """处理跟进消息"""
        session_id = decision.session_id
        if not session_id:
            # 降级为新任务
            return await self._handle_new_task(message, decision, reply)

        # Re-activate session if it was completed/paused (persistent session model)
        session = self._sessions.get_session(session_id)
        if session and session.status != SessionStatus.ACTIVE:
            self._sessions.update_session_status(session_id, SessionStatus.ACTIVE)

        # 记录用户消息
        self._record_inbound_message(session_id, message)
        # 启动 run（带上下文）
        channel_name, group_id = self._resolve_policy_scope(message)
        await self._start_run(
            session_id,
            message.content,
            reply,
            route_hint=str(message.metadata.get("route_mode", "auto")),
            channel=channel_name,
            group_id=group_id,
        )

    async def _handle_resume(
        self,
        message: InboundMessage,
        decision: RoutingDecision,
        reply: ReplyFn,
    ) -> None:
        """处理恢复历史 session"""
        session_id = decision.session_id
        if not session_id:
            return await self._handle_new_task(message, decision, reply)

        # 重新激活 session
        self._sessions.update_session_status(
            session_id, SessionStatus.ACTIVE
        )
        session = self._sessions.get_session(session_id)
        if session is not None:
            self._sessions.close_other_active_sessions(
                sender_id=session.sender_id,
                keep_session_id=session_id,
                new_status=SessionStatus.ABANDONED,
            )
        self._record_inbound_message(session_id, message)
        channel_name, group_id = self._resolve_policy_scope(message)
        await self._start_run(
            session_id,
            message.content,
            reply,
            route_hint=str(message.metadata.get("route_mode", "auto")),
            channel=channel_name,
            group_id=group_id,
        )

    async def _handle_status_query(
        self,
        message: InboundMessage,
        decision: RoutingDecision,
        reply: ReplyFn,
    ) -> None:
        """处理状态查询"""
        session_id = decision.session_id
        if not session_id:
            await reply(self._formatter.format_result(
                session_id="",
                result="当前没有进行中的任务。",
            ))
            return

        session = self._sessions.get_session(session_id)
        status_text = "未知状态"
        if session:
            status_text = f"Session [{session.summary[:40]}] 状态: {session.status.value}"
        progress = ""
        if self._task_router is not None and session_id:
            try:
                plan = self._task_router.get_session_plan(session_id)
            except Exception:
                plan = None
            if plan is not None:
                progress = self._task_router.render_status(plan)

        await reply(self._formatter.format_status(
            session_id=session_id,
            status=status_text,
            progress=progress,
        ))

    async def _handle_command(
        self,
        message: InboundMessage,
        decision: RoutingDecision,
        reply: ReplyFn,
    ) -> None:
        """处理控制命令"""
        cmd = decision.reason.replace("command:", "").strip()
        session = self._sessions.get_active_session(
            sender_id=message.sender_id
        )
        session_id = session.session_id if session else ""
        action = str((decision.metadata or {}).get("action") or "").strip().lower()

        if action == "provider":
            await self._handle_provider_command(
                session_id=session_id,
                provider_command=str((decision.metadata or {}).get("provider_command") or "status"),
                target=str((decision.metadata or {}).get("target") or ""),
                reply=reply,
            )
            return

        if action == "search_provider":
            await self._handle_search_command(
                session_id=session_id,
                search_command=str((decision.metadata or {}).get("search_command") or "status"),
                target=str((decision.metadata or {}).get("target") or ""),
                reply=reply,
            )
            return

        if cmd == "pause" and session:
            self._sessions.update_session_status(
                session.session_id, SessionStatus.PAUSED
            )
            await reply(self._formatter.format_status(
                session_id=session.session_id,
                status="已暂停",
            ))
        elif cmd == "cancel" and session:
            self._sessions.update_session_status(
                session.session_id, SessionStatus.ABANDONED
            )
            await reply(self._formatter.format_status(
                session_id=session.session_id,
                status="已取消",
            ))
        elif cmd == "new":
            # 开始新对话：关闭当前 session，下条消息将创建新 session
            if session:
                self._sessions.update_session_status(
                    session.session_id, SessionStatus.COMPLETED
                )
            await reply(self._formatter.format_result(
                session_id=session_id,
                result="已结束当前对话，下一条消息将开始新任务。",
            ))
        elif cmd == "restart":
            # 重启 Hub 服务
            await reply(self._formatter.format_result(
                session_id=session_id,
                result="正在重启 Hub 服务，请稍候 10 秒后重试…",
            ))
            import asyncio, subprocess
            await asyncio.sleep(1)
            subprocess.Popen(
                ["systemctl", "--user", "restart", "nexus-api"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif cmd == "compress":
            # 用 LLM 压缩当前 session 的对话历史
            channel_key = message.metadata.get("channel_key") or message.channel.value
            target_session, _ = self._sessions.get_or_create_persistent_session(
                sender_id=message.sender_id,
                channel=channel_key,
            )
            await reply(self._formatter.format_result(
                session_id=target_session.session_id,
                result="正在压缩对话历史，请稍候…",
            ))
            summary = await self._context.compress_with_llm(
                session_id=target_session.session_id,
                provider=self._run_manager._provider,
            )
            await reply(self._formatter.format_result(
                session_id=target_session.session_id,
                result=f"✅ 对话已压缩。摘要:\n\n{summary}",
            ))
        elif cmd == "help":
            help_text = (
                "📋 Nexus 支持的命令:\n\n"
                "| 命令 | 中文别名 | 说明 |\n"
                "|---|---|---|\n"
                "| /new | 新对话、新任务、重新开始 | 结束当前对话，开始新任务 |\n"
                "| /pause | 暂停、停、停一下 | 暂停当前任务 |\n"
                "| /resume | 继续、恢复 | 恢复已暂停的任务 |\n"
                "| /cancel | 取消 | 取消当前任务 |\n"
                "| /status | 状态 | 查看当前任务状态 |\n"
                "| /compress | 压缩、压缩对话 | 用 LLM 压缩对话历史 |\n"
                "| /restart | 重启、重启服务 | 重启 Hub 服务 |\n"
                "| /provider | - | 查看当前后端与可切换列表 |\n"
                "| /provider gemini-3-flash-preview | - | 切换到指定已配置后端 |\n"
                "| /search | - | 查看当前搜索后端与兜底链路 |\n"
                "| /search google_grounded | - | 切换搜索后端 |\n"
                "| /help | 帮助、命令 | 显示本帮助信息 |\n"
            )
            await reply(self._formatter.format_result(
                session_id=session_id,
                result=help_text,
            ))
        elif cmd == "status":
            await self._handle_status_query(message, decision, reply)
        else:
            await reply(self._formatter.format_result(
                session_id=session_id,
                result=f"命令 '{cmd}' 已收到。",
            ))

    async def _handle_provider_command(
        self,
        *,
        session_id: str,
        provider_command: str,
        target: str,
        reply: ReplyFn,
    ) -> None:
        gateway = self._provider_gateway
        if gateway is None:
            await reply(self._formatter.format_error(
                session_id=session_id,
                error="当前运行时没有可切换的 provider 网关。",
            ))
            return

        providers = gateway.list_providers()
        current = gateway.primary_provider

        if provider_command != "switch" or not target:
            lines = [f"当前后端：`{current.name}` (`{current.model}`)"]
            lines.append("")
            lines.append("可切换后端：")
            for provider in providers:
                status = "已配置" if provider.resolved_api_key() else "缺少 API Key"
                marker = " (当前)" if provider.name == current.name else ""
                lines.append(f"- `{provider.name}`: `{provider.model}` [{status}]{marker}")
            lines.append("")
            lines.append("使用方式：`/provider gemini-3-flash-preview`")
            await reply(self._formatter.format_result(session_id=session_id, result="\n".join(lines)))
            return

        try:
            selected = gateway.get_provider(name=target)
        except ProviderGatewayError:
            available = ", ".join(f"`{provider.name}`" for provider in providers)
            await reply(self._formatter.format_clarify(
                session_id=session_id,
                question=f"未找到后端 `{target}`。请选择已配置后端。",
                options=[available] if available else ["先配置 provider"],
            ))
            return

        if not selected.resolved_api_key():
            await reply(self._formatter.format_error(
                session_id=session_id,
                error=f"后端 `{selected.name}` 已配置但当前运行环境缺少可用 API Key。",
            ))
            return

        gateway.switch_primary_provider(selected.name)
        self._run_manager.set_fallback_models([provider.model for provider in gateway.list_providers()])

        persisted = False
        if self._config_path is not None:
            try:
                switch_primary_provider(self._config_path, selected.name)
                persisted = True
            except Exception:
                logger.exception("Failed to persist primary provider switch: %s", selected.name)

        lines = [
            f"已切换当前后端到 `{selected.name}` (`{selected.model}`)。",
            f"持久化配置：{'已写入 config/app.yaml' if persisted else '未写入，仅本次运行有效'}",
        ]
        await reply(self._formatter.format_result(session_id=session_id, result="\n".join(lines)))

    async def _handle_search_command(
        self,
        *,
        session_id: str,
        search_command: str,
        target: str,
        reply: ReplyFn,
    ) -> None:
        search_settings = self._search_config
        provider_settings = search_settings.setdefault("provider", {})
        google_settings = search_settings.setdefault("google_grounded", {})
        supported = {
            "google_grounded": "Google grounded search",
            "bing": "Bing 结果页抽取",
            "duckduckgo": "DuckDuckGo 结果页抽取",
        }
        aliases = {
            "google": "google_grounded",
            "grounded": "google_grounded",
            "ddg": "duckduckgo",
        }

        current = str(provider_settings.get("primary") or "google_grounded").strip() or "google_grounded"
        fallback_chain = [
            str(item).strip()
            for item in (provider_settings.get("fallbacks") or [])
            if str(item).strip()
        ]

        if search_command != "switch" or not target:
            lines = [f"当前搜索后端：`{current}`"]
            if fallback_chain:
                lines.append(f"兜底链路：`{' -> '.join(fallback_chain)}`")
            lines.append("")
            lines.append("可切换搜索后端：")
            for name, description in supported.items():
                status = "可用"
                if name == "google_grounded" and not google_settings.get("api_key"):
                    status = "缺少 GEMINI_API_KEY"
                marker = " (当前)" if name == current else ""
                lines.append(f"- `{name}`: {description} [{status}]{marker}")
            lines.append("")
            lines.append("使用方式：`/search google_grounded`")
            await reply(self._formatter.format_result(session_id=session_id, result="\n".join(lines)))
            return

        selected = aliases.get(target.strip().lower(), target.strip().lower())
        if selected not in supported:
            await reply(self._formatter.format_clarify(
                session_id=session_id,
                question=f"未找到搜索后端 `{target}`。请选择已支持的搜索后端。",
                options=["google_grounded", "bing", "duckduckgo"],
            ))
            return

        if selected == "google_grounded" and not google_settings.get("api_key"):
            await reply(self._formatter.format_error(
                session_id=session_id,
                error="搜索后端 `google_grounded` 需要 GEMINI_API_KEY，但当前运行环境未配置。",
            ))
            return

        provider_settings["primary"] = selected
        persisted = False
        if self._config_path is not None:
            try:
                switch_search_provider(self._config_path, selected)
                persisted = True
            except Exception:
                logger.exception("Failed to persist search provider switch: %s", selected)

        lines = [
            f"已切换当前搜索后端到 `{selected}` ({supported[selected]})。",
            f"持久化配置：{'已写入 config/app.yaml' if persisted else '未写入，仅本次运行有效'}",
        ]
        if fallback_chain:
            lines.append(f"当前兜底链路仍为：`{' -> '.join(fallback_chain)}`")
        await reply(self._formatter.format_result(session_id=session_id, result="\n".join(lines)))

    async def _handle_unknown(
        self,
        message: InboundMessage,
        decision: RoutingDecision,
        reply: ReplyFn,
    ) -> None:
        """处理无法确定意图的情况"""
        candidates = decision.metadata.get("candidates", []) if decision.metadata else []
        options = []
        for candidate in candidates:
            summary = str(candidate.get("summary") or "未命名任务")
            status = str(candidate.get("status") or "unknown")
            options.append(f"{summary}（{status}）")
        await reply(self._formatter.format_clarify(
            session_id=decision.session_id or "",
            question="我不太确定你要继续哪个任务，请确认。",
            options=options or [
                "开始一个新任务",
                "继续之前的任务",
                "查看任务状态",
            ],
        ))

    # 意图 → 处理器映射
    _intent_handlers: dict[MessageIntent, Any] = {
        MessageIntent.NEW_TASK: _handle_new_task,
        MessageIntent.FOLLOW_UP: _handle_follow_up,
        MessageIntent.RESUME: _handle_resume,
        MessageIntent.STATUS_QUERY: _handle_status_query,
        MessageIntent.COMMAND: _handle_command,
        MessageIntent.UNKNOWN: _handle_unknown,
    }

    # ------------------------------------------------------------------
    # Run 执行
    # ------------------------------------------------------------------

    # 编码相关关键词 — 命中任意一个即使用 coding profile
    _CODING_KEYWORDS = frozenset({
        "代码", "code", "编程", "脚本", "script", "函数", "function",
        "修复", "fix", "bug", "debug", "重构", "refactor",
        "文件", "file", "目录", "directory", "路径", "path",
        "安装", "install", "pip", "npm", "依赖", "dependency",
        "编译", "build", "运行", "run", "执行", "execute",
        "git", "commit", "push", "pull", "branch",
        "测试", "test", "部署", "deploy",
        "技能", "skill", "进化", "evolve", "创建技能",
        "grep", "搜索代码", "search", "查找文件",
    })

    def _select_tool_profile(self, task: str) -> ToolProfile | None:
        """
        根据任务内容选择 Tool Profile。

        策略：
        - 任务包含编码相关关键词 → coding profile
        - 否则 → None（使用全量工具集，由 LLM 自行决策）

        注意：
        - Tool Profile 在运行时会作为工具白名单参与过滤，不只是提示。
        - coding profile 需要保留 dispatch_subagent，避免编码/文件类任务无法委派子代理。
        """
        task_lower = task.lower()
        for keyword in self._CODING_KEYWORDS:
            if keyword in task_lower:
                logger.debug("Task matches coding keyword '%s', using coding profile", keyword)
                return ToolProfile.coding()
        return None

    async def _start_run(
        self,
        session_id: str,
        task: str,
        reply: ReplyFn,
        *,
        route_hint: str = "auto",
        channel: str | None = None,
        group_id: str | None = None,
    ) -> None:
        """启动一个 run 并将结果通过 reply 回调返回"""
        async def _execute_run() -> None:
            try:
                logger.info(
                    "Run start requested: session_id=%s task_preview=%s",
                    session_id,
                    task[:200],
                )
                # 构建上下文
                context_messages = self._context.build_context(session_id)
                effective_task = self._augment_task_with_recent_artifacts(session_id, task)
                extra_tools = None
                disabled_tool_names = None

                if self._task_router is not None:
                    routing = await self._task_router.prepare_run(
                        session_id=session_id,
                        task=effective_task,
                        context_messages=context_messages,
                        route_hint=route_hint,
                    )
                    self._sessions.update_session_metadata(
                        session_id,
                        {
                            "mesh_plan": routing.plan.to_dict(),
                            "mesh_plan_state": routing.plan.state.value,
                        },
                    )
                    local_node_id = str(routing.plan.metadata.get("local_node_id") or "")
                    if routing.status_message and routing.plan.remote_nodes(local_node_id):
                        await reply(self._formatter.format_status(
                            session_id=session_id,
                            status="已生成跨节点执行计划",
                            progress=routing.status_message,
                        ))
                    if routing.blocked_reason:
                        self._sessions.update_session_status(
                            session_id,
                            SessionStatus.PAUSED,
                        )
                        self._sessions.add_event(
                            session_id=session_id,
                            role="system",
                            content=f"[mesh_blocked] {routing.blocked_reason}",
                            metadata={"mesh_plan": routing.plan.to_dict()},
                        )
                        await reply(self._formatter.format_blocked(
                            session_id=session_id,
                            reason=routing.blocked_reason,
                        ))
                        return
                    effective_task = routing.effective_task
                    extra_tools = routing.extra_tools
                    disabled_tool_names = routing.disabled_local_tools
                    self._task_router.mark_session_plan_running(session_id)
                    self._sessions.update_session_metadata(
                        session_id,
                        {
                            "mesh_plan": routing.plan.to_dict(),
                            "mesh_plan_state": routing.plan.state.value,
                        },
                    )

                # 流式回调：将 LLM 的输出实时推给用户
                async def stream_to_reply(chunk: str) -> None:
                    # 对于飞书，流式推送暂不实现（飞书不支持流式消息）
                    # 对于 Web，通过 WebSocket 推送
                    pass

                # 根据任务内容选择 Tool Profile
                tool_profile = self._select_tool_profile(effective_task)
                provider_name = None
                if self._provider_gateway is not None:
                    provider_name = str(self._provider_gateway.primary_provider.model or self._provider_gateway.primary_provider.name)
                tool_names: list[str] = []
                if extra_tools:
                    tool_names.extend(
                        str(getattr(tool, "name", "")).strip()
                        for tool in extra_tools
                        if str(getattr(tool, "name", "")).strip()
                    )
                self._session_manager.capture_runtime_snapshot(
                    session_id,
                    provider_name=provider_name,
                    context_message_count=len(context_messages),
                    tool_profile=tool_profile.name if tool_profile is not None else None,
                    tool_names=tool_names,
                    route_hint=route_hint,
                    extra={
                        "disabled_tool_count": len(disabled_tool_names or []),
                    },
                )

                # 执行 run
                run = await self._run_manager.execute(
                    session_id=session_id,
                    task=effective_task,
                    context_messages=context_messages,
                    stream_callback=stream_to_reply,
                    extra_tools=extra_tools,
                    disabled_tool_names=disabled_tool_names,
                    tool_profile=tool_profile,
                    channel=channel,
                    group_id=group_id,
                )
                logger.info(
                    "Run finished: session_id=%s run_id=%s status=%s error=%s",
                    session_id,
                    run.run_id,
                    run.status.value,
                    run.error,
                )

                # LLM-driven routing: 不再 force-dispatch。
                # LLM 在 agent loop 中自行决定是否调用 mesh_dispatch 工具。

                if self._task_router is not None:
                    self._task_router.mark_session_plan_finished(
                        session_id,
                        success=(run.status == RunStatus.SUCCEEDED),
                    )
                    plan = self._task_router.get_session_plan(session_id)
                    if plan is not None:
                        self._sessions.update_session_metadata(
                            session_id,
                            {
                                "mesh_plan": plan.to_dict(),
                                "mesh_plan_state": plan.state.value,
                            },
                        )

                # ── 追踪 vault 写入产出物到 session artifacts ──
                vault_artifacts = run.metadata.get("vault_write_artifacts")
                if isinstance(vault_artifacts, list) and vault_artifacts:
                    try:
                        normalized = [
                            {
                                "artifact_type": "vault_output",
                                "filename": item.get("title") or item.get("relative_path", ""),
                                "relative_path": item.get("relative_path", ""),
                                "page_relative_path": item.get("relative_path", ""),
                            }
                            for item in vault_artifacts
                            if isinstance(item, dict) and item.get("relative_path")
                        ]
                        if normalized:
                            self._sessions.append_recent_artifacts(session_id, normalized)
                    except Exception:
                        logger.warning(
                            "Failed to persist vault write artifacts for session %s",
                            session_id,
                            exc_info=True,
                        )

                # 记录 assistant 回复到 session
                if run.result:
                    self._sessions.add_event(
                        session_id=session_id,
                        role="assistant",
                        content=run.result,
                    )

                # 发送结果
                if run.status == RunStatus.SUCCEEDED:
                    # 保持 session ACTIVE，允许用户多轮对话延续上下文。
                    # Session 的结束由 freshness 超时或用户显式 "新任务" 命令控制，
                    # 而非每次 run 完成后立即关闭（借鉴 Claude Code 的持续累积模型）。
                    self._sessions.touch_session(session_id)
                    result_artifacts = self._build_result_artifacts(run.metadata.get("vault_write_artifacts"))
                    await reply(self._formatter.format_result(
                        session_id=session_id,
                        result=run.result,
                        artifacts=result_artifacts,
                    ))
                    # 后处理工作全部 fire-and-forget，不阻塞 worker 释放。
                    # 这样用户发送下一条消息时 worker.running 已经是 False，
                    # 不会被误判为"任务正在执行中"。
                    asyncio.create_task(
                        self._post_run_background(session_id)
                    )
                else:
                    self._sessions.update_session_status(
                        session_id,
                        SessionStatus.PAUSED,
                    )
                    await reply(self._formatter.format_error(
                        session_id=session_id,
                        error=self._format_run_failure_error(run),
                    ))

            except Exception as e:
                self._sessions.update_session_status(
                    session_id,
                    SessionStatus.PAUSED,
                )
                logger.error(
                    f"Run execution failed for session {session_id}: {e}",
                    exc_info=True,
                )
                await reply(self._formatter.format_error(
                    session_id=session_id,
                    error=str(e),
                ))

        result, future = await self._session_manager.enqueue_run(session_id, _execute_run)
        if result == EnqueueResult.FULL:
            await reply(self._formatter.format_blocked(
                session_id=session_id,
                reason='当前队列已满，请稍后重试或发送 /new 开始新对话。',
            ))
            return
        if result == EnqueueResult.QUEUED:
            await reply(self._formatter.format_queued(
                session_id=session_id,
                position=self._session_manager.get_queue_depth(session_id),
            ))
        if future is not None:
            await future

    async def _promote_completed_session_memory(self, session_id: str) -> None:
        if self._memory_manager is None:
            return
        session = self._sessions.get_session(session_id)
        if session is None:
            return
        if not str(session.channel or "").startswith("feishu"):
            return
        try:
            result = await self._memory_manager.promote_session_to_medical_knowledge(session_id=session_id)
        except Exception:
            logger.warning("Failed to promote medical knowledge for session %s", session_id, exc_info=True)
            return
        if result.get("promoted"):
            logger.info(
                "Medical knowledge promoted for session %s: l2=%s l3=%s l4=%s conflicts=%s",
                session_id,
                result.get("l2_saved", 0),
                result.get("l3_written", 0),
                result.get("l4_written", 0),
                result.get("conflicts", 0),
            )

    async def _post_run_background(self, session_id: str) -> None:
        """Run 成功后的后台处理（fire-and-forget）。

        包含知识提取和标题生成，均为非关键路径，
        失败不影响用户体验，不阻塞下一条消息的接受。
        """
        try:
            await self._promote_completed_session_memory(session_id)
        except Exception:
            logger.debug(
                "Post-run memory promotion failed for %s (non-critical)",
                session_id[:8], exc_info=True,
            )
        try:
            await self._generate_session_title(session_id)
        except Exception:
            logger.debug(
                "Post-run title generation failed for %s (non-critical)",
                session_id[:8], exc_info=True,
            )

    async def _generate_session_title(self, session_id: str) -> None:
        """异步生成会话标题，不阻塞主流程，失败时静默降级。"""
        if self._provider_gateway is None:
            return
        try:
            from nexus.channel.session_title import generate_session_title
            events = self._sessions.get_events(session_id)
            if not events:
                return
            event_dicts = [
                {"role": e.role, "content": e.content} for e in events
            ]
            title = await generate_session_title(
                self._provider_gateway, event_dicts,
            )
            if title:
                self._sessions.update_session_summary(session_id, title)
                logger.info(
                    "Session %s title generated: %s", session_id[:8], title,
                )
        except Exception:
            logger.debug(
                "Session title generation failed for %s (non-critical)",
                session_id[:8], exc_info=True,
            )

    @staticmethod
    def _resolve_policy_scope(message: InboundMessage) -> tuple[str, str | None]:
        channel = message.channel.value
        metadata = message.metadata or {}
        chat_id = str(metadata.get("chat_id") or "").strip()
        if chat_id:
            return channel, chat_id
        channel_key = str(metadata.get("channel_key") or "").strip()
        if ":" in channel_key:
            return channel, channel_key.split(":", 1)[1] or None
        return channel, None

    def _format_run_failure_error(self, run: Any) -> str:
        base_error = str(getattr(run, "error", "") or "执行失败，请重试。").strip()
        attempt_models = [
            str(item).strip()
            for item in ((getattr(run, "metadata", {}) or {}).get("attempt_models") or [])
            if str(item).strip()
        ]
        if len(attempt_models) <= 1:
            return base_error

        humanized = (
            self._formatter._humanize_error(base_error)
            if hasattr(self._formatter, "_humanize_error")
            else base_error
        )
        chain = " -> ".join(f"`{model}`" for model in attempt_models)
        return f"本次请求已依次尝试 {chain}。{humanized}"

    async def _force_dispatch_undispatched_steps(
        self,
        session_id: str,
        run: Any,
        reply: ReplyFn,
    ) -> None:
        """Fallback for when the LLM generates text instead of calling
        mesh_dispatch tools.

        After a successful run, we inspect the TaskRouter plan for
        agent-loop steps that were assigned to edge nodes.  If the run
        event metadata does not contain evidence that a successful
        ``mesh_dispatch__*`` tool invocation already happened for a
        given step, we fire the dispatch programmatically.
        """
        task_router = self._task_router
        if task_router is None:
            return

        plan = task_router.get_session_plan(session_id)
        if plan is None:
            return

        agent_loop_steps = task_router.get_agent_loop_steps(plan)
        if not agent_loop_steps:
            return

        remote_proxy = getattr(task_router, "_remote_proxy", None)
        if remote_proxy is None:
            return

        successful_dispatches = self._successful_mesh_dispatches(run)
        steps_by_node: dict[str, list[Any]] = {}
        for step in agent_loop_steps:
            node_id = step.assigned_node or ""
            if node_id:
                steps_by_node.setdefault(node_id, []).append(step)

        for step in agent_loop_steps:
            node_id = step.assigned_node or ""
            if not node_id:
                continue

            if self._step_has_successful_dispatch(
                step=step,
                remote_proxy=remote_proxy,
                successful_dispatches=successful_dispatches,
                node_steps=steps_by_node.get(node_id, []),
            ):
                continue

            # The LLM did not dispatch this step — force-execute it.
            logger.warning(
                "Force-dispatching undispatched agent-loop step %s to %s "
                "(session=%s, run=%s). The LLM did not call mesh_dispatch.",
                step.step_id,
                node_id,
                session_id,
                run.run_id,
            )
            try:
                # Build a callback so that when the edge node completes,
                # the result is automatically pushed back to the EventSource
                # (Feishu / Desktop / etc.)
                async def _on_task_event(event, _reply=reply, _sid=session_id):
                    if event.event_type == "completed":
                        result_text = event.metadata.get("result") or event.content
                        await _reply(self._formatter.format_result(
                            session_id=_sid,
                            result=result_text,
                        ))
                        logger.info(
                            "Async task result pushed back to EventSource: "
                            "session=%s task=%s",
                            _sid, event.task_id,
                        )
                    elif event.event_type == "failed":
                        error_text = event.metadata.get("error") or event.content
                        await _reply(self._formatter.format_error(
                            session_id=_sid,
                            error=f"边缘节点执行失败: {error_text}",
                        ))
                    elif event.event_type == "timed_out":
                        await _reply(self._formatter.format_error(
                            session_id=_sid,
                            error=f"边缘节点执行超时: {event.content}",
                        ))

                dispatch_result = await remote_proxy.dispatch_to_edge(
                    target_node=node_id,
                    task_description=step.description,
                    session_id=session_id,
                    source_type="fallback",
                    source_id=run.run_id,
                    on_event=_on_task_event,
                )
                logger.info(
                    "Force-dispatch succeeded for step %s -> %s: %s",
                    step.step_id,
                    node_id,
                    dispatch_result[:200],
                )
                dispatch_record = {
                    "tool": remote_proxy.dispatch_alias_for(node_id),
                    "task_description": step.description,
                }
                successful_dispatches.append(dispatch_record)
                self._remember_successful_mesh_dispatch(run, dispatch_record)

                # Append the dispatch result to the run result so the user
                # sees that the task was actually sent.
                run.result = (run.result or "").rstrip() + "\n\n" + dispatch_result

                # Notify the user that the dispatch happened as fallback.
                await reply(self._formatter.format_status(
                    session_id=session_id,
                    status="已自动派发任务到边缘节点",
                    progress=dispatch_result,
                ))
            except Exception as exc:
                logger.error(
                    "Force-dispatch failed for step %s -> %s: %s",
                    step.step_id,
                    node_id,
                    exc,
                    exc_info=True,
                )

    @staticmethod
    def _normalize_dispatch_task_text(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    @staticmethod
    def _successful_mesh_dispatches(run: Any) -> list[dict[str, str]]:
        metadata = getattr(run, "metadata", {})
        raw = metadata.get("successful_mesh_dispatches", []) if isinstance(metadata, dict) else []
        dispatches: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool") or "").strip()
            if not tool_name:
                continue
            dispatches.append(
                {
                    "tool": tool_name,
                    "task_description": str(item.get("task_description") or ""),
                }
            )
        return dispatches

    def _step_has_successful_dispatch(
        self,
        *,
        step: Any,
        remote_proxy: Any,
        successful_dispatches: list[dict[str, str]],
        node_steps: list[Any],
    ) -> bool:
        node_id = step.assigned_node or ""
        if not node_id:
            return False

        dispatch_alias = remote_proxy.dispatch_alias_for(node_id)
        dispatches_for_node = [
            item for item in successful_dispatches
            if item.get("tool") == dispatch_alias
        ]
        if not dispatches_for_node:
            return False

        normalized_step = self._normalize_dispatch_task_text(step.description)
        if any(
            self._normalize_dispatch_task_text(item.get("task_description", "")) == normalized_step
            for item in dispatches_for_node
        ):
            return True

        return len(node_steps) == 1

    @staticmethod
    def _remember_successful_mesh_dispatch(run: Any, dispatch_record: dict[str, str]) -> None:
        metadata = getattr(run, "metadata", None)
        if not isinstance(metadata, dict):
            return

        raw = metadata.setdefault("successful_mesh_dispatches", [])
        if not isinstance(raw, list):
            raw = []
            metadata["successful_mesh_dispatches"] = raw
        if dispatch_record not in raw:
            raw.append(dispatch_record)

    def _augment_task_with_recent_artifacts(
        self,
        session_id: str,
        task: str,
    ) -> str:
        recent_artifacts = self._sessions.get_recent_artifacts(session_id, limit=3)
        if not recent_artifacts:
            return task
        # 始终注入最近 artifacts 引用，不再依赖关键词门控。
        # 这样无论用户怎么措辞，LLM 都能看到最近处理过的文件。
        lines = [task.strip(), "", "[最近附件/产出物引用]"]
        for item in recent_artifacts:
            parts = [
                f"- {item.get('artifact_type', 'file')} `{item.get('filename') or '未命名附件'}`",
            ]
            relative_path = str(item.get("relative_path") or "").strip()
            page_relative_path = str(item.get("page_relative_path") or "").strip()
            transcript_relative_path = str(item.get("transcript_relative_path") or "").strip()
            if relative_path:
                parts.append(f"原始文件 `{relative_path}`")
            if page_relative_path:
                parts.append(f"知识页 `{page_relative_path}`")
            if transcript_relative_path:
                parts.append(f"转录 `{transcript_relative_path}`")
            lines.append("，".join(parts))
        lines.append("以上是本会话最近处理的附件和产出物。若用户提到相关文件,优先指代这些路径,不要再次索要文件路径。")
        return "\n".join(lines).strip()

    def _build_result_artifacts(self, artifacts: Any) -> list[dict[str, str]]:
        if not isinstance(artifacts, list) or not artifacts:
            return []
        results: list[dict[str, str]] = []
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            relative_path = str(item.get("relative_path") or "").strip()
            if not relative_path:
                continue
            page_url = ""
            if self._external_base_url:
                page_url = f"{self._external_base_url}/?path={quote(relative_path)}&mode=read"
            results.append(
                {
                    "name": str(item.get("title") or relative_path),
                    "path": page_url or relative_path,
                }
            )
        return results

    def _record_inbound_message(
        self,
        session_id: str,
        message: InboundMessage,
    ) -> None:
        event_metadata: dict[str, Any] = {}
        if message.attachments:
            event_metadata["attachments"] = [dict(item) for item in message.attachments]
            try:
                self._sessions.append_recent_artifacts(
                    session_id,
                    [dict(item) for item in message.attachments if isinstance(item, dict)],
                )
            except Exception:
                logger.warning(
                    "Failed to persist recent artifacts for session %s",
                    session_id,
                    exc_info=True,
                )
        self._sessions.add_event(
            session_id=session_id,
            role="user",
            content=message.content,
            metadata=event_metadata or None,
        )
