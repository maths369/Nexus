"""Edge-local tool host for mesh-exposed desktop capabilities."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.agent.types import ToolDefinition, ToolResult, ToolRiskLevel
from nexus.services.workspace import WorkspaceService

logger = logging.getLogger(__name__)


def _make_ssl_context():
    """Create an SSL context for HTTPS requests on macOS conda Python."""
    import ssl as _ssl
    # conda Python on macOS often has broken CA bundle paths;
    # try certifi first, then system certs, then disable verification.
    for ca_source in [
        lambda: "/etc/ssl/cert.pem",  # macOS system certs (most reliable)
        lambda: __import__("certifi").where(),
    ]:
        try:
            path = ca_source()
            ctx = _ssl.create_default_context(cafile=path)
            return ctx
        except Exception:
            continue
    # Last resort: skip verification (local dev only)
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx

CommandRunner = Callable[[list[str], bytes | None], Awaitable["CommandResult"]]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


async def _default_command_runner(args: list[str], stdin: bytes | None = None) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(stdin)
    return CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _ensure_success(result: CommandResult, args: list[str]) -> None:
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args)}"
            + (f" | stderr: {result.stderr.strip()}" if result.stderr.strip() else "")
        )


def _allocate_output_path(path: str | None, *, suffix: str, scratch_dir: Path) -> Path:
    if path:
        target = Path(path).expanduser()
    else:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        target = scratch_dir / f"{uuid.uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def build_tool_spec_map(tools: Iterable[ToolDefinition]) -> dict[str, dict[str, Any]]:
    """Serialize tool metadata into a NodeCard-friendly shape."""
    specs: dict[str, dict[str, Any]] = {}
    for tool in tools:
        specs[tool.name] = {
            "description": tool.description,
            "parameters": tool.parameters,
            "risk_level": tool.risk_level.value,
            "requires_approval": tool.requires_approval,
            "tags": list(tool.tags),
        }
    return specs


class EdgeToolExecutor:
    """Executes structured tool calls on an edge node."""

    def __init__(self, tools: list[ToolDefinition]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def definitions(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def definition(self, tool_name: str) -> ToolDefinition | None:
        return self._tools.get(tool_name)

    def tool_names(self) -> set[str]:
        return set(self._tools)

    def tool_specs(self) -> dict[str, dict[str, Any]]:
        return build_tool_spec_map(self._tools.values())

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ValueError(f"Unknown edge tool: {tool_name}")

        started = time.perf_counter()
        try:
            output = await tool.handler(**arguments)
            return ToolResult(
                call_id=uuid.uuid4().hex[:12],
                tool_name=tool_name,
                success=True,
                output=output if isinstance(output, str) else _json(output),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            return ToolResult(
                call_id=uuid.uuid4().hex[:12],
                tool_name=tool_name,
                success=False,
                output="",
                error=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )


def build_edge_tool_registry(
    *,
    workspace_service: WorkspaceService,
    browser_service: Any | None = None,
    command_runner: CommandRunner | None = None,
    enable_macos_tools: bool | None = None,
    scratch_dir: Path | None = None,
) -> list[ToolDefinition]:
    """Build the minimal local tool set exposed by an edge node."""

    runner = command_runner or _default_command_runner
    macos_enabled = platform.system() == "Darwin" if enable_macos_tools is None else enable_macos_tools
    browser_enabled = browser_service is not None and bool(getattr(browser_service, "enabled", True))
    shortcuts_enabled = macos_enabled and shutil.which("shortcuts") is not None
    applescript_enabled = macos_enabled and shutil.which("osascript") is not None
    artifacts_dir = scratch_dir or (Path(tempfile.gettempdir()) / "nexus-edge")

    async def list_local_files(path: str = ".", pattern: str = "*", recursive: bool = False) -> str:
        items = workspace_service.list_dir(path, pattern=pattern, recursive=recursive)
        return _json([str(item) for item in items])

    async def code_read_file(path: str) -> str:
        return workspace_service.read_text(path)

    tools = [
        ToolDefinition(
            name="list_local_files",
            description="List files and directories inside the edge node workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string", "default": "*"},
                    "recursive": {"type": "boolean", "default": False},
                },
            },
            handler=list_local_files,
            risk_level=ToolRiskLevel.LOW,
            tags=["workspace", "read", "edge"],
        ),
        ToolDefinition(
            name="code_read_file",
            description="Read a text file from the edge node workspace.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=code_read_file,
            risk_level=ToolRiskLevel.LOW,
            tags=["workspace", "read", "edge"],
        ),
    ]

    if browser_enabled:
        async def browser_navigate(url: str) -> str:
            return _json(await browser_service.navigate(url))

        async def browser_extract_text(selector: str | None = None) -> str:
            return _json(await browser_service.extract_text(selector))

        async def browser_screenshot(path: str | None = None) -> str:
            return _json(await browser_service.screenshot(path))

        async def browser_fill_form(fields: dict[str, Any]) -> str:
            return _json(await browser_service.fill_form(fields))

        tools.extend([
            ToolDefinition(
                name="browser_navigate",
                description="Navigate the authenticated browser session to a URL.",
                parameters={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                handler=browser_navigate,
                risk_level=ToolRiskLevel.LOW,
                tags=["browser", "web", "edge"],
            ),
            ToolDefinition(
                name="browser_extract_text",
                description="Extract text from the current browser page.",
                parameters={
                    "type": "object",
                    "properties": {"selector": {"type": "string"}},
                },
                handler=browser_extract_text,
                risk_level=ToolRiskLevel.LOW,
                tags=["browser", "web", "extract", "edge"],
            ),
            ToolDefinition(
                name="browser_screenshot",
                description="Capture a screenshot of the current browser page.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
                handler=browser_screenshot,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["browser", "web", "screenshot", "edge"],
            ),
            ToolDefinition(
                name="browser_fill_form",
                description="Fill fields in the current browser page using selector-to-value mappings.",
                parameters={
                    "type": "object",
                    "properties": {
                        "fields": {
                            "type": "object",
                            "description": "Mapping of CSS selectors to values.",
                        }
                    },
                    "required": ["fields"],
                },
                handler=browser_fill_form,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["browser", "web", "form", "edge"],
            ),
        ])

    if macos_enabled:
        async def capture_screen(path: str | None = None) -> str:
            target = _allocate_output_path(path, suffix=".png", scratch_dir=artifacts_dir)
            args = ["screencapture", "-x", str(target)]
            result = await runner(args, None)
            _ensure_success(result, args)
            return _json({"path": str(target)})

        async def record_screen(
            path: str | None = None,
            duration_seconds: int = 10,
            capture_audio: bool = False,
            show_clicks: bool = False,
        ) -> str:
            if duration_seconds <= 0:
                raise ValueError("duration_seconds must be > 0")
            target = _allocate_output_path(path, suffix=".mov", scratch_dir=artifacts_dir)
            args = ["screencapture", "-x", "-v", "-V", str(int(duration_seconds))]
            if capture_audio:
                args.append("-g")
            if show_clicks:
                args.append("-k")
            args.append(str(target))
            result = await runner(args, None)
            _ensure_success(result, args)
            return _json({"path": str(target), "duration_seconds": int(duration_seconds)})

        async def read_clipboard() -> str:
            args = ["pbpaste"]
            result = await runner(args, None)
            _ensure_success(result, args)
            return _json({"content": result.stdout})

        async def write_clipboard(content: str) -> str:
            args = ["pbcopy"]
            payload = content.encode("utf-8")
            result = await runner(args, payload)
            _ensure_success(result, args)
            return _json({"ok": True, "bytes": len(payload)})

        tools.extend([
            ToolDefinition(
                name="capture_screen",
                description="Capture the current macOS screen to an image file.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
                handler=capture_screen,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["screen", "macos", "edge"],
            ),
            ToolDefinition(
                name="record_screen",
                description="Record the current macOS screen to a video file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "duration_seconds": {"type": "integer", "default": 10},
                        "capture_audio": {"type": "boolean", "default": False},
                        "show_clicks": {"type": "boolean", "default": False},
                    },
                },
                handler=record_screen,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["screen", "macos", "edge"],
            ),
            ToolDefinition(
                name="read_clipboard",
                description="Read text content from the macOS clipboard.",
                parameters={"type": "object", "properties": {}},
                handler=read_clipboard,
                risk_level=ToolRiskLevel.LOW,
                tags=["clipboard", "macos", "edge"],
            ),
            ToolDefinition(
                name="write_clipboard",
                description="Write text content to the macOS clipboard.",
                parameters={
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
                handler=write_clipboard,
                risk_level=ToolRiskLevel.MEDIUM,
                tags=["clipboard", "macos", "edge"],
            ),
        ])

        if shortcuts_enabled:
            async def list_shortcuts() -> str:
                args = ["shortcuts", "list"]
                result = await runner(args, None)
                _ensure_success(result, args)
                shortcuts = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                return _json({"shortcuts": shortcuts})

            async def run_shortcut(
                name: str,
                input_path: str | None = None,
                output_path: str | None = None,
                output_type: str | None = None,
            ) -> str:
                args = ["shortcuts", "run", name]
                if input_path:
                    args.extend(["--input-path", input_path])
                if output_path:
                    args.extend(["--output-path", output_path])
                if output_type:
                    args.extend(["--output-type", output_type])
                result = await runner(args, None)
                _ensure_success(result, args)
                payload: dict[str, Any] = {"shortcut": name}
                if input_path:
                    payload["input_path"] = input_path
                if output_path:
                    payload["output_path"] = output_path
                if output_type:
                    payload["output_type"] = output_type
                if result.stdout.strip():
                    payload["stdout"] = result.stdout.strip()
                if result.stderr.strip():
                    payload["stderr"] = result.stderr.strip()
                return _json(payload)

            tools.extend([
                ToolDefinition(
                    name="list_shortcuts",
                    description="List available Apple Shortcuts on this Mac.",
                    parameters={"type": "object", "properties": {}},
                    handler=list_shortcuts,
                    risk_level=ToolRiskLevel.LOW,
                    tags=["shortcuts", "macos", "automation", "edge"],
                ),
                ToolDefinition(
                    name="run_shortcut",
                    description="Run a named Apple Shortcut on this Mac.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "input_path": {"type": "string"},
                            "output_path": {"type": "string"},
                            "output_type": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                    handler=run_shortcut,
                    risk_level=ToolRiskLevel.HIGH,
                    requires_approval=True,
                    tags=["shortcuts", "macos", "automation", "edge"],
                ),
            ])

        if applescript_enabled:
            async def run_applescript(script: str, language: str = "AppleScript") -> str:
                args = ["osascript"]
                normalized_language = language.strip() if language else "AppleScript"
                if normalized_language and normalized_language.lower() != "applescript":
                    args.extend(["-l", normalized_language])
                args.extend(["-e", script])
                result = await runner(args, None)
                _ensure_success(result, args)
                payload: dict[str, Any] = {"language": normalized_language}
                if result.stdout.strip():
                    payload["stdout"] = result.stdout.strip()
                if result.stderr.strip():
                    payload["stderr"] = result.stderr.strip()
                return _json(payload)

            tools.append(
                ToolDefinition(
                    name="run_applescript",
                    description="Run AppleScript or JXA against the local macOS session.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "script": {"type": "string"},
                            "language": {"type": "string", "default": "AppleScript"},
                        },
                        "required": ["script"],
                    },
                    handler=run_applescript,
                    risk_level=ToolRiskLevel.CRITICAL,
                    requires_approval=True,
                    tags=["applescript", "macos", "automation", "edge"],
                )
            )

    # ==================================================================
    # Lightweight deterministic tools — work at tool level, save tokens
    # Inspired by OpenClaw's web_fetch / web_search / exec architecture
    # ==================================================================

    # ---- web_fetch: HTTP 抓取 + Readability 提取干净文本 ----

    async def web_fetch(url: str, extract_mode: str = "markdown", max_chars: int = 50000) -> str:
        """Fetch a URL and extract readable content (no browser needed)."""
        import aiohttp
        try:
            from trafilatura import extract as trafilatura_extract
        except ImportError:
            trafilatura_extract = None
        try:
            from readability import Document as ReadabilityDocument
        except ImportError:
            ReadabilityDocument = None

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        connector = aiohttp.TCPConnector(ssl=_make_ssl_context())
        async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
                                   allow_redirects=True, max_redirects=5) as resp:
                if resp.status >= 400:
                    return _json({"error": f"HTTP {resp.status}", "url": url})
                content_type = resp.content_type or ""
                # For non-HTML, return raw text (JSON, plain text, etc.)
                if "html" not in content_type and "xml" not in content_type:
                    raw = await resp.text(errors="replace")
                    if len(raw) > max_chars:
                        raw = raw[:max_chars]
                    return _json({
                        "url": str(resp.url), "content_type": content_type,
                        "text": raw, "length": len(raw), "truncated": len(raw) >= max_chars,
                    })
                html = await resp.text(errors="replace")

        title = ""
        text = ""

        # Strategy 1: trafilatura (best for article extraction)
        if trafilatura_extract is not None:
            try:
                extracted = trafilatura_extract(
                    html, include_links=True, include_tables=True,
                    output_format="txt" if extract_mode == "text" else "txt",
                    no_fallback=False,
                )
                if extracted and len(extracted.strip()) > 100:
                    text = extracted.strip()
            except Exception as exc:
                logger.debug("trafilatura failed: %s", exc)

        # Strategy 2: readability-lxml fallback
        if not text and ReadabilityDocument is not None:
            try:
                doc = ReadabilityDocument(html)
                title = doc.short_title() or ""
                summary_html = doc.summary()
                # Strip HTML tags for clean text
                from lxml.html import fromstring as lxml_parse, tostring as lxml_tostring
                tree = lxml_parse(summary_html)
                text = tree.text_content().strip()
            except Exception as exc:
                logger.debug("readability failed: %s", exc)

        # Strategy 3: BeautifulSoup fallback
        if not text:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")
                # Remove script/style
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                tag_title = soup.find("title")
                if tag_title:
                    title = tag_title.get_text(strip=True)
            except Exception as exc:
                logger.debug("bs4 fallback failed: %s", exc)
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()

        # Truncate to max_chars
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        return _json({
            "url": str(url),
            "title": title,
            "text": text,
            "length": len(text),
            "truncated": truncated,
            "extractor": "trafilatura" if trafilatura_extract and text else "readability",
        })

    tools.append(ToolDefinition(
        name="web_fetch",
        description=(
            "Fetch a URL via HTTP and extract readable text content. "
            "Lightweight — no browser launch, returns clean text/markdown. "
            "Use this for reading web page content, articles, documentation, etc."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "extract_mode": {
                    "type": "string", "enum": ["text", "markdown"], "default": "markdown",
                    "description": "Output format: 'text' (plain) or 'markdown'",
                },
                "max_chars": {
                    "type": "integer", "default": 50000,
                    "description": "Maximum characters to return (truncates if longer)",
                },
            },
            "required": ["url"],
        },
        handler=web_fetch,
        risk_level=ToolRiskLevel.LOW,
        tags=["web", "fetch", "extract", "edge"],
    ))

    # ---- web_search: 网页搜索 ----

    async def web_search(query: str, count: int = 5, engine: str = "duckduckgo") -> str:
        """Search the web and return results (title + URL + snippet)."""
        import aiohttp

        results: list[dict[str, str]] = []

        if engine == "duckduckgo":
            # Use DuckDuckGo HTML search (no API key needed)
            params = {"q": query, "kl": "cn-zh"}
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=_make_ssl_context()),
                    trust_env=True,
                ) as session:
                    async with session.get(
                        "https://html.duckduckgo.com/html/",
                        params=params, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        html = await resp.text(errors="replace")
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "lxml")
                for result_div in soup.select(".result")[:count]:
                    title_el = result_div.select_one(".result__title a, .result__a")
                    snippet_el = result_div.select_one(".result__snippet")
                    if title_el:
                        href = title_el.get("href", "")
                        # DuckDuckGo wraps URLs in redirect
                        if "uddg=" in str(href):
                            from urllib.parse import unquote, urlparse, parse_qs
                            parsed = parse_qs(urlparse(str(href)).query)
                            href = unquote(parsed.get("uddg", [str(href)])[0])
                        results.append({
                            "title": title_el.get_text(strip=True),
                            "url": str(href),
                            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                        })
            except Exception as exc:
                return _json({"error": str(exc), "query": query, "engine": engine})

        elif engine == "brave":
            # Brave Search API (requires BRAVE_API_KEY env var)
            import os
            api_key = os.environ.get("BRAVE_API_KEY", "")
            if not api_key:
                return _json({"error": "BRAVE_API_KEY not set", "query": query})
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=_make_ssl_context()),
                    trust_env=True,
                ) as session:
                    async with session.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": count},
                        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        data = await resp.json()
                for item in (data.get("web", {}).get("results", []))[:count]:
                    results.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": item.get("description", ""),
                    })
            except Exception as exc:
                return _json({"error": str(exc), "query": query, "engine": engine})

        return _json({"query": query, "engine": engine, "count": len(results), "results": results})

    tools.append(ToolDefinition(
        name="web_search",
        description=(
            "Search the web using a search engine and return results "
            "(title, URL, snippet). No browser needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "count": {"type": "integer", "default": 5, "description": "Number of results (max 10)"},
                "engine": {
                    "type": "string", "enum": ["duckduckgo", "brave"], "default": "duckduckgo",
                    "description": "Search engine to use",
                },
            },
            "required": ["query"],
        },
        handler=web_search,
        risk_level=ToolRiskLevel.LOW,
        tags=["web", "search", "edge"],
    ))

    # ---- system_exec: 执行 shell 命令 ----

    async def system_exec(
        command: str,
        workdir: str | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        """Execute a shell command and return stdout/stderr."""
        cwd = workdir or str(workspace_service.root)
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return _json({"error": f"Command timed out after {timeout_seconds}s", "command": command})

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            # Truncate long output
            max_out = 50000
            truncated = len(stdout) > max_out
            if truncated:
                stdout = stdout[:max_out] + f"\n... (truncated, total {len(stdout_bytes)} bytes)"

            return _json({
                "command": command,
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr[:5000] if stderr else "",
                "truncated": truncated,
            })
        except Exception as exc:
            return _json({"error": str(exc), "command": command})

    tools.append(ToolDefinition(
        name="system_exec",
        description=(
            "Execute a shell command on the local machine and return output. "
            "Use for: file operations, system info, running CLI tools (curl, jq, git, etc.), "
            "installing packages, and any deterministic task that doesn't need LLM reasoning."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "workdir": {"type": "string", "description": "Working directory (default: workspace root)"},
                "timeout_seconds": {"type": "integer", "default": 30, "description": "Max execution time"},
            },
            "required": ["command"],
        },
        handler=system_exec,
        risk_level=ToolRiskLevel.HIGH,
        requires_approval=True,
        tags=["exec", "shell", "system", "edge"],
    ))

    # ---- pdf_extract: PDF 文本提取 ----

    async def pdf_extract(path: str, page_range: str | None = None, max_chars: int = 50000) -> str:
        """Extract text from a PDF file."""
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            return _json({"error": f"File not found: {path}"})

        text_parts: list[str] = []
        page_count = 0

        # Parse page range (e.g., "1-5", "1,3,5-10")
        target_pages: set[int] | None = None
        if page_range:
            target_pages = set()
            for part in page_range.split(","):
                part = part.strip()
                if "-" in part:
                    start, end = part.split("-", 1)
                    for p in range(int(start), int(end) + 1):
                        target_pages.add(p)
                else:
                    target_pages.add(int(part))

        try:
            # Try pypdf first
            from pypdf import PdfReader
            reader = PdfReader(str(file_path))
            page_count = len(reader.pages)
            for i, page in enumerate(reader.pages, 1):
                if target_pages and i not in target_pages:
                    continue
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(f"--- Page {i} ---\n{page_text.strip()}")
        except ImportError:
            pass

        if not text_parts:
            try:
                # Try pdfplumber
                import pdfplumber
                with pdfplumber.open(str(file_path)) as pdf:
                    page_count = len(pdf.pages)
                    for i, page in enumerate(pdf.pages, 1):
                        if target_pages and i not in target_pages:
                            continue
                        page_text = page.extract_text() or ""
                        if page_text.strip():
                            text_parts.append(f"--- Page {i} ---\n{page_text.strip()}")
            except ImportError:
                return _json({"error": "No PDF library available (need pypdf or pdfplumber)"})

        full_text = "\n\n".join(text_parts)
        truncated = len(full_text) > max_chars
        if truncated:
            full_text = full_text[:max_chars]

        return _json({
            "path": str(file_path),
            "pages": page_count,
            "extracted_pages": len(text_parts),
            "text": full_text,
            "length": len(full_text),
            "truncated": truncated,
        })

    tools.append(ToolDefinition(
        name="pdf_extract",
        description=(
            "Extract text content from a PDF file. Returns clean text per page. "
            "Use for reading documents, contracts, reports, papers, etc."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to PDF file"},
                "page_range": {
                    "type": "string",
                    "description": "Page range to extract (e.g., '1-5', '1,3,5-10'). Default: all pages",
                },
                "max_chars": {"type": "integer", "default": 50000, "description": "Max characters to return"},
            },
            "required": ["path"],
        },
        handler=pdf_extract,
        risk_level=ToolRiskLevel.LOW,
        tags=["pdf", "extract", "document", "edge"],
    ))

    # ---- hash_file: 文件哈希计算 ----

    async def hash_file(path: str, algorithm: str = "sha256") -> str:
        """Compute hash of a file."""
        import hashlib
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            return _json({"error": f"File not found: {path}"})
        h = hashlib.new(algorithm)
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return _json({
            "path": str(file_path),
            "algorithm": algorithm,
            "hash": h.hexdigest(),
            "size_bytes": file_path.stat().st_size,
        })

    tools.append(ToolDefinition(
        name="hash_file",
        description="Compute hash (sha256/md5/sha1) of a file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file"},
                "algorithm": {"type": "string", "enum": ["sha256", "md5", "sha1"], "default": "sha256"},
            },
            "required": ["path"],
        },
        handler=hash_file,
        risk_level=ToolRiskLevel.LOW,
        tags=["file", "hash", "edge"],
    ))

    # ---- json_extract: JSON 数据查询 ----

    async def json_extract(path: str, jq_expr: str = ".") -> str:
        """Extract data from a JSON file using jq-like expressions."""
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            return _json({"error": f"File not found: {path}"})
        # Use jq if available, else Python fallback
        if shutil.which("jq"):
            proc = await asyncio.create_subprocess_exec(
                "jq", jq_expr, str(file_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                return _json({"error": stderr.decode("utf-8", errors="replace").strip()})
            result_text = stdout.decode("utf-8", errors="replace").strip()
            if len(result_text) > 50000:
                result_text = result_text[:50000] + "\n... (truncated)"
            return result_text
        else:
            # Python fallback: only supports "." (full doc)
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return _json(data)

    tools.append(ToolDefinition(
        name="json_extract",
        description="Extract data from a JSON file using jq expressions. Fast and no LLM needed.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to JSON file"},
                "jq_expr": {"type": "string", "default": ".", "description": "jq expression (e.g., '.data[].name')"},
            },
            "required": ["path"],
        },
        handler=json_extract,
        risk_level=ToolRiskLevel.LOW,
        tags=["json", "extract", "data", "edge"],
    ))

    # ---- text_search: 在文件/目录中搜索文本 ----

    async def text_search(
        pattern: str,
        path: str = ".",
        file_glob: str = "*",
        max_results: int = 50,
        ignore_case: bool = True,
    ) -> str:
        """Search for text pattern in files using ripgrep or grep."""
        search_path = Path(path).expanduser()
        if not search_path.exists():
            return _json({"error": f"Path not found: {path}"})

        rg = shutil.which("rg")
        if rg:
            args = [rg, pattern, str(search_path), "--max-count", "3",
                    "-g", file_glob, "--json"]
            if ignore_case:
                args.append("-i")
        else:
            grep = shutil.which("grep") or "grep"
            args = [grep, "-rn", pattern, str(search_path), "--include", file_glob]
            if ignore_case:
                args.append("-i")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")

        # Parse results
        matches: list[dict[str, Any]] = []
        if rg:
            for line in output.splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("type") == "match":
                        data = entry["data"]
                        matches.append({
                            "file": data["path"]["text"],
                            "line": data["line_number"],
                            "text": data["lines"]["text"].strip(),
                        })
                except (json.JSONDecodeError, KeyError):
                    continue
        else:
            for line in output.splitlines()[:max_results]:
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    matches.append({"file": parts[0], "line": int(parts[1]) if parts[1].isdigit() else 0, "text": parts[2].strip()})

        return _json({"pattern": pattern, "path": str(search_path), "count": len(matches), "matches": matches[:max_results]})

    tools.append(ToolDefinition(
        name="text_search",
        description=(
            "Search for a text pattern in files using ripgrep/grep. "
            "Fast code search — no LLM reasoning needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern (regex)"},
                "path": {"type": "string", "default": ".", "description": "Directory or file to search in"},
                "file_glob": {"type": "string", "default": "*", "description": "File glob filter (e.g., '*.py')"},
                "max_results": {"type": "integer", "default": 50},
                "ignore_case": {"type": "boolean", "default": True},
            },
            "required": ["pattern"],
        },
        handler=text_search,
        risk_level=ToolRiskLevel.LOW,
        tags=["search", "grep", "code", "edge"],
    ))

    # ---- system_info: 获取系统信息 ----

    async def system_info() -> str:
        """Get local system information."""
        import os
        info: dict[str, Any] = {
            "platform": platform.system(),
            "platform_version": platform.version(),
            "machine": platform.machine(),
            "hostname": platform.node(),
            "cpu_count": os.cpu_count(),
        }
        # Memory
        try:
            result = await runner(["sysctl", "-n", "hw.memsize"], None)
            if result.returncode == 0:
                info["memory_bytes"] = int(result.stdout.strip())
                info["memory_gb"] = round(int(result.stdout.strip()) / (1024**3), 1)
        except Exception:
            pass
        # Disk
        try:
            stat = os.statvfs("/")
            info["disk_free_gb"] = round(stat.f_bavail * stat.f_frsize / (1024**3), 1)
            info["disk_total_gb"] = round(stat.f_blocks * stat.f_frsize / (1024**3), 1)
        except Exception:
            pass
        # Load average
        try:
            info["load_avg"] = list(os.getloadavg())
        except Exception:
            pass
        # Current user
        info["user"] = os.environ.get("USER", "unknown")
        info["home"] = str(Path.home())
        info["cwd"] = os.getcwd()
        return _json(info)

    tools.append(ToolDefinition(
        name="system_info",
        description="Get local system information: OS, CPU, memory, disk, hostname, current user.",
        parameters={"type": "object", "properties": {}},
        handler=system_info,
        risk_level=ToolRiskLevel.LOW,
        tags=["system", "info", "edge"],
    ))

    # ---- datetime_now: 获取当前时间 ----

    async def datetime_now(timezone: str = "local") -> str:
        """Get current date and time."""
        from datetime import datetime, timezone as tz
        now_utc = datetime.now(tz.utc)
        if timezone == "local":
            import time as _time
            offset = _time.timezone if _time.daylight == 0 else _time.altzone
            local_now = datetime.now()
            return _json({
                "local": local_now.strftime("%Y-%m-%d %H:%M:%S"),
                "utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                "weekday": local_now.strftime("%A"),
                "weekday_cn": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][local_now.weekday()],
                "timezone_offset_hours": -offset / 3600,
            })
        return _json({"utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"), "weekday": now_utc.strftime("%A")})

    tools.append(ToolDefinition(
        name="datetime_now",
        description="Get current date, time, and weekday. No LLM reasoning needed.",
        parameters={
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "default": "local", "description": "'local' or 'utc'"},
            },
        },
        handler=datetime_now,
        risk_level=ToolRiskLevel.LOW,
        tags=["time", "date", "utility", "edge"],
    ))

    # ---- weather: 天气查询 ----

    async def weather(location: str = "", format: str = "short") -> str:
        """Get weather information via wttr.in (no API key needed)."""
        import aiohttp
        loc = location or ""
        fmt = "?format=j1" if format == "json" else "?format=%l:+%c+%t+%h+%w"
        url = f"https://wttr.in/{loc}{fmt}"
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=_make_ssl_context()),
                trust_env=True,
            ) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "curl/7.0", "Accept-Language": "zh-CN,zh;q=0.9"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if format == "json":
                        data = await resp.json(content_type=None)
                        current = data.get("current_condition", [{}])[0]
                        area = data.get("nearest_area", [{}])[0]
                        desc_list = current.get("lang_zh") or current.get("weatherDesc") or [{}]
                        desc = desc_list[0].get("value", "") if desc_list else ""
                        return _json({
                            "location": area.get("areaName", [{}])[0].get("value", loc),
                            "country": area.get("country", [{}])[0].get("value", ""),
                            "temp_c": current.get("temp_C", ""),
                            "feels_like_c": current.get("FeelsLikeC", ""),
                            "humidity": current.get("humidity", ""),
                            "wind_speed_kmph": current.get("windspeedKmph", ""),
                            "wind_dir": current.get("winddir16Point", ""),
                            "description": desc,
                            "visibility_km": current.get("visibility", ""),
                            "uv_index": current.get("uvIndex", ""),
                        })
                    else:
                        text = await resp.text()
                        return _json({"location": loc or "auto", "weather": text.strip()})
        except Exception as exc:
            return _json({"error": str(exc), "location": loc})

    tools.append(ToolDefinition(
        name="weather",
        description="Get current weather for a location. No API key needed. Examples: '北京', 'Tokyo', 'London'.",
        parameters={
            "type": "object",
            "properties": {
                "location": {"type": "string", "default": "", "description": "City name (empty = auto-detect)"},
                "format": {"type": "string", "enum": ["short", "json"], "default": "json"},
            },
        },
        handler=weather,
        risk_level=ToolRiskLevel.LOW,
        tags=["weather", "utility", "edge"],
    ))

    # ---- http_request: 通用 HTTP 请求 ----

    async def http_request(
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        max_chars: int = 50000,
    ) -> str:
        """Make an HTTP request and return the response."""
        import aiohttp
        req_headers = {"User-Agent": "Nexus-Sidecar/1.0"}
        if headers:
            req_headers.update(headers)
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=_make_ssl_context()),
                trust_env=True,
            ) as session:
                async with session.request(
                    method, url, headers=req_headers,
                    data=body.encode("utf-8") if body else None,
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=True,
                ) as resp:
                    content_type = resp.content_type or ""
                    text = await resp.text(errors="replace")
                    truncated = len(text) > max_chars
                    if truncated:
                        text = text[:max_chars]
                    return _json({
                        "status": resp.status,
                        "content_type": content_type,
                        "headers": dict(resp.headers),
                        "body": text,
                        "truncated": truncated,
                    })
        except Exception as exc:
            return _json({"error": str(exc), "url": url})

    tools.append(ToolDefinition(
        name="http_request",
        description=(
            "Make an HTTP request (GET/POST/PUT/DELETE) and return the response. "
            "Use for calling APIs, webhooks, checking endpoints, etc."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Request URL"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"], "default": "GET"},
                "headers": {"type": "object", "description": "Request headers"},
                "body": {"type": "string", "description": "Request body (for POST/PUT)"},
                "max_chars": {"type": "integer", "default": 50000},
            },
            "required": ["url"],
        },
        handler=http_request,
        risk_level=ToolRiskLevel.MEDIUM,
        tags=["http", "api", "web", "edge"],
    ))

    # ---- file_write: 写入文件 ----

    async def file_write(path: str, content: str, append: bool = False) -> str:
        """Write content to a file."""
        file_path = Path(path).expanduser()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(file_path, mode, encoding="utf-8") as f:
            f.write(content)
        return _json({
            "path": str(file_path),
            "bytes_written": len(content.encode("utf-8")),
            "append": append,
        })

    tools.append(ToolDefinition(
        name="file_write",
        description="Write or append text content to a file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write to"},
                "content": {"type": "string", "description": "Content to write"},
                "append": {"type": "boolean", "default": False, "description": "Append instead of overwrite"},
            },
            "required": ["path", "content"],
        },
        handler=file_write,
        risk_level=ToolRiskLevel.MEDIUM,
        tags=["file", "write", "edge"],
    ))

    # ---- process_list: 查看运行中的进程 ----

    async def process_list(filter: str = "") -> str:
        """List running processes, optionally filtered by name."""
        args = ["ps", "aux"]
        result = await runner(args, None)
        lines = result.stdout.strip().splitlines()
        if not lines:
            return _json({"processes": [], "count": 0})
        header = lines[0]
        processes: list[dict[str, str]] = []
        for line in lines[1:]:
            if filter and filter.lower() not in line.lower():
                continue
            parts = line.split(None, 10)
            if len(parts) >= 11:
                processes.append({
                    "user": parts[0], "pid": parts[1],
                    "cpu": parts[2], "mem": parts[3],
                    "command": parts[10],
                })
        # Limit to 100
        return _json({"count": len(processes), "processes": processes[:100]})

    tools.append(ToolDefinition(
        name="process_list",
        description="List running processes on the system, optionally filtered by name.",
        parameters={
            "type": "object",
            "properties": {
                "filter": {"type": "string", "default": "", "description": "Filter by process name"},
            },
        },
        handler=process_list,
        risk_level=ToolRiskLevel.LOW,
        tags=["system", "process", "edge"],
    ))

    # ---- open_url: 用默认浏览器打开 URL ----

    if macos_enabled:
        async def open_url(url: str) -> str:
            """Open a URL in the default browser."""
            args = ["open", url]
            result = await runner(args, None)
            return _json({"url": url, "opened": result.returncode == 0})

        tools.append(ToolDefinition(
            name="open_url",
            description="Open a URL in the user's default browser (macOS 'open' command).",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to open"}},
                "required": ["url"],
            },
            handler=open_url,
            risk_level=ToolRiskLevel.LOW,
            tags=["browser", "macos", "edge"],
        ))

        # ---- open_app: 打开/激活应用程序 ----

        async def open_app(name: str) -> str:
            """Open or activate a macOS application by name."""
            args = ["open", "-a", name]
            result = await runner(args, None)
            if result.returncode != 0:
                return _json({"error": f"Failed to open {name}: {result.stderr.strip()}", "app": name})
            return _json({"app": name, "opened": True})

        tools.append(ToolDefinition(
            name="open_app",
            description="Open or activate a macOS application by name (e.g., 'Google Chrome', 'Finder', 'Terminal').",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Application name"}},
                "required": ["name"],
            },
            handler=open_app,
            risk_level=ToolRiskLevel.LOW,
            tags=["app", "macos", "edge"],
        ))

        # ---- notification: 发送 macOS 通知 ----

        async def notification(title: str, message: str, sound: bool = True) -> str:
            """Send a macOS notification."""
            script = f'display notification "{message}" with title "{title}"'
            if sound:
                script += ' sound name "default"'
            args = ["osascript", "-e", script]
            result = await runner(args, None)
            return _json({"title": title, "message": message, "sent": result.returncode == 0})

        tools.append(ToolDefinition(
            name="notification",
            description="Send a macOS desktop notification.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Notification title"},
                    "message": {"type": "string", "description": "Notification body text"},
                    "sound": {"type": "boolean", "default": True},
                },
                "required": ["title", "message"],
            },
            handler=notification,
            risk_level=ToolRiskLevel.LOW,
            tags=["notification", "macos", "edge"],
        ))

    # ==================================================================
    # Chrome 浏览器交互工具 — 高阶封装，基于 AppleScript + JavaScript
    # 用于操作用户已登录的 Chrome 浏览器中的网页（邮箱、社交媒体等）
    # ==================================================================

    if macos_enabled and shutil.which("osascript") is not None:

        async def _chrome_exec_js(js_code: str, tab_index: int = 0) -> str:
            """Helper: Execute JavaScript in the active Chrome tab via AppleScript.
            Uses stdin to avoid shell escaping issues with complex JS code."""
            # Escape backslashes and double quotes for AppleScript string embedding
            escaped_js = js_code.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            script = (
                f'tell application "Google Chrome"\n'
                f'  set targetTab to tab {tab_index + 1} of front window\n'
                f'  execute targetTab javascript "{escaped_js}"\n'
                f'end tell\n'
            )
            # Use stdin to pass script — avoids shell escaping issues with -e
            result = await runner(["osascript"], script.encode("utf-8"))
            if result.returncode != 0:
                return _json({"error": result.stderr.strip() or "AppleScript execution failed"})
            # Clean surrogate characters that may come from web pages (emoji etc.)
            # These cause 'surrogates not allowed' errors in JSON/HTTP encoding.
            output = result.stdout.strip()
            output = output.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
            return output

        # ---- chrome_get_tab_info: 获取当前 Chrome 标签页信息 ----

        async def chrome_get_tab_info() -> str:
            """Get info about all open Chrome tabs."""
            script = '''
tell application "Google Chrome"
    set output to ""
    set windowCount to count of windows
    repeat with w from 1 to windowCount
        set tabCount to count of tabs of window w
        repeat with t from 1 to tabCount
            set tabTitle to title of tab t of window w
            set tabUrl to URL of tab t of window w
            set output to output & "W" & w & "T" & t & "|" & tabTitle & "|" & tabUrl & linefeed
        end repeat
    end repeat
    return output
end tell
'''
            result = await runner(["osascript"], script.encode("utf-8"))
            if result.returncode != 0:
                return _json({"error": result.stderr.strip()})
            tabs = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("|", 2)
                if len(parts) >= 3:
                    tabs.append({"ref": parts[0], "title": parts[1], "url": parts[2]})
            return _json({"tabs": tabs})

        tools.append(ToolDefinition(
            name="chrome_get_tab_info",
            description="List all open Chrome tabs with their titles and URLs.",
            parameters={"type": "object", "properties": {}},
            handler=chrome_get_tab_info,
            risk_level=ToolRiskLevel.LOW,
            tags=["chrome", "browser", "macos", "edge"],
        ))

        # ---- chrome_navigate: 在 Chrome 中导航到 URL ----

        async def chrome_navigate(url: str, new_tab: bool = False) -> str:
            """Navigate Chrome to a URL."""
            if new_tab:
                script = f'''
tell application "Google Chrome"
    activate
    tell front window
        set newTab to make new tab with properties {{URL:"{url}"}}
    end tell
    return "ok"
end tell
'''
            else:
                script = f'''
tell application "Google Chrome"
    activate
    set URL of active tab of front window to "{url}"
    return "ok"
end tell
'''
            result = await runner(["osascript"], script.encode("utf-8"))
            if result.returncode != 0:
                return _json({"error": result.stderr.strip()})
            # Wait for page load
            await asyncio.sleep(2)
            return _json({"url": url, "navigated": True, "new_tab": new_tab})

        tools.append(ToolDefinition(
            name="chrome_navigate",
            description="Navigate Chrome's active tab to a URL (or open in new tab). Waits 2s for page load.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "new_tab": {"type": "boolean", "default": False, "description": "Open in new tab"},
                },
                "required": ["url"],
            },
            handler=chrome_navigate,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["chrome", "browser", "macos", "edge"],
        ))

        # ---- chrome_get_page_text: 提取当前页面文字内容 ----

        async def chrome_get_page_text(selector: str = "body", max_length: int = 30000, iframe: str = "") -> str:
            """Extract text content from the current Chrome page or a specific element."""
            iframe_prefix = ""
            if iframe:
                iframe_prefix = f"var _iframeEl = document.querySelector('{iframe}'); if (!_iframeEl || !_iframeEl.contentDocument) return JSON.stringify({{error: 'iframe not found or cross-origin: {iframe}'}}); var _doc = _iframeEl.contentDocument;"
            else:
                iframe_prefix = "var _doc = document;"

            js = f"""
