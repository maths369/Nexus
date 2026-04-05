"""
Subagent — 子任务委派

参考: learn-claude-code s04_subagent.py

核心机制:
1. 父 Agent 通过 dispatch_subagent 工具派发子任务
2. 子 Agent 使用全新 messages=[]，独立 context
3. 子 Agent 共享文件系统和工具集（排除递归/压缩/进度工具）
4. 只返回 summary 给父 Agent，子 context 丢弃

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: subagent   | ---------->| execute_tool_loop|
    |   prompt="..."   |            |   call tools     |
    |                  |  summary   |   append results |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    Parent context stays clean.
    Subagent context is discarded.

核心洞见: "上下文隔离让父 Agent 保持干净。"
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TYPE_CHECKING

from .types import AttemptConfig, ToolDefinition

if TYPE_CHECKING:
    from nexus.channel.context_window import ContextWindowManager
    from nexus.channel.session_store import SessionStore
    from nexus.provider.gateway import ProviderGateway
    from .session_manager import SessionManager
    from .subagent_registry import SubagentRegistry
    from .tools_policy import ToolsPolicy

logger = logging.getLogger(__name__)

# 子 Agent 不允许使用的工具（防止递归和不必要的开销）
EXCLUDED_TOOLS = frozenset({
    "dispatch_subagent",  # 禁止递归生成子Agent
    "compact",            # 子Agent 寿命短，不需要压缩
    "todo_write",         # 进度由父Agent追踪
})

# 子 Agent 默认最大迭代次数
SUBAGENT_MAX_ITERATIONS = 20

# 子 Agent 最大嵌套深度（防止无限递归）
MAX_SUBAGENT_DEPTH = 3

DEFAULT_SUBAGENT_SYSTEM = (
    "你是一个子任务执行Agent。\n"
    "完成给定任务后，用简洁的文字总结你的发现和结果。\n"
    "只关注被分配的具体子任务，不要扩展范围。"
)


def _resolve_subagent_model(provider: ProviderGateway, model: str | None) -> str:
    configured = str(model or "").strip()
    if configured:
        return configured
    primary = getattr(provider, "primary_provider", None)
    candidate = str(getattr(primary, "model", None) or getattr(primary, "name", None) or "").strip()
    if candidate:
        return candidate
    return "qwen-max"


class SubagentRunner:
    """
    子 Agent 执行器。

    接收父 Agent 的 prompt，在隔离 context 中执行，只返回 summary。
    子 Agent 使用父 Agent 的 provider 和 tools_policy，但拥有独立的消息列表。
    """

    def __init__(
        self,
        provider: ProviderGateway,
        tools_policy: ToolsPolicy,
        model: str | None = None,
        system_prompt: str = "",
        max_iterations: int = SUBAGENT_MAX_ITERATIONS,
        depth: int = 0,
        session_store: SessionStore | None = None,
        session_manager: SessionManager | None = None,
        context_window: ContextWindowManager | None = None,
        registry: SubagentRegistry | None = None,
    ):
        self._provider = provider
        self._policy = tools_policy
        self._model = _resolve_subagent_model(provider, model)
        self._system_prompt = system_prompt or DEFAULT_SUBAGENT_SYSTEM
        self._max_iterations = max_iterations
        self._depth = depth
        self._dispatch_count = 0
        self._session_store = session_store
        self._session_manager = session_manager
        self._context_window = context_window
        self._registry = registry

    @property
    def stats(self) -> dict[str, int]:
        return {"dispatch_count": self._dispatch_count}

    async def dispatch(
        self,
        prompt: str,
        description: str = "",
        model: str | None = None,
        tools: list[ToolDefinition] | None = None,
        spawn_mode: str = "run",
        session_id: str | None = None,
        parent_session_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> str:
        """
        以全新 context 执行子任务，返回 summary。

        Args:
            prompt: 子任务指令
            description: 子任务简短描述（用于日志）
            model: 可选指定模型（默认使用构造时的模型）
            tools: 可用工具列表（自动过滤掉不允许的工具）

        Returns:
            子 Agent 的最终回复文本
        """
        # 深度检查
        child_depth = self._depth + 1
        if child_depth > MAX_SUBAGENT_DEPTH:
            msg = f"子代理嵌套深度超过限制 ({MAX_SUBAGENT_DEPTH})，拒绝执行。请简化任务拆分。"
            logger.warning(msg)
            return msg
        normalized_mode = str(spawn_mode or "run").strip().lower()
        if normalized_mode not in {"run", "session"}:
            return f"不支持的子代理模式: {spawn_mode}"

        # 过滤工具：排除不必要的工具
        excluded = set(EXCLUDED_TOOLS)
        # 在最后一层禁止再递归
        if child_depth >= MAX_SUBAGENT_DEPTH:
            excluded.add("dispatch_subagent")

        child_tools = [
            t for t in (tools or [])
            if t.name not in excluded
        ]
        if hasattr(self._policy, "filter_tools"):
            child_tools = self._policy.filter_tools(child_tools)

        # 如果子代理可以再派子代理，注入一个带深度跟踪的 dispatch handler
        if child_depth < MAX_SUBAGENT_DEPTH:
            for i, t in enumerate(child_tools):
                if t.name == "dispatch_subagent":
                    # 替换为带深度追踪的 handler
                    child_runner = SubagentRunner(
                        provider=self._provider,
                        tools_policy=self._policy,
                        model=self._model,
                        system_prompt=self._system_prompt,
                        max_iterations=self._max_iterations,
                        depth=child_depth,
                        session_store=self._session_store,
                        session_manager=self._session_manager,
                        context_window=self._context_window,
                        registry=self._registry,
                    )

                    async def _dispatch_with_depth(
                        prompt: str,
                        description: str = "",
                        spawn_mode: str = "run",
                        session_id: str | None = None,
                        _tool_context: dict[str, Any] | None = None,
                        _runner: SubagentRunner = child_runner,
                        _tools: list[ToolDefinition] = child_tools,
                    ) -> str:
                        return await _runner.dispatch(
                            prompt=prompt,
                            description=description,
                            tools=_tools,
                            spawn_mode=spawn_mode,
                            session_id=session_id,
                            parent_session_id=str((_tool_context or {}).get("session_id") or "") or None,
                            parent_run_id=str((_tool_context or {}).get("run_id") or "") or None,
                        )

                    child_tools[i] = ToolDefinition(
                        name=t.name,
                        description=t.description + f" (当前深度: {child_depth}/{MAX_SUBAGENT_DEPTH})",
                        parameters=t.parameters,
                        handler=_dispatch_with_depth,
                        risk_level=t.risk_level,
                        tags=t.tags,
                    )
                    break

        subagent_id = f"sub-d{child_depth}-{uuid.uuid4().hex[:8]}"
        desc_label = description or prompt[:60]
        logger.info(f"[{subagent_id}] Subagent dispatched: {desc_label}")
        if self._registry is not None:
            self._registry.register_spawn(
                run_id=subagent_id,
                prompt=prompt,
                description=description,
                spawn_mode=normalized_mode,
                model=model or self._model,
                depth=child_depth,
                session_id=session_id,
                parent_session_id=parent_session_id,
                parent_run_id=parent_run_id,
            )

        try:
            if normalized_mode == "session":
                summary = await self._dispatch_session_mode(
                    run_id=subagent_id,
                    prompt=prompt,
                    description=description,
                    model=model or self._model,
                    tools=child_tools,
                    session_id=session_id,
                    parent_session_id=parent_session_id,
                )
            else:
                summary = await self._dispatch_run_mode(
                    run_id=subagent_id,
                    prompt=prompt,
                    description=description,
                    model=model or self._model,
                    tools=child_tools,
                    parent_session_id=parent_session_id,
                )
            self._dispatch_count += 1
            logger.info(
                f"[{subagent_id}] Subagent completed, "
                f"summary length={len(summary)}"
            )
            return summary

        except Exception as e:
            logger.error(f"[{subagent_id}] Subagent failed: {e}", exc_info=True)
            self._dispatch_count += 1
            if self._registry is not None:
                self._registry.mark_failed(subagent_id, error=str(e), session_id=session_id)
            return f"子Agent执行失败: {e}"

    async def _dispatch_run_mode(
        self,
        *,
        run_id: str,
        prompt: str,
        description: str,
        model: str,
        tools: list[ToolDefinition],
        parent_session_id: str | None = None,
    ) -> str:
        from .core import execute_tool_loop

        if self._registry is not None:
            self._registry.mark_running(run_id, attempts=1)
        config = AttemptConfig(
            model=model,
            system_prompt=self._system_prompt,
            tools=tools,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=4096,
        )
        result_text, _events = await execute_tool_loop(
            config=config,
            provider=self._provider,
            tools_policy=self._policy,
            run_id=run_id,
            session_id=parent_session_id,
        )
        summary = result_text or "(子Agent未返回摘要)"
        if self._registry is not None:
            self._registry.mark_completed(run_id, result=summary)
        self._append_parent_notification(parent_session_id, run_id, summary, spawn_mode="run")
        return summary

    async def _dispatch_session_mode(
        self,
        *,
        run_id: str,
        prompt: str,
        description: str,
        model: str,
        tools: list[ToolDefinition],
        session_id: str | None,
        parent_session_id: str | None,
    ) -> str:
        from .core import execute_tool_loop

        if self._session_store is None or self._session_manager is None or self._context_window is None:
            raise RuntimeError("session 模式子代理需要 session_store/session_manager/context_window 支持")

        if session_id:
            session = self._session_store.get_session(session_id)
            if session is None:
                raise KeyError(f"Unknown subagent session: {session_id}")
        else:
            session = self._session_store.create_session(
                sender_id=f"subagent:{parent_session_id or 'system'}",
                channel="subagent",
                summary=(description or prompt[:80]).strip(),
            )
            session_id = session.session_id

        self._session_store.add_event(
            session_id=session_id,
            role="user",
            content=prompt,
            metadata={"kind": "subagent_prompt", "spawn_mode": "session"},
        )
        if self._registry is not None:
            self._registry.mark_running(run_id, attempts=1, session_id=session_id)

        result_holder: dict[str, str] = {}

        async def _run_session() -> None:
            context_messages = self._context_window.build_context(session_id)
            self._session_manager.capture_runtime_snapshot(
                session_id,
                provider_name=model,
                context_message_count=len(context_messages),
                tool_profile="subagent",
                route_hint="subagent_session",
            )
            config = AttemptConfig(
                model=model,
                system_prompt=self._system_prompt,
                tools=tools,
                messages=[{"role": "system", "content": self._system_prompt}, *context_messages],
                max_tokens=4096,
            )
            result_text, _events = await execute_tool_loop(
                config=config,
                provider=self._provider,
                tools_policy=self._policy,
                run_id=run_id,
                session_id=session_id,
            )
            summary = result_text or "(子Agent未返回摘要)"
            self._session_store.add_event(
                session_id=session_id,
                role="assistant",
                content=summary,
                metadata={"kind": "subagent_result", "spawn_mode": "session"},
            )
            result_holder["summary"] = summary

        accepted = await self._session_manager.enqueue_run(session_id, _run_session)
        if not accepted:
            return f"子Agent session `{session_id}` 当前忙碌中，请稍后重试。"

        summary = result_holder.get("summary", "(子Agent未返回摘要)")
        if self._registry is not None:
            self._registry.mark_completed(run_id, result=summary, session_id=session_id)
        self._append_parent_notification(
            parent_session_id,
            run_id,
            summary,
            spawn_mode="session",
            session_id=session_id,
        )
        return f"子Agent(session) `{session_id}` 已完成。\n{summary}"

    def _append_parent_notification(
        self,
        parent_session_id: str | None,
        run_id: str,
        summary: str,
        *,
        spawn_mode: str,
        session_id: str | None = None,
    ) -> None:
        if self._session_store is None or not parent_session_id:
            return
        content = f"[subagent:{run_id}] ({spawn_mode}) {summary}"
        if session_id:
            content = f"[subagent:{run_id}] ({spawn_mode}, session={session_id}) {summary}"
        try:
            self._session_store.add_event(
                session_id=parent_session_id,
                role="system",
                content=content,
                metadata={
                    "kind": "subagent_notification",
                    "subagent_run_id": run_id,
                    "subagent_session_id": session_id,
                    "spawn_mode": spawn_mode,
                },
            )
        except Exception:
            logger.debug("Failed to append parent subagent notification", exc_info=True)
