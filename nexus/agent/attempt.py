"""
Attempt Builder — 单次执行准备

职责:
1. 构建工具集（根据任务类型选择可用工具）
2. 构建系统提示（bootstrap 文件 + 任务指令）
3. 构建上下文（历史消息 + 记忆注入）
4. 适配 StreamFn

参考: OpenClaw attempt.ts (1,724 行)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Awaitable, TYPE_CHECKING

from .types import AttemptConfig, Run, ToolDefinition

if TYPE_CHECKING:
    from nexus.knowledge.retrieval import RetrievalIndex
    from nexus.knowledge.memory import EpisodicMemory
    from nexus.knowledge.memory_manager import MemoryManager
    from nexus.evolution.skill_manager import SkillManager

logger = logging.getLogger(__name__)

# 系统提示模板路径（仓库根目录下的 config/prompts）
_BOOTSTRAP_DIR = Path(__file__).resolve().parents[2] / "config" / "prompts"


class AttemptBuilder:
    """
    为每次执行准备完整的 AttemptConfig。

    设计原则:
    1. 每次 attempt 都独立构建，不依赖前次 attempt 的状态
    2. 工具集根据任务类型动态组装
    3. 系统提示包含 bootstrap 指令 + 记忆注入
    """

    def __init__(
        self,
        available_tools: list[ToolDefinition],
        retrieval: RetrievalIndex | None = None,
        memory: EpisodicMemory | None = None,
        memory_manager: MemoryManager | None = None,
        skill_manager: SkillManager | None = None,
        bootstrap_file: str = "system_prompt.md",
    ):
        self._all_tools = available_tools
        self._retrieval = retrieval
        self._memory = memory
        self._memory_manager = memory_manager
        self._skill_manager = skill_manager
        self._bootstrap_file = bootstrap_file

    async def build(
        self,
        run: Run,
        context_messages: list[dict[str, Any]],
        model: str,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        extra_tools: list[ToolDefinition] | None = None,
        disabled_tool_names: set[str] | None = None,
    ) -> AttemptConfig:
        """
        构建一次 attempt 的完整配置。

        步骤:
        1. 加载系统提示
        2. 注入相关记忆
        3. 注入语义检索上下文
        4. 选择工具集
        5. 组装消息列表
        """
        # Step 1: 系统提示
        system_prompt = self._load_system_prompt()

        # Step 1.5: 身份注入 (SOUL.md + USER.md)
        identity_context = self._inject_identity()
        if identity_context:
            system_prompt += f"\n\n{identity_context}"

        # Step 2: 选择工具集并注入真实工具目录
        tools = self._select_tools(
            run.task,
            extra_tools=extra_tools,
            disabled_tool_names=disabled_tool_names,
        )
        system_prompt += self._render_tool_catalog(tools)

        # Step 2.5: Skill 描述注入 (Layer 1 — 仅名称和描述，约 100 tokens/skill)
        skill_descriptions = self._inject_skill_descriptions()
        if skill_descriptions:
            system_prompt += skill_descriptions

        auto_preloaded_skills = self._inject_auto_preloaded_skill_content(run)
        if auto_preloaded_skills:
            system_prompt += auto_preloaded_skills

        installable_skill_hints = self._inject_installable_skill_hints(run.task)
        if installable_skill_hints:
            system_prompt += installable_skill_hints

        # Step 3: 记忆注入
        memory_context = await self._inject_memory(run.task)
        if memory_context:
            system_prompt += f"\n\n## 相关记忆\n{memory_context}"

        # Step 4: 语义检索
        retrieval_context = await self._inject_retrieval(run.task)
        if retrieval_context:
            system_prompt += f"\n\n## 相关文档\n{retrieval_context}"

        # Step 5: 组装消息
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(context_messages)

        return AttemptConfig(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            messages=messages,
            stream_callback=stream_callback,
        )

    # ------------------------------------------------------------------
    # 系统提示
    # ------------------------------------------------------------------

    def _load_system_prompt(self) -> str:
        """加载 bootstrap 系统提示"""
        prompt_path = _BOOTSTRAP_DIR / self._bootstrap_file
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")

        # 默认系统提示
        return (
            "你是 星策（Nexus），一个个人 AI 工作生活助手。\n"
            "你的目标是帮助用户处理文档、语音、任务、信息与决策。\n"
            "回复使用中文，简洁清晰。"
        )

    @staticmethod
    def _render_tool_catalog(tools: list[ToolDefinition]) -> str:
        if not tools:
            return (
                "\n\n## 当前实际可用工具\n"
                "- 当前这一轮没有注入可执行工具。\n"
                "- 你只能基于上下文给出分析、计划、澄清或文本回答。\n"
            )
        lines = [
            "",
            "## 当前实际可用工具",
            "以下工具由运行时真实注入；只能调用这里列出的工具，不要假设其它工具存在。",
        ]
        for tool in tools:
            required = tool.parameters.get("required", []) if isinstance(tool.parameters, dict) else []
            required_text = f"；必填参数: {', '.join(required)}" if required else ""
            lines.append(f"- `{tool.name}` — {tool.description}{required_text}")
        return "\n" + "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # 身份注入 (SOUL.md + USER.md)
    # ------------------------------------------------------------------

    def _inject_identity(self) -> str:
        """注入 Agent 身份和用户画像到 system prompt"""
        if not self._memory_manager:
            return ""
        try:
            return self._memory_manager.get_identity_context()
        except Exception as e:
            logger.warning(f"Identity injection failed: {e}")
            return ""

    # ------------------------------------------------------------------
    # Skill 注入 (Layer 1)
    # ------------------------------------------------------------------

    def _inject_skill_descriptions(self) -> str:
        """
        Layer 1 Skill 注入: 将已安装 skill 的名称和描述注入 system prompt。

        每个 skill 约占 100 tokens，不会过度膨胀 system prompt。
        Agent 可通过 load_skill 工具按需加载完整内容 (Layer 2)。
        """
        if not self._skill_manager:
            return ""

        try:
            descriptions = self._skill_manager.get_skill_descriptions()
            if not descriptions:
                return ""

            return (
                "\n\n## 可用 Skills\n"
                "遇到相关任务时，先用 `load_skill` 工具加载对应 skill 的详细指令。\n\n"
                f"{descriptions}\n"
            )
        except Exception as e:
            logger.warning(f"Skill descriptions injection failed: {e}")
            return ""

    def _inject_installable_skill_hints(self, task: str) -> str:
        """
        OpenClaw-style installable skill hints.

        For the current task, show a bounded list of installable skills that the
        agent can formally install into the runtime, instead of guessing or
        falling back to ad-hoc shell commands immediately.
        """
        if not self._skill_manager:
            return ""

        try:
            matches = self._skill_manager.list_installable_skills(query=task)
        except Exception as e:
            logger.warning(f"Installable skill hint injection failed: {e}")
            return ""

        if not matches:
            return ""

        preview = matches[:5]
        lines = [
            "",
            "## 当前任务相关的可安装 Skills",
            "如果当前已安装技能不足，先在这些 installable skills 中选择匹配项，用 `skill_install` 安装后再用 `load_skill` 读取完整指令。",
        ]
        for item in preview:
            installed_text = "已安装" if item.get("installed") else "未安装"
            lines.append(
                f"- `{item.get('skill_id')}` — {item.get('description', '')}（{installed_text}，匹配分数 {item.get('match_score', 0)}）"
            )
        return "\n" + "\n".join(lines) + "\n"

    def _inject_auto_preloaded_skill_content(self, run: Run) -> str:
        """
        Inject full instructions for skills that the runtime explicitly preloaded
        for this task after managed auto-install / auto-select.
        """
        if not self._skill_manager:
            return ""

        raw_ids = run.metadata.get("auto_preloaded_skills", [])
        if not isinstance(raw_ids, list):
            return ""

        skill_ids: list[str] = []
        seen: set[str] = set()
        for item in raw_ids:
            skill_id = str(item).strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            skill_ids.append(skill_id)

        if not skill_ids:
            return ""

        blocks: list[str] = [
            "",
            "## 本轮已自动预加载的 Skills",
            "这些 Skills 已由运行时基于当前任务自动选择或安装。优先按这些指令执行，不要先退回到“我不会”或手工方案说明。",
        ]
        for skill_id in skill_ids[:3]:
            content = self._skill_manager.get_skill_content(skill_id)
            if content.startswith("Error:"):
                continue
            blocks.append(content)
        if len(blocks) <= 3:
            return ""
        return "\n" + "\n\n".join(blocks) + "\n"

    # ------------------------------------------------------------------
    # 记忆注入
    # ------------------------------------------------------------------

    async def _inject_memory(self, task: str) -> str:
        """从 Episodic Memory 中检索相关记忆"""
        try:
            if self._memory_manager is not None:
                memories = await self._memory_manager.search(task, top_k=5)
                if not memories:
                    return ""
                return "\n".join(
                    f"- [{item.get('metadata', {}).get('kind', 'memory')}] {item.get('content', '')}"
                    for item in memories
                    if item.get("content")
                )

            if not self._memory:
                return ""

            memories = await self._memory.recall(task, limit=5)
            if not memories:
                return ""
            return "\n".join(f"- {m}" for m in memories)
        except Exception as e:
            logger.warning(f"Memory injection failed: {e}")
            return ""

    # ------------------------------------------------------------------
    # 语义检索
    # ------------------------------------------------------------------

    async def _inject_retrieval(self, task: str) -> str:
        """从 Retrieval Index 中检索相关文档片段"""
        if not self._retrieval:
            return ""

        try:
            results = await self._retrieval.search(task, top_k=5)
            if not results:
                return ""
            return "\n---\n".join(
                f"[{r.source}]\n{r.content}" for r in results
            )
        except Exception as e:
            logger.warning(f"Retrieval injection failed: {e}")
            return ""

    # ------------------------------------------------------------------
    # 工具选择
    # ------------------------------------------------------------------

    def _select_tools(
        self,
        task: str,
        *,
        extra_tools: list[ToolDefinition] | None = None,
        disabled_tool_names: set[str] | None = None,
    ) -> list[ToolDefinition]:
        """
        根据任务类型选择可用工具。

        当前阶段：返回所有工具。
        未来：根据意图分类结果过滤工具集。
        """
        disabled = set(disabled_tool_names or set())
        tools = [tool for tool in self._all_tools if tool.name not in disabled]
        if not extra_tools:
            return tools

        seen = {tool.name for tool in tools}
        for tool in extra_tools:
            if tool.name in seen:
                continue
            seen.add(tool.name)
            tools.append(tool)
        return tools
