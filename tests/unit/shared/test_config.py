from __future__ import annotations

import sys
from pathlib import Path

from nexus.shared import find_project_root, load_nexus_settings


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
