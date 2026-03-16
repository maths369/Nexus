from __future__ import annotations

import pytest

from nexus.knowledge.memory import EpisodicMemory
from nexus.knowledge.memory_manager import MemoryManager
from nexus.knowledge.retrieval import RetrievalIndex


class _StubProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "message": {
                "content": '```json\n[{"summary":"用户偏好 Markdown","kind":"preference","importance":4,"tags":["writing"]}]\n```'
            }
        }


@pytest.mark.asyncio
async def test_flush_before_compact_uses_provider_default_model(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    provider = _StubProvider()
    manager = MemoryManager(
        memory=EpisodicMemory(vault / "_system" / "memory" / "episodic.jsonl"),
        retrieval=RetrievalIndex(tmp_path / "retrieval.db"),
        vault_path=vault,
        provider=provider,
    )

    result = await manager.flush_before_compact(
        [{"role": "user", "content": "我偏好 Markdown 格式。"}]
    )

    assert result["saved"] == 1
    assert provider.calls, "expected provider to be used"
    assert "model" not in provider.calls[0]
