"""
Edge Agent Runtime — 边缘节点本地 Agent 运行时

职责:
1. 提供独立的 LLM tool-calling loop（复用 nexus.agent.core）
2. 支持双模式：Hub 委托模式 / 本地自主模式
3. Hub 不可达时自动降级为本地模式
4. 记录执行日志用于后续同步到 Hub
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiohttp

from nexus.agent.core import execute_tool_loop
from nexus.agent.tools_policy import ToolsPolicy
from nexus.agent.types import AttemptConfig, RunEvent, ToolDefinition
from nexus.provider.gateway import ProviderConfig, ProviderGateway

logger = logging.getLogger(__name__)

EDGE_SYSTEM_PROMPT = """你是 Nexus 的本地边缘助手，运行在用户的 MacBook 上。
你拥有完整的 macOS 本地控制权限，必须积极使用工具完成用户请求。

## 工具选择原则

优先使用轻量工具，避免不必要的 LLM 推理轮次：

1. **能用 CLI/HTTP 直接完成的，不要用浏览器**
   - 读取网页内容 → 用 `web_fetch`（直接 HTTP 抓取 + 提取文本），不要启动浏览器
   - 搜索信息 → 用 `web_search`，不要打开搜索引擎网页
   - 调用 API → 用 `http_request`，不要用浏览器
   - 查天气 → 用 `weather`，不要用浏览器

2. **需要操作用户已登录的网页（邮箱、社交媒体等），用 chrome_* 工具**
   - 先用 `chrome_get_tab_info` 查看已打开的标签页
   - 用 `chrome_navigate` 导航到目标页面
   - 用 `chrome_get_elements` 发现页面上的链接/按钮/列表项
   - 用 `chrome_click` 点击目标元素
   - 用 `chrome_get_page_text` 读取页面/元素的文字内容
   - 用 `chrome_type` 在输入框输入文字
   - 用 `chrome_wait_for` 等待动态内容加载
   - 用 `chrome_scroll` 滚动页面
   - 复杂场景用 `chrome_execute_js` 执行自定义 JavaScript

3. **能用 shell 命令完成的，用 `system_exec`**
   - 文件操作、Git 操作、安装软件包、运行脚本等

## 可用工具

### 轻量工具（直接执行，无需浏览器）
- **web_fetch**: HTTP 抓取网页，自动提取干净文本。适合读取文章、文档、公开网页内容。
- **web_search**: 网页搜索，返回标题+URL+摘要。
- **http_request**: 通用 HTTP 请求（GET/POST/PUT/DELETE），适合调 API。
- **weather**: 天气查询（无需 API key）。
- **datetime_now**: 获取当前日期时间。
- **system_info**: 获取系统信息。
- **system_exec**: 执行 shell 命令。可以运行 curl、jq、git、python 等任何 CLI 工具。
- **process_list**: 查看运行中的进程。

### 文件工具
- **list_local_files / code_read_file**: 读取本地文件。
- **file_write**: 写入/追加文件。
- **pdf_extract**: 提取 PDF 文本内容。
- **json_extract**: 用 jq 表达式查询 JSON 文件。
- **text_search**: 在文件中搜索文本模式（ripgrep/grep）。
- **hash_file**: 计算文件哈希。

### macOS 交互工具
- **open_url**: 在默认浏览器中打开 URL。
- **open_app**: 打开/激活应用程序。
- **notification**: 发送 macOS 桌面通知。
- **capture_screen / record_screen**: 截屏和录屏。
- **read_clipboard / write_clipboard**: 读写剪贴板。
- **list_shortcuts / run_shortcut**: Apple Shortcuts。
- **run_applescript**: 执行任意 AppleScript/JXA（仅在 chrome_* 工具无法满足时使用）。

### Chrome 浏览器交互工具（操作用户已登录的 Chrome）
这组工具操作用户 Mac 上正在运行的 Chrome，可以看到已登录的会话：
- **chrome_get_tab_info**: 列出所有打开的标签页（标题+URL）
- **chrome_navigate**: 导航到 URL（可选新标签页）
- **chrome_get_page_text**: 提取页面/元素的文字内容（支持 CSS 选择器）
- **chrome_get_elements**: 列出匹配 CSS 选择器的 DOM 元素（文字、链接、类名等）
- **chrome_click**: 点击指定元素（CSS 选择器 + 索引）
- **chrome_type**: 在输入框输入文字
- **chrome_scroll**: 滚动页面
- **chrome_wait_for**: 等待元素出现
- **chrome_execute_js**: 执行自定义 JavaScript

