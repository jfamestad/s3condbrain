"""Credential minting — HANDOFF §4.8, §8.5, §8.8, build step 3.

The data plane holds no S3 permission of its own. Every object touch goes through a
credential minted here: ``sts:AssumeRole`` on the storage role with an inline session
policy naming only what *this operation* touches. Three shapes, defined once, each
with a negative test.

This module is the only caller of ``AssumeRole`` in the codebase (§11.2).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum
from typing import Any

import boto3


class Shape(str, Enum):
    """Session-policy shapes (§8.5 table)."""

    READ = "read"  # GetObject, GetObjectVersion, kms:Decrypt
    LIST = "list"  # READ + ListBucket with s3:prefix condition
    WRITE = "write"  # READ + PutObject, kms:GenerateDataKey


def s3_key(path: str) -> str:
    """``/racing/x.md`` → ``a/racing/x.md``. Folder ``/racing`` → ``a/racing/``. Root → ``a/``."""
    raise NotImplementedError


def s3_resource(bucket: str, path: str) -> str:
    """Object-level resource ARN for a session policy.

    An article path yields the exact key ARN. A folder path yields ``.../a/<folder>/*``
    (root: ``.../a/*``). The scope is the *operation's target*, never the grant node.
    """
    raise NotImplementedError


def list_prefix(path: str) -> str:
    """Value for the ``s3:prefix`` condition on a folder: ``a/racing/`` (root: ``a/``)."""
    raise NotImplementedError


def session_policy(shape: Shape, bucket: str, kms_key_arn: str, path: str) -> dict[str, Any]:
    """Build the inline session policy for one operation.

    Args:
        shape: Which of the three shapes.
        bucket: Bucket name (not ARN).
        kms_key_arn: The one customer-managed key.
        path: Absolute article or folder path the operation targets.

    Returns:
        An IAM policy document. A READ shape carries **no** ``s3:ListBucket``; a LIST
        shape carries it on the bucket ARN with an ``s3:prefix`` StringLike condition
        of ``list_prefix(path) + "*"`` (and ``""``/the exact prefix for delimiter
        listings); a WRITE shape adds ``s3:PutObject`` and ``kms:GenerateDataKey``.
        Must stay well inside the 2 KB inline limit.
    """
    raise NotImplementedError


class CredentialMinter:
    """Mints and caches per-*(subject, shape, path)* credentials.

    Args:
        role_arn: The storage role.
        bucket: Bucket name.
        kms_key_arn: Key ARN.
        cache_seconds: Cache lifetime — a config value, never a constant (AS-6 note).
        sts_client: Injected for tests; defaults to ``boto3.client("sts")``.
        clock: Injected monotonic clock for cache-expiry tests.

    Attributes:
        mint_count: Number of ``AssumeRole`` calls made. The zero-grant security test
            asserts this stays at 0 for a subject with no grants (§12.8 item 5).
    """

    def __init__(
        self,
        role_arn: str,
        bucket: str,
        kms_key_arn: str,
        cache_seconds: int = 900,
        sts_client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        raise NotImplementedError

    @property
    def mint_count(self) -> int:
        raise NotImplementedError

    def mint(self, subject: str, shape: Shape, path: str) -> boto3.Session:
        """Return a boto3 Session whose credentials are scoped by ``session_policy``.

        Cached per *(subject, shape, path)* for ``cache_seconds``. The role session
        name carries the subject so CloudTrail attributes every object touch.
        """
        raise NotImplementedError

    def s3(self, subject: str, shape: Shape, path: str) -> Any:
        """``self.mint(...).client("s3")`` — the client every storage call uses."""
        raise NotImplementedError


__all__ = [
    "CredentialMinter",
    "Shape",
    "list_prefix",
    "s3_key",
    "s3_resource",
    "session_policy",
]
