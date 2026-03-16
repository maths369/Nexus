"""Application-level runtime assembly for Nexus."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
import shutil
import sys

from nexus.agent import (
    BackgroundTaskManager,
    ContextCompressor,
    SubagentRunner,
    TaskDAG,
    TodoManager,
)
from nexus.agent.attempt import AttemptBuilder
from nexus.agent.run import RunManager
from nexus.agent.run_store import RunStore
from nexus.agent.tool_registry import build_tool_registry
from nexus.agent.tools_policy import ToolsPolicy
from nexus.channel.context_window import ContextWindowManager
from nexus.channel.session_router import SessionRouter
from nexus.channel.session_store import SessionStore
from nexus.channel.types import ChannelType
from nexus.agent.system_run import SystemRunner
from nexus.evolution import (
    AuditLog,
    CapabilityManager,
    CapabilityPromotionAdvisor,
    ConfigManager,
    Sandbox,
    SkillManager,
)
from nexus.knowledge import (
    EpisodicMemory,
    KnowledgeIngestService,
    MemoryManager,
    RetrievalIndex,
    StructuralIndex,
    VaultContentStore,
)
from nexus.mesh import MeshRegistry, MQTTTransport, NodeCard, NodeType, TaskRouter
from nexus.mesh.edge_journal_store import EdgeJournalStore
from nexus.provider import ProviderConfig, ProviderGateway
from nexus.services.audio import AudioConfig, AudioService, RemoteAudioWorkerClient
from nexus.services.artifact import ArtifactService
from nexus.services.browser import (
    BrowserService,
    BrowserWorkerConfig,
    default_browser_worker_command,
)
from nexus.services.document import DocumentEditorService, DocumentService
from nexus.services.spreadsheet import SpreadsheetService
from nexus.services.vault import VaultManagerService
from nexus.services.workspace import WorkspaceService
from nexus.shared import NexusSettings, load_nexus_settings
from nexus.shared.config import _deep_get
from nexus.agent.types import ToolRiskLevel

logger = logging.getLogger(__name__)


@dataclass
class NexusPaths:
    root: Path
    config: Path
    data: Path
    sqlite: Path
    vault: Path
    skills: Path
    skill_registry: Path
    capabilities: Path
    staging: Path
    backups: Path


@dataclass
class NexusRuntime:
    settings: NexusSettings
    paths: NexusPaths
    provider: ProviderGateway
    session_store: SessionStore
    context_window: ContextWindowManager
    session_router: SessionRouter
    run_store: RunStore
    attempt_builder: AttemptBuilder
    run_manager: RunManager
    content_store: VaultContentStore
    structural_index: StructuralIndex
    retrieval_index: RetrievalIndex
    episodic_memory: EpisodicMemory
    ingest_service: KnowledgeIngestService
    document_service: DocumentService
    document_editor: DocumentEditorService
    audio_service: AudioService
    artifact_service: ArtifactService
    browser_service: BrowserService
    workspace_service: WorkspaceService
    tools_policy: ToolsPolicy
    available_tools: list
    compressor: ContextCompressor
    todo_manager: TodoManager
    subagent_runner: SubagentRunner
    task_dag: TaskDAG
    background_manager: BackgroundTaskManager
    sandbox: Sandbox
    audit_log: AuditLog
    skill_manager: SkillManager
    capability_manager: CapabilityManager
    config_manager: ConfigManager
    vault_manager: VaultManagerService
    spreadsheet_service: SpreadsheetService
    memory_manager: MemoryManager
    capability_promotion_advisor: CapabilityPromotionAdvisor
    mesh_transport: MQTTTransport | None = None
    mesh_registry: MeshRegistry | None = None
    mesh_task_router: TaskRouter | None = None
    mesh_node_card: NodeCard | None = None
    edge_journal_store: EdgeJournalStore | None = None


def _seed_builtin_capabilities(capabilities_dir: Path) -> None:
    builtin_root = Path(__file__).resolve().parents[2] / "capabilities"
    if not builtin_root.exists():
        return
    if any(capabilities_dir.glob("*/CAPABILITY.yaml")):
        return
    for capability_dir in builtin_root.iterdir():
        if not capability_dir.is_dir():
            continue
        manifest_path = capability_dir / "CAPABILITY.yaml"
        if not manifest_path.exists():
            continue
        shutil.copytree(capability_dir, capabilities_dir / capability_dir.name, dirs_exist_ok=True)


def _seed_builtin_skill_registry(skill_registry_dir: Path) -> None:
    builtin_root = Path(__file__).resolve().parents[2] / "skill_registry"
    if not builtin_root.exists():
        return
    if any(skill_registry_dir.glob("*/skill.yaml")):
        return
    for skill_dir in builtin_root.iterdir():
        if not skill_dir.is_dir():
            continue
        manifest_path = skill_dir / "skill.yaml"
        if not manifest_path.exists():
            continue
        shutil.copytree(skill_dir, skill_registry_dir / skill_dir.name, dirs_exist_ok=True)


def build_runtime(
    root_dir: Path | None = None,
    *,
    settings: NexusSettings | None = None,
    primary_provider: ProviderConfig | None = None,
    fallback_providers: list[ProviderConfig] | None = None,
) -> NexusRuntime:
    runtime_settings = settings or load_nexus_settings(root_dir)
    root = runtime_settings.root_dir
    paths = NexusPaths(
        root=root,
        config=runtime_settings.config_path.parent,
        data=runtime_settings.sqlite_dir.parent,
        sqlite=runtime_settings.sqlite_dir,
        vault=runtime_settings.vault_base_path,
        skills=runtime_settings.skills_dir,
        skill_registry=runtime_settings.skill_registry_dir,
        capabilities=runtime_settings.capabilities_dir,
        staging=runtime_settings.staging_dir,
        backups=runtime_settings.backups_dir,
    )
    for path in [
        paths.config,
        paths.data,
        paths.sqlite,
        paths.vault,
        paths.skills,
        paths.skill_registry,
        paths.capabilities,
        paths.staging,
        paths.backups,
    ]:
        path.mkdir(parents=True, exist_ok=True)
    _seed_builtin_capabilities(paths.capabilities)
    _seed_builtin_skill_registry(paths.skill_registry)

    configured_primary, configured_fallbacks = runtime_settings.provider_configs()
    provider = ProviderGateway(
        primary=primary_provider or configured_primary,
        fallbacks=fallback_providers or configured_fallbacks,
        unhealthy_cooldown_seconds=float(
            _deep_get(runtime_settings.raw, "provider.unhealthy_cooldown_seconds", 120.0) or 120.0
        ),
    )

    session_store = SessionStore(paths.sqlite / "sessions.db")
    context_window = ContextWindowManager(session_store=session_store)
    session_router = SessionRouter(
        session_store=session_store,
        context_window=context_window,
        provider=provider,
    )

    run_store = RunStore(paths.sqlite / "runs.db")
    content_store = VaultContentStore(paths.vault)
    structural_index = StructuralIndex(paths.sqlite / "knowledge.db")
    retrieval_index = RetrievalIndex(paths.sqlite / "retrieval.db")
    episodic_memory = EpisodicMemory(paths.vault / "_system" / "memory" / "episodic.jsonl")
    ingest_service = KnowledgeIngestService(content_store, retrieval_index)
    document_service = DocumentService(content_store, structural_index, retrieval_index)
    document_editor = DocumentEditorService(document_service, structural_index)
    audio_settings = runtime_settings.audio_config()
    audio_config = AudioConfig(
        backend=str(audio_settings.get("backend", "sensevoice")),
        language=str(audio_settings.get("language", "zh")),
        sensevoice_model_dir=runtime_settings.resolve_path(
            audio_settings.get("sensevoice_model_dir"),
            "./models/sensevoice/SenseVoiceSmall",
        ),
        sensevoice_device=str(audio_settings.get("sensevoice_device", "cpu")),
        temp_directory=runtime_settings.resolve_path(
            audio_settings.get("temp_directory"),
            paths.vault / "_system" / "audio_temp",
        ),
        final_directory=runtime_settings.resolve_path(
            audio_settings.get("final_directory"),
            paths.vault / "_system" / "audio",
        ),
        transcript_directory=runtime_settings.resolve_path(
            audio_settings.get("transcript_directory"),
            paths.vault / "_system" / "transcripts",
        ),
        base_url=str(audio_settings.get("base_url", "http://127.0.0.1:18000")),
    )
    remote_transcriber = None
    if audio_config.backend.lower() in {"sensevoice_remote", "remote"}:
        remote_client = RemoteAudioWorkerClient(audio_config.base_url)
        remote_transcriber = remote_client.transcribe
    audio_service = AudioService(
        content_store,
        retrieval_index,
        document_service,
        editor_service=document_editor,
        config=audio_config,
        transcriber=remote_transcriber,
    )
    artifact_service = ArtifactService(
        content_store,
        document_service,
        document_editor=document_editor,
        audio_service=audio_service,
    )
    workspace_service = WorkspaceService([root, paths.vault])
    browser_service = BrowserService(
        BrowserWorkerConfig(
            enabled=runtime_settings.browser_enabled,
            command=(
                runtime_settings.browser_worker_command
                or default_browser_worker_command()
            ),
            workdir=root,
        )
    )

    sandbox = Sandbox(paths.staging)
    audit_log = AuditLog(paths.sqlite / "evolution_audit.db")
    system_runner = SystemRunner(
        allowed_workdirs=[root, paths.vault, paths.skills, paths.skill_registry, paths.capabilities],
        audit=audit_log,
        default_timeout=600,
    )
    skill_manager = SkillManager(
        paths.skills,
        sandbox,
        audit_log,
        catalog_dir=paths.skill_registry,
        system_runner=system_runner,
        python_executable=runtime_settings.evolution_python_executable,
    )
    capability_manager = CapabilityManager(
        capabilities_dir=paths.capabilities,
        python_executable=runtime_settings.evolution_python_executable,
        audit=audit_log,
        staging_dir=paths.staging / "capabilities",
        backups_dir=paths.backups / "capabilities",
        skills_dir=paths.skills,
    )
    capability_promotion_advisor = CapabilityPromotionAdvisor()
    config_manager = ConfigManager(
        config_path=paths.config / "runtime.json",
        backup_dir=paths.backups / "config",
        sandbox=sandbox,
        audit=audit_log,
    )
    vault_manager = VaultManagerService(runtime_settings)
    spreadsheet_service = SpreadsheetService(workspace_service)

    memory_manager = MemoryManager(
        memory=episodic_memory,
        retrieval=retrieval_index,
        vault_path=paths.vault,
        provider=provider,
        half_life_days=30.0,
    )

    testing_mode = runtime_settings.disable_risk_controls_for_testing
    if testing_mode:
        auto_approve = {
            ToolRiskLevel.LOW,
            ToolRiskLevel.MEDIUM,
            ToolRiskLevel.HIGH,
            ToolRiskLevel.CRITICAL,
        }
        tool_allowlist = None
        logger.warning(
            "Tool policy testing mode enabled: whitelist and Nexus-level risk blocking are disabled"
        )
    else:
        auto_approve = {ToolRiskLevel.LOW, ToolRiskLevel.MEDIUM}
        tool_allowlist = runtime_settings.tool_allowlist
    tools_policy = ToolsPolicy(
        whitelist=tool_allowlist,
        auto_approve_levels=auto_approve,
    )
    compressor = ContextCompressor(
        provider=provider,
        transcript_dir=paths.vault / "_system" / "context_transcripts",
        memory_flush_callback=memory_manager.flush_before_compact,
    )
    todo_manager = TodoManager()
    subagent_runner = SubagentRunner(provider=provider, tools_policy=tools_policy)
    task_dag = TaskDAG(paths.root / ".tasks")
    background_manager = BackgroundTaskManager()
    available_tools = build_tool_registry(
        content_store=content_store,
        document_service=document_service,
        document_editor=document_editor,
        memory=episodic_memory,
        ingest_service=ingest_service,
        audio_service=audio_service,
        browser_service=browser_service,
        spreadsheet_service=spreadsheet_service,
        workspace_service=workspace_service,
        skill_manager=skill_manager,
        capability_manager=capability_manager,
        todo_manager=todo_manager,
        subagent_runner=subagent_runner,
        task_dag=task_dag,
        background_manager=background_manager,
        system_runner=system_runner,
        memory_manager=memory_manager,
        audit_log=audit_log,
        allowlist=tool_allowlist,
    )
    mesh_transport, mesh_registry, mesh_task_router, mesh_node_card = _build_mesh_components(
        runtime_settings=runtime_settings,
        provider=provider,
        available_tools=available_tools,
        capability_manager=capability_manager,
    )
    edge_journal_store = EdgeJournalStore(
        paths.data / "edge_journal_hub",
    ) if mesh_transport is not None else None
    attempt_builder = AttemptBuilder(
        available_tools=available_tools,
        retrieval=retrieval_index,
        memory=episodic_memory,
        memory_manager=memory_manager,
        skill_manager=skill_manager,
    )
    run_manager = RunManager(
        run_store=run_store,
        attempt_builder=attempt_builder,
        provider=provider,
        tools_policy=tools_policy,
        compressor=compressor,
        todo_manager=todo_manager,
        background_manager=background_manager,
        capability_promotion_advisor=capability_promotion_advisor,
        capability_manager=capability_manager,
        skill_manager=skill_manager,
        fallback_models=[
            config.model
            for config in [(primary_provider or configured_primary)]
            + (fallback_providers or configured_fallbacks)
        ],
    )

    return NexusRuntime(
        settings=runtime_settings,
        paths=paths,
        provider=provider,
        session_store=session_store,
        context_window=context_window,
        session_router=session_router,
        run_store=run_store,
        attempt_builder=attempt_builder,
        run_manager=run_manager,
        content_store=content_store,
        structural_index=structural_index,
        retrieval_index=retrieval_index,
        episodic_memory=episodic_memory,
        ingest_service=ingest_service,
        document_service=document_service,
        document_editor=document_editor,
        audio_service=audio_service,
        artifact_service=artifact_service,
        browser_service=browser_service,
        workspace_service=workspace_service,
        tools_policy=tools_policy,
        available_tools=available_tools,
        compressor=compressor,
        todo_manager=todo_manager,
        subagent_runner=subagent_runner,
        task_dag=task_dag,
        background_manager=background_manager,
        sandbox=sandbox,
        audit_log=audit_log,
        skill_manager=skill_manager,
        capability_manager=capability_manager,
        config_manager=config_manager,
        vault_manager=vault_manager,
        spreadsheet_service=spreadsheet_service,
        memory_manager=memory_manager,
        capability_promotion_advisor=capability_promotion_advisor,
        mesh_transport=mesh_transport,
        mesh_registry=mesh_registry,
        mesh_task_router=mesh_task_router,
        mesh_node_card=mesh_node_card,
        edge_journal_store=edge_journal_store,
    )


def _build_mesh_components(
    *,
    runtime_settings: NexusSettings,
    provider: ProviderGateway,
    available_tools: list,
    capability_manager: CapabilityManager | None = None,
) -> tuple[MQTTTransport | None, MeshRegistry | None, TaskRouter | None, NodeCard | None]:
    mesh_config = runtime_settings.mesh_config()
    if not mesh_config.get("enabled"):
        return None, None, None, None

    node_id = str(mesh_config.get("node_id") or "").strip()
    if not node_id:
        logger.warning("Mesh is enabled but node_id is missing; skipping mesh bootstrap")
        return None, None, None, None

    transport = MQTTTransport(
        node_id=node_id,
        hostname=str(mesh_config.get("broker_host") or "127.0.0.1"),
        port=int(mesh_config.get("broker_port") or 1883),
        username=mesh_config.get("username"),
        password=mesh_config.get("password"),
        transport=str(mesh_config.get("transport") or "tcp"),
        websocket_path=mesh_config.get("websocket_path"),
        keepalive=int(mesh_config.get("keepalive_seconds") or 60),
        qos=int(mesh_config.get("qos") or 1),
        tls_enabled=bool(mesh_config.get("tls_enabled")),
        tls_ca_path=mesh_config.get("tls_ca_path"),
        tls_cert_path=mesh_config.get("tls_cert_path"),
        tls_key_path=mesh_config.get("tls_key_path"),
        tls_insecure=bool(mesh_config.get("tls_insecure")),
    )
    registry = MeshRegistry(transport)
    node_card = None
    node_card_path = str(mesh_config.get("node_card_path") or "").strip()
    if node_card_path:
        try:
            node_card = NodeCard.from_yaml_file(str(runtime_settings.resolve_path(node_card_path, node_card_path)))
        except Exception:
            logger.warning("Failed to load mesh node card: %s", node_card_path, exc_info=True)

    task_router = TaskRouter(
        registry=registry,
        local_node_id=node_id,
        provider=provider,
        transport=transport,
        local_tool_names={getattr(tool, "name", "") for tool in available_tools},
        capability_manager=capability_manager,
    )
    return transport, registry, task_router, node_card


async def start_mesh_runtime(runtime: NexusRuntime) -> None:
    transport = getattr(runtime, "mesh_transport", None)
    registry = getattr(runtime, "mesh_registry", None)
    if transport is None or registry is None:
        return
    if not transport.connected:
        await transport.connect()

    await registry.setup_transport_handlers()
    await registry.start_timeout_monitor()

    node_card = getattr(runtime, "mesh_node_card", None)
    if node_card is not None and node_card.node_type == NodeType.HUB:
        await registry.register_node(node_card)


async def stop_mesh_runtime(runtime: NexusRuntime) -> None:
    registry = getattr(runtime, "mesh_registry", None)
    transport = getattr(runtime, "mesh_transport", None)
    if registry is not None:
        await registry.stop_timeout_monitor()
    if transport is not None:
        await transport.disconnect()