(function() {{
    {iframe_prefix}
    var el = _doc.querySelector('{selector}');
    if (!el) return JSON.stringify({{error: 'Element not found: {selector}'}});
    var text = el.innerText || el.textContent || '';
    text = text.replace(/\\s+/g, ' ').trim();
    if (text.length > {max_length}) text = text.substring(0, {max_length}) + '...(truncated)';
    return JSON.stringify({{
        title: document.title,
        url: location.href,
        selector: '{selector}',
        iframe: '{iframe}' || null,
        text_length: text.length,
        text: text
    }});
}})()
"""
            raw = await _chrome_exec_js(js)
            try:
                return _json(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                return _json({"text": raw[:max_length] if raw else "", "title": "", "url": ""})

        tools.append(ToolDefinition(
            name="chrome_get_page_text",
            description=(
                "Extract text content from the current Chrome page. Use CSS selector to target specific elements. "
                "For content inside iframes (e.g., 163 mail body), pass the iframe CSS selector."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string", "default": "body",
                        "description": "CSS selector (e.g., 'body', '.inbox-list', '#content', 'table')",
                    },
                    "max_length": {"type": "integer", "default": 30000},
                    "iframe": {
                        "type": "string", "default": "",
                        "description": "CSS selector of an iframe to read from (e.g., 'iframe#frameBody'). Same-origin only.",
                    },
                },
            },
            handler=chrome_get_page_text,
            risk_level=ToolRiskLevel.LOW,
            tags=["chrome", "browser", "extract", "macos", "edge"],
        ))

        # ---- chrome_get_elements: 列出页面中匹配选择器的元素 ----

        async def chrome_get_elements(
            selector: str, limit: int = 50,
            attributes: str = "innerText,href,class,id,type,value",
            iframe: str = ""
        ) -> str:
            """List elements matching a CSS selector with their attributes."""
            attr_list = [a.strip() for a in attributes.split(",") if a.strip()]
            attr_js_array = json.dumps(attr_list)

            iframe_prefix = ""
            if iframe:
                iframe_prefix = f"var _iframeEl = document.querySelector('{iframe}'); if (!_iframeEl || !_iframeEl.contentDocument) return JSON.stringify({{error: 'iframe not found or cross-origin: {iframe}'}}); var _doc = _iframeEl.contentDocument;"
            else:
                iframe_prefix = "var _doc = document;"

            js = f"""
