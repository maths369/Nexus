"""Configuration loading helpers for Nexus."""

from __future__ import annotations

import fnmatch
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

    def search_config(self) -> dict[str, Any]:
        raw = dict(_deep_get(self.raw, "search", {}) or {})
        provider_raw = dict(raw.get("provider") or {})
        google_raw = dict(raw.get("google_grounded") or raw.get("google") or {})
        google_api_key_env = str(google_raw.get("api_key_env") or "GEMINI_API_KEY")
        google_api_key = str(os.getenv(google_api_key_env) or google_raw.get("api_key") or "")
        return {
            "provider": {
                "primary": str(provider_raw.get("primary") or "google_grounded"),
                "fallback": str(provider_raw.get("fallback") or "bing"),
                "fallbacks": [
                    str(item).strip().lower()
                    for item in (
                        provider_raw.get("fallbacks")
                        or [provider_raw.get("fallback") or "bing", "duckduckgo"]
                    )
                    if str(item).strip()
                ],
            },
            "google_grounded": {
                "enabled": _coerce_bool(google_raw.get("enabled", True), True),
                "base_url": str(
                    google_raw.get("base_url")
                    or "https://generativelanguage.googleapis.com/v1beta"
                ),
                "api_key_env": google_api_key_env,
                "api_key": google_api_key,
                "model": str(google_raw.get("model") or "gemini-2.5-flash"),
                "timeout_seconds": float(google_raw.get("timeout_seconds", 20) or 20),
                "max_output_tokens": int(google_raw.get("max_output_tokens", 768) or 768),
            },
        }

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

    def auth_config(self) -> dict[str, Any]:
        raw = dict(_deep_get(self.raw, "auth", {}) or {})
        exempt_paths = [
            str(item).strip()
            for item in (raw.get("exempt_paths") or ["/health", "/feishu/webhook", "/weixin/"])
            if str(item).strip()
        ]
        return {
            "enabled": _coerce_bool(os.getenv("NEXUS_AUTH_ENABLED"), _coerce_bool(raw.get("enabled"), False)),
            "bearer_token": str(os.getenv("NEXUS_BEARER_TOKEN") or raw.get("bearer_token") or "").strip(),
            "cookie_name": str(raw.get("cookie_name") or "__nexus_token").strip() or "__nexus_token",
            "exempt_paths": exempt_paths,
        }

    @property
    def external_base_url(self) -> str:
        return str(
            os.getenv("NEXUS_EXTERNAL_BASE_URL")
            or _deep_get(self.raw, "external_base_url", "")
            or ""
        ).rstrip("/")

    def agent_session_config(self) -> dict[str, Any]:
        raw = dict(_deep_get(self.raw, "agent.session", {}) or {})
        return {
            "idle_timeout_minutes": int(raw.get("idle_timeout_minutes", 30) or 30),
            "max_concurrent_sessions": int(raw.get("max_concurrent_sessions", 20) or 20),
            "sweep_interval_seconds": float(raw.get("sweep_interval_seconds", 60.0) or 60.0),
        }

    def model_policy(self, model_name: str) -> dict[str, Any] | None:
        policies = dict(_deep_get(self.raw, "model_policies", {}) or {})
        query = str(model_name or "").strip()
        if not query:
            return None
        if query in policies:
            return dict(policies[query] or {})
        for pattern, policy in policies.items():
            if fnmatch.fnmatch(query, str(pattern)):
                return dict(policy or {})
        return None

    def channel_policy(self, channel: str, group_id: str | None = None) -> dict[str, Any] | None:
        policies = dict(_deep_get(self.raw, "channel_policies", {}) or {})
        channel_name = str(channel or "").strip()
        if not channel_name:
            return None
        base = dict(policies.get(channel_name) or {})
        if not base:
            return None
        groups = dict(base.get("groups") or {})
        selected_group: dict[str, Any] = {}
        if group_id:
            selected_group = dict(groups.get(str(group_id)) or {})
        if not selected_group:
            selected_group = dict(groups.get("default") or {})
        if selected_group:
            merged = dict(base)
            merged.pop("groups", None)
            merged.update(selected_group)
            return merged
        base.pop("groups", None)
        return base

    def subagent_policy(self) -> dict[str, Any]:
        return dict(_deep_get(self.raw, "subagent_policy", {"deny": []}) or {"deny": []})

    def heartbeat_config(self) -> dict[str, Any]:
        raw = dict(_deep_get(self.raw, "agent.heartbeat", {}) or {})
        return {
            "enabled": _coerce_bool(raw.get("enabled"), False),
            "interval_minutes": int(raw.get("interval_minutes", 30) or 30),
            "active_hours": str(raw.get("active_hours") or "08:00-22:00"),
            "quiet_days": [str(item) for item in (raw.get("quiet_days") or []) if str(item).strip()],
            "ack_max_chars": int(raw.get("ack_max_chars", 300) or 300),
            "model": str(raw.get("model") or "").strip() or None,
        }

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

    def weixin_config(self) -> dict[str, Any]:
        raw = dict(_deep_get(self.raw, "weixin", {}) or {})
        state_dir = self.resolve_path(
            os.getenv("WEIXIN_STATE_DIR") or raw.get("state_dir") or "./data/weixin"
        )
        return {
            "enabled": _coerce_bool(raw.get("enabled"), False),
            "base_url": str(os.getenv("WEIXIN_BASE_URL") or raw.get("base_url") or "https://ilinkai.weixin.qq.com"),
            "bot_type": str(os.getenv("WEIXIN_BOT_TYPE") or raw.get("bot_type") or "3"),
            "state_dir": str(state_dir),
            "plugin_state_dir": str(
                self.resolve_path(
                    os.getenv("WEIXIN_PLUGIN_STATE_DIR") or raw.get("plugin_state_dir") or (state_dir / "plugin-host")
                )
            ),
            "plugin_host_base_url": str(
                os.getenv("WEIXIN_PLUGIN_HOST_BASE_URL")
                or raw.get("plugin_host_base_url")
                or "http://127.0.0.1:18101"
            ),
            "long_poll_timeout_ms": int(
                os.getenv("WEIXIN_LONG_POLL_TIMEOUT_MS") or raw.get("long_poll_timeout_ms") or 35000
            ),
            "retry_delay_seconds": float(
                os.getenv("WEIXIN_RETRY_DELAY_SECONDS") or raw.get("retry_delay_seconds") or 2.0
            ),
            "backoff_delay_seconds": float(
                os.getenv("WEIXIN_BACKOFF_DELAY_SECONDS") or raw.get("backoff_delay_seconds") or 30.0
            ),
            "max_consecutive_failures": int(
                os.getenv("WEIXIN_MAX_CONSECUTIVE_FAILURES") or raw.get("max_consecutive_failures") or 3
            ),
            "session_expired_pause_seconds": float(
                os.getenv("WEIXIN_SESSION_EXPIRED_PAUSE_SECONDS")
                or raw.get("session_expired_pause_seconds")
                or 600.0
            ),
            "login_timeout_ms": int(
                os.getenv("WEIXIN_LOGIN_TIMEOUT_MS") or raw.get("login_timeout_ms") or 480000
            ),
            "default_account_id": str(
                os.getenv("WEIXIN_DEFAULT_ACCOUNT_ID") or raw.get("default_account_id") or "default"
            ),
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


def _provider_identity_candidates(entry: dict[str, Any]) -> set[str]:
    values = {
        str(entry.get("name") or "").strip().lower(),
        str(entry.get("provider_type") or "").strip().lower(),
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
    }
    return {value for value in values if value}


def switch_primary_provider(config_path: Path, provider_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    config_target = Path(config_path)
    raw: dict[str, Any] = {}
    if config_target.exists():
        raw = yaml.safe_load(config_target.read_text(encoding="utf-8")) or {}

    provider_section = dict(raw.get("provider") or {})
    primary_raw = dict(provider_section.get("primary") or {})
    fallback_raws = [dict(item or {}) for item in (provider_section.get("fallbacks") or [])]
    query = str(provider_name or "").strip().lower()
    if not query:
        raise ValueError("provider_name is required")

    if query in _provider_identity_candidates(primary_raw):
        return raw, primary_raw

    selected_index = next(
        (
            idx
            for idx, item in enumerate(fallback_raws)
            if query in _provider_identity_candidates(item)
        ),
        None,
    )
    if selected_index is None:
        raise ValueError(f"Provider not configured: {provider_name}")

    selected = fallback_raws.pop(selected_index)
    new_fallbacks: list[dict[str, Any]] = []
    if primary_raw:
        new_fallbacks.append(primary_raw)
    new_fallbacks.extend(fallback_raws)

    provider_section["primary"] = selected
    provider_section["fallbacks"] = new_fallbacks
    raw["provider"] = provider_section
    save_nexus_config(config_target, raw)
    return raw, selected


def switch_search_provider(config_path: Path, provider_name: str) -> tuple[dict[str, Any], str]:
    config_target = Path(config_path)
    raw: dict[str, Any] = {}
    if config_target.exists():
        raw = yaml.safe_load(config_target.read_text(encoding="utf-8")) or {}

    query = str(provider_name or "").strip().lower()
    aliases = {
        "google": "google_grounded",
        "google_grounded": "google_grounded",
        "grounded": "google_grounded",
        "bing": "bing",
        "duckduckgo": "duckduckgo",
        "ddg": "duckduckgo",
    }
    selected = aliases.get(query)
    if not selected:
        raise ValueError(f"Search provider not supported: {provider_name}")

    search_section = dict(raw.get("search") or {})
    provider_section = dict(search_section.get("provider") or {})
    provider_section["primary"] = selected
    search_section["provider"] = provider_section
    raw["search"] = search_section
    save_nexus_config(config_target, raw)
    return raw, selected
