"""Minimal Playwright worker exposed over JSON-lines IPC."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


class BrowserWorkerRuntime:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None

    async def handle(self, op: str, params: dict[str, Any]) -> dict[str, Any]:
        if op == "health":
            return {
                "ready": True,
                "playwright_available": self._playwright_importable(),
            }
        if op == "close":
            await self.close()
            return {"closed": True}

        await self._ensure_page()
        if self._page is None:
            raise RuntimeError("Playwright page is not initialized")

        if op == "navigate":
            url = str(params.get("url") or "").strip()
            if not url:
                raise ValueError("url is required")
            await self._page.goto(url, wait_until="networkidle")
            return {
                "url": self._page.url,
                "title": await self._page.title(),
            }
        if op == "screenshot":
            path = params.get("path")
            if path:
                target = Path(str(path)).expanduser()
                target.parent.mkdir(parents=True, exist_ok=True)
                await self._page.screenshot(path=str(target), full_page=True)
                return {"path": str(target.resolve()), "url": self._page.url}
            data = await self._page.screenshot(full_page=True)
            return {"bytes": len(data), "url": self._page.url}
        if op == "extract_text":
            selector = str(params.get("selector") or "").strip()
            if selector:
                content = await self._page.text_content(selector)
                return {"text": (content or "").strip(), "url": self._page.url}
            content = await self._page.locator("body").inner_text()
            return {"text": content.strip(), "url": self._page.url}
        if op == "fill_form":
            fields = params.get("fields") or {}
            if not isinstance(fields, dict) or not fields:
                raise ValueError("fields must be a non-empty object")
            submit_selector = fields.pop("__submit__", None)
            for selector, value in fields.items():
                await self._page.fill(str(selector), str(value))
            if submit_selector:
                await self._page.click(str(submit_selector))
            return {"filled": list(fields.keys()), "submitted": bool(submit_selector)}
        raise ValueError(f"Unsupported browser operation: {op}")

    async def _ensure_page(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Playwright is not installed in the current environment") from exc

        self._playwright = await async_playwright().start()
        headless = os.getenv("NEXUS_BROWSER_HEADLESS", "1") != "0"
        self._browser = await self._playwright.chromium.launch(headless=headless)
        context = await self._browser.new_context()
        self._page = await context.new_page()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None

    @staticmethod
    def _playwright_importable() -> bool:
        try:
            import playwright.async_api  # noqa: F401
            return True
        except Exception:
            return False


async def _main() -> int:
    runtime = BrowserWorkerRuntime()
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            op = str(payload.get("op") or "")
            params = payload.get("params") or {}
            result = await runtime.handle(op, params)
            response = {
                "id": payload.get("id"),
                "ok": True,
                "result": result,
            }
        except Exception as exc:  # noqa: BLE001
            response = {
                "id": payload.get("id") if "payload" in locals() else None,
                "ok": False,
                "error": str(exc),
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    await runtime.close()
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