(function() {{
    {iframe_prefix}
    var els = _doc.querySelectorAll('{selector}');
    var attrs = {attr_js_array};
    var results = [];
    var limit = Math.min(els.length, {limit});
    for (var i = 0; i < limit; i++) {{
        var el = els[i];
        var info = {{index: i, tag: el.tagName.toLowerCase()}};
        for (var j = 0; j < attrs.length; j++) {{
            var a = attrs[j];
            var v = null;
            if (a === 'innerText') {{
                v = (el.innerText || '').substring(0, 200).trim();
            }} else if (a === 'textContent') {{
                v = (el.textContent || '').substring(0, 200).trim();
            }} else {{
                v = el.getAttribute(a);
            }}
            if (v) info[a] = v;
        }}
        // Add bounding rect for clickability check
        var rect = el.getBoundingClientRect();
        info.visible = (rect.width > 0 && rect.height > 0);
        results.push(info);
    }}
    return JSON.stringify({{
        selector: '{selector}',
        iframe: '{iframe}' || null,
        total: els.length,
        returned: results.length,
        elements: results
    }});
}})()
"""
            raw = await _chrome_exec_js(js)
            try:
                return _json(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                return _json({"error": "Failed to parse elements", "raw": str(raw)[:2000]})

        tools.append(ToolDefinition(
            name="chrome_get_elements",
            description=(
                "List DOM elements matching a CSS selector. Returns tag, text, href, id, class, visibility. "
                "Use to discover clickable links, buttons, list items on the page."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector (e.g., 'a', 'button', 'tr', '.mail-item', '[role=listitem]')",
                    },
                    "limit": {"type": "integer", "default": 50, "description": "Max elements to return"},
                    "attributes": {
                        "type": "string", "default": "innerText,href,class,id,type,value",
                        "description": "Comma-separated attribute names to extract",
                    },
                    "iframe": {
                        "type": "string", "default": "",
                        "description": "CSS selector of an iframe to search within (e.g., 'iframe[id*=frameBody]')",
                    },
                },
                "required": ["selector"],
            },
            handler=chrome_get_elements,
            risk_level=ToolRiskLevel.LOW,
            tags=["chrome", "browser", "dom", "macos", "edge"],
        ))

        # ---- chrome_click: 点击页面元素 ----

        async def chrome_click(
            selector: str, index: int = 0,
            wait_after: float = 1.5,
            iframe: str = ""
        ) -> str:
            """Click an element on the page by CSS selector and index."""
            iframe_prefix = ""
            if iframe:
                iframe_prefix = f"var _iframeEl = document.querySelector('{iframe}'); if (!_iframeEl || !_iframeEl.contentDocument) return JSON.stringify({{error: 'iframe not found: {iframe}'}}); var _doc = _iframeEl.contentDocument;"
            else:
                iframe_prefix = "var _doc = document;"

            js = f"""
