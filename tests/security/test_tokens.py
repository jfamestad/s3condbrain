"""§12.8 items 2 and 3, client side — the authorizer actually checks tokens.

Rejections first (build step 5: a validator that accepts everything passes every
happy-path test). Then the two positive checks: a real token works, and its ``aud``
is byte-for-byte the ``resource`` the metadata document publishes (§2 check 3).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import ALL_SCOPES

pytestmark = pytest.mark.security

Call = Callable[..., httpx.Response]
PING = "ping"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _claims(metadata: dict[str, Any]) -> dict[str, Any]:
    """Claims that would pass every check except the signature: real issuer, real
    audience, fresh timestamps, both scopes."""
    now = int(time.time())
    return {
        "iss": metadata["authorization_servers"][0],
        "aud": metadata["resource"],
        "sub": "user_forged",
        "scope": " ".join(ALL_SCOPES),
        "iat": now,
        "exp": now + 300,
        "jti": "forged",
    }


def _assert_rejected(response: httpx.Response) -> None:
    """AS-3: a token that fails AS-4 is a 401 with the discovery challenge — never a
    403, never a 200 carrying an error payload."""
    assert response.status_code == 401, f"{response.status_code}: {response.text[:300]}"
    challenge = response.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer") and "resource_metadata=" in challenge, challenge


# ------------------------------------------------------------ rejections


def test_garbage_bearer_rejected(http: httpx.Client, call: Call) -> None:
    _assert_rejected(call(http, "not-a-token", PING))


def test_self_signed_rs256_rejected(
    http: httpx.Client, call: Call, metadata: dict[str, Any]
) -> None:
    """Right issuer, right audience, valid structure, signed by a key AuthKit has
    never seen. Only the JWKS check can reject this."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(_claims(metadata), key, algorithm="RS256", headers={"kid": "forged"})
    _assert_rejected(call(http, forged, PING))


def test_alg_none_rejected(http: httpx.Client, call: Call, metadata: dict[str, Any]) -> None:
    """AS-4: the algorithm is pinned, never read from the token's own header."""
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(_claims(metadata)).encode())
    _assert_rejected(call(http, f"{header}.{payload}.", PING))


def test_wrong_audience_rejected(http: httpx.Client, call: Call, token_wrong_aud: str) -> None:
    """A genuine AuthKit token minted for another resource indicator (§12.8 item 2)."""
    _assert_rejected(call(http, token_wrong_aud, PING))


def test_expired_rejected(http: httpx.Client, call: Call, token_expired: str) -> None:
    """A genuine, once-valid token whose ``exp`` has passed (§12.8 item 2)."""
    _assert_rejected(call(http, token_expired, PING))


# ------------------------------------------------------------ the real token works


def test_valid_token_ping(
    http: httpx.Client, call: Call, token: str, rpc_result: Callable[..., Any]
) -> None:
    response = call(http, token, PING)
    result = rpc_result(response)
    assert response.json()["id"] == 1
    assert isinstance(result, dict)


def test_tools_list_contains_read_article(
    http: httpx.Client, call: Call, token: str, rpc_result: Callable[..., Any]
) -> None:
    result = rpc_result(call(http, token, "tools/list", {}))
    tools = result.get("tools")
    assert isinstance(tools, list), result
    names = {tool["name"] for tool in tools}
    assert "read_article" in names, names


# ------------------------------------------------------------ §2 check 3 / §12.8 item 3


def _unverified_claims(token: str) -> dict[str, Any]:
    # Signature verification is the server's job (and tested above); here we only
    # read what the authorization server put in the token.
    return jwt.decode(token, options={"verify_signature": False})


def test_token_audience_is_canonical_resource(
    token: str, metadata: dict[str, Any], base_url: str
) -> None:
    """``aud`` equals the published ``resource`` byte for byte — the check that
    decides whether WorkOS honoured the resource indicator (§2, "why check 4")."""
    aud = _unverified_claims(token)["aud"]
    if isinstance(aud, list):
        assert len(aud) == 1, f"aud must name exactly this resource, got {aud}"
        aud = aud[0]
    assert aud == metadata["resource"]
    assert aud == f"{base_url}/mcp"


def test_token_issuer_is_advertised_authorization_server(
    token: str, metadata: dict[str, Any]
) -> None:
    iss = _unverified_claims(token)["iss"]
    assert iss.rstrip("/") == metadata["authorization_servers"][0].rstrip("/")


def test_token_carries_both_scopes(token: str) -> None:
    """§2 check 5 — custom scopes are *issued*, not merely configured."""
    scope = _unverified_claims(token).get("scope", "")
    scopes = set(scope.split()) if isinstance(scope, str) else set(scope)
    assert set(ALL_SCOPES) <= scopes, f"scope claim: {scope!r}"