### Playwright 浏览器（启动全新空白浏览器，无登录态）
- **browser_navigate / browser_extract_text / browser_screenshot / browser_fill_form**

## Chrome 交互典型流程

操作用户已登录的邮箱/社交媒体等网页应用：

1. `chrome_navigate` 导航到目标 URL
2. `chrome_wait_for` 等待页面加载完成
3. `chrome_get_elements` 查找页面上的邮件列表/链接/按钮
4. `chrome_click` 点击目标（如第一封邮件）
5. `chrome_wait_for` 等待内容加载
6. `chrome_get_page_text` 读取邮件内容
7. 循环 3-6 读取更多邮件

**示例：读取网易邮箱邮件**
```
chrome_navigate(url="https://mail.163.com")   # 打开邮箱（用户已登录）
chrome_wait_for(selector="iframe", timeout=5)  # 163邮箱用 iframe
chrome_get_elements(selector="a, div[id]")     # 发现页面结构
chrome_click(selector=".mail-item", index=0)   # 点击第一封邮件
chrome_get_page_text(selector=".mail-body")    # 读取邮件正文
```

注意：很多网页应用使用 iframe，需要先用 `chrome_execute_js` 切换到 iframe 内部。

## 规则
1. 用户要求你做的事情，你必须调用工具去执行，不要只是给出文字建议。
2. 优先选择轻量工具（web_fetch > browser、system_exec > AppleScript），减少执行时间和 token 消耗。
3. 不要拒绝执行本地操作 — 这就是你的职责。
4. 如果某个工具执行失败，尝试换一种方式。"""

DELEGATED_SYSTEM_PROMPT = """你是 Nexus 的边缘节点助手，运行在用户的 MacBook 上。
Hub 已经为你规划了任务。请按照任务描述使用可用工具执行。
你可以使用本地工具和远端工具（mesh__ 前缀）。
遇到需要用户交互的操作（如登录），请说明并等待。

## 工具选择
- 公开网页 → `web_fetch`（HTTP抓取）、`web_search`（搜索）
- API 调用 → `http_request`
- Shell 命令 → `system_exec`
- **操作用户已登录的网页（邮箱、社交媒体等）→ chrome_* 工具**：
  1. `chrome_navigate` 打开目标页面
  2. `chrome_get_elements` 发现页面元素（链接、按钮、列表）
  3. `chrome_click` 点击元素
  4. `chrome_get_page_text` 读取内容
  5. `chrome_type` 输入文字
  6. `chrome_wait_for` 等待加载
  7. `chrome_scroll` 滚动
  8. `chrome_execute_js` 执行自定义 JS（如切换 iframe）

