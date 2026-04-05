"""HTTP / WebSocket bearer-token auth helpers for Nexus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.websockets import WebSocket


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool = False
    bearer_token: str = ""
    exempt_paths: tuple[str, ...] = ("/health", "/feishu/webhook", "/weixin/")
    cookie_name: str = "__nexus_token"

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> "AuthConfig":
        raw = dict(mapping or {})
        exempt = tuple(str(item).strip() for item in (raw.get("exempt_paths") or []) if str(item).strip())
        return cls(
            enabled=bool(raw.get("enabled", False)),
            bearer_token=str(raw.get("bearer_token") or "").strip(),
            exempt_paths=exempt or cls.exempt_paths,
            cookie_name=str(raw.get("cookie_name") or "__nexus_token"),
        )

    def is_exempt(self, path: str) -> bool:
        candidate = str(path or "").strip() or "/"
        for prefix in self.exempt_paths:
            normalized = prefix.rstrip("/") or "/"
            if candidate == normalized or candidate.startswith(f"{normalized}/"):
                return True
        return False

    def is_authorized(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not self.bearer_token:
            return False
        return bool(token) and token == self.bearer_token


def _extract_bearer_header(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def resolve_request_token(request: Request, config: AuthConfig) -> str | None:
    return (
        _extract_bearer_header(request.headers.get("Authorization"))
        or request.cookies.get(config.cookie_name)
        or request.query_params.get("token")
    )


def resolve_websocket_token(websocket: WebSocket, config: AuthConfig) -> str | None:
    return (
        _extract_bearer_header(websocket.headers.get("Authorization"))
        or websocket.cookies.get(config.cookie_name)
        or websocket.query_params.get("token")
    )


class NexusAuthMiddleware(BaseHTTPMiddleware):
    """Simple bearer-token auth for API routes."""

    def __init__(self, app, *, config: AuthConfig):  # noqa: ANN001
        super().__init__(app)
        self._config = config

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        if (
            not self._config.enabled
            or request.method.upper() == "OPTIONS"
            or self._config.is_exempt(request.url.path)
        ):
            return await call_next(request)

        token = resolve_request_token(request, self._config)
        if not self._config.is_authorized(token):
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
