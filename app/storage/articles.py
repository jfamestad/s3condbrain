"""S3 reads and conditional writes — HANDOFF §8.2, §8.3, §8.4.

This is the only package importing boto3 for S3 (§11.2). Every method takes the
client it should use; callers obtain that client from ``CredentialMinter.s3`` so
that no code path can reach an object without a minted credential.

Object metadata written on every ``PutObject`` (§8.2):

    actor        human:<subject>
    kind         write | archive | unarchive | moved_in | moved_out
    moved-from   <path>   (moved_in only)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.config import ARTICLE_PREFIX

META_ACTOR = "actor"
META_KIND = "kind"
META_MOVED_FROM = "moved-from"


class PreconditionFailed(Exception):
    """S3 returned 412: the ``If-Match`` / ``If-None-Match`` condition did not hold."""


class AccessDenied(Exception):
    """S3 returned 403 under the minted credential. Tools map this to a plain 403
    or, for reads, to 404 (absence and denial are indistinguishable — §10.1)."""


@dataclass
class StoredObject:
    """One object version as read from or written to S3."""

    key: str
    etag: str
    body: bytes = b""
    version_id: str | None = None
    last_modified: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def version(self) -> str:
        """The ``if_version`` token: ETag with surrounding quotes stripped (§10.1)."""
        return self.etag.strip('"')


def key_for(path: str) -> str:
    """``/racing/x.md`` → ``a/racing/x.md``."""
    return ARTICLE_PREFIX + path.lstrip("/")


def path_for(key: str) -> str:
    """``a/racing/x.md`` → ``/racing/x.md``."""
    return "/" + key[len(ARTICLE_PREFIX) :] if key.startswith(ARTICLE_PREFIX) else "/" + key


class ArticleStore:
    """Thin, explicit wrapper over the S3 verbs the tools need.

    Args:
        bucket: Bucket name.
    """

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    def get(self, s3: Any, path: str) -> StoredObject | None:
        """``GetObject``. Returns ``None`` on NoSuchKey / 404.

        Raises:
            AccessDenied: on 403.
        """
        raise NotImplementedError

    def head(self, s3: Any, path: str) -> StoredObject | None:
        """``HeadObject`` — etag and metadata only; ``None`` on 404."""
        raise NotImplementedError

    def put_new(self, s3: Any, path: str, body: bytes, metadata: dict[str, str]) -> StoredObject:
        """``PutObject`` with ``If-None-Match: *``.

        Raises:
            PreconditionFailed: when anything already occupies the key (any live version).
            AccessDenied: on 403.
        """
        raise NotImplementedError

    def put_if_match(
        self, s3: Any, path: str, body: bytes, etag: str, metadata: dict[str, str]
    ) -> StoredObject:
        """``PutObject`` with ``If-Match: <etag>``.

        Raises:
            PreconditionFailed: when the current ETag differs.
            AccessDenied: on 403.
        """
        raise NotImplementedError

    def list_children(self, s3: Any, folder_path: str) -> tuple[list[str], list[str]]:
        """``ListObjectsV2`` with ``Prefix`` + ``Delimiter="/"``.

        Returns:
            ``(folder_paths, article_paths)`` as absolute paths. Keys whose last
            segment begins with ``_`` are skipped. Pagination is followed. The
            skeleton's ``list_folder`` uses this directly (§11.4); increment A adds
            the projection.
        """
        raise NotImplementedError


__all__ = [
    "META_ACTOR",
    "META_KIND",
    "META_MOVED_FROM",
    "AccessDenied",
    "ArticleStore",
    "PreconditionFailed",
    "StoredObject",
    "key_for",
    "path_for",
]
