"""Credential minting — HANDOFF §4.8, §8.5, §8.8, build step 3.

The data plane holds no S3 permission of its own. Every object touch goes through a
credential minted here: ``sts:AssumeRole`` on the storage role with an inline session
policy naming only what *this operation* touches. Three shapes, defined once, each
with a negative test.

This module is the only caller of ``AssumeRole`` in the codebase (§11.2).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import boto3

from app.config import ARTICLE_PREFIX

# AssumeRole's floor; credentials live at least this long regardless of cache policy.
_DURATION_SECONDS = 900
# Never hand out a cached credential in its final minute — clock skew and in-flight
# requests both eat into it.
_EXPIRY_MARGIN_SECONDS = 60
_SESSION_NAME_PREFIX = "wiki-"
_SESSION_NAME_MAX = 64
_SESSION_NAME_BAD_CHARS = re.compile(r"[^A-Za-z0-9_=,.@-]")

_READ_OBJECT_ACTIONS = ["s3:GetObject", "s3:GetObjectVersion"]
_READ_KMS_ACTIONS = ["kms:Decrypt"]
_WRITE_OBJECT_ACTIONS = [*_READ_OBJECT_ACTIONS, "s3:PutObject"]
_WRITE_KMS_ACTIONS = [*_READ_KMS_ACTIONS, "kms:GenerateDataKey"]
_LIST_ACTIONS = ["s3:ListBucket"]
_TAG_ACTIONS = ["s3:PutObjectTagging"]
_LISTING_NAME = "_listing.json"


class Shape(StrEnum):
    """Session-policy shapes (§8.5 table)."""

    READ = "read"  # GetObject, GetObjectVersion, kms:Decrypt
    LIST = "list"  # READ + ListBucket with s3:prefix condition
    WRITE = "write"  # READ + PutObject, kms:GenerateDataKey
    MAINTAIN = "maintain"  # WRITE + LIST + PutObjectTagging, for folder listing upkeep (§8.6)


def _normalise(path: str) -> str:
    """Absolute path with any trailing slash dropped; ``/`` stays ``/``.

    Raises:
        ValueError: when ``path`` is not absolute.
    """
    if not path.startswith("/"):
        raise ValueError(f"path must be absolute: {path!r}")
    return path.rstrip("/") or "/"


def _is_article(path: str) -> bool:
    return path.endswith(".md")


def _folder_prefix(folder: str) -> str:
    """``/racing`` → ``a/racing/``; ``/`` → ``a/``. ``folder`` must be normalised."""
    if folder == "/":
        return ARTICLE_PREFIX
    return f"{ARTICLE_PREFIX}{folder[1:]}/"


def s3_key(path: str) -> str:
    """``/racing/x.md`` → ``a/racing/x.md``. Folder ``/racing`` → ``a/racing/``. Root → ``a/``."""
    p = _normalise(path)
    if _is_article(p):
        return f"{ARTICLE_PREFIX}{p[1:]}"
    return _folder_prefix(p)


def s3_resource(bucket: str, path: str) -> str:
    """Object-level resource ARN for a session policy.

    An article path yields the exact key ARN. A folder path yields ``.../a/<folder>/*``
    (root: ``.../a/*``). The scope is the *operation's target*, never the grant node.
    """
    key = s3_key(path)
    if key.endswith("/"):
        key += "*"
    return f"arn:aws:s3:::{bucket}/{key}"


def list_prefix(path: str) -> str:
    """Value for the ``s3:prefix`` condition on a folder: ``a/racing/`` (root: ``a/``).

    An article path yields its parent folder's prefix.
    """
    p = _normalise(path)
    if _is_article(p):
        p = p.rsplit("/", 1)[0] or "/"
    return _folder_prefix(p)


def listing_key(path: str) -> str:
    """The ``_listing.json`` key of the folder ``path`` names (an article path yields
    its parent folder's): ``/racing`` → ``a/racing/_listing.json``; root →
    ``a/_listing.json``."""
    return f"{list_prefix(path)}{_LISTING_NAME}"


def session_policy(shape: Shape, bucket: str, kms_key_arn: str, path: str) -> dict[str, Any]:
    """Build the inline session policy for one operation.

    Args:
        shape: Which of the shapes.
        bucket: Bucket name (not ARN).
        kms_key_arn: The one customer-managed key.
        path: Absolute article or folder path the operation targets. MAINTAIN is a
            folder shape: an article path is taken to mean its parent folder.

    Returns:
        An IAM policy document. A READ shape carries **no** ``s3:ListBucket``, only
        ``s3:ListBucketVersions`` pinned to the target — ``StringEquals`` on the exact
        key for an article, ``StringLike`` on the prefix for a folder; a LIST shape
        carries ``s3:ListBucket`` on the bucket ARN with an ``s3:prefix`` StringLike
        condition of ``list_prefix(path) + "*"``; a WRITE shape adds ``s3:PutObject``
        and ``kms:GenerateDataKey``; a MAINTAIN shape is WRITE + LIST over the folder plus
        ``s3:PutObjectTagging`` on that folder's ``_listing.json`` key only (§8.6,
        §8.9 — the listing write carries a tag, and a tagged ``PutObject`` needs the
        tagging permission too).
        Must stay well inside the 2 KB inline limit.

    Raises:
        ValueError: on an unknown shape or a relative path.
    """
    shape = Shape(shape)
    if shape is Shape.MAINTAIN:
        # Listing upkeep is a folder operation whatever path it was handed.
        path = _normalise(path)
        if _is_article(path):
            path = path.rsplit("/", 1)[0] or "/"
    writes = shape in (Shape.WRITE, Shape.MAINTAIN)
    object_actions = _WRITE_OBJECT_ACTIONS if writes else _READ_OBJECT_ACTIONS
    kms_actions = _WRITE_KMS_ACTIONS if writes else _READ_KMS_ACTIONS
    statements: list[dict[str, Any]] = [
        {"Effect": "Allow", "Action": list(object_actions), "Resource": s3_resource(bucket, path)},
        {"Effect": "Allow", "Action": list(kms_actions), "Resource": kms_key_arn},
    ]
    if shape in (Shape.LIST, Shape.MAINTAIN):
        # StringLike ``a/racing/*`` matches ``a/racing/`` itself (``*`` may be empty), so
        # delimiter listings of the folder pass, and nothing shorter or elsewhere does.
        # A request with no Prefix has no ``s3:prefix`` key and fails the condition.
        # ``ListBucketVersions`` rides along: history of anything under the folder.
        statements.append(
            {
                "Effect": "Allow",
                "Action": [*_LIST_ACTIONS, "s3:ListBucketVersions"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": [f"{list_prefix(path)}*"]}},
            }
        )
    else:
        # READ and WRITE: version history (§8.3 ``ListObjectVersions`` needs
        # ``s3:ListBucketVersions`` on the bucket). For an article the condition is an
        # exact ``StringEquals`` on the key: every version walk passes ``Prefix=key``
        # verbatim (``ArticleStore.list_versions``, ``unarchive_article``), and a
        # ``StringLike key*`` would also admit ``key.bak.md`` and ``key/…`` — sibling
        # names an article grant must not see (§4.6). A folder keeps the prefix
        # wildcard: history of anything beneath it.
        normalised = _normalise(path)
        condition: dict[str, Any] = (
            {"StringEquals": {"s3:prefix": s3_key(normalised)}}
            if _is_article(normalised)
            else {"StringLike": {"s3:prefix": [f"{list_prefix(normalised)}*"]}}
        )
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucketVersions"],
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": condition,
            }
        )
    if shape is Shape.MAINTAIN:
        statements.append(
            {
                "Effect": "Allow",
                "Action": list(_TAG_ACTIONS),
                "Resource": f"arn:aws:s3:::{bucket}/{listing_key(path)}",
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _session_name(subject: str) -> str:
    cleaned = _SESSION_NAME_BAD_CHARS.sub("-", subject)
    return f"{_SESSION_NAME_PREFIX}{cleaned}"[:_SESSION_NAME_MAX]


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
        self._role_arn = role_arn
        self._bucket = bucket
        self._kms_key_arn = kms_key_arn
        # The cache may drop a credential sooner than AWS does, never later.
        self._ttl = min(cache_seconds, _DURATION_SECONDS - _EXPIRY_MARGIN_SECONDS)
        self._sts = sts_client
        self._clock = clock
        self._cache: dict[tuple[str, Shape, str], tuple[float, boto3.Session]] = {}
        self._mint_count = 0

    @property
    def mint_count(self) -> int:
        return self._mint_count

    @property
    def _sts_client(self) -> Any:
        if self._sts is None:
            self._sts = boto3.client("sts")
        return self._sts

    def mint(self, subject: str, shape: Shape, path: str) -> boto3.Session:
        """Return a boto3 Session whose credentials are scoped by ``session_policy``.

        Cached per *(subject, shape, path)* for ``cache_seconds``. The role session
        name carries the subject so CloudTrail attributes every object touch.
        """
        shape = Shape(shape)
        key = (subject, shape, path)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        policy = session_policy(shape, self._bucket, self._kms_key_arn, path)
        response = self._sts_client.assume_role(
            RoleArn=self._role_arn,
            RoleSessionName=_session_name(subject),
            Policy=json.dumps(policy, separators=(",", ":")),
            DurationSeconds=_DURATION_SECONDS,
        )
        self._mint_count += 1
        creds = response["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        self._cache[key] = (now + self._ttl, session)
        return session

    def s3(self, subject: str, shape: Shape, path: str) -> Any:
        """``self.mint(...).client("s3")`` — the client every storage call uses."""
        return self.mint(subject, shape, path).client("s3")


__all__ = [
    "CredentialMinter",
    "Shape",
    "list_prefix",
    "listing_key",
    "s3_key",
    "s3_resource",
    "session_policy",
]
