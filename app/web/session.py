"""Signed session cookie and CSRF token (HANDOFF §4.9 cookie row, §11.6).

The session is a signed, not encrypted, cookie: ``<base64url(json)>.<hmac-sha256>``.
It carries the subject, display name, expiry and the profile's ``session_epoch`` at
issue — nothing secret. The signing key comes from Secrets Manager
(``app.web.secrets``). Revocation is prospective (§12.9): a disabled user is refused
on the next request because ``app.web.app`` checks the PROFILE status per request,
and the same read refuses a cookie whose epoch is behind the profile's — "Sign out
everywhere" (§11.6) bumps the epoch. The cookie itself is only a proof of login.

CSRF: every state-changing form carries ``HMAC(key, "csrf:" + session signature)``;
the POST handler compares it against the same derivation. A token is therefore bound
to one session and cannot be replayed across logins.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

SESSION_COOKIE = "__Host-wiki_session"
OAUTH_COOKIE = "__Host-wiki_oauth"


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    display_name: str
    expires_at: int
    epoch: int = 0  # the profile's session_epoch when issued; 0 for cookies from before
    signature: str = ""  # the cookie's HMAC; the CSRF token derives from it

    @property
    def actor(self) -> str:
        return f"human:{self.subject}"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def encode(key: bytes, data: dict[str, object]) -> str:
    """Serialise and sign any JSON-able mapping."""
    payload = _b64e(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    return f"{payload}.{_sign(key, payload)}"


def decode(key: bytes, token: str, *, now: float | None = None) -> dict[str, object] | None:
    """Verify signature and ``exp``; ``None`` on any failure."""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(key, payload)):
        return None
    try:
        data = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if not isinstance(exp, int | float) or (now if now is not None else time.time()) >= exp:
        return None
    return data


def issue_session(
    key: bytes,
    principal_subject: str,
    email: str,
    display_name: str,
    hours: int,
    epoch: int = 0,
) -> str:
    exp = int(time.time()) + hours * 3600
    return encode(
        key,
        {
            "sub": principal_subject,
            "email": email,
            "name": display_name,
            "exp": exp,
            "epoch": epoch,
        },
    )


def load_session(key: bytes, cookie: str | None) -> Principal | None:
    if not cookie:
        return None
    data = decode(key, cookie)
    if not data:
        return None
    sub = data.get("sub")
    exp = data.get("exp")
    if not isinstance(sub, str) or not sub or not isinstance(exp, int | float):
        return None
    epoch = data.get("epoch", 0)
    return Principal(
        subject=sub,
        email=str(data.get("email", "")),
        display_name=str(data.get("name", "")),
        expires_at=int(exp),
        epoch=int(epoch) if isinstance(epoch, int) and not isinstance(epoch, bool) else 0,
        signature=cookie.rsplit(".", 1)[1],
    )


def csrf_token(key: bytes, principal: Principal) -> str:
    return _sign(key, "csrf:" + principal.signature)


def csrf_valid(key: bytes, principal: Principal, submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(submitted or "", csrf_token(key, principal))


__all__ = [
    "OAUTH_COOKIE",
    "SESSION_COOKIE",
    "Principal",
    "csrf_token",
    "csrf_valid",
    "decode",
    "encode",
    "issue_session",
    "load_session",
]
