from __future__ import annotations

from pathlib import Path

import httpx

from nexus.services.audio import RemoteAudioWorkerClient


def test_remote_audio_worker_client_parses_transcription_payload(monkeypatch):
    payload = {
        "text": "今天确认先做接入层。",
        "language": "zh",
        "duration": 12.5,
        "segments": [
            {"start": 0.0, "end": 12.5, "text": "今天确认先做接入层。", "confidence": 0.98}
        ],
    }

    def fake_post(self, url, json):  # noqa: ANN001
        request = httpx.Request("POST", url, json=json)
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    client = RemoteAudioWorkerClient("http://audio-worker:8010")
    result = client.transcribe(Path("/tmp/demo.wav"), language="zh")

    assert result.text == "今天确认先做接入层。"
    assert result.language == "zh"
    assert result.duration == 12.5
    assert result.segments[0].text == "今天确认先做接入层。"
