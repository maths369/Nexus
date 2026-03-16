from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nexus.provider import ProviderConfig, ProviderGateway


class _FakeChatCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        message = SimpleNamespace(
            content="provider ok",
            tool_calls=[
                SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(
                        name="read_vault",
                        arguments='{"path":"pages/demo.md"}',
                    ),
                )
            ],
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeRetryChatCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("connection reset by peer")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="recovered", tool_calls=[]))]
        )


class _FakeEmptyChatCompletions:
    async def create(self, **kwargs):
        message = SimpleNamespace(content=[], tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeEmptyThenTextChatCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=[], tool_calls=[]))]
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="retry ok", tool_calls=[]))]
        )


class _FakeClient:
    def __init__(self, completions=None):
        self.chat = SimpleNamespace(completions=completions or _FakeChatCompletions())


def test_gateway_normalizes_openai_compatible_response():
    gateway = ProviderGateway(
        primary=ProviderConfig(name="qwen", model="qwen-max"),
        client_factory=lambda cfg: _FakeClient(),
    )

    result = asyncio.run(
        gateway.chat_completion(
            messages=[{"role": "user", "content": "ping"}],
        )
    )

    assert result["provider"] == "qwen"
    assert result["message"]["content"] == "provider ok"
    assert result["message"]["tool_calls"][0]["function"]["name"] == "read_vault"
    assert result["message"]["tool_calls"][0]["function"]["arguments"]["path"] == "pages/demo.md"


def test_gateway_healthcheck_uses_selected_provider():
    gateway = ProviderGateway(
        primary=ProviderConfig(name="kimi", model="kimi-k2.5"),
        client_factory=lambda cfg: _FakeClient(),
    )

    status = asyncio.run(gateway.healthcheck())

    assert status["status"] == "ok"
    assert status["provider"] == "kimi"
    assert status["model"] == "kimi-k2.5"


def test_gateway_retries_transient_errors_and_recovers():
    completions = _FakeRetryChatCompletions()
    gateway = ProviderGateway(
        primary=ProviderConfig(name="ollama", model="qwen3", max_retries=2),
        client_factory=lambda cfg: _FakeClient(completions=completions),
    )

    result = asyncio.run(gateway.chat_completion(messages=[{"role": "user", "content": "ping"}]))

    assert completions.calls == 2
    assert result["message"]["content"] == "recovered"
    assert gateway.get_health_snapshot("ollama")["status"] == "ok"


def test_gateway_uses_empty_content_fallback_when_needed():
    gateway = ProviderGateway(
        primary=ProviderConfig(name="qwen", model="qwen-max"),
        client_factory=lambda cfg: _FakeClient(completions=_FakeEmptyChatCompletions()),
    )

    result = asyncio.run(gateway.chat_completion(messages=[{"role": "user", "content": "ping"}]))

    assert "模型返回空内容" in result["message"]["content"]


def test_gateway_generate_retries_once_when_provider_returns_empty_content():
    completions = _FakeEmptyThenTextChatCompletions()
    gateway = ProviderGateway(
        primary=ProviderConfig(name="kimi", model="kimi-k2.5"),
        client_factory=lambda cfg: _FakeClient(completions=completions),
    )

    result = asyncio.run(gateway.generate("请只回复 OK"))

    assert completions.calls == 2
    assert result == "retry ok"


class _AlwaysFailChatCompletions:
    async def create(self, **kwargs):
        raise RuntimeError("connection reset by peer")


class _AlwaysOverloadedChatCompletions:
    async def create(self, **kwargs):
        raise RuntimeError(
            "Error code: 429 - {'error': {'message': 'The engine is currently overloaded, please try again later', 'type': 'engine_overloaded_error'}}"
        )


class _AlwaysUsageLimitedChatCompletions:
    async def create(self, **kwargs):
        raise RuntimeError("usage limitation reached for current tenant")


def test_gateway_falls_back_to_next_provider_when_primary_is_unhealthy():
    gateway = ProviderGateway(
        primary=ProviderConfig(name="primary", model="qwen-max", max_retries=0),
        fallbacks=[ProviderConfig(name="fallback", model="kimi-k2.5", max_retries=0)],
        client_factory=lambda cfg: _FakeClient(
            completions=_AlwaysFailChatCompletions() if cfg.name == "primary" else _FakeChatCompletions()
        ),
    )

    result = asyncio.run(gateway.chat_completion(messages=[{"role": "user", "content": "ping"}]))

    assert result["provider"] == "fallback"
    assert result["model"] == "kimi-k2.5"


def test_gateway_falls_back_to_qwen_when_kimi_is_overloaded():
    gateway = ProviderGateway(
        primary=ProviderConfig(name="kimi", model="kimi-k2.5", max_retries=0),
        fallbacks=[ProviderConfig(name="qwen", model="qwen3.5-397b-a17b", max_retries=0)],
        client_factory=lambda cfg: _FakeClient(
            completions=_AlwaysOverloadedChatCompletions() if cfg.name == "kimi" else _FakeChatCompletions()
        ),
    )

    result = asyncio.run(gateway.chat_completion(messages=[{"role": "user", "content": "ping"}]))

    assert result["provider"] == "qwen"
    assert result["model"] == "qwen3.5-397b-a17b"


def test_gateway_falls_back_when_primary_hits_usage_limitation():
    gateway = ProviderGateway(
        primary=ProviderConfig(name="kimi", model="kimi-k2.5", max_retries=0),
        fallbacks=[ProviderConfig(name="qwen", model="qwen3.5-397b-a17b", max_retries=0)],
        client_factory=lambda cfg: _FakeClient(
            completions=_AlwaysUsageLimitedChatCompletions() if cfg.name == "kimi" else _FakeChatCompletions()
        ),
    )

    result = asyncio.run(gateway.chat_completion(messages=[{"role": "user", "content": "ping"}]))

    assert result["provider"] == "qwen"


def test_gateway_temporarily_deprioritizes_recently_unhealthy_provider():
    calls: dict[str, int] = {"kimi": 0, "qwen": 0}

    def _factory(cfg: ProviderConfig):
        async def _create(**kwargs):
            calls[cfg.name] += 1
            if cfg.name == "kimi":
                raise RuntimeError(
                    "Error code: 429 - {'error': {'message': 'The engine is currently overloaded, please try again later', 'type': 'engine_overloaded_error'}}"
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="fallback ok", tool_calls=[]))]
            )

        return _FakeClient(completions=SimpleNamespace(create=_create))

    gateway = ProviderGateway(
        primary=ProviderConfig(name="kimi", model="kimi-k2.5", max_retries=0),
        fallbacks=[ProviderConfig(name="qwen", model="qwen3.5-397b-a17b", max_retries=0)],
        client_factory=_factory,
        unhealthy_cooldown_seconds=300,
    )

    first = asyncio.run(gateway.chat_completion(messages=[{"role": "user", "content": "ping"}]))
    second = asyncio.run(gateway.chat_completion(messages=[{"role": "user", "content": "ping again"}]))

    assert first["provider"] == "qwen"
    assert second["provider"] == "qwen"
    assert calls["kimi"] == 1
    assert calls["qwen"] == 2
