"""User-space aMQTT broker entrypoint for Nexus mesh deployments."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def _load_amqtt_broker() -> type[Any]:
    try:
        from amqtt.broker import Broker
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "aMQTT is not installed. Install it in the active ai_assist environment: pip install amqtt"
        ) from exc
    return Broker


def _load_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Broker config must be a mapping: {path}")
    return raw


async def _serve(config_path: Path) -> None:
    Broker = _load_amqtt_broker()
    config = _load_config(config_path)
    broker = Broker(config)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    await broker.start()
    logger.info("aMQTT broker started with config=%s", config_path)

    try:
        await stop_event.wait()
    finally:
        logger.info("aMQTT broker shutting down")
        await broker.shutdown()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Nexus aMQTT broker.")
    parser.add_argument("--config", type=Path, required=True, help="Path to broker YAML config.")
    parser.add_argument("--log-level", default="INFO", help="Logging level.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_serve(args.config))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
