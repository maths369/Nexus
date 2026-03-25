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
from .tool_profiles import ToolProfile

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
        workspace_roots: list[Path] | None = None,
    ):
        self._all_tools = available_tools
        self._retrieval = retrieval
        self._memory = memory
        self._memory_manager = memory_manager
        self._skill_manager = skill_manager
        self._bootstrap_file = bootstrap_file
        self._workspace_roots = workspace_roots or []

    async def build(
        self,
        run: Run,
        context_messages: list[dict[str, Any]],
        model: str,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        extra_tools: list[ToolDefinition] | None = None,
        disabled_tool_names: set[str] | None = None,
        tool_profile: ToolProfile | None = None,
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

        # Step 1.1: 注入当前日期时间（LLM 无法自行获取）
        from datetime import datetime
        now = datetime.now()
        system_prompt += (
            f"\n\n## 当前时间\n"
            f"当前日期时间: {now.strftime('%Y-%m-%d %H:%M:%S')} "
            f"(星期{'一二三四五六日'[now.weekday()]})"
        )

        # Step 1.5: 身份注入 (SOUL.md + USER.md)
        identity_context = self._inject_identity()
        if identity_context:
            system_prompt += f"\n\n{identity_context}"

        # Step 1.6: 项目上下文注入 (PROJECT.md / CLAUDE.md / pyproject.toml)
        project_context = self._inject_project_context()
        if project_context:
            system_prompt += f"\n\n{project_context}"

        # Step 2: 选择工具集并注入真实工具目录
        tools = self._select_tools(
            run.task,
            extra_tools=extra_tools,
            disabled_tool_names=disabled_tool_names,
            tool_profile=tool_profile,
        )
        system_prompt += self._render_tool_catalog(tools)

        # Step 2.5: Skill 注入（对标 OpenClaw：所有 eligible skills 完整内容注入）
        skills_prompt = self._inject_skills_prompt()
        if skills_prompt:
            system_prompt += f"\n\n{skills_prompt}"

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
    # 项目上下文注入
    # ------------------------------------------------------------------

    # 按优先级扫描的 bootstrap 文件名
    _BOOTSTRAP_FILENAMES = (
        "PROJECT.md",
        "CLAUDE.md",
        "AGENTS.md",
        "README.md",
    )
    # 项目元数据文件（提取关键字段）
    _META_FILENAMES = (
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
    )
    # Bootstrap 内容上限
    _MAX_BOOTSTRAP_CHARS = 8_000

    def _inject_project_context(self) -> str:
        """
        扫描 workspace_roots 中的项目描述文件，注入到 system prompt。

        优先级:
        1. PROJECT.md / CLAUDE.md / AGENTS.md — 完整注入
        2. README.md — 截取前 2000 字符
        3. pyproject.toml / package.json — 提取项目名、描述、依赖列表
        """
        if not self._workspace_roots:
            return ""

        parts: list[str] = []
        total_chars = 0

        for root in self._workspace_roots:
            if not root.is_dir():
                continue

            # 扫描 bootstrap 文件
            for name in self._BOOTSTRAP_FILENAMES:
                fp = root / name
                if not fp.is_file():
                    continue
                try:
                    content = fp.read_text(encoding="utf-8").strip()
                except Exception:
                    continue
                if not content:
                    continue

                # README 截断
                if name == "README.md" and len(content) > 2000:
                    content = content[:2000] + "\n\n... (截断)"

                budget = self._MAX_BOOTSTRAP_CHARS - total_chars
                if budget <= 0:
                    break
                if len(content) > budget:
                    content = content[:budget] + "\n\n... (截断)"

                parts.append(f"### {name} ({root.name}/)\n{content}")
                total_chars += len(content)

                # PROJECT.md / CLAUDE.md 找到一个就够了（同一项目）
                if name in ("PROJECT.md", "CLAUDE.md", "AGENTS.md"):
                    break

            # 扫描项目元数据
            for name in self._META_FILENAMES:
                fp = root / name
                if not fp.is_file():
                    continue
                try:
                    meta_summary = self._summarize_project_meta(fp)
                except Exception:
                    continue
                if meta_summary:
                    parts.append(f"### {name} ({root.name}/)\n{meta_summary}")
                break  # 一个项目只需要一个 meta 文件

        if not parts:
            return ""

        return "## 项目上下文\n以下是当前工作区的项目描述，请据此理解项目结构和约定。\n\n" + "\n\n".join(parts)

    @staticmethod
    def _summarize_project_meta(path: Path) -> str:
        """从项目元数据文件中提取关键信息"""
        content = path.read_text(encoding="utf-8")
        name = path.name

        if name == "pyproject.toml":
            lines = []
            in_project = False
            in_deps = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped == "[project]" or stripped == "[tool.poetry]":
                    in_project = True
                    continue
                if stripped.startswith("[") and in_project:
                    in_project = False
                if stripped == "[project.dependencies]" or stripped == "[tool.poetry.dependencies]":
                    in_deps = True
                    continue
                if stripped.startswith("[") and in_deps:
                    in_deps = False

                if in_project and "=" in stripped:
                    key = stripped.split("=")[0].strip()
                    if key in ("name", "description", "version", "python"):
                        lines.append(stripped)
                if in_deps and "=" in stripped:
                    lines.append(f"  dep: {stripped}")
            return "\n".join(lines[:20]) if lines else ""

        if name == "package.json":
            import json as _json_mod
            try:
                data = _json_mod.loads(content)
            except Exception:
                return ""
            parts = []
            for key in ("name", "description", "version"):
                if key in data:
                    parts.append(f"{key}: {data[key]}")
            deps = data.get("dependencies", {})
            if deps:
                parts.append(f"dependencies: {', '.join(list(deps.keys())[:15])}")
            return "\n".join(parts)

        # Cargo.toml, go.mod — 返回前 500 字符
        return content[:500]

    # ------------------------------------------------------------------
    # Skill 注入 (Layer 1)
    # ------------------------------------------------------------------

    def _inject_skills_prompt(self) -> str:
        """对标 OpenClaw：将所有 eligible 的已安装 skill 完整内容注入 system prompt。

        LLM 根据用户任务自行决定使用哪些 skill，无需二次 load_skill 调用。
        """
        if not self._skill_manager:
            return ""

        try:
            return self._skill_manager.format_skills_for_prompt()
        except Exception as e:
            logger.warning(f"Skills prompt injection failed: {e}")
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
        tool_profile: ToolProfile | None = None,
    ) -> list[ToolDefinition]:
        """
        根据任务类型选择可用工具。

        过滤顺序:
        1. disabled_tool_names — 移除明确禁用的工具
        2. extra_tools — 追加额外工具（去重）
        3. tool_profile — 根据 Profile 过滤工具子集
        """
        disabled = set(disabled_tool_names or set())
        tools = [tool for tool in self._all_tools if tool.name not in disabled]

        if extra_tools:
            seen = {tool.name for tool in tools}
            for tool in extra_tools:
                if tool.name in seen:
                    continue
                seen.add(tool.name)
                tools.append(tool)

        # Profile 过滤（如果指定）
        if tool_profile is not None:
            tools = tool_profile.filter(tools)

        return tools
