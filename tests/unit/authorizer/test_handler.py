"""Authorizer tests — HANDOFF §11.3 step 5.

Rejection tests come first. A validator that accepts everything passes every
happy-path test, so the happy paths prove nothing until the rejections do.

Tokens are signed locally with an RSA key generated per module; a fake JWKS
client hands the handler the matching public key so nothing touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.authorizer import handler
from app.config import Settings

ISSUER = "https://test.authkit.app"
AUDIENCE = "https://wiki-dev.example.com/mcp"
METHOD_ARN = "arn:aws:execute-api:us-west-2:123456789012:abc123/dev/POST/mcp"
KID = "key-1"
REQUEST_ID = "11111111-2222-3333-4444-555555555555"


# --------------------------------------------------------------------------- helpers


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pem(key: RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user_01ABC",
        "iat": now - 10,
        "exp": now + 300,
        "scope": "wiki.read wiki.write",
    }
    claims.update(overrides)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    return claims


def _sign(key: RSAPrivateKey, claims: dict[str, Any], kid: str = KID) -> str:
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def _event(
    authorization: str | None,
    *,
    header_name: str = "Authorization",
    multi_only: bool = False,
) -> dict[str, Any]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    multi: dict[str, list[str]] = {"Content-Type": ["application/json"]}
    if authorization is not None:
        if not multi_only:
            headers[header_name] = authorization
        multi[header_name] = [authorization]
    return {
        "type": "REQUEST",
        "methodArn": METHOD_ARN,
        "resource": "/mcp",
        "path": "/mcp",
        "httpMethod": "POST",
        "headers": headers,
        "multiValueHeaders": multi,
        "requestContext": {"requestId": REQUEST_ID, "path": "/dev/mcp"},
    }


class _SigningKey:
    def __init__(self, key: Any) -> None:
        self.key = key


class FakeJWKSClient:
    """Stands in for ``jwt.PyJWKClient``: returns one key regardless of ``kid``."""

    def __init__(self, key: Any) -> None:
        self._key = key
        self.calls: list[str] = []

    def get_signing_key_from_jwt(self, token: str) -> _SigningKey:
        self.calls.append(token)
        return _SigningKey(self._key)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def rsa_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_rsa_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks(rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeJWKSClient]:
    """Fake JWKS client wired into the handler's singleton getter."""
    handler._reset()
    fake = FakeJWKSClient(rsa_key.public_key())
    monkeypatch.setattr(handler, "_get_jwks_client", lambda: fake)
    yield fake
    handler._reset()


@pytest.fixture
def good_token(rsa_key: RSAPrivateKey) -> str:
    return _sign(rsa_key, _claims())


def _assert_unauthorized(event: dict[str, Any]) -> None:
    with pytest.raises(Exception, match=r"^Unauthorized$") as info:
        handler.handle(event, None)
    assert type(info.value) is Exception
    assert str(info.value) == "Unauthorized"


# --------------------------------------------------------------------------- rejections


