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

Nothing in this module logs a token, an ``Authorization`` header value, or an
exception message that might carry either (§12.7). Rejections log the exception
class name and the API Gateway request id only.
"""

from __future__ import annotations

from typing import Any

import jwt
from aws_lambda_powertools import Logger

from app.config import Settings

logger = Logger(service="wiki-authorizer")

#: Pinned verification algorithm. A literal — never read from the token header (AS-4).
ALGORITHMS: list[str] = ["RS256"]

#: Claims that must be present; absence is a rejection, not a default.
REQUIRED_CLAIMS: list[str] = ["exp", "iss", "aud", "sub"]

_settings: Settings | None = None
_jwks_client: jwt.PyJWKClient | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def _get_jwks_client() -> jwt.PyJWKClient:
    """Lazy module-level JWKS client; keys are cached for the container's life."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(_get_settings().jwks_url, cache_keys=True)
    return _jwks_client


def _reset() -> None:
    """Drop the cached settings and JWKS client. Test hook."""
    global _settings, _jwks_client
    _settings = None
    _jwks_client = None


def validate(token: str, settings: Settings, jwks_client: Any) -> dict[str, Any]:
    """Decode and verify. Returns claims.

    Args:
        token: Compact-serialised JWT taken from the bearer header.
        settings: Supplies the pinned issuer and audience.
        jwks_client: Anything exposing ``get_signing_key_from_jwt(token).key``.

    Raises:
        jwt.PyJWTError: on any validation failure, including an unknown ``kid``.
    """
    signing_key = jwks_client.get_signing_key_from_jwt(token).key
    return jwt.decode(
        token,
        key=signing_key,
        algorithms=ALGORITHMS,
        issuer=settings.authkit_domain,
        audience=settings.canonical_mcp_url,
        options={"require": REQUIRED_CLAIMS},
    )


def _bearer_token(event: dict[str, Any]) -> str:
    """Extract the token from ``Authorization: Bearer <token>``.

    Looks in ``headers`` first, then ``multiValueHeaders``; header names are
    matched case-insensitively. Exactly one value must be present.

    Raises:
        ValueError: when the header is absent, duplicated, or not a single
            well-formed bearer credential.
    """
    value: str | None = None

    headers = event.get("headers") or {}
    for name, raw in headers.items():
        if isinstance(name, str) and name.lower() == "authorization":
            value = raw
            break

    if value is None:
        multi = event.get("multiValueHeaders") or {}
        for name, raw_list in multi.items():
            if isinstance(name, str) and name.lower() == "authorization":
                if not isinstance(raw_list, list) or len(raw_list) != 1:
                    raise ValueError("authorization header count")
                value = raw_list[0]
                break

    if not isinstance(value, str):
        raise ValueError("authorization header missing")

    scheme, sep, token = value.strip().partition(" ")
    if scheme.lower() != "bearer" or not sep:
        raise ValueError("authorization scheme")
    if not token or token != token.strip() or any(c.isspace() for c in token):
        raise ValueError("bearer token shape")
    return token


def _subject(claims: dict[str, Any]) -> str:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ValueError("sub claim shape")
    return sub


def _scope(claims: dict[str, Any]) -> str:
    """Normalise ``scope`` (or ``scp``) to a single space-separated string.

    Raises:
        ValueError: when the claim is present but neither a string nor a list
            of strings. A malformed claim fails closed.
    """
    raw = claims.get("scope")
    if raw is None:
        raw = claims.get("scp")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return " ".join(raw.split())
    if isinstance(raw, list) and all(isinstance(s, str) for s in raw):
        return " ".join(s for s in raw if s)
    raise ValueError("scope claim shape")


def _allow(sub: str, scope: str, method_arn: str) -> dict[str, Any]:
    return {
        "principalId": sub,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {"Action": "execute-api:Invoke", "Effect": "Allow", "Resource": method_arn}
            ],
        },
        # API Gateway forwards only str/int/bool context values; keep them str.
        "context": {"sub": sub, "scope": scope},
    }


def handle(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Authorizer entry point.

    Raises:
        Exception: ``"Unauthorized"`` — exactly that string — on any failure, so
            API Gateway emits the customised 401 rather than a 403 (§9.3).
    """
    request_id = (event.get("requestContext") or {}).get("requestId")
    try:
        token = _bearer_token(event)
        claims = validate(token, _get_settings(), _get_jwks_client())
        sub = _subject(claims)
        scope = _scope(claims)
        method_arn = event["methodArn"]
    except (jwt.PyJWTError, ValueError) as exc:
        logger.warning(
            "authorizer rejected request",
            reason=type(exc).__name__,
            request_id=request_id,
        )
        raise Exception("Unauthorized") from None
    except Exception as exc:  # fail closed; the class name is all we log
        logger.error(
            "authorizer failed closed on unexpected error",
            reason=type(exc).__name__,
            request_id=request_id,
        )
        raise Exception("Unauthorized") from None

    logger.info("authorizer allowed request", sub=sub, request_id=request_id)
    return _allow(sub, scope, method_arn)


__all__ = ["handle", "validate"]
