from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

from nexus.agent import tool_registry as tool_registry_module
from nexus.agent.tool_registry import build_tool_registry


class _FakeBrowserService:
    def __init__(self) -> None:
        self.enabled = True
        self.last_url: str | None = None

    async def navigate(self, url: str):
        self.last_url = url
        return {"url": url, "title": "Search Results"}

    async def extract_text(self, selector: str | None = None):
        return {"text": "Result A\nResult B\nResult C"}

    async def screenshot(self, path: str | None = None):
        return {"path": path or "/tmp/fake.png"}

    async def fill_form(self, fields):
        return {"ok": True, "fields": fields}


def _build_registry(*, search_config: dict):
    return build_tool_registry(
        content_store=MagicMock(),
        document_service=MagicMock(),
        document_editor=MagicMock(),
        memory=MagicMock(),
        ingest_service=MagicMock(),
        audio_service=MagicMock(),
        browser_service=_FakeBrowserService(),
        spreadsheet_service=MagicMock(),
        workspace_service=MagicMock(),
        search_config=search_config,
        allowlist=None,
    )


def test_search_web_falls_back_to_bing_without_google_key():
    registry = _build_registry(
        search_config={
            "provider": {"primary": "google_grounded", "fallback": "bing"},
            "google_grounded": {"enabled": True, "api_key": "", "model": "gemini-2.5-flash"},
        }
    )
    tool_map = {tool.name: tool for tool in registry}

    payload = json.loads(asyncio.run(tool_map["search_web"].handler(query="nexus")))

    assert payload["provider"] == "bing"
    assert payload["fallback_used"] is True
    assert payload["results"][0]["url"].startswith("https://www.bing.com/search")


def test_search_web_structured_uses_google_grounded_when_key_is_configured(monkeypatch):
    async def _fake_google_grounded_search(**kwargs):
        assert "Question: nexus" in kwargs["query"]
        return {
            "candidates": [
                {
                    "content": {"parts": [{"text": "Nexus 是一个 AI 工作中枢。"}]},
                    "groundingMetadata": {
                        "webSearchQueries": ["nexus ai assistant"],
                        "groundingChunks": [
                            {"web": {"title": "Nexus Docs", "uri": "https://example.com/docs"}},
                            {"web": {"title": "Nexus Repo", "uri": "https://example.com/repo"}},
                        ],
                    },
                }
            ]
        }

    monkeypatch.setattr(tool_registry_module, "_perform_google_grounded_search", _fake_google_grounded_search)

    registry = _build_registry(
        search_config={
            "provider": {"primary": "google_grounded", "fallback": "bing"},
            "google_grounded": {
                "enabled": True,
                "api_key": "gemini-key",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": "gemini-2.5-flash",
                "timeout_seconds": 20,
                "max_output_tokens": 768,
            },
        }
    )
    tool_map = {tool.name: tool for tool in registry}

    payload = json.loads(asyncio.run(tool_map["search_web_structured"].handler(query="nexus")))

    assert payload["provider"] == "google_grounded"
    assert payload["fallback_used"] is False
    assert payload["answer"] == "Nexus 是一个 AI 工作中枢。"
    assert payload["results"][0]["domain"] == "example.com"
    assert payload["search_queries"] == ["nexus ai assistant"]


def test_search_web_structured_falls_back_when_google_quota_is_exhausted(monkeypatch):
    async def _fake_google_grounded_search(**kwargs):
        raise RuntimeError("Google grounded search error 429 [RESOURCE_EXHAUSTED]: quota exceeded")

    monkeypatch.setattr(tool_registry_module, "_perform_google_grounded_search", _fake_google_grounded_search)

    registry = _build_registry(
        search_config={
            "provider": {"primary": "google_grounded", "fallback": "bing"},
            "google_grounded": {
                "enabled": True,
                "api_key": "gemini-key",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "model": "gemini-2.5-flash",
            },
        }
    )
    tool_map = {tool.name: tool for tool in registry}

    payload = json.loads(asyncio.run(tool_map["search_web_structured"].handler(query="nexus")))

    assert payload["provider"] == "bing"
    assert payload["fallback_used"] is True
    assert payload["fallback_reason"] == "google_quota_exhausted"


def test_search_tools_pick_up_runtime_search_provider_switch():
    search_config = {
        "provider": {
            "primary": "google_grounded",
            "fallback": "bing",
            "fallbacks": ["bing", "duckduckgo"],
        },
        "google_grounded": {
            "enabled": False,
            "api_key": "",
            "model": "gemini-2.5-flash",
        },
    }
    registry = _build_registry(search_config=search_config)
    tool_map = {tool.name: tool for tool in registry}

    first_payload = json.loads(asyncio.run(tool_map["search_web_structured"].handler(query="nexus")))
    assert first_payload["provider"] == "bing"

    search_config["provider"]["primary"] = "duckduckgo"
    second_payload = json.loads(asyncio.run(tool_map["search_web_structured"].handler(query="nexus")))

    assert second_payload["provider"] == "duckduckgo"
    assert second_payload["results"][0]["url"].startswith("https://duckduckgo.com/")
