"""
Agent Core — 工具调用循环

职责:
1. LLM 调用 → 解析 tool_calls → 治理检查 → 执行 → 回传结果
2. 循环直到 LLM 不再请求工具调用或达到最大迭代次数

参考: OpenClaw attempt.ts 中的执行循环
迁移来源: macos-ai-assistant/orchestrator/services/agent.py (execute_with_tools)
"""

from __future__ import annotations

import logging
import time
import uuid
import json
import inspect
from typing import Any, TYPE_CHECKING

from .types import (
    AttemptConfig,
    ContextOverflowError,
    ProviderError,
    RunEvent,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from .context import estimate_messages_tokens
from .tool_loop_detection import (
    LoopDetectionConfig,
    LoopDetectionState,
    LoopSeverity,
    detect_tool_call_loop,
    record_tool_call,
    record_tool_call_outcome,
    should_emit_loop_warning,
)

if TYPE_CHECKING:
    from nexus.provider.gateway import ProviderGateway
    from .tools_policy import ToolsPolicy
    from .background import BackgroundTaskManager
    from .compressor import ContextCompressor
    from .todo import TodoManager
    from .approval import ApprovalEngine

logger = logging.getLogger(__name__)

# 单次 run 中工具调用的最大迭代次数，防止无限循环
MAX_TOOL_ITERATIONS = 30

# 上下文 token 上限（粗估），超过此值抛出 ContextOverflowError
CONTEXT_TOKEN_LIMIT = 120_000


async def execute_tool_loop(
    config: AttemptConfig,
    provider: ProviderGateway,
    tools_policy: ToolsPolicy,
    run_id: str,
    compressor: ContextCompressor | None = None,
    todo_manager: TodoManager | None = None,
    background_manager: BackgroundTaskManager | None = None,
    loop_detection_config: LoopDetectionConfig | None = None,
    approval_engine: ApprovalEngine | None = None,
    session_id: str | None = None,
    channel: str | None = None,
) -> tuple[str, list[RunEvent]]:
    """
    核心工具调用循环。

    流程:
      0. 排空后台任务通知（如果有 background_manager）
      1. 压缩上下文（如果有 compressor）
      2. 发送 messages + tools 给 LLM
      3. 如果 LLM 返回 tool_calls:
         a. Tool loop 检测（参考 OpenClaw tool-loop-detection）
         b. 对每个 tool_call 执行治理检查
         c. 处理特殊工具（compact）
         d. 执行通过检查的工具
         e. 记录 outcome 到检测器
         f. 追踪 todo 使用，必要时注入提醒
         g. 继续循环
      4. 如果 LLM 返回纯文本或 stop，结束循环

    Args:
        config: 本次执行的配置（模型、系统提示、工具集、消息历史）
        provider: LLM 提供者网关
        tools_policy: 工具治理策略
        run_id: 当前 Run ID
        compressor: 可选的上下文压缩器（三层压缩）
        todo_manager: 可选的 Todo 管理器（进度追踪 + 提醒）
        background_manager: 可选的后台任务管理器
        loop_detection_config: 可选的 loop 检测配置

    Returns:
        (final_text, events): 最终回复文本和执行事件列表
    """
    messages = list(config.messages)
    events: list[RunEvent] = []
    iteration = 0

    # 构建工具 schema 列表（OpenAI function calling 格式）
    tool_schemas = _build_tool_schemas(config.tools)
    tool_map = {t.name: t for t in config.tools}

    # Tool loop 检测状态（对标 OpenClaw ToolLoopDetectionState）
    loop_state = LoopDetectionState()
    loop_config = loop_detection_config or LoopDetectionConfig()

    while iteration < MAX_TOOL_ITERATIONS:
        iteration += 1
        logger.debug(f"[{run_id}] Tool loop iteration {iteration}")

        # 后台任务通知注入: 在 LLM 调用前排空已完成的后台任务结果
        if background_manager:
            notifs = await background_manager.drain_notifications()
            if notifs:
                notif_text = background_manager.format_notifications(notifs)
                messages.append({"role": "user", "content": notif_text})
                messages.append({
                    "role": "assistant",
                    "content": "收到后台任务结果。",
                })
                logger.debug(f"[{run_id}] Injected {len(notifs)} background notifications")

        # Layer 1+2 压缩: 在每轮 LLM 调用前执行
        if compressor:
            messages = await compressor.compress_before_call(
                messages,
                run_id=run_id,
                session_id=session_id,
            )

        # 上下文溢出检查（压缩后再检查）
        token_estimate = estimate_messages_tokens(messages)
        if token_estimate > CONTEXT_TOKEN_LIMIT:
            raise ContextOverflowError(
                f"Context size ~{token_estimate} tokens exceeds limit "
                f"{CONTEXT_TOKEN_LIMIT} at iteration {iteration}"
            )

        # Step 1: 调用 LLM
        try:
            response = await provider.chat_completion(
                model=config.model,
                messages=messages,
                tools=tool_schemas if tool_schemas else None,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                stream_callback=config.stream_callback,
            )
        except ContextOverflowError:
            raise
        except Exception as exc:
            # 检查是否是 context length 相关错误
            err_msg = str(exc).lower()
            if any(k in err_msg for k in [
                "context length", "token limit", "too many tokens",
                "maximum context", "context_length_exceeded",
            ]):
                raise ContextOverflowError(str(exc)) from exc
            raise ProviderError(str(exc)) from exc

        # Step 2: 解析响应
        assistant_message = response.get("message", {})
        tool_calls_raw = assistant_message.get("tool_calls", [])
        content = assistant_message.get("content", "")

        # 记录 LLM 响应事件
        events.append(RunEvent(
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            event_type="llm_response",
            data={
                "iteration": iteration,
                "has_tool_calls": bool(tool_calls_raw),
                "content_length": len(content or ""),
                "model": config.model,
            },
        ))

        # 将 assistant 消息追加到上下文
        messages.append(_serialize_assistant_message(assistant_message))

        # Step 3: 如果没有工具调用，循环结束
        if not tool_calls_raw:
            logger.info(f"[{run_id}] Tool loop ended at iteration {iteration} (no tool calls)")
            return content or "", events

        # Step 4: 解析并执行每个工具调用
        parsed_calls = _parse_tool_calls(tool_calls_raw)
        compact_triggered = False
        used_todo = False

        for tc in parsed_calls:
            events.append(RunEvent(
                event_id=str(uuid.uuid4()),
                run_id=run_id,
                event_type="tool_call",
                data={
                    "call_id": tc.call_id,
                    "tool": tc.tool_name,
                    "arguments": tc.arguments,
                    "iteration": iteration,
                },
            ))
            # --- 特殊处理: compact 工具（Layer 3 手动压缩）---
            if tc.tool_name == "compact" and compressor:
                focus = tc.arguments.get("focus", "")
                messages = await compressor.manual_compact(
                    messages,
                    focus=focus,
                    run_id=run_id,
                    session_id=session_id,
                )
                compact_triggered = True
                events.append(RunEvent(
                    event_id=str(uuid.uuid4()),
                    run_id=run_id,
                    event_type="context_compacted",
                    data={"focus": focus, "trigger": "manual"},
                ))
                logger.info(f"[{run_id}] Manual compact triggered, focus='{focus[:60]}'")
                continue  # 跳过正常工具执行

            # --- Tool loop 检测（对标 OpenClaw before-tool-call hook）---
            loop_result = detect_tool_call_loop(
                loop_state, tc.tool_name, tc.arguments, loop_config,
            )
            if loop_result.stuck and loop_result.severity == LoopSeverity.CRITICAL:
                # Critical → 阻断执行
                result = ToolResult(
                    call_id=tc.call_id,
                    tool_name=tc.tool_name,
                    success=False,
                    output="",
                    error=loop_result.message,
                )
                events.append(RunEvent(
                    event_id=str(uuid.uuid4()),
                    run_id=run_id,
                    event_type="tool_loop_blocked",
                    data={
                        "tool": tc.tool_name,
                        "kind": loop_result.kind.value if loop_result.kind else "",
                        "count": loop_result.count,
                    },
                ))
                logger.warning(
                    f"[{run_id}] Tool loop BLOCKED ({loop_result.kind}): "
                    f"{tc.tool_name} count={loop_result.count}"
                )
                # 记录调用（即使被阻断也要记录，维护滑动窗口）
                record_tool_call(loop_state, tc.tool_name, tc.arguments, tc.call_id, loop_config)
                record_tool_call_outcome(
                    loop_state, tc.call_id, tc.tool_name, tc.arguments,
                    error=loop_result.message,
                )
            else:
                # Warning → 允许执行但记录警告
                if loop_result.stuck and loop_result.severity == LoopSeverity.WARNING:
                    if should_emit_loop_warning(loop_state, loop_result):
                        events.append(RunEvent(
                            event_id=str(uuid.uuid4()),
                            run_id=run_id,
                            event_type="tool_loop_warning",
                            data={
                                "tool": tc.tool_name,
                                "kind": loop_result.kind.value if loop_result.kind else "",
                                "count": loop_result.count,
                                "message": loop_result.message,
                            },
                        ))
                        logger.warning(
                            f"[{run_id}] Tool loop WARNING ({loop_result.kind}): "
                            f"{tc.tool_name} count={loop_result.count}"
                        )

                # 记录调用到滑动窗口
                record_tool_call(loop_state, tc.tool_name, tc.arguments, tc.call_id, loop_config)

                # 治理检查 + 执行
                tool_def = tool_map.get(tc.tool_name)
                if not tool_def:
                    result = ToolResult(
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        success=False,
                        output="",
                        error=f"Unknown tool: {tc.tool_name}",
                    )
                else:
                    if hasattr(tools_policy, "check_runtime"):
                        policy_result = await tools_policy.check_runtime(tc, tool_def)
                    else:
                        policy_result = await tools_policy.check(tc, tool_def)
                    if not policy_result.allowed:
                        # 如果需要审批且有审批引擎，走审批流程
                        if policy_result.requires_approval and approval_engine:
                            events.append(RunEvent(
                                event_id=str(uuid.uuid4()),
                                run_id=run_id,
                                event_type="approval_requested",
                                data={
                                    "tool": tc.tool_name,
                                    "risk_level": tool_def.risk_level.value,
                                },
                            ))
                            approval = await approval_engine.request_approval(
                                tc, tool_def, run_id,
                                session_id=session_id,
                                channel=channel,
                            )
                            if approval.approved:
                                # 审批通过，执行工具
                                events.append(RunEvent(
                                    event_id=str(uuid.uuid4()),
                                    run_id=run_id,
                                    event_type="approval_granted",
                                    data={
                                        "tool": tc.tool_name,
                                        "approval_id": approval.approval_id,
                                    },
                                ))
                                result = await _execute_tool(
                                    tc,
                                    tool_def,
                                    tool_context={
                                        "run_id": run_id,
                                        "session_id": session_id,
                                        "channel": channel,
                                        "iteration": iteration,
                                        "tool_name": tc.tool_name,
                                    },
                                )
                            else:
                                # 审批拒绝
                                result = ToolResult(
                                    call_id=tc.call_id,
                                    tool_name=tc.tool_name,
                                    success=False,
                                    output="",
                                    error=f"工具被用户拒绝: {approval.comment or '未通过审批'}",
                                )
                                events.append(RunEvent(
                                    event_id=str(uuid.uuid4()),
                                    run_id=run_id,
                                    event_type="approval_rejected",
                                    data={
                                        "tool": tc.tool_name,
                                        "approval_id": approval.approval_id,
                                        "reason": approval.comment,
                                    },
                                ))
                        else:
                            # 无审批引擎或不需要审批，直接阻断
                            result = ToolResult(
                                call_id=tc.call_id,
                                tool_name=tc.tool_name,
                                success=False,
                                output="",
                                error=f"Tool blocked by policy: {policy_result.reason}",
                            )
                            events.append(RunEvent(
                                event_id=str(uuid.uuid4()),
                                run_id=run_id,
                                event_type="tool_blocked",
                                data={
                                    "tool": tc.tool_name,
                                    "reason": policy_result.reason,
                                },
                            ))
                    else:
                        # 执行工具
                        result = await _execute_tool(
                            tc,
                            tool_def,
                            tool_context={
                                "run_id": run_id,
                                "session_id": session_id,
                                "channel": channel,
                                "iteration": iteration,
                                "tool_name": tc.tool_name,
                            },
                        )

                # 记录 outcome 到检测器
                record_tool_call_outcome(
                    loop_state, tc.call_id, tc.tool_name, tc.arguments,
                    output=result.output if result.success else None,
                    error=result.error if not result.success else None,
                )

            # 追踪 todo 使用
            if tc.tool_name == "todo_write":
                used_todo = True

            # 记录工具结果事件
            events.append(RunEvent(
                event_id=str(uuid.uuid4()),
                run_id=run_id,
                event_type="tool_result",
                data={
                    "call_id": tc.call_id,
                    "tool": tc.tool_name,
                    "success": result.success,
                    "duration_ms": result.duration_ms,
                    "error": result.error,
                },
            ))

            # 将工具结果追加到消息
            messages.append({
                "role": "tool",
                "tool_call_id": tc.call_id,
                "content": result.output if result.success else f"Error: {result.error}",
            })

        # compact 触发后跳过后续处理，直接进入下一轮
        if compact_triggered:
            continue

        # Todo 提醒注入
        if todo_manager:
            if used_todo:
                todo_manager._rounds_since_update = 0
            else:
                todo_manager.tick()

            if todo_manager.should_nag:
                nag_msg = todo_manager.get_nag_message()
                messages.append({
                    "role": "user",
                    "content": nag_msg,
                })
                logger.debug(f"[{run_id}] Todo nag injected")

    # 达到最大迭代次数
    logger.warning(f"[{run_id}] Tool loop reached max iterations ({MAX_TOOL_ITERATIONS})")
    return content or "", events


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------

def _build_tool_schemas(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """将 ToolDefinition 列表转为 OpenAI function calling 格式"""
    schemas = []
    for tool in tools:
        schemas.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        })
    return schemas


