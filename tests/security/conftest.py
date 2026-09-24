"""Fixtures for the §12.8 pre-launch checklist (HANDOFF build step 9).

These are integration tests against a deployed **development** instance. Every
fixture reads its own ``WIKI_TEST_*`` variable (names deliberately distinct from the
runtime ``WIKI_BUCKET`` / ``GRANT_TABLE`` set that ``tests/conftest.py`` fakes) and
calls ``pytest.skip`` when it is absent — a missing variable must never look like a
pass. The full environment contract is in ``tests/security/README.md``.

AWS credentials: the autouse fixture in ``tests/conftest.py`` overwrites
``AWS_ACCESS_KEY_ID`` and friends with fakes for every test. The storage-isolation
tests need the operator's real, ambient credentials, so this module snapshots the
``AWS_*`` environment at import (collection) time — before any fixture runs — and
resolves a ``boto3.Session`` from that snapshot in a session-scoped fixture, which
pytest instantiates ahead of the function-scoped fakes.
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import httpx
import pytest

PROTOCOL_VERSION = "2025-06-18"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
MCP_PATH = "/mcp"
HTTP_TIMEOUT_SECONDS = 20.0

# Snapshot before tests/conftest.py's autouse fixture replaces these with "testing".
_AMBIENT_AWS_ENV: dict[str, str | None] = {
    name: os.environ.get(name)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
    )
}

# Response headers that would reveal the stack behind the gateway. API Gateway itself
# sets x-amzn-requestid, x-amz-apigw-id and x-amzn-trace-id; none of these.
_REVEALING_HEADERS = frozenset(
    {"server", "x-powered-by", "x-runtime", "x-aspnet-version", "x-generator", "x-debug"}
)


class RevealingHeader(UserWarning):
    """Informational: a response carried a header that names the stack (§12.3)."""


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every test under tests/security/ is a `security` test, whether or not the
    module remembered its own ``pytestmark`` — so the default ``pytest`` run, whose
    ``addopts`` deselects the marker, can never pick one up."""
    here = os.path.dirname(os.path.abspath(__file__))
    for item in items:
        if os.path.abspath(str(item.path)).startswith(here + os.sep):
            item.add_marker(pytest.mark.security)


# ------------------------------------------------------------------ environment


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is not set — see tests/security/README.md")
    return value


@pytest.fixture(scope="session")
def base_url() -> str:
    """``WIKI_BASE_URL`` without a trailing slash, e.g. ``https://wiki-dev.famestad.com``."""
    url = _require_env("WIKI_BASE_URL").rstrip("/")
    if not url.startswith("https://"):
        pytest.fail(f"WIKI_BASE_URL must be https (AS-11): {url!r}")
    return url


@pytest.fixture(scope="session")
def token() -> str:
    """A live access token for the bootstrap owner (``own`` on ``/``)."""
    return _require_env("WIKI_TEST_TOKEN")


@pytest.fixture(scope="session")
def token_nogrants() -> str:
    """A live access token for a second user who holds zero grants."""
    return _require_env("WIKI_TEST_TOKEN_NOGRANTS")


@pytest.fixture(scope="session")
def token_wrong_aud() -> str:
    """A live token issued for a *different* resource indicator. Optional."""
    return _require_env("WIKI_TEST_TOKEN_WRONG_AUD")


@pytest.fixture(scope="session")
def token_expired() -> str:
    """A once-valid token for this resource whose ``exp`` has passed. Optional."""
    return _require_env("WIKI_TEST_TOKEN_EXPIRED")


@pytest.fixture(scope="session")
def bucket_name() -> str:
    return _require_env("WIKI_TEST_BUCKET")


@pytest.fixture(scope="session")
def table_name() -> str:
    return _require_env("WIKI_TEST_TABLE")


@pytest.fixture(scope="session")
def region() -> str:
    return _require_env("WIKI_TEST_REGION")


# ------------------------------------------------------------------ HTTP


def _note_revealing_headers(response: httpx.Response) -> None:
    """Informational only (§12.3 "errors say nothing"): warn, never fail."""
    for name in _REVEALING_HEADERS:
        if name in response.headers:
            warnings.warn(
                f"{response.request.method} {response.request.url.path} -> "
                f"{response.status_code} carried {name}: {response.headers[name]!r}",
                RevealingHeader,
                stacklevel=2,
            )


@pytest.fixture(scope="session")
def http(base_url: str) -> Iterator[httpx.Client]:
    """Plain client: no default auth, no redirects (a redirect on an authenticated
    path is an unsupported deployment per AS-11 and should surface as a failure)."""
    with httpx.Client(
        base_url=base_url,
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": "wiki-security-suite/1"},
        event_hooks={"response": [_note_revealing_headers]},
    ) as client:
        yield client


