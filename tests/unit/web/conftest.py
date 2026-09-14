"""Fixtures for the web application tests.

The app's module singletons are reset per test; GrantAdmin runs over the moto grant
table; the minter is a fake that hands back the moto S3 client and counts mints.
"""

from __future__ import annotations

import base64
import json
import secrets as _secrets
from collections.abc import Iterator
from typing import Any

import pytest

from app.auth.admin import GrantAdmin
from app.auth.credentials import Shape
from app.web import app as web_app
from app.web import secrets as secrets_mod
from app.web import session as sess
from app.web.http import Request, Response

SESSION_KEY = _secrets.token_bytes(32)
SUBJECT = "user_01TEST"


class FakeMinter:
    def __init__(self, s3: Any) -> None:
        self._s3 = s3
        self.mint_count = 0
        self.minted: list[tuple[str, str, str]] = []

    def mint(self, subject: str, shape: Shape, path: str) -> Any:
        self.mint_count += 1
        self.minted.append((subject, shape.value, path))
        return self

    def client(self, name: str) -> Any:  # boto3.Session interface
        return self._s3

    def s3(self, subject: str, shape: Shape, path: str) -> Any:
        return self.mint(subject, shape, path)._s3


@pytest.fixture(autouse=True)
def _web_env(monkeypatch: pytest.MonkeyPatch, bucket: Any, grant_table: Any) -> Iterator[None]:
    monkeypatch.setenv("WEB_BASE_URL", "https://wiki-dev.famestad.com/app")
    monkeypatch.setenv("WORKOS_CLIENT_ID", "client_01TEST")
    monkeypatch.setenv("WEB_SECRET_ARN", "")
    monkeypatch.setenv(
        "WEB_DEV_SECRETS",
        json.dumps(
            {
                "client_secret": "shh",
                "api_key": "sk_test",
                "session_key": base64.urlsafe_b64encode(SESSION_KEY).decode(),
            }
        ),
    )
    web_app._reset()
    minter = FakeMinter(bucket)
    monkeypatch.setattr(web_app, "CredentialMinter", lambda *a, **k: minter)
    yield
    web_app._reset()


@pytest.fixture
def admin(settings: Any) -> GrantAdmin:
    return GrantAdmin(settings.grant_table)


@pytest.fixture
def profile(admin: GrantAdmin) -> Any:
    return admin.create_profile(SUBJECT, "test@example.com", "Test Person", "active")


@pytest.fixture
def session_cookie(profile: Any) -> str:
    return sess.issue_session(SESSION_KEY, SUBJECT, "test@example.com", "Test Person", 12)


def event(
    method: str = "GET",
    path: str = "/app/",
    *,
    query: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    hdrs = {"Host": "wiki-dev.famestad.com", **(headers or {})}
    body = ""
    if form is not None:
        from urllib.parse import urlencode

        body = urlencode(form)
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {
        "httpMethod": method,
        "path": path,
        "headers": hdrs,
        "queryStringParameters": query,
        "body": body,
        "isBase64Encoded": False,
        "requestContext": {"requestId": "req-1"},
    }


def call(ev: dict[str, Any]) -> dict[str, Any]:
    return web_app.handle(ev, None)


def csrf_for(cookie: str) -> str:
    principal = sess.load_session(SESSION_KEY, cookie)
    assert principal is not None
    return sess.csrf_token(SESSION_KEY, principal)


__all__ = [
    "SESSION_KEY",
    "SUBJECT",
    "FakeMinter",
    "Request",
    "Response",
    "call",
    "csrf_for",
    "event",
    "secrets_mod",
]
