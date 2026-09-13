"""Lambda REQUEST authorizer — HANDOFF AS-4, §9.3, §11.1, build step 5.

Validates the bearer token and nothing else. Holds no AWS data permissions.

* Missing/malformed ``Authorization`` header, bad signature, wrong ``iss``, wrong
  ``aud``, expired, or any algorithm other than the pinned one → ``raise
  Exception("Unauthorized")``. That exact string makes API Gateway emit the
  customised 401 gateway response (§9.3). Never return a Deny policy for these.
* Success → Allow policy for the invoked method ARN with ``context``
  ``{"sub": <sub>, "scope": <space-separated scopes>}``.

The four lines that matter most in the codebase (§11.1):

    jwt.decode(token, key=jwks_key, algorithms=["RS256"],
               issuer=settings.authkit_domain, audience=settings.canonical_mcp_url)

JWKS is fetched via ``jwt.PyJWKClient`` and cached in the module for the life of the
container. Authorizer result cache TTL is 0 (AS-6); that is set in infra.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings


def validate(token: str, settings: Settings, jwks_client: Any) -> dict[str, Any]:
    """Decode and verify. Returns claims.

    Raises:
        jwt.PyJWTError: on any validation failure.
    """
    raise NotImplementedError


def handle(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Authorizer entry point."""
    raise NotImplementedError


__all__ = ["handle", "validate"]
