from __future__ import annotations

import asyncio

from nexus.agent.types import Run, RunStatus
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.message_formatter import MessageFormatter
from nexus.channel.session_store import SessionStore
from nexus.channel.types import ChannelType, InboundMessage, MessageIntent, RoutingDecision
from nexus.mesh import (
    AvailabilitySpec,
    CapabilitySpec,
    InMemoryTransport,
    MeshRegistry,
    NodeCard,
    NodeType,
    ProviderSpec,
    RemoteToolProxy,
    ResourceSpec,
    TaskRouter,
)
from nexus.orchestrator import Orchestrator


class _FakeRouter:
    def __init__(self, decision: RoutingDecision):
        self._decision = decision

    async def route(self, message):
        return self._decision


class _FakeRunManager:
    def __init__(self):
        self.last_kwargs = None

    async def execute(self, **kwargs):
        self.last_kwargs = kwargs
        return Run(
            run_id='run-1',
            session_id=kwargs['session_id'],
            status=RunStatus.SUCCEEDED,
            task=kwargs['task'],
            result='done',
            model='qwen-max',
        )


class _FailingRunManager(_FakeRunManager):
    async def execute(self, **kwargs):
        self.last_kwargs = kwargs
        return Run(
            run_id='run-2',
            session_id=kwargs['session_id'],
            status=RunStatus.FAILED,
            task=kwargs['task'],
            error='boom',
            model='qwen-max',
        )


def _hub_card() -> NodeCard:
    return NodeCard(
        node_id="ubuntu-server-5090",
        node_type=NodeType.HUB,
        display_name="Ubuntu Server",
        platform="linux",
        arch="x86_64",
        providers=[ProviderSpec(name="kimi", model="kimi-k2.5", via="api")],
        capabilities=[
            CapabilitySpec(
                capability_id="knowledge_store",
                description="Knowledge base",
                tools=["read_vault", "write_vault"],
            ),
            CapabilitySpec(
                capability_id="long_running_analysis",
                description="Long running analysis",
                tools=["background_run"],
            ),
        ],
        resources=ResourceSpec(cpu_cores=32, memory_gb=128, battery_powered=False),
        availability=AvailabilitySpec(schedule="24/7"),
    )


def _edge_card() -> NodeCard:
    return NodeCard(
        node_id="macbook-pro",
        node_type=NodeType.EDGE,
        display_name="MacBook Pro",
        platform="macos",
        arch="arm64",
        providers=[ProviderSpec(name="kimi", model="kimi-k2.5", via="api")],
        capabilities=[
            CapabilitySpec(
                capability_id="browser_automation",
                description="Authenticated browser automation",
                tools=["browser_navigate", "browser_extract_text"],
                requires_user_interaction=True,
            ),
        ],
        resources=ResourceSpec(cpu_cores=10, memory_gb=16, battery_powered=True),
        availability=AvailabilitySpec(intermittent=True, max_task_duration_seconds=1800),
    )


def _message(content: str) -> InboundMessage:
    return InboundMessage(
        message_id='msg-1',
        channel=ChannelType.FEISHU,
        sender_id='user-1',
        content=content,
    )


def test_orchestrator_status_query_uses_decision_session_id(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    old_session = store.create_session('user-1', 'feishu', summary='旧任务')
    new_session = store.create_session('user-1', 'feishu', summary='新任务')
    store.update_session_status(old_session.session_id, old_session.status.PAUSED)
    store.update_session_status(new_session.session_id, new_session.status.ACTIVE)

    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.STATUS_QUERY, session_id=old_session.session_id)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message('上次那个怎么样了'), reply))

    assert replies
    assert '旧任务' in replies[0].content
    assert '新任务' not in replies[0].content


def test_orchestrator_unknown_includes_router_candidates(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    orchestrator = Orchestrator(
        session_router=_FakeRouter(
            RoutingDecision(
                intent=MessageIntent.UNKNOWN,
                metadata={
                    'candidates': [
                        {'summary': 'OpenClaw 架构分析', 'status': 'completed'},
                        {'summary': '今日会议纪要', 'status': 'active'},
                    ]
                },
            )
        ),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message('继续刚才那个'), reply))

    assert replies
    assert 'OpenClaw 架构分析' in replies[0].content
    assert '今日会议纪要' in replies[0].content


