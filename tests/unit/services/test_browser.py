from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from nexus.services.browser import (
    BrowserService,
    BrowserServiceUnavailableError,
    BrowserWorkerConfig,
)


def _write_fake_worker(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, sys",
                "for raw in sys.stdin:",
                "    payload = json.loads(raw)",
                "    op = payload.get('op')",
                "    if op == 'health':",
                "        result = {'ready': True, 'playwright_available': False}",
                "    elif op == 'navigate':",
                "        result = {'url': payload['params']['url'], 'title': 'Fake Page'}",
                "    elif op == 'fill_form':",
                "        result = {'filled': sorted(payload['params']['fields'].keys())}",
                "    elif op == 'close':",
                "        result = {'closed': True}",
                "    else:",
                "        result = {'echo': payload.get('params', {})}",
                "    sys.stdout.write(json.dumps({'id': payload.get('id'), 'ok': True, 'result': result}) + '\\n')",
                "    sys.stdout.flush()",
            ]
        ),
        encoding='utf-8',
    )


def test_browser_service_rejects_when_disabled():
    service = BrowserService(BrowserWorkerConfig(enabled=False, command=[]))

    try:
        asyncio.run(service.health())
    except BrowserServiceUnavailableError:
        pass
    else:
        raise AssertionError("Expected BrowserServiceUnavailableError")


def test_browser_service_uses_subprocess_worker(tmp_path):
    worker = tmp_path / "fake_browser_worker.py"
    _write_fake_worker(worker)
    service = BrowserService(
        BrowserWorkerConfig(
            enabled=True,
            command=[sys.executable, str(worker)],
            workdir=tmp_path,
        )
    )

    async def scenario():
        health = await service.health()
        page = await service.navigate("https://example.com")
        filled = await service.fill_form({"#email": "foo@example.com"})
        await service.aclose()
        return health, page, filled

    health, page, filled = asyncio.run(scenario())

    assert health["ready"] is True
    assert page["url"] == "https://example.com"
    assert page["title"] == "Fake Page"
    assert filled["filled"] == ["#email"]
