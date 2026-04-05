from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from nexus.agent.tool_registry import build_tool_registry


class _FakeSkillManager:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, object]] = []
        self.import_calls: list[dict[str, object]] = []

    async def search_clawhub_skills(
        self,
        query: str,
        *,
        limit: int = 10,
        base_url: str | None = None,
    ) -> list[dict[str, object]]:
        self.search_calls.append({"query": query, "limit": limit, "base_url": base_url})
        return [
            {
                "skill_id": "obsidian-notes",
                "slug": "obsidian-notes",
                "name": "Obsidian Notes",
                "source_type": "clawhub",
            }
        ]

    async def import_from_clawhub(
        self,
        slug: str,
        *,
        version: str | None = None,
        actor: str = "system",
        install: bool = False,
        skill_id: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, object]:
        self.import_calls.append(
            {
                "slug": slug,
                "version": version,
                "actor": actor,
                "install": install,
                "skill_id": skill_id,
                "base_url": base_url,
            }
        )
        return {
            "success": True,
            "skill_id": skill_id or slug,
            "installed": install,
            "source_type": "clawhub",
        }


def _build_registry(skill_manager: _FakeSkillManager):
    return build_tool_registry(
        content_store=MagicMock(),
        document_service=MagicMock(),
        document_editor=MagicMock(),
        memory=MagicMock(),
        ingest_service=MagicMock(),
        audio_service=MagicMock(),
        browser_service=MagicMock(),
        spreadsheet_service=MagicMock(),
        workspace_service=MagicMock(),
        skill_manager=skill_manager,
        allowlist=None,
    )


def test_skill_search_clawhub_tool_calls_manager():
    skill_manager = _FakeSkillManager()
    registry = _build_registry(skill_manager)
    tool_map = {tool.name: tool for tool in registry}

    payload = json.loads(
        asyncio.run(
            tool_map["skill_search_clawhub"].handler(
                query="obsidian",
                limit=5,
                base_url="https://clawhub.example.com",
            )
        )
    )

    assert payload[0]["slug"] == "obsidian-notes"
    assert skill_manager.search_calls == [
        {
            "query": "obsidian",
            "limit": 5,
            "base_url": "https://clawhub.example.com",
        }
    ]


def test_skill_import_clawhub_tool_calls_manager():
    skill_manager = _FakeSkillManager()
    registry = _build_registry(skill_manager)
    tool_map = {tool.name: tool for tool in registry}

    payload = json.loads(
        asyncio.run(
            tool_map["skill_import_clawhub"].handler(
                slug="obsidian-notes",
                version="2.0.1",
                install=True,
                skill_id="obsidian-notes",
                base_url="https://clawhub.example.com",
            )
        )
    )

    assert payload["success"] is True
    assert skill_manager.import_calls == [
        {
            "slug": "obsidian-notes",
            "version": "2.0.1",
            "actor": "agent",
            "install": True,
            "skill_id": "obsidian-notes",
            "base_url": "https://clawhub.example.com",
        }
    ]
