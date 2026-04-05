from __future__ import annotations

import sys
from pathlib import Path

import yaml

from nexus.shared import find_project_root, load_nexus_settings
from nexus.shared.config import switch_primary_provider, switch_search_provider


def test_repo_scheduler_config_exists_and_defines_core_jobs():
    root = find_project_root(Path(__file__).resolve())
    scheduler_path = root / "config" / "scheduler.yaml"

    assert scheduler_path.exists()
    text = scheduler_path.read_text(encoding="utf-8")
    assert "morning_brief:" in text
    assert "evening_wrapup:" in text
    assert "daily_reindex:" in text


def test_load_nexus_settings_resolves_scheduler_config_path(tmp_path):
    root = tmp_path
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "scheduler.yaml").write_text(
        "timezone: Asia/Shanghai\njobs: {}\n",
        encoding="utf-8",
    )
    (root / "config" / "app.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  host: 127.0.0.1",
                "  port: 8000",
                "vault:",
                "  base_path: ./vault",
                "scheduler:",
                "  enabled: false",
                "  config_path: ./config/scheduler.yaml",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_nexus_settings(root_dir=root)

    assert settings.scheduler_config_path == root / "config" / "scheduler.yaml"


def test_load_nexus_settings_defaults_evolution_python_to_current_interpreter(tmp_path):
    root = tmp_path
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "app.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  host: 127.0.0.1",
                "  port: 8000",
                "vault:",
                "  base_path: ./vault",
                "evolution:",
                "  sandbox:",
                "    enabled: true",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_nexus_settings(root_dir=root)

    assert settings.evolution_python_executable == sys.executable


def test_load_nexus_settings_ignores_invalid_evolution_python_path(tmp_path):
    root = tmp_path
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "app.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  host: 127.0.0.1",
                "  port: 8000",
                "vault:",
                "  base_path: ./vault",
                "evolution:",
                "  sandbox:",
                "    enabled: true",
                "    python_path: /definitely/missing/python",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_nexus_settings(root_dir=root)

    assert settings.evolution_python_executable == sys.executable


def test_load_nexus_settings_parses_weixin_config(tmp_path):
    root = tmp_path
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "app.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  host: 127.0.0.1",
                "  port: 8000",
                "vault:",
                "  base_path: ./vault",
                "weixin:",
                "  enabled: true",
                "  state_dir: ./data/weixin",
                "  long_poll_timeout_ms: 45000",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_nexus_settings(root_dir=root)
    weixin = settings.weixin_config()

    assert weixin["enabled"] is True
    assert weixin["state_dir"] == str(root / "data" / "weixin")
    assert weixin["plugin_state_dir"] == str(root / "data" / "weixin" / "plugin-host")
    assert weixin["plugin_host_base_url"] == "http://127.0.0.1:18101"
    assert weixin["long_poll_timeout_ms"] == 45000


def test_load_nexus_settings_parses_search_config(tmp_path, monkeypatch):
    root = tmp_path
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "app.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  host: 127.0.0.1",
                "  port: 8000",
                "vault:",
                "  base_path: ./vault",
                "search:",
                "  provider:",
                "    primary: google_grounded",
                "    fallback: duckduckgo",
                "    fallbacks:",
                "    - bing",
                "    - duckduckgo",
                "  google_grounded:",
                "    enabled: true",
                "    api_key_env: GEMINI_API_KEY",
                "    model: gemini-2.5-flash",
                "    timeout_seconds: 20",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")

    settings = load_nexus_settings(root_dir=root)
    search = settings.search_config()

    assert search["provider"]["primary"] == "google_grounded"
    assert search["provider"]["fallback"] == "duckduckgo"
    assert search["provider"]["fallbacks"] == ["bing", "duckduckgo"]
    assert search["google_grounded"]["api_key"] == "gemini-test-key"
    assert search["google_grounded"]["model"] == "gemini-2.5-flash"
    assert search["google_grounded"]["timeout_seconds"] == 20


def test_switch_primary_provider_reorders_provider_config(tmp_path):
    root = tmp_path
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "app.yaml"
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

    raw, selected = switch_primary_provider(config_path, "gemini")

    assert selected["name"] == "gemini"
    assert raw["provider"]["primary"]["name"] == "gemini"
    assert raw["provider"]["fallbacks"][0]["name"] == "qwen"
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["provider"]["primary"]["name"] == "gemini"


def test_switch_search_provider_updates_primary_search_engine(tmp_path):
    root = tmp_path
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "app.yaml"
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
            ]
        ),
        encoding="utf-8",
    )

    raw, selected = switch_search_provider(config_path, "ddg")

    assert selected == "duckduckgo"
    assert raw["search"]["provider"]["primary"] == "duckduckgo"
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["search"]["provider"]["primary"] == "duckduckgo"


def test_load_nexus_settings_matches_model_and_channel_policies(tmp_path):
    root = tmp_path
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "app.yaml").write_text(
        "\n".join(
            [
                "model_policies:",
                "  qwen3.5-plus:",
                "    max_risk_level: high",
                "  ollama-*:",
                "    max_tools_count: 12",
                "channel_policies:",
                "  feishu:",
                "    deny:",
                "    - system_run",
                "    groups:",
                "      default:",
                "        also_allow:",
                "        - dispatch_subagent",
                "subagent_policy:",
                "  deny:",
                "  - capability_promote",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_nexus_settings(root_dir=root)

    assert settings.model_policy("qwen3.5-plus") == {"max_risk_level": "high"}
    assert settings.model_policy("ollama-qwen") == {"max_tools_count": 12}
    assert settings.channel_policy("feishu") == {
        "deny": ["system_run"],
        "also_allow": ["dispatch_subagent"],
    }
    assert settings.channel_policy("feishu", "default") == {
        "deny": ["system_run"],
        "also_allow": ["dispatch_subagent"],
    }
    assert settings.subagent_policy() == {"deny": ["capability_promote"]}