browser_navigate 等 Playwright 工具会启动全新空白浏览器（无登录态），仅用于不需要登录的场景。"""


@dataclass(slots=True)
class LocalRunResult:
    """本地执行结果"""
    run_id: str
    task: str
    success: bool
    output: str
    events: list[RunEvent] = field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0.0
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "model": self.model,
            "event_count": len(self.events),
        }


@dataclass(slots=True)
class JournalEntry:
    """任务执行日志条目，供后续同步到 Hub"""
    entry_id: str
    timestamp: float
    task: str
    run_id: str
    mode: str               # "local" | "delegated"
    model: str
    success: bool
    output: str
    error: str | None
    duration_ms: float
    tool_calls: list[dict[str, Any]]
    synced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "task": self.task,
            "run_id": self.run_id,
            "mode": self.mode,
            "model": self.model,
            "success": self.success,
            "output": self.output[:500],
            "error": self.error,
            "duration_ms": self.duration_ms,
            "tool_calls": self.tool_calls,
            "synced": self.synced,
        }


class TaskJournal:
    """记录本地执行日志，供后续同步到 Hub。"""

    def __init__(self, journal_dir: Path | None = None) -> None:
        self._journal_dir = journal_dir
        self._entries: list[JournalEntry] = []

    def record(
        self,
        *,
        task: str,
        run_id: str,
        mode: str,
        model: str,
        success: bool,
        output: str,
        error: str | None,
        duration_ms: float,
        events: list[RunEvent],
    ) -> JournalEntry:
        tool_calls = [
            {"tool": e.data.get("tool", ""), "success": e.data.get("success")}
            for e in events
            if e.event_type == "tool_result"
        ]
        entry = JournalEntry(
            entry_id=uuid.uuid4().hex[:12],
            timestamp=time.time(),
            task=task,
            run_id=run_id,
            mode=mode,
            model=model,
            success=success,
            output=output,
            error=error,
            duration_ms=duration_ms,
            tool_calls=tool_calls,
        )
        self._entries.append(entry)
        if self._journal_dir:
            self._persist(entry)
        return entry

    def unsynced_entries(self) -> list[JournalEntry]:
        return [e for e in self._entries if not e.synced]

    def mark_synced(self, entry_ids: list[str]) -> None:
        id_set = set(entry_ids)
        for entry in self._entries:
            if entry.entry_id in id_set:
                entry.synced = True

    async def sync_to_hub(
        self,
        *,
        hub_host: str,
        hub_port: int,
        node_id: str,
        max_batch: int = 10,
    ) -> int:
        """Sync unsynced entries to the Hub's /edge/journal/sync endpoint.

        Returns the number of entries successfully synced.
        """
        unsynced = self.unsynced_entries()
        if not unsynced:
            return 0

        batch = unsynced[:max_batch]
        url = f"http://{hub_host}:{hub_port}/edge/journal/sync"
        payload = {
            "node_id": node_id,
            "entries": [e.to_dict() for e in batch],
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        accepted_ids = data.get("entry_ids", [])
                        if accepted_ids:
                            self.mark_synced(accepted_ids)
                            logger.info("Synced %d journal entries to Hub", len(accepted_ids))
                        return len(accepted_ids)
                    else:
                        logger.warning("Hub journal sync returned status %d", resp.status)
                        return 0
        except Exception:
            logger.debug("Journal sync to Hub failed (Hub may be offline)", exc_info=True)
            return 0

    def _persist(self, entry: JournalEntry) -> None:
        try:
            self._journal_dir.mkdir(parents=True, exist_ok=True)
            path = self._journal_dir / f"{entry.entry_id}.json"
            path.write_text(json.dumps(entry.to_dict(), ensure_ascii=False, indent=2))
        except Exception:
            logger.warning("Failed to persist journal entry %s", entry.entry_id, exc_info=True)


class EdgeAgentRuntime:
    """
    边缘节点本地 Agent 运行时。

    复用 nexus.agent.core.execute_tool_loop 实现 tool-calling loop。
    不依赖 Hub 即可独立工作。
    """

    def __init__(
        self,
        *,
        provider: ProviderGateway,
        tools: list[ToolDefinition],
        tools_policy: ToolsPolicy | None = None,
        journal: TaskJournal | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._provider = provider
        self._tools = list(tools)
        self._tools_policy = tools_policy or ToolsPolicy(
            auto_approve_levels=set(),
        )
        self._journal = journal or TaskJournal()
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._stream_callback = stream_callback

    @property
    def provider(self) -> ProviderGateway:
        return self._provider

    @property
    def journal(self) -> TaskJournal:
        return self._journal

    def set_tools(self, tools: list[ToolDefinition]) -> None:
        """更新可用工具集"""
        self._tools = list(tools)

    async def run_local(
        self,
        task: str,
        *,
        context_messages: list[dict[str, Any]] | None = None,
        extra_tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        provider_name: str | None = None,
    ) -> LocalRunResult:
        """
        本地自主模式执行。

        MacBook 用自己的 ProviderGateway 驱动 tool-calling loop。
        provider_name 可指定使用哪个 LLM provider（如 "minimax", "kimi", "ollama"）。
        """
        run_id = f"edge-{uuid.uuid4().hex[:12]}"
        selected = self._provider.get_provider(name=provider_name) if provider_name else self._provider.get_provider()
        model = selected.model
        started = time.perf_counter()

        tools = list(self._tools)
        if extra_tools:
            existing = {t.name for t in tools}
            tools.extend(t for t in extra_tools if t.name not in existing)

        messages: list[dict[str, Any]] = []
        prompt = system_prompt or EDGE_SYSTEM_PROMPT
        messages.append({"role": "system", "content": prompt})

        if context_messages:
            messages.extend(context_messages)

        messages.append({"role": "user", "content": task})

        config = AttemptConfig(
            model=model,
            system_prompt=prompt,
            tools=tools,
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            stream_callback=stream_callback or self._stream_callback,
        )

        self._tools_policy.reset_counts()

        try:
            output, events = await execute_tool_loop(
                config=config,
                provider=self._provider,
                tools_policy=self._tools_policy,
                run_id=run_id,
            )
            duration_ms = (time.perf_counter() - started) * 1000
            result = LocalRunResult(
                run_id=run_id,
                task=task,
                success=True,
                output=output,
                events=events,
                duration_ms=duration_ms,
                model=model,
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            result = LocalRunResult(
                run_id=run_id,
                task=task,
                success=False,
                output="",
                events=[],
                error=str(exc),
                duration_ms=duration_ms,
                model=model,
            )
            logger.error("Local run failed: %s", exc, exc_info=True)

        self._journal.record(
            task=task,
            run_id=run_id,
            mode="local",
            model=model,
            success=result.success,
            output=result.output,
            error=result.error,
            duration_ms=result.duration_ms,
            events=result.events,
        )

        return result

    async def run_delegated(
        self,
        task_description: str,
        *,
        tools: list[ToolDefinition] | None = None,
        constraints: dict[str, Any] | None = None,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> LocalRunResult:
        """
        Hub 委托模式执行。

        Hub 已经规划了任务，MacBook 用自己的 LLM 驱动多步本地工具执行。
        """
        run_id = f"delegated-{uuid.uuid4().hex[:12]}"
        model = self._provider.get_provider().model
        started = time.perf_counter()

        available_tools = tools if tools is not None else self._tools
        constraint_text = ""
        if constraints:
            constraint_text = f"\n\n执行约束：\n{json.dumps(constraints, ensure_ascii=False, indent=2)}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": DELEGATED_SYSTEM_PROMPT},
            {"role": "user", "content": task_description + constraint_text},
        ]

        config = AttemptConfig(
            model=model,
            system_prompt=DELEGATED_SYSTEM_PROMPT,
            tools=available_tools,
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            stream_callback=stream_callback or self._stream_callback,
        )

        self._tools_policy.reset_counts()

        try:
            output, events = await execute_tool_loop(
                config=config,
                provider=self._provider,
                tools_policy=self._tools_policy,
                run_id=run_id,
            )
            duration_ms = (time.perf_counter() - started) * 1000
            result = LocalRunResult(
                run_id=run_id,
                task=task_description,
                success=True,
                output=output,
                events=events,
                duration_ms=duration_ms,
                model=model,
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            result = LocalRunResult(
                run_id=run_id,
                task=task_description,
                success=False,
                output="",
                events=[],
                error=str(exc),
                duration_ms=duration_ms,
                model=model,
            )
            logger.error("Delegated run failed: %s", exc, exc_info=True)

        self._journal.record(
            task=task_description,
            run_id=run_id,
            mode="delegated",
            model=model,
            success=result.success,
            output=result.output,
            error=result.error,
            duration_ms=result.duration_ms,
            events=result.events,
        )

        return result


def build_edge_provider(
    provider_configs: list[dict[str, Any]],
) -> ProviderGateway | None:
    """
    从配置构建边缘节点的 ProviderGateway。

    provider_configs 格式:
    [
        {"name": "kimi", "model": "kimi-k2.5", "base_url": "...", "api_key_env": "KIMI_API_KEY"},
        {"name": "qwen", "model": "qwen3.5-397b-a17b", "base_url": "...", "api_key_env": "QWEN_API_KEY"},
    ]
    """
    if not provider_configs:
        return None

    configs = []
    for raw in provider_configs:
        api_key = raw.get("api_key") or ""
        api_key_env = raw.get("api_key_env") or ""
        if not api_key and api_key_env:
            import os
            api_key = os.getenv(api_key_env, "")

        if not api_key and not api_key_env:
            logger.debug("Skipping provider %s: no API key", raw.get("name", "?"))
            continue

        configs.append(
            ProviderConfig(
                name=raw.get("name", ""),
                model=raw.get("model", ""),
                provider=raw.get("provider", raw.get("provider_type", "")),
                base_url=raw.get("base_url", ""),
                api_key=api_key if api_key else None,
                api_key_env=api_key_env if api_key_env and not api_key else None,
                timeout_seconds=float(raw.get("timeout_seconds", 60)),
                max_retries=int(raw.get("max_retries", 2)),
            )
        )

    if not configs:
        return None

    return ProviderGateway(
        primary=configs[0],
        fallbacks=configs[1:] if len(configs) > 1 else None,
    )