(function() {{
    {iframe_prefix}
    var els = _doc.querySelectorAll('{selector}');
    if (els.length === 0) return JSON.stringify({{error: 'No elements found for: {selector}'}});
    if ({index} >= els.length) return JSON.stringify({{error: 'Index {index} out of range, found ' + els.length + ' elements'}});
    var el = els[{index}];
    // Scroll into view first
    el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
    // Simulate click
    el.click();
    return JSON.stringify({{
        clicked: true,
        selector: '{selector}',
        index: {index},
        iframe: '{iframe}' || null,
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || '').substring(0, 100).trim()
    }});
}})()
"""
            raw = await _chrome_exec_js(js)
            if wait_after > 0:
                await asyncio.sleep(wait_after)
            try:
                return _json(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                return _json({"error": "Click may have succeeded but result parsing failed", "raw": str(raw)[:500]})

        tools.append(ToolDefinition(
            name="chrome_click",
            description=(
                "Click a DOM element by CSS selector and optional index. "
                "Use chrome_get_elements first to identify the right selector and index. "
                "Scrolls element into view before clicking. Waits after click for page update."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for the element to click",
                    },
                    "index": {
                        "type": "integer", "default": 0,
                        "description": "Which matching element to click (0-based)",
                    },
                    "wait_after": {
                        "type": "number", "default": 1.5,
                        "description": "Seconds to wait after click for page update",
                    },
                    "iframe": {
                        "type": "string", "default": "",
                        "description": "CSS selector of an iframe containing the element",
                    },
                },
                "required": ["selector"],
            },
            handler=chrome_click,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["chrome", "browser", "click", "macos", "edge"],
        ))

        # ---- chrome_type: 在输入框中输入文字 ----

        async def chrome_type(selector: str, text: str, clear_first: bool = True, press_enter: bool = False) -> str:
            """Type text into an input field."""
            escaped_text = text.replace("\\", "\\\\").replace("'", "\\'")
            js = f"""
