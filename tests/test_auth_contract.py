from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError
from starlette.requests import Request

from kanx_ai4s_master.core.application import create_app
from kanx_ai4s_master.core.config import Settings
from kanx_ai4s_master.modules.auth import router as auth_router
from kanx_ai4s_master.modules.auth.dependencies import AuthPrincipal
from kanx_ai4s_master.modules.auth.module import module as auth_module
from kanx_ai4s_master.modules.auth.router import LoginRequest, RegisterRequest, TokenRequest
from kanx_ai4s_master.modules.auth.service import AuthenticationError, TokenPair


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@asynccontextmanager
async def empty_lifespan(app: FastAPI, settings: Settings) -> AsyncIterator[None]:
    del app, settings
    yield


def auth_app() -> FastAPI:
    module = auth_module.__class__(
        name=auth_module.name,
        routers=(auth_router.router,),
        lifespan=empty_lifespan,
    )
    return create_app(
        Settings(cors_allowed_origins="http://localhost:3000"),
        modules=(module,),
    )


def test_auth_routes_are_registered() -> None:
    paths = {route.path for route in auth_router.router.routes}
    assert {
        "/auth/register",
        "/auth/verify-email",
        "/auth/login",
        "/auth/refresh",
        "/auth/logout",
        "/auth/resend-verification",
        "/users/me",
    } <= paths


def test_registration_password_matches_backend_minimum() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(email="user@example.com", password="short", display_name="User")

    request = RegisterRequest(
        email="user@example.com",
        password="twelve-chars",
        display_name="User",
    )
    assert request.password == "twelve-chars"


@pytest.mark.asyncio
async def test_registration_keeps_a_generic_response_for_duplicate_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        async def register(self, session: Any, **values: str) -> None:
            del session, values
            raise AuthenticationError("Unable to register account")

    monkeypatch.setattr(auth_router, "AuthService", Service)
    session = FakeSession()
    response = await auth_router.register(
        RegisterRequest(
            email="existing@example.com",
            password="a-secure-password",
            display_name="Existing User",
        ),
        session,  # type: ignore[arg-type]
    )

    assert response == {
        "message": "If registration is available, a verification email will be sent."
    }
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_login_returns_backend_token_contract_and_sets_session_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        async def login(self, session: Any, email: str, password: str) -> TokenPair:
            del session
            assert email == "user@example.com"
            assert password == "a-secure-password"
            return TokenPair("access-1", "refresh-1", "csrf-1", 900)

    async def audit(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    monkeypatch.setattr(auth_router, "AuthService", Service)
    monkeypatch.setattr(auth_router, "record_audit", audit)
    session = FakeSession()
    response = Response()

    payload = await auth_router.login(
        LoginRequest(email="user@example.com", password="a-secure-password"),
        response,
        session,  # type: ignore[arg-type]
    )

    assert payload == {
        "access_token": "access-1",
        "token_type": "bearer",
        "expires_in": 900,
    }
    cookies = response.headers.getlist("set-cookie")
    assert any("refresh_token=refresh-1" in cookie and "HttpOnly" in cookie for cookie in cookies)
    assert any("csrf_token=csrf-1" in cookie and "HttpOnly" not in cookie for cookie in cookies)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_email_verification_consumes_the_backend_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        async def verify_email(self, session: Any, token: str) -> None:
            del session
            assert token == "verification-token"

    monkeypatch.setattr(auth_router, "AuthService", Service)
    session = FakeSession()
    payload = await auth_router.verify_email(
        TokenRequest(token="verification-token"),
        session,  # type: ignore[arg-type]
    )

    assert payload == {"message": "Email verified"}
    assert session.commits == 1


def test_refresh_rejects_an_untrusted_origin_before_using_tokens() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/refresh",
            "headers": [(b"origin", b"https://untrusted.example")],
        }
    )

    with pytest.raises(HTTPException, match="Origin is not allowed"):
        auth_router._validate_origin(request)


@pytest.mark.asyncio
async def test_current_user_response_uses_the_public_backend_fields() -> None:
    payload = await auth_router.me(
        AuthPrincipal(
            id="user-1",
            email="user@example.com",
            display_name="Test User",
            email_verified_at=None,
        )
    )
    assert payload == {
        "id": "user-1",
        "email": "user@example.com",
        "display_name": "Test User",
        "email_verified": False,
    }
