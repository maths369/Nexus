"""Provider Gateway for OpenAI-compatible model backends."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    import openai
except Exception:  # pragma: no cover - optional at import time
    openai = None

logger = logging.getLogger(__name__)
EMPTY_CONTENT_FALLBACK = "模型返回空内容，请重试或切换模型。"


class ProviderGatewayError(RuntimeError):
    """Raised when provider requests fail or clients are misconfigured."""


# 常见模型的上下文窗口大小（tokens）
# 用于计算动态压缩阈值，未列出的模型使用 DEFAULT_CONTEXT_WINDOW
DEFAULT_CONTEXT_WINDOW = 128_000
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "qwen-max": 128_000,
    "qwen-plus": 128_000,
    "qwen-turbo": 128_000,
    "qwen3-max": 128_000,
    "qwen3-plus": 128_000,
    "kimi-k2.5": 128_000,
    "kimi-k2": 128_000,
    "moonshot-v1-128k": 128_000,
    "moonshot-v1-32k": 32_000,
    "moonshot-v1-8k": 8_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
}


def get_context_window(model: str) -> int:
    """查询模型的上下文窗口大小（tokens）。"""
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]
    # 模糊匹配: "qwen-max-latest" → "qwen-max"
    for key, value in MODEL_CONTEXT_WINDOWS.items():
        if model.startswith(key):
            return value
    return DEFAULT_CONTEXT_WINDOW


@dataclass
class ProviderConfig:
    """Single provider configuration."""

    name: str
    model: str
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 60.0
    healthcheck_timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.5
    retry_max_backoff_seconds: float = 8.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    def resolved_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env)
        return None

    @property
    def provider_id(self) -> str:
        return (self.provider or self.name or "").strip().lower()


class ProviderGateway:
    """
    Unified gateway for Kimi/Qwen/Ollama and other OpenAI-compatible providers.

    Migration constraints:
    1. Keep OpenAI-compatible path first
    2. Keep the surface small and explicit
    3. Port only the useful pieces from the legacy LLMService
    """

    def __init__(
        self,
        primary: ProviderConfig,
        fallbacks: list[ProviderConfig] | None = None,
        client_factory: Callable[[ProviderConfig], Any] | None = None,
        unhealthy_cooldown_seconds: float = 120.0,
    ) -> None:
        self._primary = primary
        self._fallbacks = fallbacks or []
        self._client_factory = client_factory or self._default_client_factory
        self._clients: dict[str, Any] = {}
        self._health_state: dict[str, dict[str, Any]] = {}
        self._health_lock = asyncio.Lock()
        self._unhealthy_cooldown_seconds = max(0.0, unhealthy_cooldown_seconds)

    def list_providers(self) -> list[ProviderConfig]:
        return [self._primary, *self._fallbacks]

    @property
    def primary_provider(self) -> ProviderConfig:
        return self._primary

    def get_provider(self, name: str | None = None, model: str | None = None) -> ProviderConfig:
        for candidate in self.list_providers():
            if name and self._matches_provider_query(candidate, name):
                return candidate
            if model and candidate.model == model:
                return candidate
        if name or model:
            raise ProviderGatewayError(
                f"Provider not configured: name={name or '-'} model={model or '-'}"
            )
        return self._primary

    def switch_primary_provider(self, name: str) -> ProviderConfig:
        providers = self.list_providers()
        selected_index = next(
            (idx for idx, candidate in enumerate(providers) if self._matches_provider_query(candidate, name)),
            None,
        )
        if selected_index is None:
            raise ProviderGatewayError(f"Provider not configured: name={name}")
        if selected_index == 0:
            return self._primary

        selected = providers[selected_index]
        remaining = [candidate for idx, candidate in enumerate(providers) if idx != selected_index]
        self._primary = selected
        self._fallbacks = remaining
        return selected

    def get_health_snapshot(self, provider_name: str | None = None) -> dict[str, Any]:
        if provider_name:
            return dict(self._health_state.get(provider_name, {}))
        return {key: dict(value) for key, value in self._health_state.items()}

    async def generate(
        self,
        prompt: str,
        *,
        context: str | None = None,
        provider_name: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        messages = []
        if context:
            messages.append({"role": "system", "content": context})
        messages.append({"role": "user", "content": prompt})
        response = await self.chat_completion(
            messages=messages,
            provider_name=provider_name,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = response.get("message", {}).get("content", "") or ""
        if content and content != EMPTY_CONTENT_FALLBACK:
            return content

        retry_messages = list(messages)
        retry_messages.insert(
            0,
            {
                "role": "system",
                "content": (
                    "你必须返回非空的纯文本答案。"
                    "不要留空，不要只返回结构化空对象。"
                    "如果信息不足，也要明确说明缺口并给出下一步建议。"
                ),
            },
        )
        retry_response = await self.chat_completion(
            messages=retry_messages,
            provider_name=provider_name,
            model=model,
            temperature=min(temperature, 0.3),
            max_tokens=max_tokens,
        )
        retry_content = retry_response.get("message", {}).get("content", "") or ""
        return retry_content or content

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str | None = None,
        provider_name: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        stream_callback=None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Call an OpenAI-compatible chat completion endpoint and normalize output.

        Returned shape:
            {
              "message": {"role": "assistant", "content": str, "tool_calls": [...]},
              "provider": str,
              "latency_ms": int,
            }
        """
        candidates = self._candidate_sequence(provider_name=provider_name, model=model)
        last_error: ProviderGatewayError | None = None
        for candidate in candidates:
            try:
                return await self._chat_with_provider(
                    provider=candidate,
                    messages=messages,
                    model=model,
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream_callback=stream_callback,
                    extra_body=extra_body,
                )
            except ProviderGatewayError as exc:
                last_error = exc
                if provider_name or model:
                    raise
                logger.warning(
                    "Provider %s failed, trying next fallback if available: %s",
                    candidate.name,
                    exc,
                )
        if last_error is not None:
            raise last_error
        raise ProviderGatewayError("No providers configured")

    async def _chat_with_provider(
        self,
        *,
        provider: ProviderConfig,
        messages: list[dict[str, Any]],
        model: str | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        temperature: float,
        max_tokens: int,
        stream_callback,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._get_client(provider)
        kwargs: dict[str, Any] = {
            "model": model or provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": provider.timeout_seconds,
        }
        # 调用方传入的 extra_body 优先
        if extra_body:
            kwargs["extra_body"] = {**extra_body}

        if tools:
            kwargs["tools"] = tools
            if tool_choice and tool_choice != "auto":
                kwargs["tool_choice"] = tool_choice
            if self._should_disable_thinking_for_tools(provider, model or provider.model):
                extra_body = kwargs.get("extra_body")
                if not isinstance(extra_body, dict):
                    extra_body = {}
                extra_body.setdefault("thinking", {"type": "disabled"})
                kwargs["extra_body"] = extra_body

        if self._is_moonshot(provider):
            has_tool_calls = any(isinstance(m, dict) and m.get("tool_calls") for m in messages)
            has_tooling = bool(tools or has_tool_calls)
            if has_tooling:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            preferred_temp = self._moonshot_preferred_temperature(has_tooling)
            if kwargs.get("temperature") != preferred_temp:
                kwargs["temperature"] = preferred_temp

        attempt = 0
        forced_temperature = False
        started = time.perf_counter()
        while True:
            try:
                response = await client.chat.completions.create(**kwargs)
                latency_ms = int((time.perf_counter() - started) * 1000)
                self._record_health(provider, status="ok", latency_ms=latency_ms, error=None)
                break
            except Exception as exc:  # noqa: BLE001
                required_temp = self._required_temperature_from_error(exc)
                if required_temp is not None and not forced_temperature:
                    kwargs["temperature"] = required_temp
                    forced_temperature = True
                    logger.warning(
                        "Provider %s rejected temperature; retrying with %.1f",
                        provider.name,
                        required_temp,
                    )
                    continue

                attempt += 1
                if attempt > provider.max_retries or not self._should_retry(exc):
                    self._record_health(provider, status="down", latency_ms=None, error=str(exc))
                    raise ProviderGatewayError(
                        self._terminal_error_message(provider, exc)
                    ) from exc

                delay = self._backoff_seconds(provider, attempt)
                logger.warning(
                    "Provider %s request failed attempt=%d/%d error=%s retry_in=%.2fs",
                    provider.name,
                    attempt,
                    provider.max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        normalized = self._normalize_response(response)
        if not normalized["message"]["content"] and not normalized["message"]["tool_calls"]:
            fallback = self._fallback_for_empty_content(response.choices[0])
            normalized["message"]["content"] = fallback
        if stream_callback and normalized["message"]["content"]:
            await stream_callback(normalized["message"]["content"])

        normalized["provider"] = provider.name
        normalized["model"] = model or provider.model
        normalized["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return normalized

    def _candidate_sequence(
        self,
        *,
        provider_name: str | None,
        model: str | None,
    ) -> list[ProviderConfig]:
        if provider_name or model:
            return [self.get_provider(name=provider_name, model=model)]
        providers = self.list_providers()
        return sorted(providers, key=self._provider_priority)

    async def healthcheck(
        self,
        provider_name: str | None = None,
        model: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        provider = self.get_provider(name=provider_name, model=model)
        now = time.time()
        async with self._health_lock:
            previous = self._health_state.get(provider.name)
            min_interval = max(3.0, min(provider.healthcheck_timeout_seconds, 10.0))
            if not force and previous and previous.get("checked_at"):
                if now - float(previous["checked_at"]) < min_interval:
                    return dict(previous)

            client = self._get_client(provider)
            started = time.perf_counter()
            try:
                temperature = 1.0 if self._is_moonshot(provider) else 0.0
                try:
                    await client.chat.completions.create(
                        model=provider.model,
                        messages=[{"role": "user", "content": "ping"}],
                        temperature=temperature,
                        max_tokens=1,
                        timeout=provider.healthcheck_timeout_seconds,
                    )
                except Exception as exc:  # noqa: BLE001
                    required = self._required_temperature_from_error(exc)
                    if required is not None and required != temperature:
                        await client.chat.completions.create(
                            model=provider.model,
                            messages=[{"role": "user", "content": "ping"}],
                            temperature=required,
                            max_tokens=1,
                            timeout=provider.healthcheck_timeout_seconds,
                        )
                    else:
                        raise
                latency_ms = int((time.perf_counter() - started) * 1000)
                self._record_health(provider, status="ok", latency_ms=latency_ms, error=None)
            except Exception as exc:  # noqa: BLE001
                latency_ms = int((time.perf_counter() - started) * 1000)
                self._record_health(provider, status="down", latency_ms=latency_ms, error=str(exc))
            return dict(self._health_state[provider.name])

    def _get_client(self, provider: ProviderConfig) -> Any:
        if provider.name not in self._clients:
            self._clients[provider.name] = self._client_factory(provider)
        return self._clients[provider.name]

    def _record_health(
        self,
        provider: ProviderConfig,
        *,
        status: str,
        latency_ms: int | None,
        error: str | None,
    ) -> None:
        previous = self._health_state.get(provider.name, {})
        consecutive_failures = 0 if status == "ok" else int(previous.get("consecutive_failures") or 0) + 1
        self._health_state[provider.name] = {
            "provider": provider.name,
            "model": provider.model,
            "status": status,
            "checked_at": time.time(),
            "latency_ms": latency_ms,
            "error": error,
            "consecutive_failures": consecutive_failures,
            "last_ok_at": time.time() if status == "ok" else previous.get("last_ok_at"),
        }

    def _provider_priority(self, provider: ProviderConfig) -> tuple[int, int]:
        snapshot = self._health_state.get(provider.name) or {}
        status = str(snapshot.get("status") or "").lower()
        checked_at = float(snapshot.get("checked_at") or 0.0)
        if (
            status == "down"
            and checked_at > 0.0
            and (time.time() - checked_at) < self._unhealthy_cooldown_seconds
        ):
            # Recently unhealthy providers stay at the back of the candidate list.
            return (1, self.list_providers().index(provider))
        return (0, self.list_providers().index(provider))

    @staticmethod
    def _default_client_factory(provider: ProviderConfig) -> Any:
        if openai is None:
            raise ProviderGatewayError(
                "openai package is not installed; cannot create provider client"
            )
        kwargs: dict[str, Any] = {
            "timeout": provider.timeout_seconds,
            "max_retries": provider.max_retries,
        }
        if provider.base_url:
            kwargs["base_url"] = provider.base_url.rstrip("/")
        api_key = provider.resolved_api_key()
        if api_key:
            kwargs["api_key"] = api_key
        if provider.extra_headers:
            kwargs["default_headers"] = provider.extra_headers
        return openai.AsyncOpenAI(**kwargs)

    @classmethod
    def _normalize_response(cls, response: Any) -> dict[str, Any]:
        message_obj = response.choices[0].message
        content = cls._extract_text_content(getattr(message_obj, "content", ""))
        if not content and hasattr(message_obj, "parsed"):
            try:
                content = json.dumps(getattr(message_obj, "parsed"), ensure_ascii=False)
            except Exception:  # noqa: BLE001
                content = str(getattr(message_obj, "parsed"))

        tool_calls = []
        for call in getattr(message_obj, "tool_calls", []) or []:
            function_obj = getattr(call, "function", None)
            arguments = getattr(function_obj, "arguments", {}) if function_obj else {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:  # noqa: BLE001
                    arguments = {"raw": arguments}
            tool_calls.append(
                {
                    "id": getattr(call, "id", ""),
                    "type": getattr(call, "type", "function") or "function",
                    "function": {
                        "name": getattr(function_obj, "name", ""),
                        "arguments": arguments,
                    },
                }
            )
        return {
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            },
            "raw": response,
        }

    @staticmethod
    def _extract_text_content(raw_content: Any) -> str:
        if raw_content is None:
            return ""
        if isinstance(raw_content, str):
            return raw_content.strip()
        if isinstance(raw_content, list):
            parts: list[str] = []
            for item in raw_content:
                if isinstance(item, dict):
                    item_type = str(item.get("type") or "").lower()
                    if item_type and item_type not in {"text", "output_text"}:
                        continue
                    parts.append(str(item.get("text", "")))
                else:
                    text = getattr(item, "text", None)
                    if text:
                        parts.append(str(text))
            return "".join(parts).strip()
        return str(raw_content).strip()

    @staticmethod
    def _fallback_for_empty_content(choice: Any) -> str:
        message_obj = getattr(choice, "message", None)
        refusal = getattr(message_obj, "refusal", None)
        if isinstance(refusal, str) and refusal.strip():
            return refusal.strip()
        return EMPTY_CONTENT_FALLBACK

    @staticmethod
    def _should_retry(exc: Exception) -> bool:
        if ProviderGateway._is_hard_quota_error(exc):
            return False
        retry_type_names = [
            "APITimeoutError",
            "APIConnectionError",
            "RateLimitError",
            "InternalServerError",
            "APIStatusError",
        ]
        retry_types = tuple(
            getattr(openai, name)
            for name in retry_type_names
            if openai is not None and hasattr(openai, name)
        )
        if retry_types and isinstance(exc, retry_types):
            return True
        message = str(exc).lower()
        return any(
            token in message
            for token in [
                "timeout",
                "timed out",
                "connection error",
                "connection reset",
                "rate limit",
                "too many requests",
                "overloaded",
                "overload",
                "engine_overloaded_error",
                "usage limitation",
                "quota exceeded",
                "insufficient_quota",
                "temporarily unavailable",
                "service unavailable",
                "429",
                "502",
                "503",
                "504",
            ]
        )

    @staticmethod
    def _is_hard_quota_error(exc: Exception) -> bool:
        message = str(exc).lower()
        if "quota exceeded" not in message and "insufficient_quota" not in message:
            return False
        return any(
            token in message
            for token in [
                "freetier",
                "free tier",
                "limit: 0",
                "per day",
                "per minute",
                "resourceexhausted",
                "resource_exhausted",
                "billing details",
                "current quota",
            ]
        )

    @staticmethod
    def _terminal_error_message(provider: ProviderConfig, exc: Exception) -> str:
        message = str(exc)
        if ProviderGateway._is_hard_quota_error(exc):
            return (
                f"Provider quota exhausted ({provider.name}): "
                "当前额度已用尽，请切换后端或稍后重试。"
            )
        return f"Provider request failed ({provider.name}): {message}"

    @staticmethod
    def _required_temperature_from_error(exc: Exception) -> float | None:
        message = str(exc).lower()
        if "invalid temperature" not in message or "only" not in message:
            return None
        import re

        match = re.search(r"only\s+([0-9.]+)\s+is allowed", message)
        if not match:
            return None
        try:
            return float(match.group(1))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _moonshot_preferred_temperature(has_tooling: bool) -> float:
        return 0.6 if has_tooling else 1.0

    @staticmethod
    def _is_moonshot(provider: ProviderConfig) -> bool:
        provider_id = provider.provider_id
        return provider_id in {"moonshot", "kimi", "moonshot-ai"} or "kimi" in provider.model.lower()

    @staticmethod
    def _should_disable_thinking_for_tools(provider: ProviderConfig, model: str) -> bool:
        provider_id = provider.provider_id
        model_id = (model or "").lower()
        if provider_id not in {"openai-compatible", "qwen", "ollama"}:
            return False
        return "qwen" in model_id

    @staticmethod
    def _backoff_seconds(provider: ProviderConfig, attempt: int) -> float:
        base = provider.retry_backoff_seconds * (2 ** max(0, attempt - 1))
        jitter = random.uniform(0.0, 0.3)
        return min(provider.retry_max_backoff_seconds, base + jitter)

    @staticmethod
    def _matches_provider_query(provider: ProviderConfig, query: str) -> bool:
        normalized = str(query or "").strip().lower()
        if not normalized:
            return False
        candidates = {
            str(provider.name or "").strip().lower(),
            str(provider.provider or "").strip().lower(),
            str(provider.model or "").strip().lower(),
            str(provider.provider_id or "").strip().lower(),
        }
        return normalized in {value for value in candidates if value}
