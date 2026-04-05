from __future__ import annotations

import asyncio
import yaml

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
    TaskPlan,
    TaskRouter,
    TaskStep,
)
from nexus.orchestrator import Orchestrator
from nexus.provider.gateway import ProviderConfig, ProviderGateway


class _FakeRouter:
    def __init__(self, decision: RoutingDecision):
        self._decision = decision

    async def route(self, message):
        return self._decision


class _FakeRunManager:
    def __init__(self):
        self.last_kwargs = None
        self.fallback_models = None

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

    def set_fallback_models(self, models):
        self.fallback_models = list(models)


class _SlowRunManager(_FakeRunManager):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, **kwargs):
        self.last_kwargs = kwargs
        self.started.set()
        await self.release.wait()
        return Run(
            run_id="run-slow",
            session_id=kwargs["session_id"],
            status=RunStatus.SUCCEEDED,
            task=kwargs["task"],
            result="slow-done",
            model="qwen-max",
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


class _FakeMemoryManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def promote_session_to_medical_knowledge(self, *, session_id: str):
        self.calls.append(session_id)
        return {"promoted": True, "l2_saved": 1, "l3_written": 0, "l4_written": 0, "conflicts": 0}


class _FakeRemoteProxy:
    def __init__(self):
        self.calls: list[dict[str, str]] = []

    def dispatch_alias_for(self, node_id: str) -> str:
        return RemoteToolProxy.dispatch_alias_for(node_id)

    async def dispatch_to_edge(self, **kwargs):
        self.calls.append(dict(kwargs))
        task_number = len(self.calls)
        return (
            f"任务已异步派发到 {kwargs['target_node']}，"
            f"task_id: task-fallback-{task_number:02d}。"
        )


class _FakeTaskRouterForFallback:
    def __init__(self, plan: TaskPlan, remote_proxy: _FakeRemoteProxy):
        self._plan = plan
        self._remote_proxy = remote_proxy

    def get_session_plan(self, session_id: str):
        if session_id == self._plan.session_id:
            return self._plan
        return None

    @staticmethod
    def get_agent_loop_steps(plan: TaskPlan) -> list[TaskStep]:
        return [
            step
            for step in plan.steps
            if step.metadata.get("execution_mode") == "agent_loop"
            and step.assigned_node
        ]


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


def test_orchestrator_coding_profile_keeps_subagent_dispatch(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
    )

    profile = orchestrator._select_tool_profile("请调用 dispatch_subagent 检查这个文件")  # noqa: SLF001

    assert profile is not None
    assert profile.name == "coding"
    assert profile.include is not None
    assert "dispatch_subagent" in profile.include


def test_orchestrator_switches_provider_and_persists_config(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    run_manager = _FakeRunManager()
    provider = ProviderGateway(
        primary=ProviderConfig(name="qwen", model="qwen-plus", api_key="qwen-key"),
        fallbacks=[
            ProviderConfig(name="kimi", model="kimi-k2.5", api_key="kimi-key"),
            ProviderConfig(name="gemini", model="gemini-2.5-flash", api_key="gemini-key"),
        ],
    )
    config_path = tmp_path / "config" / "app.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "provider:",
                "  primary:",
                "    name: qwen",
                "    model: qwen-plus",
                "    provider_type: qwen",
                "  fallbacks:",
                "  - name: kimi",
                "    model: kimi-k2.5",
                "    provider_type: moonshot",
                "  - name: gemini",
                "    model: gemini-2.5-flash",
                "    provider_type: openai-compatible",
            ]
        ),
        encoding="utf-8",
    )
    orchestrator = Orchestrator(
        session_router=_FakeRouter(
            RoutingDecision(
                intent=MessageIntent.COMMAND,
                reason="command:provider",
                metadata={"action": "provider", "provider_command": "switch", "target": "gemini"},
            )
        ),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        formatter=MessageFormatter(),
        provider_gateway=provider,
        config_path=config_path,
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("/provider gemini"), reply))

    assert provider.primary_provider.name == "gemini"
    assert run_manager.fallback_models == ["gemini-2.5-flash", "qwen-plus", "kimi-k2.5"]
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["provider"]["primary"]["name"] == "gemini"
    assert replies
    assert "已切换当前后端到 `gemini`" in replies[0].content


