"""The web function's one secret (HANDOFF §12.3 "no secrets in files").

One Secrets Manager JSON secret holds ``client_secret`` (the AuthKit confidential
client) and ``session_key`` (cookie signing). Fetched once per container.

There is deliberately no WorkOS management API key here or anywhere (§11.6 "Adding a
person"): people are created in the WorkOS dashboard, and the console only records
them. The client secret can do nothing but exchange this client's own login codes.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

import boto3


@dataclass(frozen=True)
class WebSecrets:
    client_secret: str
    session_key: bytes


_cache: WebSecrets | None = None


def _reset() -> None:
    global _cache
    _cache = None


def load(secret_arn: str, client: Any | None = None) -> WebSecrets:
    """Fetch and cache. With an empty ARN (unit tests, local runs) the values come
    from ``WEB_DEV_SECRETS`` — a JSON string — or are empty."""
    global _cache
    if _cache is not None:
        return _cache
    if not secret_arn:
        raw = os.environ.get("WEB_DEV_SECRETS", "{}")
        data = json.loads(raw)
    else:
        sm = client or boto3.client("secretsmanager")
        data = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
    key = data.get("session_key", "")
    key_bytes = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)) if key else b""
    if len(key_bytes) < 32:
        raise RuntimeError("session_key must be at least 32 random bytes, base64url")
    _cache = WebSecrets(
        client_secret=str(data.get("client_secret", "")),
        session_key=key_bytes,
    )
    return _cache


__all__ = ["WebSecrets", "load"]
