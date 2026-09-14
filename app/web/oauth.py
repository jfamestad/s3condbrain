"""The web application as an OAuth client of AuthKit (HANDOFF §4.9, §11.6, §15.2).

Authorization code + PKCE (S256), confidential client (``client_secret_post``),
scopes ``openid profile email``. The login is proven by the **ID token**, verified
against the AuthKit JWKS with the algorithm pinned — the same four lines as the
authorizer (§11.1). The access token is discarded: the web app never calls another
service on the user's behalf; it acts under its own role with the user's grants.

``state``, the OIDC ``nonce`` and the PKCE verifier travel in a short-lived signed
cookie set when the login starts; the callback requires all three and clears it. The
ID token must echo the nonce, so a token minted for some other login attempt — or
captured and replayed — is refused even if the code exchange succeeds. Exact-match
redirect URI.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from app.config import Settings

ALGORITHMS = ["RS256"]  # never from the token header
STATE_TTL_SECONDS = 600


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str
    display_name: str


def _same(expected: str, given: str) -> bool:
    """Constant-time equality over bytes, so a non-ASCII value from the wire is a
    plain mismatch rather than a ``TypeError``."""
    return hmac.compare_digest(expected.encode(), given.encode())


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


class AuthKitClient:
    """Builds the authorize URL and completes the code exchange.

    Args:
        settings: needs ``authkit_domain``, ``jwks_url``, ``workos_client_id``,
            ``web_base_url``.
        client_secret: from Secrets Manager.
        http: injected ``httpx.Client`` for tests.
        jwks_client: injected ``jwt.PyJWKClient`` for tests.
    """

    def __init__(
        self,
        settings: Settings,
        client_secret: str,
        http: httpx.Client | None = None,
        jwks_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.client_secret = client_secret
        self.http = http or httpx.Client(timeout=15)
        self._jwks = jwks_client

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.web_base_url.rstrip('/')}/callback"

    @property
    def authorize_endpoint(self) -> str:
        return f"{self.settings.authkit_domain}/oauth2/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.settings.authkit_domain}/oauth2/token"

    def start(self) -> tuple[str, dict[str, object]]:
        """Return ``(authorize_url, cookie_payload)``. The payload is signed by the
        caller into the OAuth cookie; it holds state, nonce, verifier, expiry and
        where to return after login."""
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.settings.workos_client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = self.authorize_endpoint + "?" + urllib.parse.urlencode(params)
        payload = {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "exp": int(time.time()) + STATE_TTL_SECONDS,
        }
        return url, payload

    def finish(self, code: str, returned_state: str, cookie_payload: dict[str, object]) -> Identity:
        """Exchange the code and verify the ID token.

        Raises:
            ValueError: on any mismatch or verification failure (the caller maps it
                to a 400 page; the message is safe to show).
        """
        expected = cookie_payload.get("state")
        if not isinstance(expected, str) or not _same(expected, returned_state):
            raise ValueError("login state mismatch")
        verifier = cookie_payload.get("verifier")
        nonce = cookie_payload.get("nonce")
        if not isinstance(verifier, str) or not verifier or not isinstance(nonce, str) or not nonce:
            raise ValueError("login session incomplete")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.settings.workos_client_id,
            "client_secret": self.client_secret,
            "code_verifier": verifier,
        }
        r = self.http.post(self.token_endpoint, data=data)
        if r.status_code != 200:
            raise ValueError("token exchange refused")
        body = r.json()
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            raise ValueError("no id_token in response")
        try:
            claims = self._verify(id_token)
        except jwt.PyJWTError as e:
            raise ValueError(f"id_token rejected: {type(e).__name__}") from None
        # The nonce binds this ID token to this login attempt (OIDC Core 3.1.3.7 step
        # 11). Absent or different means the token was not minted for this cookie.
        claimed = claims.get("nonce")
        if not isinstance(claimed, str) or not _same(claimed, nonce):
            raise ValueError("id_token nonce mismatch")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise ValueError("id_token without subject")
        name = claims.get("name") or " ".join(
            p for p in (claims.get("given_name"), claims.get("family_name")) if p
        )
        return Identity(
            subject=sub,
            email=str(claims.get("email", "")),
            display_name=str(name or claims.get("email", "") or sub),
        )

    def _verify(self, id_token: str) -> dict[str, Any]:
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(self.settings.jwks_url, cache_keys=True)
        key = self._jwks.get_signing_key_from_jwt(id_token).key
        return jwt.decode(
            id_token,
            key=key,
            algorithms=ALGORITHMS,
            issuer=self.settings.authkit_domain,
            audience=self.settings.workos_client_id,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )


__all__ = ["ALGORITHMS", "STATE_TTL_SECONDS", "AuthKitClient", "Identity"]
