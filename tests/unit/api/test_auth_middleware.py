from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.auth_middleware import (
    AuthConfig,
    NexusAuthMiddleware,
    resolve_request_token,
    resolve_websocket_token,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        NexusAuthMiddleware,
        config=AuthConfig(
            enabled=True,
            bearer_token="secret-token",
            exempt_paths=("/health", "/feishu/webhook", "/weixin/"),
            cookie_name="__nexus_token",
        ),
    )

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/secure")
    async def secure():
        return {"secure": True}

    return app


def test_auth_middleware_allows_exempt_health_endpoint():
    client = TestClient(_build_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_auth_middleware_rejects_missing_token():
    client = TestClient(_build_app())

    response = client.get("/secure")

    assert response.status_code == 401
    assert response.json()["error"] == "Unauthorized"


def test_auth_middleware_accepts_bearer_header():
    client = TestClient(_build_app())

    response = client.get("/secure", headers={"Authorization": "Bearer secret-token"})

    assert response.status_code == 200
    assert response.json() == {"secure": True}


def test_auth_middleware_accepts_cookie_token():
    client = TestClient(_build_app())
    client.cookies.set("__nexus_token", "secret-token")

    response = client.get("/secure")

    assert response.status_code == 200
    assert response.json() == {"secure": True}


def test_resolve_request_token_prefers_bearer_header():
    request = SimpleNamespace(
        headers={"Authorization": "Bearer secret-token"},
        cookies={},
        query_params={},
    )
    auth_config = AuthConfig(enabled=True, bearer_token="secret-token")

    token = resolve_request_token(request, auth_config)

    assert token == "secret-token"


def test_resolve_websocket_token_supports_cookie_and_query():
    auth_config = AuthConfig(enabled=True, bearer_token="secret-token")
    websocket = SimpleNamespace(
        headers={},
        cookies={"__nexus_token": "cookie-token"},
        query_params={"token": "query-token"},
    )

    token = resolve_websocket_token(websocket, auth_config)

    assert token == "cookie-token"