def test_orchestrator_provider_status_lists_current_and_available(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    orchestrator = Orchestrator(
        session_router=_FakeRouter(
            RoutingDecision(
                intent=MessageIntent.COMMAND,
                reason="command:provider",
                metadata={"action": "provider", "provider_command": "status"},
            )
        ),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        provider_gateway=ProviderGateway(
            primary=ProviderConfig(name="qwen", model="qwen-plus", api_key="qwen-key"),
            fallbacks=[ProviderConfig(name="gemini", model="gemini-2.5-flash", api_key="gemini-key")],
        ),
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("/provider"), reply))

    assert replies
    assert "当前后端：" in replies[0].content
    assert "`qwen`" in replies[0].content
    assert "`gemini`" in replies[0].content


def test_orchestrator_switches_search_provider_and_persists_config(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    config_path = tmp_path / "config" / "app.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "search:",
                "  provider:",
                "    primary: google_grounded",
                "    fallback: bing",
                "    fallbacks:",
                "    - bing",
                "    - duckduckgo",
                "  google_grounded:",
                "    enabled: true",
                "    api_key_env: GEMINI_API_KEY",
                "    api_key: gemini-key",
            ]
        ),
        encoding="utf-8",
    )
    search_config = {
        "provider": {
            "primary": "google_grounded",
            "fallback": "bing",
            "fallbacks": ["bing", "duckduckgo"],
        },
        "google_grounded": {
            "enabled": True,
            "api_key": "gemini-key",
        },
    }
    orchestrator = Orchestrator(
        session_router=_FakeRouter(
            RoutingDecision(
                intent=MessageIntent.COMMAND,
                reason="command:search",
                metadata={"action": "search_provider", "search_command": "switch", "target": "bing"},
            )
        ),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        search_config=search_config,
        config_path=config_path,
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("/search bing"), reply))

    assert search_config["provider"]["primary"] == "bing"
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["search"]["provider"]["primary"] == "bing"
    assert replies
    assert "已切换当前搜索后端到 `bing`" in replies[0].content


def test_orchestrator_search_status_lists_current_and_fallbacks(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    orchestrator = Orchestrator(
        session_router=_FakeRouter(
            RoutingDecision(
                intent=MessageIntent.COMMAND,
                reason="command:search",
                metadata={"action": "search_provider", "search_command": "status"},
            )
        ),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        search_config={
            "provider": {
                "primary": "google_grounded",
                "fallback": "bing",
                "fallbacks": ["bing", "duckduckgo"],
            },
            "google_grounded": {
                "enabled": True,
                "api_key": "gemini-key",
            },
        },
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("/search"), reply))

    assert replies
    assert "当前搜索后端：" in replies[0].content
    assert "`google_grounded`" in replies[0].content
    assert "`bing -> duckduckgo`" in replies[0].content


def test_orchestrator_failed_run_mentions_attempted_provider_chain(tmp_path):
    class _RetryingFailRunManager(_FakeRunManager):
        async def execute(self, **kwargs):
            return Run(
                run_id="run-3",
                session_id=kwargs["session_id"],
                status=RunStatus.FAILED,
                task=kwargs["task"],
                error="Provider quota exhausted (gemini-3-flash-preview): 当前额度已用尽，请切换后端或稍后重试。",
                model="gemini-3-flash-preview",
                metadata={
                    "attempt_models": [
                        "qwen-plus",
                        "gemini-2.5-flash",
                        "gemini-3-flash-preview",
                    ]
                },
            )

    store = SessionStore(tmp_path / "sessions.db")
    orchestrator = Orchestrator(
        session_router=_FakeRouter(
            RoutingDecision(
                intent=MessageIntent.NEW_TASK,
                reason="default new task",
            )
        ),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_RetryingFailRunManager(),
        formatter=MessageFormatter(),
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("帮我查 ISO14971"), reply))

    assert len(replies) >= 2
    assert "本次请求已依次尝试 `qwen-plus` -> `gemini-2.5-flash` -> `gemini-3-flash-preview`。" in replies[-1].content
    assert "当前后端 `gemini-3-flash-preview` 已达到额度限制。" in replies[-1].content


