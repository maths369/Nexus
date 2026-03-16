from __future__ import annotations

import asyncio

from nexus.api.agent_smoke import run_agent_capability_smoke
from nexus.api.runtime import build_runtime
from nexus.provider import ProviderConfig


def test_agent_capability_smoke_passes(tmp_path):
    runtime = build_runtime(
        tmp_path,
        primary_provider=ProviderConfig(name="qwen", model="qwen-max"),
    )

    checks = asyncio.run(run_agent_capability_smoke(runtime))

    assert checks
    assert all(check.ok for check in checks), [(check.name, check.detail) for check in checks]

