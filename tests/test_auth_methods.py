"""Tests for auth methods toggling and Google-only sign-in configuration."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

import app.app_state as app_state
from app.api.auth_routes import (
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    forgot_password,
    login,
    register,
    reset_password,
)
from app.settings_service import get_auth_flags
from app.templating import templates


@asynccontextmanager
async def _fake_db_factory():
    yield AsyncMock()


@pytest.fixture(autouse=True)
def _setup_test_env():
    original_db = app_state.db_session_factory
    app_state.db_session_factory = _fake_db_factory
    yield
    app_state.db_session_factory = original_db


@pytest.mark.asyncio
async def test_auth_flags_default_to_password_disabled():
    with patch(
        "app.settings_service.get_runtime_setting",
        new=AsyncMock(side_effect=lambda db, k, default=None: default),
    ):
        flags = await get_auth_flags()
        assert flags["google_enabled"] is True
        assert flags["password_enabled"] is False


@pytest.mark.asyncio
async def test_password_endpoints_blocked_when_password_disabled():
    # When password_enabled is False, register/login/forgot/reset must be rejected with 403
    mock_request = AsyncMock(spec=Request)
    mock_request.json = AsyncMock(return_value={"email": "test@example.com"})

    with patch(
        "app.settings_service.get_auth_flags",
        new=AsyncMock(
            return_value={"google_enabled": True, "password_enabled": False, "signup_enabled": True}
        ),
    ):
        # Register
        resp_reg = await register(
            RegisterRequest(email="test@example.com", password="password123"),
            mock_request,
        )
        assert resp_reg.status_code == 403
        import json

        assert "disabled" in json.loads(resp_reg.body.decode())["error"]

        # Login
        resp_login = await login(
            LoginRequest(email="test@example.com", password="password123"),
            mock_request,
        )
        assert resp_login.status_code == 403
        assert "disabled" in json.loads(resp_login.body.decode())["error"]

        # Forgot password
        resp_forgot = await forgot_password(mock_request)
        assert resp_forgot.status_code == 403
        assert "disabled" in json.loads(resp_forgot.body.decode())["error"]

        # Reset password
        resp_reset = await reset_password(ResetPasswordRequest(token="tok123", password="newpassword123"))
        assert resp_reset.status_code == 403
        assert "disabled" in json.loads(resp_reset.body.decode())["error"]


def test_signin_template_google_only_mode():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/signin",
        "headers": [],
        "server": ("testserver", 80),
    }
    request = Request(scope)

    html = templates.get_template("auth/signin.html").render(
        {
            "request": request,
            "user": None,
            "embed": False,
            "google_configured": True,
            "first_run": False,
            "signup_enabled": True,
            "password_enabled": False,
            "next_url": "/home",
        }
    )

    # Google button is shown
    assert "Continue with Google" in html

    # Mode toggle and password forms are NOT shown
    assert 'id="authToggle"' not in html
    assert '<div class="auth-divider">' not in html
    assert 'id="signinForm"' not in html
    assert 'id="forgotForm"' not in html
    assert "Sign in or get started with your Google account." in html


def test_signin_template_password_enabled_mode():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/signin",
        "headers": [],
        "server": ("testserver", 80),
    }
    request = Request(scope)

    html = templates.get_template("auth/signin.html").render(
        {
            "request": request,
            "user": None,
            "embed": False,
            "google_configured": True,
            "first_run": False,
            "signup_enabled": True,
            "password_enabled": True,
            "next_url": "/home",
        }
    )

    # Both Google and password form are shown
    assert "Continue with Google" in html
    assert 'id="authToggle"' in html
    assert '<div class="auth-divider">' in html
    assert 'id="signinForm"' in html


@pytest.mark.asyncio
async def test_request_access_redirects_to_signin():
    from app.api.access_request_routes import request_access_page

    resp = await request_access_page()
    assert resp.status_code == 302
    assert resp.headers["location"] == "/signin"