def test_orchestrator_queues_concurrent_run_for_same_session(tmp_path):
    """繁忙时第二条消息应排队并通知用户，而非直接拒绝。"""
    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "feishu", summary="长任务")
    run_manager = _SlowRunManager()
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        formatter=MessageFormatter(),
    )
    replies: list = []

    async def reply(message):
        replies.append(message)

    async def scenario():
        first = asyncio.create_task(
            orchestrator._start_run(session.session_id, "先执行这个", reply)
        )
        await run_manager.started.wait()

        # 第二条消息: 应排队而非拒绝
        second = asyncio.create_task(
            orchestrator._start_run(session.session_id, "后执行这个", reply)
        )
        # 给 enqueue 和通知一点时间
        await asyncio.sleep(0.05)

        run_manager.release.set()
        await first
        await second

    asyncio.run(scenario())

    # 应收到排队通知（而非"任务正在执行中"的拒绝）
    queued_replies = [r for r in replies if "已排队" in r.content]
    assert len(queued_replies) >= 1
    assert "/new" in queued_replies[0].content


def test_orchestrator_returns_full_when_queue_is_saturated(tmp_path):
    """队列满时应告知用户无法接受更多消息。"""
    from nexus.agent.session_manager import SessionManager

    store = SessionStore(tmp_path / "sessions.db")
    session = store.create_session("user-1", "feishu", summary="满队列")
    run_manager = _SlowRunManager()
    session_manager = SessionManager(
        store,
        _FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        max_queue_size=1,
    )
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=run_manager,
        formatter=MessageFormatter(),
        session_manager=session_manager,
    )
    replies: list = []

    async def reply(message):
        replies.append(message)

    async def scenario():
        first = asyncio.create_task(
            orchestrator._start_run(session.session_id, "第一个", reply)
        )
        await run_manager.started.wait()

        # 填满队列 (maxsize=1)
        second = asyncio.create_task(
            orchestrator._start_run(session.session_id, "第二个", reply)
        )
        await asyncio.sleep(0.05)

        # 第三个: 队列满
        await orchestrator._start_run(session.session_id, "第三个", reply)

        run_manager.release.set()
        await first
        await second

    asyncio.run(scenario())

    full_replies = [r for r in replies if "队列已满" in r.content]
    assert len(full_replies) >= 1


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


## test_orchestrator_self_evolution_query_bypasses_session_context — 已移除
## 原因：_handle_runtime_fact_query 已删除，"自我进化"等关键词不再做硬编码拦截，
## 统一交给 LLM 理解用户意图。

## test_orchestrator_mesh_inventory_query_bypasses_session_context — 已移除
## 原因：同上，Mesh 盘点查询也不再做关键词拦截。


## test_orchestrator_can_install_skill_from_catalog_deterministically — 已移除
## 原因：_match_installable_skill_request 已删除，技能安装不再做关键词匹配，
## 统一交给 LLM 理解用户意图并调用工具。


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
    assert "最近附件" in effective_task
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
    # _handle_new_task 将旧 session 标记为 completed（持续会话模型）
    assert status_by_summary["旧 PDF 任务"] == "completed"


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
    # LLM-driven routing: dispatch tools 被注入，LLM 自行决定是否使用
    dispatch_alias = RemoteToolProxy.dispatch_alias_for("macbook-pro")
    assert dispatch_alias in extra_names


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

    # LLM-driven routing: 不再阻断任务。没有 edge 节点在线时，
    # 不注入 dispatch tools，LLM 用自己的能力处理或告知用户。
    assert run_manager.last_kwargs is not None
    extra_tools = run_manager.last_kwargs["extra_tools"]
    # 没有 edge 节点在线 → 没有 dispatch tools
    assert len(extra_tools) == 0


def _agent_loop_plan(session_id: str, steps: list[TaskStep]) -> TaskPlan:
    return TaskPlan(
        task_id="plan-1",
        session_id=session_id,
        user_task="agent-loop task",
        steps=steps,
    )


