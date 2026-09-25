"""Local fixtures for the transport tests.

Everything AWS-shaped is stubbed: the registry is a fake with three tools, and
``GrantStore`` / ``CredentialMinter`` are replaced with inert stand-ins so
``build_context`` never touches boto3.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from aws_lambda_powertools import Logger

from app import errors
from app.mcp import server
from app.mcp import tools as tools_pkg
from app.mcp.protocol import Tool, ToolContext

SUB = "user_01ABC"
REQUEST_ID = "req-0001"


# --- fake tools ---------------------------------------------------------------


def _echo(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    return dict(arguments)


def _boom_grant(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    raise errors.forbidden()


def _boom_conflict(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    raise errors.conflict("Changed since you read it.", current_version="v2", edits_apply=True)


def _crash(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    # message built from runtime data, as a real failure would be (content leaking
    # into an exception message is the case the log must survive)
    raise RuntimeError(arguments.get("body", "boom"))


def _tool(name: str, scope: str | None, handler: Any) -> Tool:
    return Tool(
        name=name,
        description=f"fake {name}",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        output_schema={"type": "object"} if name == "echo" else None,
        scope=scope,
        handler=handler,
    )


def fake_registry() -> dict[str, Tool]:
    tools = (
        _tool("echo", "wiki.read", _echo),
        _tool("boom_grant", "wiki.write", _boom_grant),
        _tool("boom_conflict", "wiki.write", _boom_conflict),
        _tool("crash", "wiki.read", _crash),
    )
    return {t.name: t for t in tools}


# --- stubs for the auth singletons ---------------------------------------------


class StubGrantStore:
    def __init__(self, table_name: str, dynamodb_resource: Any | None = None) -> None:
        self.table_name = table_name


class StubMinter:
    def __init__(self, role_arn: str, bucket: str, kms_key_arn: str, cache_seconds: int) -> None:
        self.role_arn = role_arn
        self.bucket = bucket
        self.kms_key_arn = kms_key_arn
        self.cache_seconds = cache_seconds


@pytest.fixture(autouse=True)
def _isolate_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh module state per test, fake registry, stubbed AWS-backed classes."""
    monkeypatch.setattr(tools_pkg, "registry", fake_registry)
    monkeypatch.setattr(server, "GrantStore", StubGrantStore)
    monkeypatch.setattr(server, "CredentialMinter", StubMinter)
    server._reset()
    yield
    server._reset()


@pytest.fixture
def log_lines(monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[dict[str, Any]]]:
    """Swap in a real Powertools logger writing JSON to a buffer; return a reader."""
    buf = io.StringIO()
    logger = Logger(
        service=f"wiki-mcp-test-{uuid.uuid4().hex}", logger_handler=logging.StreamHandler(buf)
    )
    monkeypatch.setattr(server, "logger", logger)

    def read() -> list[dict[str, Any]]:
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

    return read


# --- event construction --------------------------------------------------------


def make_event(
    body: Any = None,
    *,
    method: str = "POST",
    headers: dict[str, str] | None = None,
    multi_value_headers: dict[str, list[str]] | None = None,
    authorizer: dict[str, str] | None | str = "default",
    raw_body: str | None = None,
    base64_body: bool = False,
) -> dict[str, Any]:
    """Build an API Gateway REST proxy event.

    ``authorizer="default"`` supplies a normal sub/scope context; ``None`` omits it.
    ``raw_body`` bypasses JSON encoding for malformed-body tests.
    """
    if raw_body is not None:
        text = raw_body
    elif body is None:
        text = ""
    else:
        text = json.dumps(body)
    if base64_body:
        text = base64.b64encode(text.encode()).decode()
    request_context: dict[str, Any] = {"requestId": REQUEST_ID}
    if authorizer == "default":
        request_context["authorizer"] = {"sub": SUB, "scope": "wiki.read wiki.write"}
    elif authorizer is not None:
        request_context["authorizer"] = authorizer
    event: dict[str, Any] = {
        "httpMethod": method,
        "path": "/mcp",
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "multiValueHeaders": multi_value_headers,
        "requestContext": request_context,
        "body": text,
        "isBase64Encoded": base64_body,
    }
    return event


def rpc(method: str, params: dict[str, Any] | None = None, id: Any = 1) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if id is not None:
        msg["id"] = id
    return msg


def call(event: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
    """Invoke ``handle`` and return ``(status, headers, parsed_body_or_raw)``."""
    resp = server.handle(event, None)
    body = resp.get("body", "")
    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = body
    return resp["statusCode"], resp.get("headers", {}), parsed


def sentinel(value: str) -> str:
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="
