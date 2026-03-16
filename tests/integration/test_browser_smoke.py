from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from nexus.services.browser import BrowserService, BrowserWorkerConfig


@pytest.mark.integration
def test_real_browser_worker_can_navigate_and_extract_text(tmp_path):
    pytest.importorskip("playwright.async_api")

    screenshot_path = tmp_path / "browser-smoke.png"
    service = BrowserService(
        BrowserWorkerConfig(
            enabled=True,
            command=[sys.executable, "-m", "nexus.services.browser.worker"],
            workdir=Path(__file__).resolve().parents[2],
            request_timeout_seconds=60.0,
        )
    )

    async def scenario():
        try:
            await service.health()
            await service.navigate(
                "data:text/html,<html><head><title>Nexus Smoke</title></head>"
                "<body><h1>Nexus Browser Smoke</h1><p>Playwright worker online.</p></body></html>"
            )
            extracted = await service.extract_text()
            screenshot = await service.screenshot(screenshot_path)
            return extracted, screenshot
        finally:
            await service.aclose()

    try:
        extracted, screenshot = asyncio.run(scenario())
    except RuntimeError as exc:
        pytest.skip(f"Playwright browser runtime unavailable: {exc}")

    assert "Nexus Browser Smoke" in extracted["text"]
    assert Path(screenshot["path"]).exists()
