from __future__ import annotations

from nexus.api import build_runtime
from nexus.shared import find_project_root, load_nexus_settings


def test_builtin_audio_skills_are_visible_in_runtime_catalog():
    settings = load_nexus_settings(find_project_root())
    runtime = build_runtime(settings=settings)

    skill_ids = {skill["skill_id"] for skill in runtime.skill_manager.list_skills()}
    assert "meeting-transcription" in skill_ids
    assert "voice-note-processing" in skill_ids

    descriptions = runtime.skill_manager.get_skill_descriptions()
    assert "meeting-transcription" in descriptions
    assert "voice-note-processing" in descriptions

    content = runtime.skill_manager.get_skill_content("meeting-transcription")
    assert "audio_transcribe_path" in content
    assert "audio_materialize_transcript" in content

