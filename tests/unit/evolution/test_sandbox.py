from __future__ import annotations

import asyncio

from nexus.evolution.sandbox import Sandbox


def test_sandbox_loadable_check_fails_on_import_error(tmp_path):
    skill_dir = tmp_path / 'skills' / 'broken-skill'
    skill_dir.mkdir(parents=True)
    (skill_dir / 'skill.json').write_text('{}', encoding='utf-8')
    (skill_dir / 'main.py').write_text('import definitely_missing_dependency\n', encoding='utf-8')

    sandbox = Sandbox(tmp_path / 'staging')
    result = asyncio.run(sandbox.verify_skill(skill_dir))

    loadable = [check for check in result.checks if check.name == 'loadable'][0]
    assert loadable.passed is False


def test_sandbox_verify_config_change_rejects_schema_type_mismatch(tmp_path):
    sandbox = Sandbox(tmp_path / 'staging')

    result = asyncio.run(
        sandbox.verify_config_change(
            "agent.max_steps",
            "eight",
            schema={"type": "integer", "minimum": 1, "maximum": 20},
        )
    )

    assert result.passed is False
    assert "expected type integer" in result.summary


def test_sandbox_verify_config_change_blocks_forbidden_path(tmp_path):
    sandbox = Sandbox(tmp_path / 'staging')

    result = asyncio.run(sandbox.verify_config_change("vault.base_path", "/etc/nexus"))

    assert result.passed is False
    assert "forbidden location" in result.summary
