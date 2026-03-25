from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient

from nexus.shared import NexusSettings


class _FakeBackgroundManager:
    async def aclose(self) -> None:
        return None


class _FakeBrowserService:
    async def aclose(self) -> None:
        return None


class _FakeRuntime:
    def __init__(self) -> None:
        self.session_router = object()
        self.session_store = object()
        self.context_window = object()
        self.run_manager = object()
        self.available_tools = []
        self.skill_manager = object()
        self.capability_manager = object()
        self.background_manager = _FakeBackgroundManager()
        self.browser_service = _FakeBrowserService()


class _FakeOrchestrator:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class _FakeWeixinAdapter:
    def __init__(self, config):
        self._config = config
        self.configured = True

    async def aclose(self) -> None:
        return None

    def list_account_ids(self) -> list[str]:
        return []

    def status_snapshot(self) -> dict:
        return {"enabled": True, "accounts": []}


def _settings(tmp_path: Path) -> NexusSettings:
    return NexusSettings(
        root_dir=tmp_path,
        config_path=tmp_path / "config" / "app.yaml",
        raw={
            "weixin": {
                "enabled": True,
                "state_dir": str(tmp_path / "data" / "weixin"),
            }
        },
    )


def test_app_lifespan_starts_and_stops_weixin_long_poll(monkeypatch, tmp_path):
    app_module = importlib.import_module("nexus.api.app")
    starts: list[tuple] = []
    stops: list[str] = []

    class _FakeRunner:
        def __init__(self, adapter, **kwargs) -> None:
            self.adapter = adapter
            self.kwargs = kwargs

        def start(self, *, loop) -> None:
            starts.append((self.adapter, self.kwargs, loop))

        def shutdown(self) -> None:
            stops.append("shutdown")

    monkeypatch.setattr(app_module, "load_nexus_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(app_module, "build_runtime", lambda settings=None: _FakeRuntime())
    monkeypatch.setattr(app_module, "Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(app_module, "WeixinAdapter", _FakeWeixinAdapter)
    monkeypatch.setattr(app_module, "WeixinLongPollRunner", _FakeRunner)

    with TestClient(app_module.app):
        assert starts, "runner should be started during lifespan"

    assert stops == ["shutdown"]
