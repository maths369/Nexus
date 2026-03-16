"""Configuration loading helpers for Nexus."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def _deep_get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def _deep_set(data: dict[str, Any], path: str, value: Any) -> None:
    current: dict[str, Any] = data
    keys = path.split(".")
    for key in keys[:-1]:
        next_value = current.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            current[key] = next_value
        current = next_value
    current[keys[-1]] = value


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def find_project_root(start: Path | None = None) -> Path:
    candidates: list[Path] = []
    env_root = os.getenv("NEXUS_ROOT")
    if env_root:
        candidates.append(Path(env_root))

    anchor = Path(start or Path.cwd()).resolve()
    candidates.extend([anchor, *anchor.parents])
    candidates.append(Path(__file__).resolve().parents[2])

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "config" / "app.yaml").exists():
            return candidate
    return anchor


@dataclass(frozen=True)
class NexusSettings:
    """Loaded application settings."""

    root_dir: Path
    config_path: Path
    raw: dict[str, Any]

    @property
    def server_host(self) -> str:
        return str(os.getenv("NEXUS_HOST") or _deep_get(self.raw, "server.host", "127.0.0.1"))

    @property
    def server_port(self) -> int:
        value = os.getenv("NEXUS_PORT") or _deep_get(self.raw, "server.port", 8000)
        return int(value)

    @property
    def scheduler_config_path(self) -> Path:
        path = os.getenv("NEXUS_SCHEDULER_CONFIG_PATH") or _deep_get(
            self.raw, "scheduler.config_path", "./config/scheduler.yaml"
        )
        return self.resolve_path(path, "./config/scheduler.yaml")

    @property
    def vault_base_path(self) -> Path:
        path = os.getenv("NEXUS_VAULT_ROOT") or _deep_get(self.raw, "vault.base_path", "./vault")
        return self.resolve_path(path, "./vault")

    @property
    def sqlite_dir(self) -> Path:
        path = _deep_get(self.raw, "storage.sqlite_dir", "./data/sqlite")
        return self.resolve_path(path, "./data/sqlite")

    @property
    def skills_dir(self) -> Path:
        path = _deep_get(self.raw, "storage.skills_dir", "./skills")
        return self.resolve_path(path, "./skills")

    @property
    def skill_registry_dir(self) -> Path:
        path = _deep_get(self.raw, "storage.skill_registry_dir", "./skill_registry")
        return self.resolve_path(path, "./skill_registry")

    @property
    def capabilities_dir(self) -> Path:
        path = _deep_get(self.raw, "storage.capabilities_dir", "./capabilities")
        return self.resolve_path(path, "./capabilities")

    @property
    def staging_dir(self) -> Path:
        path = _deep_get(self.raw, "storage.staging_dir", "./data/staging")
        return self.resolve_path(path, "./data/staging")

    @property
    def backups_dir(self) -> Path:
        path = _deep_get(self.raw, "storage.backups_dir", "./data/backups")
        return self.resolve_path(path, "./data/backups")

    @property
    def browser_enabled(self) -> bool:
        return bool(_deep_get(self.raw, "browser.enabled", False))

    @property
    def browser_worker_command(self) -> list[str]:
        command = _deep_get(self.raw, "browser.worker_command", []) or []
        return [str(part) for part in command]

    @property
    def tool_policy_enabled(self) -> bool:
        return bool(_deep_get(self.raw, "tool_policy.enabled", True))

    @property
    def tool_allowlist(self) -> set[str] | None:
        if not self.tool_policy_enabled:
            return None
        allowlist = _deep_get(self.raw, "tool_policy.allowlist", [])
        if not allowlist:
            return None
        return {str(item) for item in allowlist}

    @property
    def disable_risk_controls_for_testing(self) -> bool:
        return _coerce_bool(
            os.getenv("NEXUS_DISABLE_RISK_CONTROLS"),
            _coerce_bool(_deep_get(self.raw, "tool_policy.testing_disable_risk_controls", False), False),
        )

    @property
    def evolution_python_executable(self) -> str:
        candidate = os.getenv("NEXUS_EVOLUTION_PYTHON") or _deep_get(
            self.raw, "evolution.sandbox.python_path", ""
        )
        if candidate:
            path = self.resolve_path(candidate)
            if path.exists():
                return str(path)
        return sys.executable

    def resolve_path(self, value: str | Path | None, default: str | Path | None = None) -> Path:
        candidate = Path(value or default or ".")
        if not candidate.is_absolute():
            candidate = (self.root_dir / candidate).resolve()
        return candidate.resolve()

    def provider_configs(self):
        from nexus.provider import ProviderConfig

        primary_raw = dict(_deep_get(self.raw, "provider.primary", {}) or {})
        primary = ProviderConfig(
            name=str(primary_raw.get("name", "kimi")),
            model=str(primary_raw.get("model") or os.getenv("NEXUS_PRIMARY_MODEL") or "kimi-k2.5"),
            provider=str(primary_raw.get("provider_type") or primary_raw.get("provider") or primary_raw.get("name") or "kimi"),
            base_url=str(primary_raw.get("base_url") or os.getenv("NEXUS_PRIMARY_BASE_URL") or ""),
            api_key=str(primary_raw.get("api_key") or ""),
            api_key_env=str(primary_raw.get("api_key_env") or "MOONSHOT_API_KEY"),
            timeout_seconds=float(_deep_get(self.raw, "provider.request_timeout_seconds", 60)),
            healthcheck_timeout_seconds=float(_deep_get(self.raw, "provider.healthcheck_timeout_seconds", 15)),
            max_retries=int(_deep_get(self.raw, "provider.max_retries", 2)),
            retry_backoff_seconds=float(_deep_get(self.raw, "provider.retry_backoff_seconds", 1.5)),
        )

        fallbacks: list[ProviderConfig] = []
        fallback_model_override = os.getenv("NEXUS_FALLBACK_MODEL")
        for idx, item in enumerate(_deep_get(self.raw, "provider.fallbacks", []) or []):
            name = str(item.get("name") or f"fallback-{idx}")
            model = str(item.get("model") or fallback_model_override or "")
            if not model:
                continue
            fallbacks.append(
                ProviderConfig(
                    name=name,
                    model=model,
                    provider=str(item.get("provider_type") or item.get("provider") or name),
                    base_url=str(item.get("base_url") or ""),
                    api_key=str(item.get("api_key") or ""),
                    api_key_env=str(item.get("api_key_env") or ""),
                    timeout_seconds=float(_deep_get(self.raw, "provider.request_timeout_seconds", 60)),
                    healthcheck_timeout_seconds=float(_deep_get(self.raw, "provider.healthcheck_timeout_seconds", 15)),
                    max_retries=int(_deep_get(self.raw, "provider.max_retries", 2)),
                    retry_backoff_seconds=float(_deep_get(self.raw, "provider.retry_backoff_seconds", 1.5)),
                )
            )

        return primary, fallbacks

    def audio_config(self) -> dict[str, Any]:
        raw = dict(_deep_get(self.raw, "audio", {}) or {})
        if os.getenv("NEXUS_AUDIO_BACKEND"):
            raw["backend"] = os.getenv("NEXUS_AUDIO_BACKEND")
        if os.getenv("NEXUS_AUDIO_BASE_URL"):
            raw["base_url"] = os.getenv("NEXUS_AUDIO_BASE_URL")
        if os.getenv("NEXUS_AUDIO_LANGUAGE"):
            raw["language"] = os.getenv("NEXUS_AUDIO_LANGUAGE")
        if os.getenv("NEXUS_AUDIO_MODEL_DIR"):
            raw["sensevoice_model_dir"] = os.getenv("NEXUS_AUDIO_MODEL_DIR")
        if os.getenv("NEXUS_AUDIO_DEVICE"):
            raw["sensevoice_device"] = os.getenv("NEXUS_AUDIO_DEVICE")
        return raw

    def feishu_config(self) -> dict[str, Any]:
        raw = dict(_deep_get(self.raw, "feishu", {}) or {})
        return {
            "enabled": bool(raw.get("enabled", False)),
            "app_id": os.getenv("FEISHU_APP_ID", str(raw.get("app_id", "") or "")),
            "app_secret": os.getenv("FEISHU_APP_SECRET", str(raw.get("app_secret", "") or "")),
            "verification_token": os.getenv("FEISHU_VERIFICATION_TOKEN", str(raw.get("verification_token", "") or "")),
            "encrypt_key": os.getenv("FEISHU_ENCRYPT_KEY", str(raw.get("encrypt_key", "") or "")),
            "base_url": os.getenv("FEISHU_BASE_URL", raw.get("base_url", "https://open.feishu.cn/open-apis")),
            "subscription_mode": raw.get("subscription_mode", "webhook"),
            "verify_signature": bool(raw.get("verify_signature", True)),
            "require_mention_in_group": bool(raw.get("require_mention_in_group", False)),
            "bot_open_id": str(raw.get("bot_open_id", "") or ""),
            "receive_id_type": raw.get("receive_id_type", "chat_id"),
            "long_connection_auto_restart": bool(raw.get("long_connection_auto_restart", True)),
            "long_connection_restart_initial_seconds": float(
                raw.get("long_connection_restart_initial_seconds", 2.0) or 2.0
            ),
            "long_connection_restart_max_seconds": float(
                raw.get("long_connection_restart_max_seconds", 60.0) or 60.0
            ),
            "long_connection_max_restarts": int(raw.get("long_connection_max_restarts", 0) or 0),
            "long_connection_ack_timeout_seconds": float(
                raw.get("long_connection_ack_timeout_seconds", 2.5) or 2.5
            ),
            "long_connection_log_level": str(raw.get("long_connection_log_level", "INFO") or "INFO"),
        }

    def mesh_config(self) -> dict[str, Any]:
        raw = dict(_deep_get(self.raw, "mesh", {}) or {})
        mqtt = dict(raw.get("mqtt", {}) or {})
        return {
            "enabled": _coerce_bool(os.getenv("NEXUS_MESH_ENABLED"), _coerce_bool(raw.get("enabled"), False)),
            "node_id": str(os.getenv("NEXUS_MESH_NODE_ID") or raw.get("node_id") or ""),
            "node_type": str(raw.get("node_type") or "edge"),
            "node_card_path": str(raw.get("node_card_path") or ""),
            "broker_host": str(os.getenv("NEXUS_MESH_BROKER_HOST") or mqtt.get("broker_host") or "127.0.0.1"),
            "broker_port": int(os.getenv("NEXUS_MESH_BROKER_PORT") or mqtt.get("broker_port") or 1883),
            "transport": str(os.getenv("NEXUS_MESH_TRANSPORT") or mqtt.get("transport") or "tcp"),
            "websocket_path": str(
                os.getenv("NEXUS_MESH_WEBSOCKET_PATH") or mqtt.get("websocket_path") or "/mqtt"
            ),
            "username": str(os.getenv("NEXUS_MESH_USERNAME") or mqtt.get("username") or "") or None,
            "password": str(os.getenv("NEXUS_MESH_PASSWORD") or mqtt.get("password") or "") or None,
            "keepalive_seconds": int(
                os.getenv("NEXUS_MESH_KEEPALIVE_SECONDS") or mqtt.get("keepalive_seconds") or 60
            ),
            "qos": int(os.getenv("NEXUS_MESH_QOS") or mqtt.get("qos") or 1),
            "tls_enabled": _coerce_bool(
                os.getenv("NEXUS_MESH_TLS_ENABLED"),
                _coerce_bool(mqtt.get("tls_enabled"), False),
            ),
            "tls_ca_path": str(mqtt.get("tls_ca_path") or "") or None,
            "tls_cert_path": str(mqtt.get("tls_cert_path") or "") or None,
            "tls_key_path": str(mqtt.get("tls_key_path") or "") or None,
            "tls_insecure": _coerce_bool(
                os.getenv("NEXUS_MESH_TLS_INSECURE"),
                _coerce_bool(mqtt.get("tls_insecure"), False),
            ),
        }


def load_nexus_settings(
    root_dir: Path | None = None,
    *,
    config_path: Path | None = None,
) -> NexusSettings:
    explicit_root = Path(root_dir).resolve() if root_dir is not None else None
    root = explicit_root or find_project_root()
    load_dotenv(root / ".env", override=False)
    config_candidate = Path(
        os.getenv("NEXUS_CONFIG_PATH")
        or config_path
        or (root / "config" / "app.yaml")
    )
    if not config_candidate.is_absolute():
        config_candidate = (root / config_candidate).resolve()
    raw: dict[str, Any] = {}
    if config_candidate.exists():
        raw = yaml.safe_load(config_candidate.read_text(encoding="utf-8")) or {}
    return NexusSettings(root_dir=root, config_path=config_candidate, raw=raw)


def save_nexus_config(config_path: Path, raw: dict[str, Any]) -> None:
    config_target = Path(config_path)
    config_target.parent.mkdir(parents=True, exist_ok=True)
    config_target.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def update_nexus_config(config_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    config_target = Path(config_path)
    raw: dict[str, Any] = {}
    if config_target.exists():
        raw = yaml.safe_load(config_target.read_text(encoding="utf-8")) or {}
    for key, value in updates.items():
        _deep_set(raw, key, value)
    save_nexus_config(config_target, raw)
    return raw