(function() {{
    var el = document.querySelector('{selector}');
    if (!el) return JSON.stringify({{error: 'Element not found: {selector}'}});
    el.focus();
    {'el.value = "";' if clear_first else ''}
    el.value = '{escaped_text}';
    el.dispatchEvent(new Event('input', {{bubbles: true}}));
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
    {'''
    el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
    el.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
    el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
    ''' if press_enter else ''}
    return JSON.stringify({{
        typed: true,
        selector: '{selector}',
        text_length: {len(text)},
        press_enter: {'true' if press_enter else 'false'}
    }});
}})()
"""
            raw = await _chrome_exec_js(js)
            try:
                return _json(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                return _json({"error": "Type action result unclear", "raw": str(raw)[:500]})

        tools.append(ToolDefinition(
            name="chrome_type",
            description="Type text into an input field on the current Chrome page.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the input field"},
                    "text": {"type": "string", "description": "Text to type"},
                    "clear_first": {"type": "boolean", "default": True, "description": "Clear field before typing"},
                    "press_enter": {"type": "boolean", "default": False, "description": "Press Enter after typing"},
                },
                "required": ["selector", "text"],
            },
            handler=chrome_type,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["chrome", "browser", "input", "macos", "edge"],
        ))

        # ---- chrome_scroll: 页面滚动 ----

        async def chrome_scroll(direction: str = "down", amount: int = 500, selector: str = "") -> str:
            """Scroll the page or a specific element."""
            if selector:
                js = f"""