def _serialize_assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    """确保回灌给 provider 的 assistant/tool_calls 结构满足 OpenAI-compatible 要求。"""
    normalized = dict(message)
    raw_tool_calls = normalized.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return normalized

    serialized_calls: list[dict[str, Any]] = []
    for raw in raw_tool_calls:
        if not isinstance(raw, dict):
            continue
        call = dict(raw)
        func = dict(call.get("function") or {})
        arguments = func.get("arguments", {})
        if not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                arguments = "{}"
        func["arguments"] = arguments
        call["function"] = func
        serialized_calls.append(call)
    normalized["tool_calls"] = serialized_calls
    return normalized


def _parse_tool_calls(raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
    """解析 LLM 返回的 tool_calls"""
    calls = []
    for raw in raw_calls:
        func = raw.get("function", {})
        raw_arguments = func.get("arguments", {})
        if isinstance(raw_arguments, str):
            try:
                parsed_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                parsed_arguments = {}
        elif isinstance(raw_arguments, dict):
            parsed_arguments = raw_arguments
        else:
            parsed_arguments = {}
        calls.append(ToolCall(
            call_id=raw.get("id", str(uuid.uuid4())),
            tool_name=func.get("name", ""),
            arguments=parsed_arguments,
        ))
    return calls


# 工具结果全局截断上限 (防止 context 溢出)
TOOL_RESULT_MAX_CHARS = 50_000


def _truncate_output(output: str, tool_name: str) -> str:
    """截断过长的工具输出"""
    if len(output) <= TOOL_RESULT_MAX_CHARS:
        return output
    truncated = output[:TOOL_RESULT_MAX_CHARS]
    logger.warning(
        "Tool '%s' output truncated: %d → %d chars",
        tool_name, len(output), TOOL_RESULT_MAX_CHARS,
    )
    return truncated + f"\n\n... [截断, 原始 {len(output)} 字符, 保留前 {TOOL_RESULT_MAX_CHARS} 字符]"


async def _execute_tool(
    call: ToolCall,
    tool_def: ToolDefinition,
    *,
    tool_context: dict[str, Any] | None = None,
) -> ToolResult:
    """执行单个工具调用"""
    start = time.monotonic()
    try:
        kwargs = dict(call.arguments)
        if tool_context:
            try:
                signature = inspect.signature(tool_def.handler)
                if "_tool_context" in signature.parameters:
                    kwargs["_tool_context"] = tool_context
            except (TypeError, ValueError):
                pass
        output = await tool_def.handler(**kwargs)
        duration = (time.monotonic() - start) * 1000
        text = _truncate_output(str(output), call.tool_name)
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=True,
            output=text,
            duration_ms=duration,
        )
    except Exception as e:
        duration = (time.monotonic() - start) * 1000
        logger.error(f"Tool {call.tool_name} failed: {e}", exc_info=True)
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            output="",
            error=str(e),
            duration_ms=duration,
        )
