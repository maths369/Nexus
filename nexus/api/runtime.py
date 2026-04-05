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
    HeartbeatEngine,
    SessionManager,
    SubagentRegistry,
    SubagentRunner,
    TaskDAG,
    TranscriptWriter,
    TranscriptStore,
    TodoManager,
)
from nexus.agent.attempt import AttemptBuilder
from nexus.agent.run import RunManager
from nexus.agent.run_store import RunStore
from nexus.agent.tool_registry import build_tool_registry
from nexus.agent.approval import ApprovalEngine
from nexus.agent.tool_profiles import ToolProfile
from nexus.agent.tools_policy import PolicyLayer, ToolsPolicy
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
from nexus.mesh.task_manager import TaskManager
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


def _resolve_audio_device(raw_device: str) -> str:
    candidate = str(raw_device or "auto").strip().lower()
    if candidate and candidate != "auto":
        return candidate
    try:
        import torch

        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


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
    search_config: dict[str, object]
    session_store: SessionStore
    context_window: ContextWindowManager
    session_router: SessionRouter
    run_store: RunStore
    attempt_builder: AttemptBuilder
    run_manager: RunManager
    session_manager: SessionManager
    heartbeat_engine: HeartbeatEngine
    transcript_writer: TranscriptWriter
    transcript_store: TranscriptStore
    subagent_registry: SubagentRegistry
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
    approval_engine: ApprovalEngine | None = None
    mesh_transport: MQTTTransport | None = None
    mesh_registry: MeshRegistry | None = None
    mesh_task_router: TaskRouter | None = None
    mesh_task_manager: TaskManager | None = None
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
    if any(skill_registry_dir.glob("*/SKILL.md")):
        return
    for skill_dir in builtin_root.iterdir():
        if not skill_dir.is_dir():
            continue
        manifest_path = skill_dir / "SKILL.md"
        if not manifest_path.exists():
            continue
        shutil.copytree(skill_dir, skill_registry_dir / skill_dir.name, dirs_exist_ok=True)