(function() {{
    var el = document.querySelector('{selector}');
    if (!el) return JSON.stringify({{error: 'Element not found: {selector}'}});
    el.scrollBy(0, {'amount' if direction == 'down' else '-amount'});
    return JSON.stringify({{scrolled: true, selector: '{selector}', direction: '{direction}', amount: {amount}}});
}})()
"""
            else:
                scroll_y = amount if direction == "down" else -amount
                js = f"""
(function() {{
    window.scrollBy(0, {scroll_y});
    return JSON.stringify({{scrolled: true, direction: '{direction}', amount: {amount}, scrollY: window.scrollY}});
}})()
"""
            raw = await _chrome_exec_js(js)
            try:
                return _json(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                return _json({"scrolled": True, "direction": direction})

        tools.append(ToolDefinition(
            name="chrome_scroll",
            description="Scroll the current Chrome page or a specific scrollable element.",
            parameters={
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                    "amount": {"type": "integer", "default": 500, "description": "Pixels to scroll"},
                    "selector": {"type": "string", "default": "", "description": "CSS selector for scrollable container (empty = whole page)"},
                },
            },
            handler=chrome_scroll,
            risk_level=ToolRiskLevel.LOW,
            tags=["chrome", "browser", "scroll", "macos", "edge"],
        ))

        # ---- chrome_wait_for: 等待元素出现 ----

        async def chrome_wait_for(selector: str, timeout: float = 10.0) -> str:
            """Wait for an element to appear on the page."""
            start = time.time()
            while time.time() - start < timeout:
                js = f"""
