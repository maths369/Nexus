from __future__ import annotations

from pathlib import Path

from nexus.agent.transcript_store import TranscriptStore


def test_transcript_store_appends_and_loads_latest_snapshot(tmp_path):
    store = TranscriptStore(tmp_path / "transcripts")
    store.append_snapshot(
        "session-1",
        [{"role": "user", "content": "第一次"}],
        trigger="auto_compact",
    )
    path = store.append_snapshot(
        "session-1",
        [
            {"role": "user", "content": "第二次"},
            {"role": "assistant", "content": "好的"},
        ],
        trigger="manual_compact",
    )

    assert Path(path).exists()
    latest = store.load_latest_snapshot("session-1")
    assert latest == [
        {"role": "user", "content": "第二次"},
        {"role": "assistant", "content": "好的"},
    ]

