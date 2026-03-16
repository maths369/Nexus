"""Edge-local tool host for mesh-exposed desktop capabilities."""

from __future__ import annotations

import asyncio
import json
import platform
import shutil
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.agent.types import ToolDefinition, ToolResult, ToolRiskLevel
from nexus.services.workspace import WorkspaceService

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

    return tools