(function() {{
    var el = document.querySelector('{selector}');
    if (el) {{
        var rect = el.getBoundingClientRect();
        return JSON.stringify({{found: true, visible: rect.width > 0 && rect.height > 0, tag: el.tagName.toLowerCase(), text: (el.innerText || '').substring(0, 100)}});
    }}
    return JSON.stringify({{found: false}});
}})()
"""
                raw = await _chrome_exec_js(js)
                try:
                    data = json.loads(raw)
                    if data.get("found"):
                        return _json(data)
                except (json.JSONDecodeError, TypeError):
                    pass
                await asyncio.sleep(0.5)
            return _json({"found": False, "selector": selector, "timeout": timeout})

        tools.append(ToolDefinition(
            name="chrome_wait_for",
            description="Wait for a DOM element to appear on the page (polls every 0.5s).",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector to wait for"},
                    "timeout": {"type": "number", "default": 10.0, "description": "Max seconds to wait"},
                },
                "required": ["selector"],
            },
            handler=chrome_wait_for,
            risk_level=ToolRiskLevel.LOW,
            tags=["chrome", "browser", "wait", "macos", "edge"],
        ))

        # ---- chrome_execute_js: 直接执行 JS（高级用户） ----

        async def chrome_execute_js(code: str) -> str:
            """Execute arbitrary JavaScript in the active Chrome tab."""
            raw = await _chrome_exec_js(code)
            try:
                return _json(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                return _json({"result": raw[:10000] if raw else ""})

        tools.append(ToolDefinition(
            name="chrome_execute_js",
            description=(
                "Execute arbitrary JavaScript in Chrome's active tab and return the result. "
                "For complex interactions not covered by other chrome_* tools."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "JavaScript code to execute. Must return a value (use an IIFE)."},
                },
                "required": ["code"],
            },
            handler=chrome_execute_js,
            risk_level=ToolRiskLevel.HIGH,
            requires_approval=True,
            tags=["chrome", "browser", "javascript", "macos", "edge"],
        ))

        # ---- chrome_read_mail: 高阶工具 — 读取邮箱邮件列表和正文 ----
        # 封装完整的"找邮件列表 → 点击 → 读iframe正文 → 返回"流程

        async def chrome_read_mail(action: str = "list", index: int = 0, count: int = 10) -> str:
            """Read emails from web mail clients (163, Gmail, Outlook etc.) open in Chrome."""
            # Detect which mail client is open
            url_raw = await _chrome_exec_js("location.href")

            if "mail.163.com" in url_raw:
                return await _read_mail_163(action, index, count)
            elif "mail.google.com" in url_raw:
                return await _read_mail_gmail(action, index, count)
            else:
                return _json({"error": f"Unsupported mail client. Current URL: {url_raw[:200]}", "supported": ["mail.163.com", "mail.google.com"]})

        async def _read_mail_163(action: str, index: int, count: int) -> str:
            """网易163邮箱专用：列出邮件/读取邮件正文"""
            if action == "list":
                # 163邮箱v6: 邮件行是 div 元素，class 通常含 "nl0"
                # 容器在 #_dvModuleContainer_mbox.ListModule_0 中
                js = """
(function(){
    // 先确保在收件箱列表模块
    var container = document.querySelector('div[id*="ListModule"]');
    if (!container) {
        // 尝试通过 hash 导航到收件箱
        if (location.hash.indexOf('ListModule') < 0) {
            location.hash = '#module=mbox.ListModule%7C%7B%22fid%22%3A1%7D';
            return JSON.stringify({action: 'list', mail_client: '163', note: 'Navigating to inbox, please retry in 2 seconds'});
        }
        return JSON.stringify({error: 'Cannot find mail list container'});
    }

    // 163v6 邮件行: div with class containing nl0, 高度30-70px, 宽度>500px
    var allDivs = container.querySelectorAll('div');
    var items = [];
    for (var i = 0; i < allDivs.length; i++) {
        var d = allDivs[i];
        if (d.children.length > 3 && d.offsetHeight > 25 && d.offsetHeight < 70
            && d.offsetWidth > 500 && (d.innerText || '').length > 15
            && d.className.indexOf('toolbar') < 0) {
            items.push(d);
        }
    }

    var results = [];
    var limit = Math.min(items.length, """ + str(count) + """);
    for (var i = 0; i < limit; i++) {
        var el = items[i];
        var parts = (el.innerText || '').split('\\n').map(function(s){return s.trim()}).filter(function(s){return s.length>0});
        var from = parts[0] || '';
        var subject = parts.length > 1 ? parts[1] : '';
        var time = parts.length > 2 ? parts[parts.length-1] : '';
        results.push({index: i, from: from, subject: subject, time: time, full_text: parts.join(' | ').substring(0, 300)});
    }
    return JSON.stringify({action: 'list', mail_client: '163', found: items.length, returned: results.length, emails: results});
})()
"""
                raw = await _chrome_exec_js(js)
                try:
                    return _json(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    return _json({"error": "Failed to parse mail list", "raw": str(raw)[:2000]})

            elif action == "read":
                # 点击指定邮件并读取正文
                # Step 1: 找到邮件行并点击
                click_js = """
