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