def _ensure_heartbeat_template(heartbeat_path: Path) -> None:
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    if heartbeat_path.exists():
        return
    heartbeat_path.write_text(
        "\n".join(
            [
                "# Heartbeat Inbox",
                "",
                "<!--",
                "在这里写入需要心跳巡检处理的待办。",
                "只有存在非注释正文时，HeartbeatEngine 才会触发巡检。",
                "-->",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _build_base_tool_layers(
    settings: NexusSettings,
    *,
    testing_mode: bool,
) -> list[PolicyLayer]:
    layers: list[PolicyLayer] = []

    profile_name = str(_deep_get(settings.raw, "agent.tool_profile", "full") or "full").strip().lower()
    if profile_name and profile_name != "full":
        try:
            profile = ToolProfile.from_name(profile_name)
        except ValueError:
            logger.warning("Unknown agent.tool_profile=%s; ignoring base profile layer", profile_name)
        else:
            layers.append(
                PolicyLayer(
                    name=f"profile:{profile.name}",
                    allow=sorted(profile.include) if profile.include is not None else None,
                    deny=sorted(profile.exclude),
                    also_allow=sorted(profile.also_allow),
                    max_risk_level=profile.max_risk_level,
                )
            )

    tool_policy_raw = dict(_deep_get(settings.raw, "tool_policy", {}) or {})
    if tool_policy_raw.get("enabled", True):
        allowlist = None if testing_mode else tool_policy_raw.get("allowlist")
        layers.append(
            PolicyLayer(
                name="global",
                allow=[str(item).strip() for item in (allowlist or []) if str(item).strip()] if allowlist else None,
                deny=[str(item).strip() for item in (tool_policy_raw.get("denylist") or []) if str(item).strip()],
                also_allow=[str(item).strip() for item in (tool_policy_raw.get("also_allow") or []) if str(item).strip()],
            )
        )
    return layers


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
    search_config = runtime_settings.search_config()

    session_store = SessionStore(paths.sqlite / "sessions.db")
    context_window = ContextWindowManager(session_store=session_store)
    session_router = SessionRouter(
        session_store=session_store,
        context_window=context_window,
        provider=provider,
    )
    session_config = runtime_settings.agent_session_config()
    session_manager = SessionManager(
        session_store=session_store,
        session_router=session_router,
        idle_timeout_minutes=session_config["idle_timeout_minutes"],
        max_concurrent_sessions=session_config["max_concurrent_sessions"],
        sweep_interval_seconds=session_config["sweep_interval_seconds"],
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
    requested_audio_device = str(audio_settings.get("sensevoice_device", "auto"))
    resolved_audio_device = _resolve_audio_device(requested_audio_device)
    audio_config = AudioConfig(
        backend=str(audio_settings.get("backend", "faster_whisper")),
        language=str(audio_settings.get("language", "auto")),
        sensevoice_model_dir=runtime_settings.resolve_path(
            audio_settings.get("sensevoice_model_dir"),
            "./models/sensevoice/SenseVoiceSmall",
        ),
        sensevoice_device=requested_audio_device,
        faster_whisper_model=str(audio_settings.get("faster_whisper_model", "large-v3")),
        faster_whisper_compute_type=str(audio_settings.get("faster_whisper_compute_type", "float16")),
        preprocessing_enabled=bool(audio_settings.get("preprocessing_enabled", True)),
        preprocessing_backend=str(audio_settings.get("preprocessing_backend", "ffmpeg")),
        preprocessing_filters=str(
            audio_settings.get("preprocessing_filters", "highpass=f=120,lowpass=f=7600,afftdn,loudnorm")
        ),
        deepfilternet_model=str(audio_settings.get("deepfilternet_model", "DeepFilterNet3")),
        deepfilternet_post_filter=bool(audio_settings.get("deepfilternet_post_filter", True)),
        enhancement_target_rate=int(audio_settings.get("enhancement_target_rate", 48000)),
        asr_sample_rate=int(audio_settings.get("asr_sample_rate", 16000)),
        vad_enabled=bool(audio_settings.get("vad_enabled", False)),
        vad_threshold=float(audio_settings.get("vad_threshold", 0.45)),
        vad_min_speech_ms=int(audio_settings.get("vad_min_speech_ms", 200)),
        vad_min_silence_ms=int(audio_settings.get("vad_min_silence_ms", 400)),
        vad_speech_pad_ms=int(audio_settings.get("vad_speech_pad_ms", 120)),
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

    # -- 说话人分离 + 声纹白名单 --
    diarization_engine = None
    voiceprint_store = None
    diar_settings = audio_settings.get("diarization", {})
    if diar_settings.get("enabled", False):
        from nexus.services.audio.diarization import DiarizationConfig, DiarizationEngine
        from nexus.services.audio.voiceprint import VoiceprintStore

        _diar_device = resolved_audio_device
        diarization_engine = DiarizationEngine(
            config=DiarizationConfig(
                enabled=True,
                vad_model=str(diar_settings.get("vad_model", "fsmn-vad")),
                embedding_model=str(diar_settings.get("embedding_model", "iic/speech_campplus_sv_zh-cn_16k-common")),
                device=_diar_device,
                min_speakers=int(diar_settings.get("min_speakers", 1)),
                max_speakers=int(diar_settings.get("max_speakers", 10)),
                clustering=str(diar_settings.get("clustering", "spectral")),
                similarity_threshold=float(diar_settings.get("similarity_threshold", 0.65)),
            )
        )
        _vp_dir = paths.vault / "_system" / "voiceprints"
        _vp_dir.mkdir(parents=True, exist_ok=True)
        voiceprint_store = VoiceprintStore(
            storage_dir=_vp_dir,
            similarity_threshold=float(diar_settings.get("similarity_threshold", 0.65)),
            embedding_extractor=diarization_engine,
        )
        logger.info("说话人分离已启用: device=%s, voiceprints=%s", _diar_device, _vp_dir)

    audio_service = AudioService(
        content_store,
        retrieval_index,
        document_service,
        editor_service=document_editor,
        config=audio_config,
        transcriber=remote_transcriber,
        diarization_engine=diarization_engine,
        voiceprint_store=voiceprint_store,
    )
    # 视觉 OCR 提取器（优先使用 Qwen-VL，回退到 Tesseract）
    vision_ocr_extractor = None
    try:
        from nexus.services.artifact.vision_ocr import build_vision_ocr_extractor

        _primary_cfg = primary_provider or configured_primary
        _vl_api_key = _primary_cfg.resolved_api_key() if _primary_cfg else None
        _vl_base_url = (
            _deep_get(runtime_settings.raw, "vision_ocr.base_url")
            or (_primary_cfg.base_url if _primary_cfg else None)
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        _vl_model = str(
            _deep_get(runtime_settings.raw, "vision_ocr.model") or "qwen-vl-max"
        )
        if _vl_api_key:
            vision_ocr_extractor = build_vision_ocr_extractor(
                api_key=_vl_api_key,
                base_url=_vl_base_url,
                model=_vl_model,
            )
            logger.warning("视觉 OCR 已启用: model=%s base_url=%s", _vl_model, _vl_base_url)
        else:
            logger.warning("视觉 OCR 未启用: 未找到 API key，将回退到 Tesseract")
    except Exception:
        logger.warning("视觉 OCR 初始化失败，将回退到 Tesseract", exc_info=True)

    artifact_service = ArtifactService(
        content_store,
        document_service,
        document_editor=document_editor,
        audio_service=audio_service,
        image_text_extractor=vision_ocr_extractor,
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
        remote_sources=_deep_get(runtime_settings.raw, "evolution.skills.remote_sources", []) or [],
        clawhub_config=_deep_get(runtime_settings.raw, "evolution.skills.clawhub", {}) or {},
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
        session_store=session_store,
        document_service=document_service,
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
    for layer in _build_base_tool_layers(runtime_settings, testing_mode=testing_mode):
        tools_policy.add_layer(layer)
    approval_engine = ApprovalEngine(
        default_timeout=runtime_settings.approval_timeout
        if hasattr(runtime_settings, "approval_timeout")
        else 120.0,
    )
    transcript_writer = TranscriptWriter(paths.data / "transcripts")
    transcript_store = TranscriptStore(paths.vault / "_system" / "transcripts")
    subagent_registry = SubagentRegistry(paths.vault / "_system" / "subagent_runs")
    subagent_registry.recover_orphans()
    from nexus.provider.gateway import get_context_window
    primary_model = provider.primary_provider.model if provider else ""
    compressor = ContextCompressor(
        provider=provider,
        transcript_dir=paths.vault / "_system" / "context_transcripts",
        transcript_store=transcript_store,
        memory_flush_callback=memory_manager.flush_before_compact,
        context_window_tokens=get_context_window(primary_model),
    )
    todo_manager = TodoManager()
    subagent_policy_cfg = runtime_settings.subagent_policy()
    subagent_tools_policy = tools_policy.with_layers(
        PolicyLayer(
            name="subagent",
            allow=[str(item).strip() for item in (subagent_policy_cfg.get("allow") or []) if str(item).strip()] or None,
            deny=[str(item).strip() for item in (subagent_policy_cfg.get("deny") or []) if str(item).strip()],
            also_allow=[str(item).strip() for item in (subagent_policy_cfg.get("also_allow") or []) if str(item).strip()],
        )
    )
    subagent_runner = SubagentRunner(
        provider=provider,
        tools_policy=subagent_tools_policy,
        session_store=session_store,
        session_manager=session_manager,
        context_window=context_window,
        registry=subagent_registry,
    )
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
        search_config=search_config,
        allowlist=tool_allowlist,
    )
    mesh_transport, mesh_registry, mesh_task_router, mesh_node_card, mesh_task_mgr = _build_mesh_components(
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
        workspace_roots=workspace_service.allowed_roots,
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
        memory_manager=memory_manager,
        approval_engine=approval_engine,
        transcript_writer=transcript_writer,
        settings=runtime_settings,
        fallback_models=[
            config.model
            for config in [(primary_provider or configured_primary)]
            + (fallback_providers or configured_fallbacks)
        ],
    )
    heartbeat_config = runtime_settings.heartbeat_config()
    heartbeat_path = paths.vault / "_system" / "heartbeat.md"
    _ensure_heartbeat_template(heartbeat_path)
    heartbeat_engine = HeartbeatEngine(
        session_store=session_store,
        session_manager=session_manager,
        context_window=context_window,
        run_manager=run_manager,
        heartbeat_path=heartbeat_path,
        enabled=bool(heartbeat_config["enabled"]),
        interval_minutes=int(heartbeat_config["interval_minutes"]),
        active_hours=str(heartbeat_config["active_hours"]),
        quiet_days=list(heartbeat_config["quiet_days"]),
        ack_max_chars=int(heartbeat_config["ack_max_chars"]),
        model=heartbeat_config["model"],
    )

    return NexusRuntime(
        settings=runtime_settings,
        paths=paths,
        provider=provider,
        search_config=search_config,
        session_store=session_store,
        context_window=context_window,
        session_router=session_router,
        run_store=run_store,
        attempt_builder=attempt_builder,
        run_manager=run_manager,
        session_manager=session_manager,
        heartbeat_engine=heartbeat_engine,
        transcript_writer=transcript_writer,
        transcript_store=transcript_store,
        subagent_registry=subagent_registry,
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
        approval_engine=approval_engine,
        mesh_transport=mesh_transport,
        mesh_registry=mesh_registry,
        mesh_task_router=mesh_task_router,
        mesh_task_manager=mesh_task_mgr,
        mesh_node_card=mesh_node_card,
        edge_journal_store=edge_journal_store,
    )


def _build_mesh_components(
    *,
    runtime_settings: NexusSettings,
    provider: ProviderGateway,
    available_tools: list,
    capability_manager: CapabilityManager | None = None,
) -> tuple[MQTTTransport | None, MeshRegistry | None, TaskRouter | None, NodeCard | None, TaskManager | None]:
    mesh_config = runtime_settings.mesh_config()
    if not mesh_config.get("enabled"):
        return None, None, None, None, None

    node_id = str(mesh_config.get("node_id") or "").strip()
    if not node_id:
        logger.warning("Mesh is enabled but node_id is missing; skipping mesh bootstrap")
        return None, None, None, None, None

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

    # Create TaskManager for async fire-and-forget dispatch
    data_dir = runtime_settings.root_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    task_db_path = str(data_dir / "task_store.db")
    task_mgr = TaskManager(
        transport=transport,
        local_node_id=node_id,
        db_path=task_db_path,
    )

    task_router = TaskRouter(
        registry=registry,
        local_node_id=node_id,
        provider=provider,
        transport=transport,
        local_tool_names={getattr(tool, "name", "") for tool in available_tools},
        capability_manager=capability_manager,
        task_manager=task_mgr,
    )
    return transport, registry, task_router, node_card, task_mgr


async def start_mesh_runtime(runtime: NexusRuntime) -> None:
    transport = getattr(runtime, "mesh_transport", None)
    registry = getattr(runtime, "mesh_registry", None)
    if transport is None or registry is None:
        return
    if not transport.connected:
        await transport.connect()

    await registry.setup_transport_handlers()
    await registry.start_timeout_monitor()

    # Start TaskManager for async dispatch
    task_mgr: TaskManager | None = getattr(runtime, "mesh_task_manager", None)
    if task_mgr is not None:
        await task_mgr.start()

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