(function(){
    var container = document.querySelector('div[id*="ListModule"]');
    if (!container) return JSON.stringify({error: 'Not on inbox page. Use action=back first.'});

    var allDivs = container.querySelectorAll('div');
    var items = [];
    for (var i = 0; i < allDivs.length; i++) {
        var d = allDivs[i];
        if (d.children.length > 3 && d.offsetHeight > 25 && d.offsetHeight < 70
            && d.offsetWidth > 500 && (d.innerText || '').length > 15
            && d.className.indexOf('toolbar') < 0) {
            items.push(d);
        }
    }

    var idx = """ + str(index) + """;
    if (idx >= items.length) return JSON.stringify({error: 'Index ' + idx + ' out of range, only ' + items.length + ' emails'});
    var el = items[idx];
    var preview = (el.innerText || '').replace(/[\\n\\r]+/g, ' | ').trim().substring(0, 200);
    el.scrollIntoView({block:'center'});
    // 163邮箱需要完整的鼠标事件序列来触发双击打开
    var events = ['mousedown','mouseup','click','mousedown','mouseup','click','dblclick'];
    for (var e = 0; e < events.length; e++) {
        el.dispatchEvent(new MouseEvent(events[e], {bubbles:true, cancelable:true, view:window, detail: events[e]==='dblclick'?2:1}));
    }
    return JSON.stringify({clicked: true, index: idx, preview: preview});
})()
"""
                click_raw = await _chrome_exec_js(click_js)
                try:
                    click_result = json.loads(click_raw)
                    if click_result.get("error"):
                        return _json(click_result)
                except (json.JSONDecodeError, TypeError):
                    pass

                # Step 2: 等待邮件正文加载（iframe出现且有内容）
                await asyncio.sleep(2)

                # Step 3: 读取可见iframe中的邮件正文
                read_js = """
(function(){
    var iframes = document.querySelectorAll('iframe');
    // 找到可见的、有内容的iframe（正文iframe）
    for (var i = 0; i < iframes.length; i++) {
        var f = iframes[i];
        if (f.offsetWidth > 100 && f.offsetHeight > 100) {
            try {
                var doc = f.contentDocument;
                if (!doc || !doc.body) continue;
                var text = doc.body.innerText || '';
                if (text.length > 30) {
                    // 也获取邮件头信息（在主页面中）
                    var subject = '';
                    var from = '';
                    var date = '';
                    // 163邮箱v6: 邮件标题在 h2.nui-title 或 div.subjectText 中
                    var subjectEl = document.querySelector('h2, .subjectText, div[class*="subject"]');
                    if (subjectEl) subject = subjectEl.innerText.trim();
                    var fromEl = document.querySelector('.nui-addr, span[class*="from"], [class*="sender"]');
                    if (fromEl) from = fromEl.innerText.trim();
                    var dateEl = document.querySelector('.nui-editdate, span[class*="date"], [class*="time"]');
                    if (dateEl) date = dateEl.innerText.trim();

                    return JSON.stringify({
                        action: 'read',
                        mail_client: '163',
                        subject: subject,
                        from: from,
                        date: date,
                        body_length: text.length,
                        body: text.substring(0, 15000)
                    });
                }
            } catch(e) {
                // cross-origin iframe, skip
            }
        }
    }
    return JSON.stringify({error: 'No email body found in iframes. The email may not have loaded yet.'});
})()
"""
                read_raw = await _chrome_exec_js(read_js)
                try:
                    return _json(json.loads(read_raw))
                except (json.JSONDecodeError, TypeError):
                    return _json({"error": "Failed to read email body", "raw": str(read_raw)[:2000]})

            elif action == "back":
                # 返回收件箱列表
                back_js = """
(function(){
    // 163邮箱返回按钮
    var backBtns = document.querySelectorAll('a[action], span[class*="goBack"], .js-component-button, [class*="return"], [class*="back"]');
    for (var i = 0; i < backBtns.length; i++) {
        var text = (backBtns[i].innerText || '').trim();
        if (text.indexOf('返回') >= 0 || text.indexOf('收件箱') >= 0) {
            backBtns[i].click();
            return JSON.stringify({action: 'back', clicked: true, text: text});
        }
    }
    // Fallback: 修改URL hash回到收件箱
    if (location.hash.indexOf('read.ReadModule') >= 0 || location.hash.indexOf('ReadModule') >= 0) {
        location.hash = '#module=mbox.ListModule%7C%7B%22fid%22%3A1%7D';
        return JSON.stringify({action: 'back', method: 'hash_navigation', success: true});
    }
    return JSON.stringify({action: 'back', error: 'Could not find back button'});
})()
"""
                back_raw = await _chrome_exec_js(back_js)
                await asyncio.sleep(1.5)
                try:
                    return _json(json.loads(back_raw))
                except (json.JSONDecodeError, TypeError):
                    return _json({"action": "back", "result": str(back_raw)[:500]})

            else:
                return _json({"error": f"Unknown action: {action}. Use 'list', 'read', or 'back'."})

        async def _read_mail_gmail(action: str, index: int, count: int) -> str:
            """Gmail专用（基础实现）"""
            if action == "list":
                js = """
(function(){
    var rows = document.querySelectorAll('tr.zA');
    var results = [];
    var limit = Math.min(rows.length, """ + str(count) + """);
    for (var i = 0; i < limit; i++) {
        var row = rows[i];
        var from = '';
        var subject = '';
        var snippet = '';
        var fromEl = row.querySelector('.yX .yW span');
        if (fromEl) from = fromEl.getAttribute('name') || fromEl.innerText;
        var subjectEl = row.querySelector('.y6 span[data-thread-id]');
        if (subjectEl) subject = subjectEl.innerText;
        var snippetEl = row.querySelector('.y2');
        if (snippetEl) snippet = snippetEl.innerText;
        results.push({index: i, from: from, subject: subject, snippet: snippet});
    }
    return JSON.stringify({action: 'list', mail_client: 'gmail', found: rows.length, returned: results.length, emails: results});
})()
"""
                raw = await _chrome_exec_js(js)
                try:
                    return _json(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    return _json({"error": "Failed to parse Gmail list", "raw": str(raw)[:2000]})

            elif action == "read":
                click_js = f"""
(function(){{
    var rows = document.querySelectorAll('tr.zA');
    if ({index} >= rows.length) return JSON.stringify({{error: 'Index out of range'}});
    rows[{index}].click();
    return JSON.stringify({{clicked: true, index: {index}}});
}})()
"""
                await _chrome_exec_js(click_js)
                await asyncio.sleep(2)
                read_js = """
(function(){
    var body = document.querySelector('.a3s.aiL, .gmail_default, div[data-message-id] .a3s');
    if (!body) return JSON.stringify({error: 'Email body not found'});
    var subject = '';
    var subEl = document.querySelector('h2.hP');
    if (subEl) subject = subEl.innerText;
    return JSON.stringify({action: 'read', mail_client: 'gmail', subject: subject, body: body.innerText.substring(0, 15000)});
})()
"""
                raw = await _chrome_exec_js(read_js)
                try:
                    return _json(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    return _json({"error": "Failed to read Gmail body", "raw": str(raw)[:2000]})

            elif action == "back":
                await _chrome_exec_js("document.querySelector('.T-I.J-J5-Ji.lS.T-I-ax7[act=\"20\"]').click()")
                await asyncio.sleep(1.5)
                return _json({"action": "back", "success": True, "mail_client": "gmail"})
            else:
                return _json({"error": f"Unknown action: {action}"})

        tools.append(ToolDefinition(
            name="chrome_read_mail",
            description=(
                "High-level tool to read emails from web mail clients open in Chrome (163 Mail, Gmail). "
                "Actions: 'list' = show inbox emails, 'read' = click and read email body at index, 'back' = return to inbox. "
                "Example workflow: list → read(index=0) → back → read(index=1) → back"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string", "enum": ["list", "read", "back"],
                        "description": "'list': show inbox, 'read': open and read email at index, 'back': return to inbox",
                    },
                    "index": {
                        "type": "integer", "default": 0,
                        "description": "Which email to read (0-based, for 'read' action)",
                    },
                    "count": {
                        "type": "integer", "default": 10,
                        "description": "Max emails to list (for 'list' action)",
                    },
                },
                "required": ["action"],
            },
            handler=chrome_read_mail,
            risk_level=ToolRiskLevel.MEDIUM,
            tags=["chrome", "email", "mail", "macos", "edge"],
        ))

    return tools