class _FakeSkillManager:
    def __init__(self):
        self.installed_from_catalog: list[str] = []

    def list_skills(self):
        return [
            {"skill_id": "meeting-transcription"},
            {"skill_id": "excel-processing"},
        ]

    def list_installable_skills(self):
        return [
            {"skill_id": "office-conversion", "name": "Office Conversion", "description": "处理 PPT/PPTX 和 Office 文件转换", "installed": False},
            {"skill_id": "api-integration-bootstrap", "name": "API Integration Bootstrap", "description": "API 集成引导", "installed": False},
        ]

    async def install_from_catalog(self, skill_id: str, actor: str = "agent"):
        self.installed_from_catalog.append(skill_id)
        return {
            "success": True,
            "skill_id": skill_id,
            "reason": f"installed by {actor}",
            "installed": True,
        }


class _FakeCapabilityManager:
    def list_capabilities(self):
        return [
            {
                "capability_id": "excel_processing",
                "name": "Excel Processing",
                "enabled": False,
            }
        ]

    async def enable(self, capability_id: str, *, actor: str = "system"):
        return type("R", (), {"success": True, "reason": f"enabled by {actor}"})()


def test_orchestrator_self_evolution_query_bypasses_session_context(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    skill_manager = _FakeSkillManager()
    capability_manager = _FakeCapabilityManager()
    active = store.create_session('user-1', 'feishu', summary='旧任务')
    store.add_event(
        session_id=active.session_id,
        role='assistant',
        content='我没有自我进化能力',
    )
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.FOLLOW_UP, session_id=active.session_id)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        available_tools=[
            type('T', (), {'name': 'skill_list_installable'})(),
            type('T', (), {'name': 'skill_install'})(),
            type('T', (), {'name': 'skill_create'})(),
            type('T', (), {'name': 'skill_update'})(),
            type('T', (), {'name': 'skill_list_installed'})(),
            type('T', (), {'name': 'load_skill'})(),
            type('T', (), {'name': 'capability_list_available'})(),
            type('T', (), {'name': 'capability_status'})(),
            type('T', (), {'name': 'capability_enable'})(),
            type('T', (), {'name': 'evolution_audit'})(),
        ],
        skill_manager=skill_manager,
        capability_manager=capability_manager,
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message('你有自我进化能力吗？'), reply))

    assert replies
    assert '受控的自我进化能力' in replies[0].content
    assert '我没有自我进化能力' not in replies[0].content
    assert 'skill_list_installable' in replies[0].content
    assert 'skill_install' in replies[0].content
    assert 'office-conversion' in replies[0].content


def test_orchestrator_mesh_inventory_query_bypasses_session_context(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    active = store.create_session('user-1', 'feishu', summary='旧节点对话')
    store.add_event(
        session_id=active.session_id,
        role='assistant',
        content='当前只有 Hub 节点可用，没有 Mac 节点。',
    )
    registry = MeshRegistry()
    asyncio.run(registry.register_node(_hub_card()))
    asyncio.run(registry.register_node(_edge_card()))
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.FOLLOW_UP, session_id=active.session_id)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        mesh_registry=registry,
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message('那好，我想问你，现在你的控制下，有几个节点？每个节点都有什么能力？'), reply))

    assert replies
    assert 'Mesh 注册表中共有 **2** 个节点' in replies[0].content
    assert 'MacBook Pro' in replies[0].content
    assert 'browser_automation' in replies[0].content
    assert '当前只有 Hub 节点可用' not in replies[0].content


def test_orchestrator_can_install_skill_from_catalog_deterministically(tmp_path):
    store = SessionStore(tmp_path / 'sessions.db')
    skill_manager = _FakeSkillManager()
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        skill_manager=skill_manager,
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message('请安装 office-conversion skill'), reply))

    assert skill_manager.installed_from_catalog == ['office-conversion']
    assert replies
    assert 'office-conversion' in replies[0].content
    assert '已安装成功' in replies[0].content


