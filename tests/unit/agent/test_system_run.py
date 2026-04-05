from __future__ import annotations

import asyncio

from nexus.agent.system_run import SystemRunner
from nexus.evolution import AuditLog


def test_system_runner_run_argv_executes_without_shell(tmp_path):
    audit = AuditLog(tmp_path / "audit.db")
    runner = SystemRunner(
        allowed_workdirs=[tmp_path],
        audit=audit,
        default_timeout=5,
    )

    result = asyncio.run(
        runner.run_argv(
            ["python3", "-c", "print('ok')"],
            workdir=str(tmp_path),
            actor="agent",
        )
    )

    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert "ok" in result["stdout"]


def test_system_runner_blocks_rm_rf_root_in_argv_mode(tmp_path):
    audit = AuditLog(tmp_path / "audit.db")
    runner = SystemRunner(
        allowed_workdirs=[tmp_path],
        audit=audit,
        default_timeout=5,
    )

    result = asyncio.run(
        runner.run_argv(
            ["rm", "-rf", "/"],
            workdir=str(tmp_path),
            actor="agent",
        )
    )

    assert result["exit_code"] == -1
    assert "[BLOCKED]" in result["stderr"]