def test_orchestrator_force_dispatch_ignores_hallucinated_result_text(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = "session-force-1"
    step = TaskStep(
        step_id="step-1",
        description="打开语音备忘录，并开始录制",
        assigned_node="macbook-pro",
        metadata={"execution_mode": "agent_loop"},
    )
    remote_proxy = _FakeRemoteProxy()
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        task_router=_FakeTaskRouterForFallback(_agent_loop_plan(session_id, [step]), remote_proxy),
    )
    run = Run(
        run_id="run-force-1",
        session_id=session_id,
        status=RunStatus.SUCCEEDED,
        task=step.description,
        result="任务已异步派发到 macbook-pro，task_id: task-fake-123。",
        model="qwen-max",
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(
        orchestrator._force_dispatch_undispatched_steps(
            session_id=session_id,
            run=run,
            reply=reply,
        )
    )

    assert len(remote_proxy.calls) == 1
    assert remote_proxy.calls[0]["target_node"] == "macbook-pro"
    assert remote_proxy.calls[0]["task_description"] == step.description
    assert any("已自动派发任务到边缘节点" in item.content for item in replies)


def test_orchestrator_force_dispatch_skips_matching_successful_dispatch_record(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = "session-force-2"
    step = TaskStep(
        step_id="step-1",
        description="打开语音备忘录，并开始录制",
        assigned_node="macbook-pro",
        metadata={"execution_mode": "agent_loop"},
    )
    remote_proxy = _FakeRemoteProxy()
    dispatch_alias = remote_proxy.dispatch_alias_for("macbook-pro")
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        task_router=_FakeTaskRouterForFallback(_agent_loop_plan(session_id, [step]), remote_proxy),
    )
    run = Run(
        run_id="run-force-2",
        session_id=session_id,
        status=RunStatus.SUCCEEDED,
        task=step.description,
        result="done",
        model="qwen-max",
        metadata={
            "successful_mesh_dispatches": [
                {
                    "tool": dispatch_alias,
                    "task_description": step.description,
                }
            ]
        },
    )

    asyncio.run(
        orchestrator._force_dispatch_undispatched_steps(
            session_id=session_id,
            run=run,
            reply=lambda _message: asyncio.sleep(0),
        )
    )

    assert remote_proxy.calls == []


def test_orchestrator_force_dispatch_only_dispatches_missing_step_on_same_node(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    session_id = "session-force-3"
    step_a = TaskStep(
        step_id="step-a",
        description="打开语音备忘录",
        assigned_node="macbook-pro",
        metadata={"execution_mode": "agent_loop"},
    )
    step_b = TaskStep(
        step_id="step-b",
        description="点击录制按钮",
        assigned_node="macbook-pro",
        metadata={"execution_mode": "agent_loop"},
    )
    remote_proxy = _FakeRemoteProxy()
    dispatch_alias = remote_proxy.dispatch_alias_for("macbook-pro")
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        task_router=_FakeTaskRouterForFallback(_agent_loop_plan(session_id, [step_a, step_b]), remote_proxy),
    )
    run = Run(
        run_id="run-force-3",
        session_id=session_id,
        status=RunStatus.SUCCEEDED,
        task="打开语音备忘录并开始录制",
        result="done",
        model="qwen-max",
        metadata={
            "successful_mesh_dispatches": [
                {
                    "tool": dispatch_alias,
                    "task_description": step_a.description,
                }
            ]
        },
    )

    asyncio.run(
        orchestrator._force_dispatch_undispatched_steps(
            session_id=session_id,
            run=run,
            reply=lambda _message: asyncio.sleep(0),
        )
    )

    assert len(remote_proxy.calls) == 1
    assert remote_proxy.calls[0]["task_description"] == step_b.description


def test_orchestrator_promotes_completed_feishu_session_memory(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    memory_manager = _FakeMemoryManager()
    orchestrator = Orchestrator(
        session_router=_FakeRouter(RoutingDecision(intent=MessageIntent.NEW_TASK)),
        session_store=store,
        context_window=ContextWindowManager(store),
        run_manager=_FakeRunManager(),
        formatter=MessageFormatter(),
        memory_manager=memory_manager,
    )
    replies = []

    async def reply(message):
        replies.append(message)

    asyncio.run(orchestrator.handle_message(_message("请整理这次 BF 漏电流讨论"), reply))

    sessions = store.get_recent_sessions("user-1", limit=1)
    assert sessions
    assert memory_manager.calls == [sessions[0].session_id]