@pytest.fixture(scope="session")
def metadata(http: httpx.Client, base_url: str) -> dict[str, Any]:
    """The path-suffixed protected resource metadata document (AS-2), fetched once.

    Anything wrong here is a failure, not a skip: ``WIKI_BASE_URL`` is set, so the
    instance is expected to be up and discoverable.
    """
    response = http.get(METADATA_PATH)
    if response.status_code != 200:
        pytest.fail(
            f"GET {METADATA_PATH} returned {response.status_code}; "
            "the instance is not discoverable (AS-2)"
        )
    try:
        document = response.json()
    except ValueError:
        pytest.fail(f"GET {METADATA_PATH} did not return JSON")
    if not isinstance(document, dict) or "resource" not in document:
        pytest.fail(f"metadata document has no `resource`: {document!r}")
    return document


@pytest.fixture(scope="session")
def metadata_url(base_url: str) -> str:
    """The URL the 401 challenge must name in ``resource_metadata`` (§9.3)."""
    return base_url + METADATA_PATH


def _call(
    http: httpx.Client,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    id: int = 1,
) -> httpx.Response:
    """POST one JSON-RPC request to ``/mcp`` with the mandatory MCP headers (§6.2).

    Args:
        http: The session client.
        token: Bearer token, or ``None`` to send no ``Authorization`` header.
        method: JSON-RPC method, also sent as ``Mcp-Method``.
        params: JSON-RPC params; for ``tools/call`` the ``name`` is sent as ``Mcp-Name``.
        id: JSON-RPC id.

    Returns:
        The raw response. Nothing is asserted here.
    """
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if method == "tools/call" and params and "name" in params:
        headers["Mcp-Name"] = str(params["name"])
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return http.post(MCP_PATH, content=json.dumps(body), headers=headers)


Call = Callable[..., httpx.Response]


@pytest.fixture(scope="session")
def call() -> Call:
    """``call(http, token, method, params=None, id=1) -> httpx.Response``."""
    return _call


def _rpc_result(response: httpx.Response) -> dict[str, Any]:
    """Assert a 200 JSON-RPC success envelope and return its ``result``."""
    assert response.status_code == 200, (
        f"expected 200 JSON-RPC response, got {response.status_code}: {response.text[:300]}"
    )
    body = response.json()
    assert body.get("jsonrpc") == "2.0", body
    assert "error" not in body, f"JSON-RPC error: {body['error']}"
    assert "result" in body, body
    return body["result"]


def _tool_error(response: httpx.Response) -> dict[str, Any]:
    """Assert a tool-level error (§10.14: ``isError`` + envelope) and return the envelope.

    A tool error is an HTTP 200 — a 401/403 at the HTTP level is a different row of
    §6.5 and is reported as such rather than silently accepted.
    """
    result = _rpc_result(response)
    assert result.get("isError") is True, f"expected isError, got {result}"
    envelope = result.get("structuredContent")
    assert isinstance(envelope, dict), f"tool error without structuredContent: {result}"
    for key in ("status", "code", "message"):
        assert key in envelope, f"envelope missing {key!r}: {envelope}"
    return envelope


@pytest.fixture(scope="session")
def rpc_result() -> Callable[[httpx.Response], dict[str, Any]]:
    return _rpc_result


@pytest.fixture(scope="session")
def tool_error() -> Callable[[httpx.Response], dict[str, Any]]:
    return _tool_error


# ------------------------------------------------------------------ AWS


@pytest.fixture(scope="session")
def aws_session(region: str) -> boto3.Session:
    """The operator's ambient credentials, resolved from the import-time snapshot so
    the fakes in ``tests/conftest.py`` never leak in. Skips when there are none."""
    session = boto3.Session(
        aws_access_key_id=_AMBIENT_AWS_ENV["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=_AMBIENT_AWS_ENV["AWS_SECRET_ACCESS_KEY"],
        aws_session_token=_AMBIENT_AWS_ENV["AWS_SESSION_TOKEN"],
        profile_name=_AMBIENT_AWS_ENV["AWS_PROFILE"],
        region_name=region,
    )
    credentials = session.get_credentials()
    if credentials is None:
        pytest.skip("no ambient AWS credentials; storage-isolation tests need the operator's")
    credentials.get_frozen_credentials()  # resolve now, from the snapshot
    return session


@pytest.fixture(scope="session")
def account_id(aws_session: boto3.Session) -> str:
    return aws_session.client("sts").get_caller_identity()["Account"]
