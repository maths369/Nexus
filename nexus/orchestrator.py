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
from typing import Any, Callable, Awaitable

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
from nexus.agent.types import RunStatus

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
        available_tools: list[Any] | None = None,
        skill_manager: Any | None = None,
        capability_manager: Any | None = None,
        task_router: Any | None = None,
        mesh_registry: Any | None = None,
    ):
        self._router = session_router
        self._sessions = session_store
        self._context = context_window
        self._run_manager = run_manager
        self._formatter = formatter
        self._available_tools = available_tools or []
        self._skill_manager = skill_manager
        self._capability_manager = capability_manager
        self._task_router = task_router
        self._mesh_registry = mesh_registry
        # 防止同一 session 并发执行多个 run
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def handle_message(
        self,
        message: InboundMessage,
        reply: ReplyFn,
    ) -> None:
        """
        处理入站消息的完整流程。

        Channel Adapter 调用此方法即可，不用关心内部路由逻辑。
        """
        if await self._handle_control_plane_action(message, reply):
            return
        if await self._handle_runtime_fact_query(message, reply):
            return

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

    async def _handle_runtime_fact_query(
        self,
        message: InboundMessage,
        reply: ReplyFn,
    ) -> bool:
        """
        对极少数“系统真相”问题做确定性回答，避免被会话历史污染。

        这里只处理 runtime capability / self-evolution 能力盘点，
        不扩散到普通任务型问题。
        """
        is_mesh_query = self._is_mesh_inventory_query(message.content)
        is_evolution_query = self._is_self_evolution_query(message.content)
        if not is_mesh_query and not is_evolution_query:
            return False

        tool_names = {tool.name for tool in self._available_tools}
        capability_tools = [
            name for name in (
                "capability_list_available",
                "capability_status",
                "capability_enable",
                "capability_create",
                "capability_register",
                "capability_stage",
                "capability_verify",
                "capability_promote",
                "capability_rollback",
            )
            if name in tool_names
        ]
        skill_tools = [
            name for name in (
                "skill_list_installable",
                "skill_install",
                "skill_list_installed",
                "skill_create",
                "skill_update",
                "load_skill",
            )
            if name in tool_names
        ]
        audit_tools = [
            name for name in ("evolution_audit",) if name in tool_names
        ]

        installed_skills: list[str] = []
        installable_skills: list[dict[str, Any]] = []
        if self._skill_manager is not None:
            try:
                installed_skills = sorted(
                    skill["skill_id"] for skill in self._skill_manager.list_skills()
                )
                installable_skills = list(self._skill_manager.list_installable_skills())
            except Exception:
                installed_skills = []
                installable_skills = []

        capabilities: list[dict[str, Any]] = []
        if self._capability_manager is not None:
            try:
                capabilities = list(self._capability_manager.list_capabilities())
            except Exception:
                capabilities = []

        sections: list[str] = []

        if is_mesh_query:
            sections.append(self._render_mesh_inventory(tool_names))

        if is_evolution_query:
            if not capability_tools and not skill_tools and not audit_tools:
                sections.append("根据当前真实运行时，我现在没有注入自我进化相关工具。")
            else:
                sections.append(
                    self._render_self_evolution_inventory(
                        tool_names=tool_names,
                        capability_tools=capability_tools,
                        skill_tools=skill_tools,
                        audit_tools=audit_tools,
                        capabilities=capabilities,
                        installable_skills=installable_skills,
                        installed_skills=installed_skills,
                    )
                )

        content = "\n\n---\n\n".join(section for section in sections if section.strip())

        active = self._sessions.get_active_session(message.sender_id)
        session_id = active.session_id if active else ""
        if session_id:
            self._sessions.add_event(session_id=session_id, role="user", content=message.content)
            self._sessions.add_event(session_id=session_id, role="assistant", content=content)
        await reply(self._formatter.format_result(session_id=session_id, result=content))
        return True

    def _render_mesh_inventory(self, tool_names: set[str]) -> str:
        if self._mesh_registry is None:
            return "当前未初始化 Mesh registry，所以我现在无法给出可靠的节点与能力盘点。"

        try:
            cards = list(self._mesh_registry.list_nodes(online_only=False))
        except Exception as exc:
            return f"Mesh registry 当前不可用：{exc}"

        if not cards:
            return "当前 Mesh 网络中还没有已注册节点。"

        online_count = 0
        lines = [
            "## Mesh 网络当前真实状态",
            "",
            f"当前 Mesh 注册表中共有 **{len(cards)}** 个节点。",
        ]
        for card in cards:
            status = self._mesh_registry.get_node_status(card.node_id)
            online = bool(status.online) if status else False
            if online:
                online_count += 1
            capability_ids = sorted(card.capability_ids())
            capabilities = "、".join(capability_ids) if capability_ids else "无"
            lines.append(
                f"- **{card.display_name}** (`{card.node_id}` · {card.node_type.value} · {'online' if online else 'offline'})"
            )
            lines.append(f"  capabilities: {capabilities}")

        lines.insert(3, f"其中在线节点 **{online_count}** 个，离线节点 **{len(cards) - online_count}** 个。")

        online_edges = [
            card for card in cards
            if getattr(card, "node_type", None) is not None
            and card.node_type.value == "edge"
            and bool((self._mesh_registry.get_node_status(card.node_id).online) if self._mesh_registry.get_node_status(card.node_id) else False)
        ]
        if online_edges:
            node_names = "、".join(f"{card.display_name}(`{card.node_id}`)" for card in online_edges)
            lines.extend([
                "",
                f"当前可调度的在线边缘节点：{node_names}",
                "说明：`mesh_dispatch__*` 不是常驻基础工具，而是 TaskRouter 在识别到跨节点步骤时按在线 edge 节点动态注入。",
            ])
        else:
            lines.extend([
                "",
                "当前没有在线的 edge 节点可供跨节点委托。",
            ])
        if any(name.startswith("mesh_dispatch__") for name in tool_names):
            lines.append("当前这轮运行里已经注入了 `mesh_dispatch__*` 工具。")
        return "\n".join(lines)

    def _render_self_evolution_inventory(
        self,
        *,
        tool_names: set[str],
        capability_tools: list[str],
        skill_tools: list[str],
        audit_tools: list[str],
        capabilities: list[dict[str, Any]],
        installable_skills: list[dict[str, Any]],
        installed_skills: list[str],
    ) -> str:
        lines = [
            "是的，我具备受控的自我进化能力。",
            "",
            "当前真实运行时里已注入的自我进化相关工具：",
        ]
        if capability_tools:
            lines.append("")
            lines.append("正式 capability（兼容层）：")
            for name in capability_tools:
                lines.append(f"- `{name}`")
        if skill_tools:
            lines.append("")
            lines.append("受管 Skill 扩展：")
            for name in skill_tools:
                lines.append(f"- `{name}`")
        if audit_tools:
            lines.append("")
            lines.append("审计：")
            for name in audit_tools:
                lines.append(f"- `{name}`")
        if "system_run" in tool_names:
            lines.append("")
            lines.append("底层 substrate：")
            lines.append("- `system_run`")
            lines.append("")
            lines.append("说明：长期扩展优先走 `skill_list_installable -> skill_install -> load_skill`；`system_run` 只负责底层安装、脚本执行和验证，不代表正式能力本身。")
        if capabilities:
            lines.append("")
            lines.append("当前可用的正式 capability（兼容层）：")
            for item in capabilities:
                status = "已启用" if item.get("enabled") else "未启用"
                lines.append(
                    f"- `{item.get('capability_id')}`: {item.get('name')}（{status}）"
                )
        if installable_skills:
            lines.append("")
            lines.append(f"当前可安装 Skills（{len(installable_skills)} 个）：")
            preview_installable = installable_skills[:8]
            for item in preview_installable:
                status = "已安装" if item.get("installed") else "未安装"
                lines.append(
                    f"- `{item.get('skill_id')}`: {item.get('description', '')}（{status}）"
                )
            if len(installable_skills) > len(preview_installable):
                lines.append(f"- 以及其他 {len(installable_skills) - len(preview_installable)} 个")
        if installed_skills:
            lines.append("")
            lines.append(
                f"当前已安装 Skills（{len(installed_skills)} 个）："
            )
            preview = installed_skills[:8]
            for skill_id in preview:
                lines.append(f"- `{skill_id}`")
            if len(installed_skills) > len(preview):
                lines.append(f"- 以及其他 {len(installed_skills) - len(preview)} 个")
        lines.append("")
        lines.append(
            "边界：这是受控自我进化，不是任意安装任意软件；正式扩展对象优先是受管 Skill，capability 目前只保留兼容层。"
        )
        return "\n".join(lines)

    async def _handle_control_plane_action(
        self,
        message: InboundMessage,
        reply: ReplyFn,
    ) -> bool:
        installable = self._match_installable_skill_request(message.content)
        if installable is not None and self._skill_manager is not None:
            result = await self._skill_manager.install_from_catalog(installable["skill_id"], actor="agent")
            content = self._render_skill_install_result(installable, result)
            await reply(self._formatter.format_result(session_id="", result=content))
            return True

        capability = self._match_enable_capability_request(message.content)
        if capability is not None and self._capability_manager is not None:
            result = await self._capability_manager.enable(capability["capability_id"], actor="agent")
            content = self._render_capability_enable_result(capability, result)
            await reply(self._formatter.format_result(session_id="", result=content))
            return True

        return False

    def _match_installable_skill_request(self, text: str) -> dict[str, Any] | None:
        if self._skill_manager is None:
            return None
        compact = text.strip().lower()
        if not compact:
            return None
        if not any(token in compact for token in ("安装", "install", "装上")):
            return None
        specs = list(self._skill_manager.list_installable_skills())
        for item in specs:
            skill_id = str(item.get("skill_id") or "").lower()
            name = str(item.get("name") or "").lower()
            if skill_id and skill_id in compact:
                return item
            if name and name in compact:
                return item
        return None

    def _match_enable_capability_request(self, text: str) -> dict[str, Any] | None:
        if self._capability_manager is None:
            return None
        compact = text.strip().lower()
        if not compact:
            return None
        if not any(token in compact for token in ("启用", "enable", "开通", "开启")):
            return None
        capabilities = list(self._capability_manager.list_capabilities())
        for item in capabilities:
            capability_id = str(item.get("capability_id") or "").lower()
            name = str(item.get("name") or "").lower()
            if capability_id and capability_id in compact:
                return item
            if name and name in compact:
                return item
        return None

    @staticmethod
    def _render_skill_install_result(skill: dict[str, Any], result: dict[str, Any]) -> str:
        success = bool(result.get("success"))
        status = "已安装成功" if success else "安装失败"
        reason = str(result.get("reason") or "")
        return "\n".join([
            f"Skill `{skill.get('skill_id')}` {status}。",
            *([""] if reason else []),
            *([reason] if reason else []),
        ])

    @staticmethod
    def _render_capability_enable_result(capability: dict[str, Any], result: Any) -> str:
        success = bool(getattr(result, "success", False))
        reason = str(getattr(result, "reason", "") or "")
        status = "已启用成功" if success else "启用失败"
        return "\n".join([
            f"Capability `{capability.get('capability_id')}` {status}。",
            *([""] if reason else []),
            *([reason] if reason else []),
        ])

    @staticmethod
    def _is_self_evolution_query(text: str) -> bool:
        compact = re.sub(r"\s+", "", text.strip().lower())
        if not compact:
            return False
        if "自我进化" in compact:
            return True
        if "capability" in compact:
            return True
        if "技能包" in compact or "扩展包" in compact:
            return True
        if "skill" in compact and any(token in compact for token in ("工具", "能力", "安装", "进化")):
            return True
        return False

    @staticmethod
    def _is_mesh_inventory_query(text: str) -> bool:
        compact = re.sub(r"\s+", "", text.strip().lower())
        if not compact:
            return False
        mesh_tokens = ("mesh", "节点", "设备", "macbook", "ubuntuhub", "hub", "edge")
        action_tokens = ("几个", "多少", "有哪些", "能力", "能干什么", "在线", "离线", "控制下")
        return any(token in compact for token in mesh_tokens) and any(token in compact for token in action_tokens)

    # ------------------------------------------------------------------
    # 意图处理器
    # ------------------------------------------------------------------

    async def _handle_new_task(
        self,
        message: InboundMessage,
        decision: RoutingDecision,
        reply: ReplyFn,
    ) -> None:
        """处理新任务意图"""
        # 创建新 session
        session = self._sessions.create_session(
            sender_id=message.sender_id,
            channel=message.channel.value,
            summary=message.content[:100],
        )
        self._sessions.close_other_active_sessions(
            sender_id=message.sender_id,
            keep_session_id=session.session_id,
            new_status=SessionStatus.ABANDONED,
        )
        # 记录用户消息
        self._record_inbound_message(session.session_id, message)
        # 发送 ACK
        await reply(self._formatter.format_ack(
            session.session_id, message.content[:50]
        ))

        # 启动 run
        await self._start_run(session.session_id, message.content, reply)

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

        # 记录用户消息
        self._record_inbound_message(session_id, message)
        # 启动 run（带上下文）
        await self._start_run(session_id, message.content, reply)

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
        await self._start_run(session_id, message.content, reply)

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
        elif cmd == "status":
            await self._handle_status_query(message, decision, reply)
        else:
            await reply(self._formatter.format_result(
                session_id=session_id,
                result=f"命令 '{cmd}' 已收到。",
            ))

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

    async def _start_run(
        self,
        session_id: str,
        task: str,
        reply: ReplyFn,
    ) -> None:
        """启动一个 run 并将结果通过 reply 回调返回"""
        # 获取 session 锁，防止并发
        lock = self._session_locks.setdefault(
            session_id, asyncio.Lock()
        )

        if lock.locked():
            await reply(self._formatter.format_blocked(
                session_id=session_id,
                reason='当前有一个任务正在执行中，请等待完成或发送“取消”终止。',
            ))
            return

        async with lock:
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

                # 执行 run
                run = await self._run_manager.execute(
                    session_id=session_id,
                    task=effective_task,
                    context_messages=context_messages,
                    stream_callback=stream_to_reply,
                    extra_tools=extra_tools,
                    disabled_tool_names=disabled_tool_names,
                )
                logger.info(
                    "Run finished: session_id=%s run_id=%s status=%s error=%s",
                    session_id,
                    run.run_id,
                    run.status.value,
                    run.error,
                )

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

                # 记录 assistant 回复到 session
                if run.result:
                    self._sessions.add_event(
                        session_id=session_id,
                        role="assistant",
                        content=run.result,
                    )

                # 发送结果
                if run.status == RunStatus.SUCCEEDED:
                    self._sessions.update_session_status(
                        session_id,
                        SessionStatus.COMPLETED,
                    )
                    await reply(self._formatter.format_result(
                        session_id=session_id,
                        result=run.result,
                    ))
                else:
                    self._sessions.update_session_status(
                        session_id,
                        SessionStatus.PAUSED,
                    )
                    await reply(self._formatter.format_error(
                        session_id=session_id,
                        error=run.error or "执行失败，请重试。",
                    ))

            except Exception as e:
                logger.error(
                    f"Run execution failed for session {session_id}: {e}",
                    exc_info=True,
                )
                await reply(self._formatter.format_error(
                    session_id=session_id,
                    error=str(e),
                ))

    def _augment_task_with_recent_artifacts(
        self,
        session_id: str,
        task: str,
    ) -> str:
        recent_artifacts = self._sessions.get_recent_artifacts(session_id, limit=3)
        if not recent_artifacts:
            return task
        compact = re.sub(r"\s+", "", task.strip().lower())
        markers = (
            "上传",
            "附件",
            "这个pdf",
            "这个文件",
            "这份pdf",
            "这份文件",
            "刚上传",
            "刚发的",
            "管理在vault",
        )
        if not any(marker in compact for marker in markers):
            return task
        lines = [task.strip(), "", "[最近附件引用]"]
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
        lines.append("若用户提到“刚上传的文件/这个PDF/附件”，默认指向以上最近附件，不要再次索要文件路径。")
        return "\n".join(lines).strip()

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
