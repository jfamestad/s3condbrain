"""§12.8 item 1 — the route table — and item 8, the error surface.

With no ``Authorization`` header every route answers exactly as §9.2 lists and
nothing else answers at all. Each row of the table is one parametrised test.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

import httpx
import pytest

pytestmark = pytest.mark.security

Call = Callable[..., httpx.Response]

METADATA_PATHS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
)
UNKNOWN_PATHS = ("/", "/health", "/metrics", "/admin", "/mcp/x", "/.well-known/nope")


def _header(response: httpx.Response, name: str) -> str:
    return response.headers.get(name, "")


# ------------------------------------------------------------ metadata (AS-2)


@pytest.mark.parametrize("path", METADATA_PATHS)
def test_metadata_document(http: httpx.Client, base_url: str, path: str) -> None:
    response = http.get(path)
    assert response.status_code == 200, response.text[:300]
    assert "application/json" in _header(response, "content-type").lower()
    document = response.json()

    # The canonical resource: lowercase, no default port, no trailing slash, path
    # included — and byte-for-byte what the tokens will carry as `aud` (§4.3).
    assert document["resource"] == f"{base_url}/mcp"
    assert document["resource"] == document["resource"].lower()
    assert not document["resource"].endswith("/")

    servers = document["authorization_servers"]
    assert isinstance(servers, list) and servers, "authorization_servers must be non-empty"
    assert len(servers) == 1, "AS-2: the AuthKit domain is the first *and only* entry"
    assert servers[0].startswith("https://"), servers

    # ADR-0016: custom scopes are withdrawn — nothing advertised here any more.
    assert "scopes_supported" not in document, document


def test_both_metadata_documents_agree(http: httpx.Client) -> None:
    root, suffixed = (http.get(p).json() for p in METADATA_PATHS)
    assert root["resource"] == suffixed["resource"]
    assert root["authorization_servers"] == suffixed["authorization_servers"]


# ------------------------------------------------------------ CORS preflight (§6.4, §9.4)


def test_preflight_needs_no_auth(http: httpx.Client) -> None:
    response = http.options(
        "/mcp",
        headers={
            "Origin": "https://claude.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization, content-type, mcp-protocol-version, mcp-method, mcp-name"
            ),
        },
    )
    assert response.status_code in (200, 204), response.text[:300]
    assert "access-control-allow-origin" in response.headers
    exposed = _header(response, "access-control-expose-headers").lower()
    assert "www-authenticate" in exposed, "browser discovery fails invisibly without this"
    allowed = _header(response, "access-control-allow-headers").lower()
    assert "mcp-protocol-version" in allowed, allowed


# ------------------------------------------------------------ /mcp without a token


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_mcp_get_and_delete_are_405(http: httpx.Client, method: str) -> None:
    response = http.request(method, "/mcp")
    assert response.status_code == 405, response.text[:300]


def test_post_without_token_is_401_challenge(
    http: httpx.Client, call: Call, metadata_url: str
) -> None:
    response = call(http, None, "ping")
    assert response.status_code == 401, response.text[:300]

    challenge = _header(response, "www-authenticate")
    assert challenge.startswith("Bearer"), challenge
    assert f'resource_metadata="{metadata_url}"' in challenge, challenge
    # ADR-0016: the gateway challenge no longer names a scope — there is none to ask for.
    assert "scope=" not in challenge, challenge

    # §9.3: the CORS headers must be on the gateway response itself.
    exposed = _header(response, "access-control-expose-headers").lower()
    assert "www-authenticate" in exposed, dict(response.headers)


# ------------------------------------------------------------ everything else is 404


def _random_path() -> str:
    return "/" + secrets.token_hex(6)  # 12 characters


@pytest.mark.parametrize("path", [*UNKNOWN_PATHS, _random_path()])
def test_unknown_path_is_404(http: httpx.Client, path: str) -> None:
    response = http.get(path)
    assert response.status_code == 404, f"{path}: {response.status_code} {response.text[:300]}"
    body = response.text
    assert "Missing Authentication Token" not in body, "MISSING_AUTHENTICATION_TOKEN not remapped"
    assert "Traceback" not in body
    if path not in UNKNOWN_PATHS:
        assert path.lstrip("/") not in body, "§9.3: generic body, no request echo"


# ------------------------------------------------------------ §12.8 item 8: errors say nothing


def test_malformed_body_is_400_and_reveals_nothing(http: httpx.Client, token: str) -> None:
    marker = secrets.token_hex(8)
    response = http.post(
        "/mcp",
        content=f"this is not json {marker}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
            "Mcp-Method": "ping",
        },
    )
    assert response.status_code == 400, response.text[:300]
    body = response.text
    for needle in ("Traceback", "/var/task", "Exception"):
        assert needle not in body, f"error body reveals {needle!r}"
    assert marker not in body, "§9.3: no request echo"


# --- the web application (§11.6) --------------------------------------------------------


def test_web_app_root_redirects_to_login_without_session(http: httpx.Client) -> None:
    """``/app/`` is the human surface: no session → the login page, never content."""
    r = http.get("/app/")
    assert r.status_code == 303
    assert r.headers.get("location", "").startswith("/app/login")
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_web_login_page_is_public_and_hardened(http: httpx.Client) -> None:
    r = http.get("/app/login")
    assert r.status_code == 200
    assert "Sign in" in r.text
    csp = r.headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
    assert r.headers.get("x-frame-options", "").upper() == "DENY"
    assert r.headers.get("cache-control") == "no-store"


def test_web_admin_without_session_is_redirect_not_content(http: httpx.Client) -> None:
    for path in ("/app/admin", "/app/admin/people", "/app/admin/grants"):
        r = http.get(path)
        assert r.status_code == 303, path
        assert r.headers.get("location", "").startswith("/app/login"), path


def test_web_post_without_session_or_csrf_is_refused(http: httpx.Client) -> None:
    r = http.post("/app/admin/grants", data={"subject": "x", "node": "/", "permission": "own"})
    assert r.status_code in (303, 403)
    assert "granted" not in r.text.lower()


def test_web_unknown_path_is_404_page_without_details(http: httpx.Client) -> None:
    r = http.get("/app/definitely-not-a-route")
    assert r.status_code == 404
    assert "Traceback" not in r.text and "/var/task" not in r.text