def test_orchestrator_records_recent_attachments_and_augments_follow_up(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    run_manager = _FakeRunManager()
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        formatter=MessageFormatter(),
    )
    replies = []

    async def reply(message):
        replies.append(message)

    attachment_message = InboundMessage(
        message_id="msg-attachment",
        channel=ChannelType.FEISHU,
        sender_id="user-1",
        content="[附加资产摘要]\n- file `report.pdf` 已保存到 `_system/artifacts/files/2026/03/report.pdf`，知识页 `inbox/imports/feishu/导入-PDF-report.md`",
        attachments=[
            {
                "artifact_id": "art_1",
                "artifact_type": "file",
                "filename": "report.pdf",
                "relative_path": "_system/artifacts/files/2026/03/report.pdf",
                "page_relative_path": "inbox/imports/feishu/导入-PDF-report.md",
            }
        ],
    )
    asyncio.run(orchestrator.handle_message(attachment_message, reply))

    session = store.get_most_recent_session("user-1")
    assert session is not None
    assert store.get_recent_artifacts(session.session_id)[0]["filename"] == "report.pdf"

    follow_up_orchestrator = Orchestrator(
        session_router=_FakeRouter(
            RoutingDecision(intent=MessageIntent.FOLLOW_UP, session_id=session.session_id)
        ),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        formatter=MessageFormatter(),
    )
    asyncio.run(
        follow_up_orchestrator.handle_message(
            _message("我上传给你个PDF文件，你帮我管理在vault中，这是第三方的一个研究报告"),
            reply,
        )
    )

    assert run_manager.last_kwargs is not None
    effective_task = run_manager.last_kwargs["task"]
    context_messages = run_manager.last_kwargs["context_messages"]
    assert "最近附件引用" in effective_task
    assert "report.pdf" in effective_task
    assert "inbox/imports/feishu/导入-PDF-report.md" in effective_task
    assert context_messages[0]["role"] == "system"
    assert "session_recent_artifacts" in context_messages[0]["content"]
    assert "report.pdf" in context_messages[0]["content"]


def test_orchestrator_new_task_abandons_previous_active_sessions(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    old_session = store.create_session("user-1", "feishu", summary="旧 PDF 任务")
    store.add_event(old_session.session_id, "user", "我上传给你个PDF文件")

    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("列出现在你已经有的PDF文件"), reply))

    sessions = store.get_recent_sessions("user-1", limit=5)
    status_by_summary = {session.summary: session.status.value for session in sessions}
    assert status_by_summary["旧 PDF 任务"] == "abandoned"
    assert any(status == "completed" for status in status_by_summary.values())


def test_orchestrator_marks_failed_run_session_paused(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FailingRunManager(),
        formatter=MessageFormatter(),
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("请处理失败场景"), reply))

    session = store.get_most_recent_session("user-1")
    assert session is not None
    assert session.status.value == "paused"


def test_orchestrator_injects_remote_tools_for_mesh_run(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    run_manager = _FakeRunManager()
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="orch-mesh-tools")
    asyncio.run(transport.connect())
    registry = MeshRegistry()
    asyncio.run(registry.register_node(_hub_card()))
    asyncio.run(registry.register_node(_edge_card()))
    task_router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "write_vault", "background_run", "browser_navigate", "browser_extract_text"},
        planner_mode="heuristic",
    )

    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        formatter=MessageFormatter(),
        task_router=task_router,
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("打开浏览器登录网站抓取内容，然后长时间分析整理"), reply))

    assert run_manager.last_kwargs is not None
    extra_tools = run_manager.last_kwargs["extra_tools"]
    extra_names = {tool.name for tool in extra_tools}
    # Browser step on edge node now uses dispatch tool (agent_loop) instead of individual mesh__ tools
    dispatch_alias = RemoteToolProxy.dispatch_alias_for("macbook-pro")
    assert dispatch_alias in extra_names
    assert "Mesh 执行上下文" in run_manager.last_kwargs["task"]
    assert any("跨节点执行计划" in item.content for item in replies)


def test_orchestrator_blocks_when_required_node_is_offline(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    run_manager = _FakeRunManager()
    transport = InMemoryTransport("ubuntu-server-5090", hub_name="orch-mesh-blocked")
    asyncio.run(transport.connect())
    registry = MeshRegistry()
    asyncio.run(registry.register_node(_hub_card()))
    task_router = TaskRouter(
        registry=registry,
        local_node_id="ubuntu-server-5090",
        transport=transport,
        local_tool_names={"read_vault", "write_vault", "background_run"},
        planner_mode="heuristic",
    )

    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        formatter=MessageFormatter(),
        task_router=task_router,
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("打开浏览器抓取网页内容"), reply))

    assert run_manager.last_kwargs is None
    assert any(item.message_type.value == "blocked" for item in replies)
    session = store.get_most_recent_session("user-1")
    assert session is not None
    assert session.status.value == "paused"
