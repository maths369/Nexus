"""
Run Manager — 任务编排与状态机

职责:
1. 管理 Run 生命周期: queued → planning → running → waiting → validating → succeeded/failed
2. 重试预算管理
3. 模型故障切换
4. 上下文溢出处理

参考: OpenClaw run.ts (1,322 行)
迁移来源: macos-ai-assistant/orchestrator/services/run_control.py
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, TYPE_CHECKING

from .types import (
    AttemptConfig,
    ContextOverflowError,
    ProviderError,
    Run,
    RunEvent,
    RunStatus,
)
from .core import execute_tool_loop
from .tool_profiles import ToolProfile

if TYPE_CHECKING:
    from .attempt import AttemptBuilder
    from .approval import ApprovalEngine
    from .background import BackgroundTaskManager
    from .compressor import ContextCompressor
    from .run_store import RunStore
    from .todo import TodoManager
    from .tools_policy import ToolsPolicy
    from nexus.evolution import CapabilityManager, CapabilityPromotionAdvisor
    from nexus.evolution.skill_manager import SkillManager

from nexus.provider.gateway import ProviderGateway, ProviderGatewayError

logger = logging.getLogger(__name__)


class RunManager:
    """
    Run 生命周期管理。

    一个 Run 代表一次完整的任务执行，可能包含多次 attempt（重试）。
    每次 attempt 由 AttemptBuilder 构建配置，由 core.execute_tool_loop 执行。
    """

    def __init__(
        self,
        run_store: RunStore,
        attempt_builder: AttemptBuilder,
        provider: ProviderGateway,
        tools_policy: ToolsPolicy,
        # 故障切换模型列表（按优先级）
        fallback_models: list[str] | None = None,
        # 新增: 上下文压缩器 + Todo 管理器 + 后台任务管理器
        compressor: ContextCompressor | None = None,
        todo_manager: TodoManager | None = None,
        background_manager: BackgroundTaskManager | None = None,
        capability_promotion_advisor: CapabilityPromotionAdvisor | None = None,
        capability_manager: CapabilityManager | None = None,
        skill_manager: SkillManager | None = None,
        approval_engine: ApprovalEngine | None = None,
    ):
        self._store = run_store
        self._attempt = attempt_builder
        self._provider = provider
        self._policy = tools_policy
        self._fallback_models = fallback_models or []
        self._compressor = compressor
        self._todo_manager = todo_manager
        self._background_manager = background_manager
        self._capability_promotion_advisor = capability_promotion_advisor
        self._capability_manager = capability_manager
        self._skill_manager = skill_manager
        self._approval_engine = approval_engine

    async def execute(
        self,
        session_id: str,
        task: str,
        context_messages: list[dict[str, Any]],
        model: str | None = None,
        stream_callback=None,
        extra_tools=None,
        disabled_tool_names=None,
        tool_profile: ToolProfile | None = None,
    ) -> Run:
        """
        执行一个新的 Run。

        流程:
          1. 创建 Run 记录（QUEUED）
          2. 构建 AttemptConfig
          3. 执行工具调用循环
          4. 如果失败且可重试，进行故障切换后重试
          5. 更新最终状态
        """
        # 创建 Run
        run = Run(
            run_id=str(uuid.uuid4()),
            session_id=session_id,
            task=task,
            model=model or self._get_default_model(),
        )
        await self._store.save_run(run)
        await self._transition(run, RunStatus.PLANNING)

        # 尝试执行
        while not run.is_terminal and run.can_retry:
            run.attempt_count += 1
            current_model = self._select_model(run)
            run.model = current_model
            attempted_models = run.metadata.setdefault("attempt_models", [])
            if current_model and current_model not in attempted_models:
                attempted_models.append(current_model)

            logger.info(
                f"[{run.run_id}] Attempt {run.attempt_count}/{run.max_attempts} "
                f"with model {current_model}"
            )

            try:
                await self._transition(run, RunStatus.RUNNING)

                preflight_events = await self._maybe_prepare_extensions_for_task(run)
                for event in preflight_events:
                    await self._store.save_event(event)
                if preflight_events:
                    await self._store.save_run(run)

                # 构建 AttemptConfig
                config = await self._attempt.build(
                    run=run,
                    context_messages=context_messages,
                    model=current_model,
                    stream_callback=stream_callback,
                    extra_tools=extra_tools,
                    disabled_tool_names=set(disabled_tool_names or []),
                    tool_profile=tool_profile,
                )

                # 执行工具调用循环
                result_text, events = await execute_tool_loop(
                    config=config,
                    provider=self._provider,
                    tools_policy=self._policy,
                    run_id=run.run_id,
                    compressor=self._compressor,
                    todo_manager=self._todo_manager,
                    background_manager=self._background_manager,
                    approval_engine=self._approval_engine,
                    session_id=session_id,
                )

                self._record_successful_mesh_dispatches(run, events)
                self._record_vault_write_artifacts(run, events)

                # 保存事件
                for event in events:
                    await self._store.save_event(event)

                if await self._maybe_retry_after_missing_extension_response(
                    run=run,
                    result_text=result_text,
                ):
                    await self._store.save_run(run)
                    logger.info("[%s] Auto-skill recovery triggered, retrying task", run.run_id)
                    continue

                if self._capability_promotion_advisor is not None:
                    suggestion = self._capability_promotion_advisor.suggest(run=run, events=events)
                    if suggestion is not None:
                        suggestion_payload = suggestion.to_dict()
                        run.metadata["capability_promotion_suggestion"] = suggestion_payload
                        suggestion_event = RunEvent(
                            event_id=str(uuid.uuid4()),
                            run_id=run.run_id,
                            event_type="capability_promotion_suggested",
                            data=suggestion_payload,
                        )
                        events.append(suggestion_event)
                        await self._store.save_event(suggestion_event)

                # 成功
                run.result = result_text
                await self._transition(run, RunStatus.SUCCEEDED)

            except ContextOverflowError:
                logger.warning(f"[{run.run_id}] Context overflow, will retry with truncation")
                # TODO: 上下文截断后重试
                run.error = "context_overflow"
                if not run.can_retry:
                    await self._transition(run, RunStatus.FAILED)

            except (ProviderError, ProviderGatewayError) as e:
                logger.error(f"[{run.run_id}] Provider error: {e}")
                run.error = str(e)
                if run.can_retry:
                    logger.info(f"[{run.run_id}] Will retry with fallback model")
                else:
                    await self._transition(run, RunStatus.FAILED)

            except Exception as e:
                logger.error(f"[{run.run_id}] Unexpected error: {e}", exc_info=True)
                run.error = str(e)
                await self._transition(run, RunStatus.FAILED)

        return run

    async def _maybe_prepare_extensions_for_task(self, run: Run) -> list[RunEvent]:
        events: list[RunEvent] = []

        if self._capability_manager is not None and run.metadata.get("auto_capability_preflight_done") is not True:
            run.metadata["auto_capability_preflight_done"] = True
            events.extend(
                await self._auto_select_or_enable_capability(
                    run,
                    query_text=run.task,
                    min_score=2.0,
                    phase="preflight",
                )
            )

        if self._skill_manager is not None and run.metadata.get("auto_skill_preflight_done") is not True:
            run.metadata["auto_skill_preflight_done"] = True
            events.extend(
                await self._auto_select_or_prepare_skill(
                    run,
                    query_text=run.task,
                    min_score=3.0,
                    phase="preflight",
                )
            )

        return events

    async def _maybe_retry_after_missing_extension_response(self, run: Run, result_text: str) -> bool:
        if self._skill_manager is None and self._capability_manager is None:
            return False
        if run.metadata.get("auto_extension_recovery_done") is True:
            return False
        if not self._response_suggests_missing_extension(result_text):
            return False

        run.metadata["auto_extension_recovery_done"] = True
        events: list[RunEvent] = []
        recovery_query = f"{run.task}\n{result_text}".strip()

        if self._capability_manager is not None:
            events.extend(
                await self._auto_select_or_enable_capability(
                    run,
                    query_text=recovery_query,
                    min_score=1.0,
                    phase="recovery",
                    explicit_ids=self._extract_explicit_capability_ids(result_text),
                )
            )

        if self._skill_manager is not None:
            events.extend(
                await self._auto_select_or_prepare_skill(
                    run,
                    query_text=recovery_query,
                    min_score=2.0,
                    phase="recovery",
                )
            )

        if not events:
            return False

        for event in events:
            await self._store.save_event(event)
        await self._store.save_event(RunEvent(
            event_id=str(uuid.uuid4()),
            run_id=run.run_id,
            event_type="auto_extension_retry_scheduled",
            data={
                "task": run.task,
                "enabled_capabilities": list(run.metadata.get("auto_enabled_capabilities", [])),
                "preloaded_skills": list(run.metadata.get("auto_preloaded_skills", [])),
            },
        ))
        return True

    async def _auto_select_or_prepare_skill(
        self,
        run: Run,
        *,
        query_text: str,
        min_score: float,
        phase: str,
    ) -> list[RunEvent]:
        if self._skill_manager is None:
            return []

        events: list[RunEvent] = []

        installed_candidate = self._match_installed_skill(query_text, min_score=min_score)
        if installed_candidate is not None:
            skill_id = str(installed_candidate.get("skill_id") or "").strip()
            if skill_id:
                self._mark_preloaded_skill(run, skill_id)
                events.append(RunEvent(
                    event_id=str(uuid.uuid4()),
                    run_id=run.run_id,
                    event_type="auto_skill_selected",
                    data={
                        "phase": phase,
                        "skill_id": skill_id,
                        "match_score": float(installed_candidate.get("match_score") or 0.0),
                        "installed": True,
                        "source": "installed",
                    },
                ))
            return events

        matches = self._skill_manager.list_installable_skills(query=query_text)
        candidate = next(
            (
                item
                for item in matches
                if float(item.get("match_score") or 0.0) >= min_score
            ),
            None,
        )
        if candidate is None:
            return events

        skill_id = str(candidate.get("skill_id") or "").strip()
        if not skill_id:
            return events

        self._mark_preloaded_skill(run, skill_id)
        events.append(RunEvent(
            event_id=str(uuid.uuid4()),
            run_id=run.run_id,
            event_type="auto_skill_selected",
            data={
                "phase": phase,
                "skill_id": skill_id,
                "match_score": float(candidate.get("match_score") or 0.0),
                "installed": bool(candidate.get("installed")),
                "source": "installable_registry",
            },
        ))

        if bool(candidate.get("installed")):
            return events

        install_result = await self._skill_manager.install_from_catalog(skill_id, actor="agent")
        success = bool(install_result.get("success"))
        events.append(RunEvent(
            event_id=str(uuid.uuid4()),
            run_id=run.run_id,
            event_type="auto_skill_installed",
            data={
                "phase": phase,
                "skill_id": skill_id,
                "success": success,
                "reason": install_result.get("reason", ""),
                "installed_path": install_result.get("installed_path", ""),
            },
        ))
        if not success:
            return []

        installed = run.metadata.setdefault("auto_installed_skills", [])
        if isinstance(installed, list) and skill_id not in installed:
            installed.append(skill_id)
        return events

    async def _auto_select_or_enable_capability(
        self,
        run: Run,
        *,
        query_text: str,
        min_score: float,
        phase: str,
        explicit_ids: list[str] | None = None,
    ) -> list[RunEvent]:
        if self._capability_manager is None:
            return []

        capabilities = list(self._capability_manager.list_capabilities())
        if not capabilities:
            return []

        explicit = [
            capability_id
            for capability_id in (explicit_ids or [])
            if any(str(item.get("capability_id")) == capability_id for item in capabilities)
        ]
        if explicit:
            selected = [
                next(item for item in capabilities if str(item.get("capability_id")) == capability_id)
                for capability_id in explicit
            ]
        else:
            scored = sorted(
                (
                    (self._score_capability(item, query_text), item)
                    for item in capabilities
                ),
                key=lambda pair: pair[0],
                reverse=True,
            )
            selected = [item for score, item in scored if score >= min_score][:1]

        events: list[RunEvent] = []
        for item in selected:
            capability_id = str(item.get("capability_id") or "").strip()
            if not capability_id:
                continue
            enabled = bool(item.get("enabled"))
            self._mark_capability_hint_skill(run, item)
            events.append(RunEvent(
                event_id=str(uuid.uuid4()),
                run_id=run.run_id,
                event_type="auto_capability_selected",
                data={
                    "phase": phase,
                    "capability_id": capability_id,
                    "enabled": enabled,
                    "match_score": self._score_capability(item, query_text),
                },
            ))
            if enabled:
                continue

            result = await self._capability_manager.enable(capability_id, actor="agent")
            success = bool(result.success)
            events.append(RunEvent(
                event_id=str(uuid.uuid4()),
                run_id=run.run_id,
                event_type="auto_capability_enabled",
                data={
                    "phase": phase,
                    "capability_id": capability_id,
                    "success": success,
                    "reason": result.reason,
                },
            ))
            if not success:
                continue

            enabled_caps = run.metadata.setdefault("auto_enabled_capabilities", [])
            if isinstance(enabled_caps, list) and capability_id not in enabled_caps:
                enabled_caps.append(capability_id)
            self._mark_capability_hint_skill(run, self._capability_manager.get_status(capability_id))
        return events

    @staticmethod
    def _mark_preloaded_skill(run: Run, skill_id: str) -> None:
        preloaded = run.metadata.setdefault("auto_preloaded_skills", [])
        if isinstance(preloaded, list) and skill_id not in preloaded:
            preloaded.append(skill_id)

    def _mark_capability_hint_skill(self, run: Run, capability: dict[str, Any]) -> None:
        if self._skill_manager is None:
            return
        skill_id = str(capability.get("skill_hint") or "").strip()
        if not skill_id:
            return
        if self._skill_manager.get_skill_path(skill_id) is None:
            return
        self._mark_preloaded_skill(run, skill_id)

    @staticmethod
    def _response_suggests_missing_extension(text: str) -> bool:
        lowered = text.lower()
        patterns = [
            "没有",
            "无法",
            "不具备",
            "做不到",
            "not available",
            "don't have",
            "do not have",
            "missing capability",
            "missing skill",
            "需要先",
            "需要安装",
            "建议",
            "not enabled",
            "run capability_enable",
        ]
        missing_context = [
            "能力",
            "skill",
            "技能",
            "扩展",
            "转换",
            "读取",
            "处理",
            "capability",
            "excel",
            "pdf",
        ]
        return any(marker in lowered for marker in patterns) and any(
            marker in lowered for marker in missing_context
        )

    def _match_installed_skill(self, query_text: str, *, min_score: float) -> dict[str, Any] | None:
        if self._skill_manager is None:
            return None
        query = self._normalize_text(query_text)
        if not query:
            return None

        best_score = -1.0
        best_item: dict[str, Any] | None = None
        for item in self._skill_manager.list_skills():
            score = self._score_skill(item, query_text)
            if score < min_score or score <= best_score:
                continue
            best_score = score
            best_item = dict(item)
            best_item["match_score"] = score
        return best_item

    def _extract_explicit_capability_ids(self, text: str) -> list[str]:
        if self._capability_manager is None:
            return []
        known_ids = {
            str(item.get("capability_id") or "").strip()
            for item in self._capability_manager.list_capabilities()
        }
        explicit: list[str] = []
        for match in re.findall(r"capability_enable\(['\"]([a-z0-9_]+)['\"]\)", text, flags=re.IGNORECASE):
            capability_id = str(match).strip()
            if capability_id in known_ids and capability_id not in explicit:
                explicit.append(capability_id)
        return explicit

    def _score_skill(self, item: dict[str, Any], query_text: str) -> float:
        query = self._normalize_text(query_text)
        score = 0.0
        token_hits = 0
        skill_id = self._normalize_text(str(item.get("skill_id") or ""))
        name = self._normalize_text(str(item.get("name") or ""))
        description = self._normalize_text(str(item.get("description") or ""))
        tags = self._normalize_text(str(item.get("tags") or ""))

        for field, weight in ((skill_id, 4.0), (name, 4.0), (description, 2.0), (tags, 1.5)):
            if field and field in query:
                score += weight

        for token in self._iter_match_tokens(skill_id, name, description, tags):
            if token in query:
                score += 1.0
                token_hits += 1
        if token_hits >= 2:
            score += 1.5
        return score

    def _score_capability(self, item: dict[str, Any], query_text: str) -> float:
        query = self._normalize_text(query_text)
        score = 0.0
        token_hits = 0
        capability_id = self._normalize_text(str(item.get("capability_id") or ""))
        name = self._normalize_text(str(item.get("name") or ""))
        description = self._normalize_text(str(item.get("description") or ""))
        tools = self._normalize_text(" ".join(str(tool) for tool in item.get("tools") or []))
        skill_hint = self._normalize_text(str(item.get("skill_hint") or ""))

        for field, weight in (
            (capability_id, 4.0),
            (name, 3.0),
            (description, 2.0),
            (tools, 1.5),
            (skill_hint, 1.0),
        ):
            if field and field in query:
                score += weight

        for token in self._iter_match_tokens(capability_id, name, description, tools, skill_hint):
            if token in query:
                score += 1.0
                token_hits += 1
        if token_hits >= 2:
            score += 1.5
        return score

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())

    @staticmethod
    def _iter_match_tokens(*fields: str) -> list[str]:
        tokens: list[str] = []
        seen: set[str] = set()
        for field in fields:
            for token in re.split(r"[^a-z0-9_\-\u4e00-\u9fff]+", field):
                base = token.strip("-_ ").lower()
                if len(base) >= 2 and base not in seen:
                    seen.add(base)
                    tokens.append(base)
                for part in re.split(r"[_\-]+", base):
                    item = part.strip()
                    if len(item) < 2 or item in seen:
                        continue
                    seen.add(item)
                    tokens.append(item)
        return tokens

    @staticmethod
    def _extract_successful_mesh_dispatches(events: list[RunEvent]) -> list[dict[str, str]]:
        tool_calls: dict[str, dict[str, str]] = {}
        dispatches: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for event in events:
            if event.event_type == "tool_call":
                call_id = str(event.data.get("call_id") or "").strip()
                tool_name = str(event.data.get("tool") or "").strip()
                if not call_id or not tool_name.startswith("mesh_dispatch__"):
                    continue
                arguments = event.data.get("arguments")
                task_description = ""
                if isinstance(arguments, dict):
                    task_description = str(arguments.get("task_description") or "")
                tool_calls[call_id] = {
                    "tool": tool_name,
                    "task_description": task_description,
                }
                continue

            if event.event_type != "tool_result":
                continue

            call_id = str(event.data.get("call_id") or "").strip()
            if not call_id or not bool(event.data.get("success")):
                continue

            record = tool_calls.get(call_id)
            if record is None:
                continue

            key = (record["tool"], record["task_description"])
            if key in seen:
                continue
            seen.add(key)
            dispatches.append(dict(record))

        return dispatches

    def _record_successful_mesh_dispatches(self, run: Run, events: list[RunEvent]) -> None:
        dispatches = self._extract_successful_mesh_dispatches(events)
        if not dispatches:
            return

        existing = run.metadata.setdefault("successful_mesh_dispatches", [])
        if not isinstance(existing, list):
            existing = []
            run.metadata["successful_mesh_dispatches"] = existing

        seen = {
            (
                str(item.get("tool") or ""),
                str(item.get("task_description") or ""),
            )
            for item in existing
            if isinstance(item, dict)
        }
        for record in dispatches:
            key = (record["tool"], record["task_description"])
            if key in seen:
                continue
            seen.add(key)
            existing.append(record)

    @staticmethod
    def _extract_vault_write_artifacts(events: list[RunEvent]) -> list[dict[str, str]]:
        """从 run events 中提取成功的 write_vault / document_append_block 调用，
        以便 orchestrator 将产出物注册到 session artifacts。"""
        # 收集 write 类工具的 call_id → 参数
        _WRITE_TOOLS = {"write_vault", "document_append_block", "create_page"}
        tool_calls: dict[str, dict[str, str]] = {}
        artifacts: list[dict[str, str]] = []
        seen_paths: set[str] = set()

        for event in events:
            if event.event_type == "tool_call":
                call_id = str(event.data.get("call_id") or "").strip()
                tool_name = str(event.data.get("tool") or "").strip()
                if not call_id or tool_name not in _WRITE_TOOLS:
                    continue
                arguments = event.data.get("arguments")
                rel_path = ""
                title = ""
                if isinstance(arguments, dict):
                    rel_path = str(arguments.get("relative_path") or "").strip()
                    title = str(arguments.get("title") or "").strip()
                tool_calls[call_id] = {
                    "tool": tool_name,
                    "relative_path": rel_path,
                    "title": title,
                }
                continue

            if event.event_type != "tool_result":
                continue
            call_id = str(event.data.get("call_id") or "").strip()
            if not call_id or not bool(event.data.get("success")):
                continue
            record = tool_calls.get(call_id)
            if record is None or not record["relative_path"]:
                continue
            if record["relative_path"] in seen_paths:
                continue
            seen_paths.add(record["relative_path"])
            artifacts.append(record)

        return artifacts

    def _record_vault_write_artifacts(self, run: Run, events: list[RunEvent]) -> None:
        artifacts = self._extract_vault_write_artifacts(events)
        if not artifacts:
            return
        existing = run.metadata.setdefault("vault_write_artifacts", [])
        if not isinstance(existing, list):
            existing = []
            run.metadata["vault_write_artifacts"] = existing
        seen = {str(item.get("relative_path") or "") for item in existing if isinstance(item, dict)}
        for record in artifacts:
            if record["relative_path"] in seen:
                continue
            seen.add(record["relative_path"])
            existing.append(record)

    # ------------------------------------------------------------------
    # 状态转换
    # ------------------------------------------------------------------

    async def _transition(self, run: Run, new_status: RunStatus) -> None:
        """执行状态转换并持久化"""
        old_status = run.status
        run.status = new_status
        await self._store.save_run(run)
        await self._store.save_event(RunEvent(
            event_id=str(uuid.uuid4()),
            run_id=run.run_id,
            event_type="status_change",
            data={"from": old_status.value, "to": new_status.value},
        ))
        logger.info(f"[{run.run_id}] {old_status.value} → {new_status.value}")

    # ------------------------------------------------------------------
    # 模型选择
    # ------------------------------------------------------------------

    def _get_default_model(self) -> str:
        """获取默认模型"""
        if self._fallback_models:
            return self._fallback_models[0]
        return "qwen-max"

    def set_fallback_models(self, fallback_models: list[str]) -> None:
        self._fallback_models = [str(model) for model in fallback_models if str(model).strip()]

    def _select_model(self, run: Run) -> str:
        """根据重试次数选择模型（故障切换）"""
        if run.attempt_count <= 1:
            return run.model

        # 故障切换到下一个可用模型
        idx = min(run.attempt_count - 1, len(self._fallback_models) - 1)
        if idx < len(self._fallback_models):
            return self._fallback_models[idx]
        return run.model
