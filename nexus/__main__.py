"""
Nexus CLI 入口

用法:
    python -m nexus serve          # 启动 HTTP 服务
    python -m nexus serve --port 9000
    python -m nexus reindex        # 重建 Vault 知识索引
    python -m nexus reindex --delta # 增量索引
    python -m nexus vault-import   # 导入 legacy Vault 内容并重建索引
    python -m nexus health         # 检查运行状态
    python -m nexus audio-worker   # 启动独立音频转录服务
    python -m nexus agent-smoke    # 验证 Core Agent 六项能力 wiring
    python -m nexus chat           # 在命令行中与 Agent 对话
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from nexus.channel.message_formatter import MessageFormatter
from nexus.channel.session_store import SessionStatus
from nexus.channel.types import ChannelType, InboundMessage, OutboundMessage, OutboundMessageType
from nexus.orchestrator import Orchestrator
from nexus.shared import find_project_root, load_nexus_settings


async def _close_runtime(runtime) -> None:
    stop_mesh_runtime = getattr(runtime, "mesh_transport", None)
    if stop_mesh_runtime is not None:
        from nexus.api.runtime import stop_mesh_runtime as _stop_mesh_runtime

        await _stop_mesh_runtime(runtime)
    background_manager = getattr(runtime, "background_manager", None)
    if background_manager is not None:
        await background_manager.aclose()
    browser_service = getattr(runtime, "browser_service", None)
    if browser_service is not None:
        await browser_service.aclose()


def cmd_serve(args: argparse.Namespace) -> None:
    """启动 FastAPI 服务"""
    import uvicorn

    settings = load_nexus_settings(find_project_root())
    host = args.host or settings.server_host
    port = args.port or settings.server_port

    uvicorn.run(
        "nexus.api.app:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )


def cmd_reindex(args: argparse.Namespace) -> None:
    """重建 Vault 知识索引"""
    settings = load_nexus_settings(find_project_root())

    from nexus.api.runtime import build_runtime

    runtime = build_runtime(settings=settings)

    print(f"Vault: {runtime.paths.vault}")
    print(f"DB:    {runtime.paths.sqlite / 'retrieval.db'}")
    print(f"Mode:  {'delta' if args.delta else 'full'}")
    print()

    stats = runtime.ingest_service.ingest_directory("", delta_only=args.delta)

    print(f"Files processed: {stats['files_processed']}")
    print(f"Chunks created:  {stats['chunks_created']}")
    print(f"Files skipped:   {stats['files_skipped']}")
    print(f"Errors:          {stats['errors']}")

    # 打印总体统计
    db_stats = runtime.retrieval_index.get_stats()
    print(f"\nTotal chunks: {db_stats['chunks']}")
    print(f"Total docs:   {db_stats['documents']}")


def cmd_health(args: argparse.Namespace) -> None:
    """检查服务健康状态"""
    import json

    try:
        import httpx
    except ImportError:
        print("httpx not installed, run: pip install httpx")
        sys.exit(1)

    settings = load_nexus_settings(find_project_root())
    base = f"http://{args.host or settings.server_host}:{args.port or settings.server_port}"
    try:
        r = httpx.get(f"{base}/health", timeout=5)
        data = r.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except httpx.ConnectError:
        print(f"Cannot connect to {base}")
        sys.exit(1)

    try:
        r = httpx.get(f"{base}/health/providers", timeout=5)
        data = r.json()
        print("\nProviders:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception:
        pass


def cmd_audio_worker(args: argparse.Namespace) -> None:
    """启动独立音频转录服务"""
    import uvicorn

    uvicorn.run(
        "nexus.services.audio.worker_app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


def cmd_agent_smoke(args: argparse.Namespace) -> None:
    """验证 Core Agent 六项能力是否已接到真实 runtime。"""
    from nexus.api.agent_smoke import run_agent_capability_smoke
    from nexus.api.runtime import build_runtime

    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(settings=settings)
    checks = asyncio.run(run_agent_capability_smoke(runtime))

    payload = {
        "root": str(runtime.paths.root),
        "checks": [
            {"name": check.name, "ok": check.ok, "detail": check.detail}
            for check in checks
        ],
        "passed": sum(1 for check in checks if check.ok),
        "total": len(checks),
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Root: {payload['root']}")
        print(f"Checks: {payload['passed']}/{payload['total']} passed")
        print()
        for item in payload["checks"]:
            marker = "PASS" if item["ok"] else "FAIL"
            print(f"[{marker}] {item['name']}: {item['detail']}")

    if payload["passed"] != payload["total"]:
        sys.exit(2)


def cmd_vault_status(args: argparse.Namespace) -> None:
    from nexus.services.vault import VaultManagerService

    settings = load_nexus_settings(find_project_root())
    manager = VaultManagerService(settings)
    payload = manager.status()
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_vault_create(args: argparse.Namespace) -> None:
    from nexus.services.vault import VaultManagerService

    settings = load_nexus_settings(find_project_root())
    manager = VaultManagerService(settings)
    result = manager.create_under(
        args.path,
        folder_name=args.name,
        switch=not args.no_switch,
        exact=args.exact,
    )
    print(json.dumps(
        {
            "action": result.action,
            "new_root": str(result.new_root),
            "old_root": str(result.old_root) if result.old_root else None,
            "files_count": result.files_count,
            "mode": result.mode,
            "config_path": str(result.config_path),
            "config_updates": result.config_updates,
            "note": result.note,
        },
        indent=2,
        ensure_ascii=False,
    ))


def cmd_vault_migrate(args: argparse.Namespace) -> None:
    from nexus.services.vault import VaultManagerService

    settings = load_nexus_settings(find_project_root())
    manager = VaultManagerService(settings)
    result = manager.migrate_to(
        args.path,
        mode=args.mode,
        path_is_parent=args.parent,
        folder_name=args.name,
    )
    print(json.dumps(
        {
            "action": result.action,
            "new_root": str(result.new_root),
            "old_root": str(result.old_root) if result.old_root else None,
            "files_count": result.files_count,
            "mode": result.mode,
            "config_path": str(result.config_path),
            "config_updates": result.config_updates,
            "note": result.note,
        },
        indent=2,
        ensure_ascii=False,
    ))


def cmd_vault_import(args: argparse.Namespace) -> None:
    from nexus.api.runtime import build_runtime
    from nexus.services.vault import VaultManagerService

    settings = load_nexus_settings(find_project_root())
    manager = VaultManagerService(settings)
    result = manager.import_legacy_vault(
        args.source,
        target_root=args.target,
        source_label=args.label,
    )

    runtime = build_runtime(settings=load_nexus_settings(find_project_root()))
    structural_stats = runtime.structural_index.rebuild_from_vault(runtime.paths.vault)
    retrieval_stats = runtime.ingest_service.reindex_all()
    asyncio.run(_close_runtime(runtime))

    print(json.dumps(
        {
            "source_root": str(result.source_root),
            "target_root": str(result.target_root),
            "files_copied": result.files_copied,
            "files_skipped": result.files_skipped,
            "summary_note_path": str(result.summary_note_path),
            "note": result.note,
            "structural_rebuild": structural_stats,
            "retrieval_reindex": retrieval_stats,
        },
        indent=2,
        ensure_ascii=False,
    ))


def cmd_memory_status(args: argparse.Namespace) -> None:
    from nexus.api.runtime import build_runtime

    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(settings=settings)
    payload = {
        "vault_root": str(runtime.paths.vault),
        "episodic_memory": runtime.episodic_memory.describe(),
        "context_compression": runtime.compressor.describe(),
        "summary": {
            "long_term_memory": "显式长期记忆已启用（preferences / decisions / project_state 等），与会话上下文分离。",
            "compression": "具备三层上下文压缩；长期记忆本身当前不做语义压缩，只做容量截断。",
        },
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    asyncio.run(_close_runtime(runtime))


def _build_orchestrator():
    from nexus.api.runtime import build_runtime, start_mesh_runtime

    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(settings=settings)
    if getattr(runtime, "mesh_transport", None) is not None:
        asyncio.run(start_mesh_runtime(runtime))
    orchestrator = Orchestrator(
        session_router=runtime.session_router,
        session_store=runtime.session_store,
        context_window=runtime.context_window,
        run_manager=runtime.run_manager,
        formatter=MessageFormatter(),
        provider_gateway=runtime.provider,
        config_path=runtime.settings.config_path,
        available_tools=runtime.available_tools,
        skill_manager=runtime.skill_manager,
        capability_manager=runtime.capability_manager,
        task_router=getattr(runtime, "mesh_task_router", None),
        mesh_registry=getattr(runtime, "mesh_registry", None),
    )
    return runtime, orchestrator


async def _run_chat_turn(
    orchestrator: Orchestrator,
    *,
    sender_id: str,
    content: str,
    channel: ChannelType = ChannelType.WEB,
) -> list[OutboundMessage]:
    replies: list[OutboundMessage] = []

    async def reply_fn(msg: OutboundMessage) -> None:
        replies.append(msg)

    inbound = InboundMessage(
        message_id=f"cli-{uuid.uuid4().hex}",
        channel=channel,
        sender_id=sender_id,
        content=content,
    )
    await orchestrator.handle_message(inbound, reply_fn)
    return replies


def _print_cli_reply(outbound: OutboundMessage) -> None:
    prefix_map = {
        OutboundMessageType.ACK: "ACK",
        OutboundMessageType.STATUS: "STATUS",
        OutboundMessageType.BLOCKED: "BLOCKED",
        OutboundMessageType.RESULT: "RESULT",
        OutboundMessageType.CLARIFY: "CLARIFY",
        OutboundMessageType.ERROR: "ERROR",
    }
    prefix = prefix_map.get(outbound.message_type, outbound.message_type.value.upper())
    print(f"[{prefix}] {outbound.content}")


async def _format_cli_status(runtime, sender_id: str) -> str:
    active = runtime.session_store.get_active_session(sender_id)
    session = active or runtime.session_store.get_most_recent_session(sender_id)
    if session is None:
        return "当前没有任何会话记录。"

    lines = []
    if active:
        lines.append("当前活跃会话")
    else:
        lines.append("当前没有活跃会话，以下是最近一次会话")
    lines.append(f"- session_id: {session.session_id}")
    lines.append(f"- 状态: {session.status.value}")
    lines.append(f"- 摘要: {session.summary or '（无摘要）'}")

    runs = await runtime.run_store.get_runs_by_session(session.session_id, limit=3)
    if runs:
        latest = runs[0]
        lines.append(f"- 最近 Run: {latest.run_id} [{latest.status.value}]")
        if latest.task:
            lines.append(f"- 最近任务: {latest.task[:120]}")
        if latest.error:
            lines.append(f"- 最近错误: {latest.error[:160]}")
    else:
        lines.append("- 最近 Run: （无）")

    events = runtime.session_store.get_events(session.session_id, limit=4)
    if events:
        lines.append("")
        lines.append("最近会话事件")
        for event in reversed(events):
            preview = event.content.strip().replace("\n", " ")
            lines.append(f"  - [{event.role}] {preview[:120]}")
    return "\n".join(lines)


async def _format_cli_history(runtime, sender_id: str, limit: int = 5) -> str:
    sessions = runtime.session_store.get_recent_sessions(sender_id, limit=limit)
    if not sessions:
        return "当前没有可显示的会话历史。"

    lines = ["最近会话历史"]
    for idx, session in enumerate(sessions, start=1):
        runs = await runtime.run_store.get_runs_by_session(session.session_id, limit=1)
        latest_run = runs[0] if runs else None
        summary = (session.summary or "（无摘要）").replace("\n", " ")
        lines.append(
            f"{idx}. {summary[:80]} | session={session.session_id} | status={session.status.value}"
        )
        if latest_run:
            lines.append(
                f"   run={latest_run.run_id} [{latest_run.status.value}] model={latest_run.model or '-'}"
            )
    active = runtime.session_store.get_active_session(sender_id)
    if active is not None:
        events = runtime.session_store.get_events(active.session_id, limit=6)
        if events:
            lines.append("")
            lines.append(f"当前活跃会话最近事件 ({active.session_id})")
            for event in reversed(events):
                preview = event.content.strip().replace("\n", " ")
                lines.append(f"  - [{event.role}] {preview[:120]}")
    return "\n".join(lines)


def _clear_cli_session(runtime, sender_id: str) -> str:
    active = runtime.session_store.get_active_session(sender_id)
    if active is None:
        return "当前没有活跃会话，无需清理。"
    runtime.session_store.update_session_status(active.session_id, SessionStatus.ABANDONED)
    runtime.context_window.reset(active.session_id)
    return f"已清理当前会话：{active.session_id}"


async def _handle_local_cli_command(runtime, sender_id: str, text: str) -> bool:
    command = text.strip()
    if command == "/vault-status":
        print(json.dumps(runtime.vault_manager.status(), indent=2, ensure_ascii=False))
        return True
    if command == "/memory-status":
        payload = {
            "episodic_memory": runtime.episodic_memory.describe(),
            "context_compression": runtime.compressor.describe(),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return True
    if command == "/status":
        print(await _format_cli_status(runtime, sender_id))
        return True
    if command.startswith("/history"):
        limit = 5
        parts = command.split(maxsplit=1)
        if len(parts) == 2:
            try:
                limit = max(1, min(int(parts[1]), 20))
            except ValueError:
                pass
        print(await _format_cli_history(runtime, sender_id, limit=limit))
        return True
    if command == "/clear":
        print(_clear_cli_session(runtime, sender_id))
        return True
    return False


def cmd_chat(args: argparse.Namespace) -> None:
    """命令行对话入口，复用真实 SessionRouter + Orchestrator + RunManager。"""
    runtime, orchestrator = _build_orchestrator()

    async def run_once(text: str) -> None:
        if await _handle_local_cli_command(runtime, args.sender_id, text):
            return
        replies = await _run_chat_turn(
            orchestrator,
            sender_id=args.sender_id,
            content=text,
            channel=ChannelType.WEB,
        )
        for reply in replies:
            _print_cli_reply(reply)

    async def interactive() -> None:
        print("Nexus CLI chat")
        print("输入内容直接对话。")
        print("本地命令：/status  /history [n]  /clear  /vault-status  /memory-status  /exit")
        while True:
            try:
                text = input("You> ").strip()
            except EOFError:
                print()
                break
            if not text:
                continue
            if text in {"/exit", "/quit", "exit", "quit"}:
                break
            await run_once(text)

    async def chat_main() -> None:
        try:
            if args.message:
                await run_once(args.message)
            else:
                await interactive()
        finally:
            await _close_runtime(runtime)

    asyncio.run(chat_main())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nexus",
        description="Nexus / 星策 — Personal AI Agent Runtime",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    serve_p = sub.add_parser("serve", help="启动 HTTP 服务")
    serve_p.add_argument("--host", default=None, help="监听地址 (default: 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=None, help="监听端口 (default: 8000)")
    serve_p.add_argument("--reload", action="store_true", help="开发模式热重载")

    # reindex
    reindex_p = sub.add_parser("reindex", help="重建 Vault 知识索引")
    reindex_p.add_argument("--delta", action="store_true", help="增量索引（跳过未变化文件）")

    # health
    health_p = sub.add_parser("health", help="检查服务健康状态")
    health_p.add_argument("--host", default=None, help="服务地址")
    health_p.add_argument("--port", type=int, default=None, help="服务端口")

    # audio-worker
    audio_p = sub.add_parser("audio-worker", help="启动独立音频转录服务")
    audio_p.add_argument("--host", default="0.0.0.0", help="监听地址")
    audio_p.add_argument("--port", type=int, default=8010, help="监听端口")
    audio_p.add_argument("--reload", action="store_true", help="开发模式热重载")

    # agent-smoke
    smoke_p = sub.add_parser("agent-smoke", help="验证 Core Agent 六项能力 wiring")
    smoke_p.add_argument("--json", action="store_true", help="输出 JSON 结果")

    # vault-status
    sub.add_parser("vault-status", help="查看当前受管 Vault 根目录")

    # vault-create
    vault_create_p = sub.add_parser("vault-create", help="在指定路径下创建并切换到新的 Vault")
    vault_create_p.add_argument("--path", required=True, help="父目录路径；默认会在其下创建 vault/ 子目录")
    vault_create_p.add_argument("--name", default="vault", help="Vault 文件夹名 (default: vault)")
    vault_create_p.add_argument("--exact", action="store_true", help="将 --path 视为 Vault 根目录本身，而不是父目录")
    vault_create_p.add_argument("--no-switch", action="store_true", help="只创建，不更新 config/app.yaml")

    # vault-migrate
    vault_migrate_p = sub.add_parser("vault-migrate", help="迁移当前受管 Vault 到新路径并切换管理")
    vault_migrate_p.add_argument("--path", required=True, help="目标 Vault 根目录；若配合 --parent 则视为父目录")
    vault_migrate_p.add_argument("--mode", choices=["copy", "move"], default="copy", help="迁移模式 (default: copy)")
    vault_migrate_p.add_argument("--parent", action="store_true", help="将 --path 视为父目录，并在其下使用 --name")
    vault_migrate_p.add_argument("--name", default="vault", help="当 --parent 启用时使用的 Vault 文件夹名")

    # vault-import
    vault_import_p = sub.add_parser("vault-import", help="导入 legacy Vault 内容到当前 Nexus Vault，并重建结构/检索索引")
    vault_import_p.add_argument("--source", required=True, help="待导入的 legacy Vault 根目录")
    vault_import_p.add_argument("--target", help="目标 Vault 根目录；默认使用当前受管 Vault")
    vault_import_p.add_argument("--label", default="macos-ai-assistant", help="导入来源标签，用于生成导入目录和摘要")

    # memory-status
    sub.add_parser("memory-status", help="查看当前记忆与上下文压缩能力")

    # chat
    chat_p = sub.add_parser("chat", help="在命令行中与 Agent 对话")
    chat_p.add_argument("--sender-id", default="cli-user", help="会话发送者 ID")
    chat_p.add_argument("--message", default=None, help="单次对话输入；不传则进入交互模式")

    args = parser.parse_args()

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "reindex":
        cmd_reindex(args)
    elif args.command == "health":
        cmd_health(args)
    elif args.command == "audio-worker":
        cmd_audio_worker(args)
    elif args.command == "agent-smoke":
        cmd_agent_smoke(args)
    elif args.command == "vault-status":
        cmd_vault_status(args)
    elif args.command == "vault-create":
        cmd_vault_create(args)
    elif args.command == "vault-migrate":
        cmd_vault_migrate(args)
    elif args.command == "vault-import":
        cmd_vault_import(args)
    elif args.command == "memory-status":
        cmd_memory_status(args)
    elif args.command == "chat":
        cmd_chat(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
