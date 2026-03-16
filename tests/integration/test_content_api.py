from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from nexus.api.runtime import build_runtime as build_real_runtime
from nexus.shared import NexusSettings


def _make_settings(tmp_path) -> NexusSettings:
    return NexusSettings(
        root_dir=tmp_path,
        config_path=tmp_path / "config" / "app.yaml",
        raw={
            "server": {"host": "127.0.0.1", "port": 8000},
            "storage": {
                "sqlite_dir": "./data/sqlite",
                "skills_dir": "./skills",
                "staging_dir": "./data/staging",
                "backups_dir": "./data/backups",
            },
            "provider": {
                "primary": {
                    "name": "kimi",
                    "model": "kimi-k2-0711-preview",
                    "provider_type": "moonshot",
                    "base_url": "https://api.moonshot.cn/v1",
                    "api_key_env": "MOONSHOT_API_KEY",
                }
            },
            "vault": {"base_path": "./vault"},
            "audio": {
                "backend": "sensevoice",
                "language": "zh",
                "sensevoice_model_dir": "./models/sensevoice/SenseVoiceSmall",
                "sensevoice_device": "cpu",
                "base_url": "http://127.0.0.1:8010",
            },
            "tool_policy": {"enabled": True, "allowlist": []},
            "scheduler": {"enabled": False, "config_path": "./config/scheduler.yaml"},
            "browser": {"enabled": False, "worker_command": []},
            "feishu": {"enabled": False},
        },
    )


def _make_client(monkeypatch, tmp_path):
    settings = _make_settings(tmp_path)
    runtime = build_real_runtime(settings=settings)
    app_module = importlib.import_module("nexus.api.app")
    monkeypatch.setattr(app_module, "load_nexus_settings", lambda: settings)
    monkeypatch.setattr(app_module, "build_runtime", lambda settings=None: runtime)
    return TestClient(app_module.app)


def test_document_endpoints_support_create_get_update_and_append(monkeypatch, tmp_path):
    with _make_client(monkeypatch, tmp_path) as client:
        created = client.post(
            "/documents/page",
            json={
                "title": "飞书 API 传输方案",
                "section": "strategy",
                "body": "# 飞书 API 传输方案\n\n初版思路",
            },
        )
        assert created.status_code == 200
        relative_path = created.json()["page"]["relative_path"]

        fetched = client.get("/documents/page", params={"path": relative_path})
        assert fetched.status_code == 200
        assert "初版思路" in fetched.json()["page"]["content"]

        updated = client.post(
            "/documents/page/update",
            json={
                "relative_path": relative_path,
                "content": "# 飞书 API 传输方案\n\n更新为正式版本",
            },
        )
        assert updated.status_code == 200

        appended = client.post(
            "/documents/edit/append",
            json={
                "relative_path": relative_path,
                "block_markdown": "## 接口约束\n\n- 保持回调 3 秒内返回",
            },
        )
        assert appended.status_code == 200

        final_page = client.get("/documents/page", params={"path": relative_path})
        assert "更新为正式版本" in final_page.json()["page"]["content"]
        assert "保持回调 3 秒内返回" in final_page.json()["page"]["content"]


def test_audio_materialize_endpoint_creates_page_and_transcript(monkeypatch, tmp_path):
    with _make_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/audio/materialize",
            json={
                "source_name": "strategy-sync.m4a",
                "transcript": "今天确认先做接入层，再做协议层。",
                "summary": "先定义接入层边界。",
                "action_items": ["补 Feishu 事件模型"],
                "target_section": "meetings",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["transcript_path"].endswith(".md")
        assert payload["page"]["page_type"] == "audio_note"


def test_document_delete_endpoint_removes_page(monkeypatch, tmp_path):
    with _make_client(monkeypatch, tmp_path) as client:
        created = client.post(
            "/documents/page",
            json={
                "title": "日志 2026-03-11",
                "section": "pages",
                "body": "# 日志 2026-03-11\n\n待删除",
            },
        )
        assert created.status_code == 200
        relative_path = created.json()["page"]["relative_path"]

        deleted = client.request("DELETE", "/documents/page", params={"path": relative_path})
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        missing = client.get("/documents/page", params={"path": relative_path})
        assert missing.status_code == 404