def test_wrong_audience_is_unauthorized(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    token = _sign(rsa_key, _claims(aud="https://wiki-prod.example.com/mcp"))
    _assert_unauthorized(_event(f"Bearer {token}"))


def test_multi_valued_audience_is_unauthorized(
    jwks: FakeJWKSClient, rsa_key: RSAPrivateKey, settings: Settings
) -> None:
    # AS-4: aud is the canonical URL byte for byte, never a list containing it.
    token = _sign(rsa_key, _claims(aud=[settings.canonical_mcp_url, "https://other/mcp"]))
    _assert_unauthorized(_event(f"Bearer {token}"))
    token = _sign(rsa_key, _claims(aud=[settings.canonical_mcp_url]))
    _assert_unauthorized(_event(f"Bearer {token}"))


def test_alg_none_is_unauthorized(jwks: FakeJWKSClient) -> None:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT", "kid": KID}).encode())
    payload = _b64url(json.dumps(_claims()).encode())
    for token in (f"{header}.{payload}.", f"{header}.{payload}"):
        _assert_unauthorized(_event(f"Bearer {token}"))


def test_expired_is_unauthorized(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    now = int(time.time())
    token = _sign(rsa_key, _claims(iat=now - 600, exp=now - 60))
    _assert_unauthorized(_event(f"Bearer {token}"))


def test_missing_exp_is_unauthorized(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    token = _sign(rsa_key, _claims(exp=None))
    _assert_unauthorized(_event(f"Bearer {token}"))


def test_wrong_issuer_is_unauthorized(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    token = _sign(rsa_key, _claims(iss="https://evil.authkit.app"))
    _assert_unauthorized(_event(f"Bearer {token}"))


def test_wrong_signing_key_is_unauthorized(
    jwks: FakeJWKSClient, other_rsa_key: RSAPrivateKey
) -> None:
    token = _sign(other_rsa_key, _claims())
    _assert_unauthorized(_event(f"Bearer {token}"))


def test_tampered_payload_is_unauthorized(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    header, _, signature = _sign(rsa_key, _claims()).split(".")
    payload = _b64url(json.dumps(_claims(sub="someone_else")).encode())
    _assert_unauthorized(_event(f"Bearer {header}.{payload}.{signature}"))


def test_hs256_with_public_key_as_secret_is_unauthorized(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Algorithm confusion: attacker HMACs the token with the public key PEM.

    The JWKS client hands back the PEM string, so if the handler ever took the
    algorithm from the token header this token would verify.
    """
    handler._reset()
    pem = _pem(rsa_key)
    monkeypatch.setattr(handler, "_get_jwks_client", lambda: FakeJWKSClient(pem))
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    payload = _b64url(json.dumps(_claims()).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    mac = hmac.new(pem.encode("ascii"), signing_input, hashlib.sha256).digest()
    token = f"{header}.{payload}.{_b64url(mac)}"
    # Sanity: the MAC is correct for the confused key, so only the pinned
    # algorithm list stands between this token and an Allow. (PyJWT >= 2.10
    # also refuses PEM as an HMAC secret; that is a second line, not the first.)
    assert hmac.compare_digest(
        hmac.new(pem.encode("ascii"), signing_input, hashlib.sha256).digest(), mac
    )
    _assert_unauthorized(_event(f"Bearer {token}"))


def test_rs256_alg_header_still_rejected_if_key_is_wrong_type(
    rsa_key: RSAPrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An RS256 token must still fail when the JWKS hands back an unusable key."""
    handler._reset()
    monkeypatch.setattr(handler, "_get_jwks_client", lambda: FakeJWKSClient("not-a-key"))
    _assert_unauthorized(_event(f"Bearer {_sign(rsa_key, _claims())}"))


def test_missing_authorization_header_is_unauthorized(jwks: FakeJWKSClient) -> None:
    _assert_unauthorized(_event(None))
    assert jwks.calls == []


def test_missing_headers_entirely_is_unauthorized(jwks: FakeJWKSClient) -> None:
    event = _event(None)
    event["headers"] = None
    event["multiValueHeaders"] = None
    _assert_unauthorized(event)
    event.pop("headers")
    event.pop("multiValueHeaders")
    _assert_unauthorized(event)


@pytest.mark.parametrize(
    "value",
    [
        "Basic xyz",
        "Bearer",
        "Bearer ",
        "Bearer  ",
        "bearer",
        "Token abc.def.ghi",
        "abc.def.ghi",
        "",
        "   ",
    ],
)
def test_malformed_authorization_header_is_unauthorized(jwks: FakeJWKSClient, value: str) -> None:
    _assert_unauthorized(_event(value))
    assert jwks.calls == []


def test_bearer_with_embedded_whitespace_is_unauthorized(
    jwks: FakeJWKSClient, good_token: str
) -> None:
    _assert_unauthorized(_event(f"Bearer  {good_token}"))
    _assert_unauthorized(_event(f"Bearer {good_token} extra"))
    _assert_unauthorized(_event(f"Bearer\t{good_token}"))


def test_missing_sub_is_unauthorized(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    _assert_unauthorized(_event(f"Bearer {_sign(rsa_key, _claims(sub=None))}"))


@pytest.mark.parametrize("sub", ["", 12345, ["user_01ABC"], {"id": "x"}])
def test_bad_sub_is_unauthorized(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey, sub: Any) -> None:
    _assert_unauthorized(_event(f"Bearer {_sign(rsa_key, _claims(sub=sub))}"))


@pytest.mark.parametrize("scope", [42, {"wiki.read": True}, ["wiki.read", 7]])
def test_malformed_scope_claim_is_unauthorized(
    jwks: FakeJWKSClient, rsa_key: RSAPrivateKey, scope: Any
) -> None:
    _assert_unauthorized(_event(f"Bearer {_sign(rsa_key, _claims(scope=scope))}"))


def test_missing_method_arn_is_unauthorized(jwks: FakeJWKSClient, good_token: str) -> None:
    event = _event(f"Bearer {good_token}")
    del event["methodArn"]
    _assert_unauthorized(event)


def test_jwks_client_failure_is_unauthorized(
    good_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JWKS outage or unknown kid fails closed, not open."""
    handler._reset()

    class Broken:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            raise jwt.PyJWKClientError("jwks unavailable")

    monkeypatch.setattr(handler, "_get_jwks_client", lambda: Broken())
    _assert_unauthorized(_event(f"Bearer {good_token}"))


def test_unexpected_exception_is_unauthorized(
    good_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler._reset()

    class Exploding:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            raise RuntimeError("boom")

    monkeypatch.setattr(handler, "_get_jwks_client", lambda: Exploding())
    _assert_unauthorized(_event(f"Bearer {good_token}"))


# --------------------------------------------------------------------------- the four lines


def _forged(alg: str, rsa_key: RSAPrivateKey) -> str:
    """A token whose header claims ``alg`` with a plausible signature for it."""
    if alg == "RS256":
        return _sign(rsa_key, _claims())
    header = _b64url(json.dumps({"alg": alg, "typ": "JWT", "kid": KID}).encode())
    payload = _b64url(json.dumps(_claims()).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    if alg == "none":
        return f"{header}.{payload}."
    mac = hmac.new(_pem(rsa_key).encode("ascii"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(mac)}"


@pytest.mark.parametrize("alg", ["RS256", "HS256", "HS512", "none", "ES256", "PS256"])
def test_decode_pins_algorithm_regardless_of_token_header(
    jwks: FakeJWKSClient,
    rsa_key: RSAPrivateKey,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    alg: str,
) -> None:
    """AS-4 / §11.1: the algorithm list is a literal, never the token header.

    Whatever ``alg`` the token claims, the handler must hand PyJWT ``["RS256"]``
    with the pinned issuer, audience and required claims. Only the RS256 token
    is expected to be accepted; the rest must be rejected *and* must have been
    decoded with the pinned list, not the header's.
    """
    seen: dict[str, Any] = {}
    real_decode = jwt.decode

    def spy(token: str, *args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_decode(token, *args, **kwargs)

    monkeypatch.setattr(handler.jwt, "decode", spy)
    token = _forged(alg, rsa_key)
    event = _event(f"Bearer {token}")

    if alg == "RS256":
        assert handler.handle(event, None)["principalId"] == "user_01ABC"
    else:
        _assert_unauthorized(event)

    assert seen, "jwt.decode was never reached"
    assert seen["algorithms"] == ["RS256"]
    assert seen["issuer"] == settings.authkit_domain == ISSUER
    assert seen["audience"] == settings.canonical_mcp_url == AUDIENCE
    assert set(seen["options"]["require"]) >= {"exp", "iss", "aud", "sub"}


def test_algorithm_constant_is_the_literal_rs256() -> None:
    assert handler.ALGORITHMS == ["RS256"]


def test_validate_rejects_wrong_audience_directly(
    rsa_key: RSAPrivateKey, settings: Settings
) -> None:
    client = FakeJWKSClient(rsa_key.public_key())
    with pytest.raises(jwt.InvalidAudienceError):
        handler.validate(_sign(rsa_key, _claims(aud="https://other/mcp")), settings, client)


# --------------------------------------------------------------------------- happy paths


def test_happy_path_string_scope(jwks: FakeJWKSClient, good_token: str) -> None:
    result = handler.handle(_event(f"Bearer {good_token}"), None)

    assert result == {
        "principalId": "user_01ABC",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {"Action": "execute-api:Invoke", "Effect": "Allow", "Resource": METHOD_ARN}
            ],
        },
        "context": {"sub": "user_01ABC", "scope": "wiki.read wiki.write"},
    }
    assert jwks.calls == [good_token]


def test_happy_path_list_scope(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    token = _sign(rsa_key, _claims(scope=["wiki.read", "wiki.write"]))
    result = handler.handle(_event(f"Bearer {token}"), None)
    assert result["context"] == {"sub": "user_01ABC", "scope": "wiki.read wiki.write"}
    assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"


def test_happy_path_no_scope_claim(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    token = _sign(rsa_key, _claims(scope=None))
    result = handler.handle(_event(f"Bearer {token}"), None)
    assert result["context"] == {"sub": "user_01ABC", "scope": ""}


def test_scp_claim_is_accepted_as_fallback(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    token = _sign(rsa_key, _claims(scope=None, scp=["wiki.read"]))
    result = handler.handle(_event(f"Bearer {token}"), None)
    assert result["context"]["scope"] == "wiki.read"


def test_scope_whitespace_is_normalised(jwks: FakeJWKSClient, rsa_key: RSAPrivateKey) -> None:
    token = _sign(rsa_key, _claims(scope="  wiki.read   wiki.write "))
    result = handler.handle(_event(f"Bearer {token}"), None)
    assert result["context"]["scope"] == "wiki.read wiki.write"


def test_context_values_are_strings(jwks: FakeJWKSClient, good_token: str) -> None:
    result = handler.handle(_event(f"Bearer {good_token}"), None)
    assert all(isinstance(v, str) for v in result["context"].values())
    assert isinstance(result["principalId"], str)


@pytest.mark.parametrize("header_name", ["Authorization", "authorization", "AUTHORIZATION"])
def test_header_name_is_case_insensitive(
    jwks: FakeJWKSClient, good_token: str, header_name: str
) -> None:
    result = handler.handle(_event(f"Bearer {good_token}", header_name=header_name), None)
    assert result["principalId"] == "user_01ABC"


def test_bearer_scheme_is_case_insensitive(jwks: FakeJWKSClient, good_token: str) -> None:
    result = handler.handle(_event(f"bearer {good_token}"), None)
    assert result["principalId"] == "user_01ABC"
    result = handler.handle(_event(f"BEARER {good_token}"), None)
    assert result["principalId"] == "user_01ABC"


def test_header_only_in_multi_value_headers(jwks: FakeJWKSClient, good_token: str) -> None:
    result = handler.handle(_event(f"Bearer {good_token}", multi_only=True), None)
    assert result["principalId"] == "user_01ABC"


def test_duplicate_authorization_headers_are_unauthorized(
    jwks: FakeJWKSClient, good_token: str
) -> None:
    event = _event(None)
    event["multiValueHeaders"]["Authorization"] = [f"Bearer {good_token}", "Bearer other"]
    _assert_unauthorized(event)


# --------------------------------------------------------------------------- logging


def _rendered_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every rejection record, rendered by the handler's own JSON formatter."""
    fmt = handler.logger.logger_handler
    return [fmt.format(r) for r in caplog.records if r.name.startswith("wiki-authorizer")]


def test_rejection_log_never_contains_the_token(
    jwks: FakeJWKSClient, rsa_key: RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    token = _sign(rsa_key, _claims(aud="https://wrong/mcp"))
    header_value = f"Bearer {token}"
    with caplog.at_level(logging.DEBUG):
        _assert_unauthorized(_event(header_value))

    assert caplog.records, "a rejection must be logged"
    lines = _rendered_lines(caplog)
    assert lines
    for rec, line in zip(caplog.records, lines, strict=False):
        blob = line + rec.getMessage() + repr(rec.__dict__)
        assert token not in blob
        assert header_value not in blob
        for part in token.split("."):
            assert part not in blob
    assert any("InvalidAudienceError" in line for line in lines)
    assert any(REQUEST_ID in line for line in lines)


def test_rejection_log_for_unexpected_error_never_contains_the_token(
    good_token: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handler._reset()

    class Exploding:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            raise RuntimeError(f"leaky message with {token}")

    monkeypatch.setattr(handler, "_get_jwks_client", lambda: Exploding())
    with caplog.at_level(logging.DEBUG):
        _assert_unauthorized(_event(f"Bearer {good_token}"))

    for rec, line in zip(caplog.records, _rendered_lines(caplog), strict=False):
        blob = line + rec.getMessage() + repr(rec.__dict__)
        assert good_token not in blob
        assert "leaky message" not in blob


def test_success_log_never_contains_the_token(
    jwks: FakeJWKSClient, good_token: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        handler.handle(_event(f"Bearer {good_token}"), None)
    for rec, line in zip(caplog.records, _rendered_lines(caplog), strict=False):
        assert good_token not in line + rec.getMessage() + repr(rec.__dict__)


# --------------------------------------------------------------------------- singleton


def test_jwks_client_singleton_is_built_from_settings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler._reset()
    built: list[tuple[str, dict[str, Any]]] = []

    class Recorder:
        def __init__(self, uri: str, **kwargs: Any) -> None:
            built.append((uri, kwargs))

    monkeypatch.setattr(handler.jwt, "PyJWKClient", Recorder)
    a = handler._get_jwks_client()
    b = handler._get_jwks_client()
    assert a is b
    # No per-key cache: it never expires, so a rotated key would verify for the
    # container's life. The JWK-set cache (300 s, refresh on unknown kid) is enough.
    assert built == [(settings.jwks_url, {})]
    assert settings.jwks_url == "https://test.authkit.app/oauth2/jwks"
    handler._reset()
    handler._get_jwks_client()
    assert len(built) == 2
    handler._reset()
