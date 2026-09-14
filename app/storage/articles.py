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

from botocore.exceptions import ClientError

from app.config import ARTICLE_PREFIX

META_ACTOR = "actor"
META_KIND = "kind"
META_MOVED_FROM = "moved-from"

CONTENT_TYPE = "text/markdown; charset=utf-8"

_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NotFound", "404"})
_DENIED_CODES = frozenset({"AccessDenied", "403"})
# S3 answers 412 when the condition fails outright and 409 ConditionalRequestConflict
# when another conditional write on the same key is in flight. Both mean "the write
# did not land; re-read before retrying", which is what PreconditionFailed conveys.
_PRECONDITION_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict", "412"})


class PreconditionFailed(Exception):
    """S3 returned 412: the ``If-Match`` / ``If-None-Match`` condition did not hold."""


class AccessDenied(Exception):
    """S3 returned 403 under the minted credential. Tools map this to a plain 403
    or, for reads, to 404 (absence and denial are indistinguishable — §10.1)."""


@dataclass
class StoredObject:
    """One object version as read from or written to S3.

    Attributes:
        key: Full S3 key (``a/...``).
        etag: ETag exactly as S3 returned it (quotes included).
        body: Object bytes — empty for ``head`` and listing entries.
        size: Object size in bytes (``ContentLength`` / listing ``Size``), when known.
        version_id: S3 version id, when the response carried one.
        last_modified: When the response carried one.
        metadata: User metadata (``actor``, ``kind``, ...) — empty for listing entries,
            which never carry it (§8.6).
    """

    key: str
    etag: str
    body: bytes = b""
    size: int | None = None
    version_id: str | None = None
    last_modified: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def version(self) -> str:
        """The ``if_version`` token: ETag with surrounding quotes stripped (§10.1)."""
        return self.etag.strip('"')

    @property
    def path(self) -> str:
        """Absolute article path for this key."""
        return path_for(self.key)


def key_for(path: str) -> str:
    """``/racing/x.md`` → ``a/racing/x.md``."""
    return ARTICLE_PREFIX + path.lstrip("/")


def path_for(key: str) -> str:
    """``a/racing/x.md`` → ``/racing/x.md``."""
    return "/" + key[len(ARTICLE_PREFIX) :] if key.startswith(ARTICLE_PREFIX) else "/" + key


def folder_prefix(folder_path: str) -> str:
    """``/racing`` → ``a/racing/``; ``/`` → ``a/``. The ``ListObjectsV2`` prefix."""
    trimmed = folder_path.strip("/")
    return ARTICLE_PREFIX if not trimmed else f"{ARTICLE_PREFIX}{trimmed}/"


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _status(error: ClientError) -> int:
    return int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def _is_not_found(error: ClientError) -> bool:
    return _code(error) in _NOT_FOUND_CODES or _status(error) == 404


def _is_denied(error: ClientError) -> bool:
    return _code(error) in _DENIED_CODES or _status(error) == 403


def _is_precondition(error: ClientError) -> bool:
    return _code(error) in _PRECONDITION_CODES or _status(error) == 412


def _quoted(etag: str) -> str:
    """S3 accepts ``If-Match`` with or without quotes; always send it quoted."""
    return etag if etag.startswith('"') else f'"{etag}"'


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
        key = key_for(path)
        try:
            response = s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                return None
            if _is_denied(error):
                raise AccessDenied(path) from error
            raise
        body = response["Body"].read()
        return StoredObject(
            key=key,
            etag=response["ETag"],
            body=body,
            size=int(response.get("ContentLength", len(body))),
            version_id=response.get("VersionId"),
            last_modified=response.get("LastModified"),
            metadata=dict(response.get("Metadata") or {}),
        )

    def get_version(self, s3: Any, path: str, version_id: str) -> StoredObject | None:
        """``GetObject?versionId=``. ``None`` on 404 / NoSuchVersion.

        Raises:
            AccessDenied: on 403.
        """
        key = key_for(path)
        try:
            response = s3.get_object(Bucket=self.bucket, Key=key, VersionId=version_id)
        except ClientError as error:
            if _is_not_found(error) or _code(error) in ("NoSuchVersion", "InvalidArgument"):
                return None
            if _is_denied(error):
                raise AccessDenied(path) from error
            raise
        body = response["Body"].read()
        return StoredObject(
            key=key,
            etag=response["ETag"],
            body=body,
            size=int(response.get("ContentLength", len(body))),
            version_id=response.get("VersionId"),
            last_modified=response.get("LastModified"),
            metadata=dict(response.get("Metadata") or {}),
        )

    def head_version(self, s3: Any, path: str, version_id: str) -> StoredObject | None:
        """``HeadObject?versionId=`` — metadata for one historical version (§8.3).
        Increment C implements this."""
        raise NotImplementedError

    def list_versions(
        self, s3: Any, path: str, *, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[StoredObject], str | None]:
        """``ListObjectVersions`` for exactly one key, newest first (§8.3, §10.7).

        Returns ``(entries, next_cursor)``. Entries carry ``version_id``, ``etag``,
        ``size``, ``last_modified`` and **no** metadata — callers ``head_version``
        each entry for actor/kind. Delete markers are ignored (nothing writes them).
        Increment C implements this.
        """
        raise NotImplementedError

    def head(self, s3: Any, path: str) -> StoredObject | None:
        """``HeadObject`` — etag and metadata only; ``None`` on 404.

        Raises:
            AccessDenied: on 403.
        """
        key = key_for(path)
        try:
            response = s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                return None
            if _is_denied(error):
                raise AccessDenied(path) from error
            raise
        return StoredObject(
            key=key,
            etag=response["ETag"],
            size=int(response["ContentLength"]) if "ContentLength" in response else None,
            version_id=response.get("VersionId"),
            last_modified=response.get("LastModified"),
            metadata=dict(response.get("Metadata") or {}),
        )

    def put_new(self, s3: Any, path: str, body: bytes, metadata: dict[str, str]) -> StoredObject:
        """``PutObject`` with ``If-None-Match: *``.

        Raises:
            PreconditionFailed: when anything already occupies the key (any live version).
            AccessDenied: on 403.
        """
        return self._put(s3, path, body, metadata, IfNoneMatch="*")

    def put_if_match(
        self, s3: Any, path: str, body: bytes, etag: str, metadata: dict[str, str]
    ) -> StoredObject:
        """``PutObject`` with ``If-Match: <etag>``.

        Raises:
            PreconditionFailed: when the current ETag differs.
            AccessDenied: on 403.
        """
        return self._put(s3, path, body, metadata, IfMatch=_quoted(etag))

    def _put(
        self, s3: Any, path: str, body: bytes, metadata: dict[str, str], **condition: str
    ) -> StoredObject:
        key = key_for(path)
        try:
            response = s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=CONTENT_TYPE,
                Metadata=metadata,
                **condition,
            )
        except ClientError as error:
            # ``If-Match`` on an absent key is 404 NoSuchKey: the condition did not
            # hold either, and the caller's re-read will find nothing there.
            if _is_precondition(error) or (condition and _is_not_found(error)):
                raise PreconditionFailed(path) from error
            if _is_denied(error):
                raise AccessDenied(path) from error
            raise
        return StoredObject(
            key=key,
            etag=response["ETag"],
            body=body,
            size=len(body),
            version_id=response.get("VersionId"),
            metadata=dict(metadata),
        )

    def list_children(self, s3: Any, folder_path: str) -> tuple[list[str], list[StoredObject]]:
        """``ListObjectsV2`` with ``Prefix`` + ``Delimiter="/"``.

        Returns:
            ``(folder_paths, articles)``. Folders are absolute paths without a trailing
            slash. Articles are ``StoredObject`` entries carrying ``key``, ``etag``,
            ``size`` and ``last_modified`` only — no body, no metadata (a listing never
            returns user metadata, §8.6). Keys whose last segment begins with ``_``
            (``_listing.json`` and friends) and keys not ending in ``.md`` are skipped.
            Pagination is followed. The skeleton's ``list_folder`` uses this directly
            (§11.4); increment A adds the projection.

            The stub declared ``list[str]`` for articles; ``list_folder`` needs the
            ETag (``version``) and size for each summary, so entries are returned
            whole rather than as bare paths.

        Raises:
            AccessDenied: on 403.
        """
        prefix = folder_prefix(folder_path)
        folders: list[str] = []
        articles: list[StoredObject] = []
        paginator = s3.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
                for common in page.get("CommonPrefixes") or []:
                    child = common["Prefix"]
                    name = child[len(prefix) :].rstrip("/")
                    if not name or name.startswith("_"):
                        continue
                    folders.append(path_for(child).rstrip("/"))
                for item in page.get("Contents") or []:
                    key = item["Key"]
                    name = key[len(prefix) :]
                    if not name or name.startswith("_") or not name.endswith(".md"):
                        continue
                    articles.append(
                        StoredObject(
                            key=key,
                            etag=item.get("ETag", ""),
                            size=int(item.get("Size", 0)),
                            last_modified=item.get("LastModified"),
                        )
                    )
        except ClientError as error:
            if _is_denied(error):
                raise AccessDenied(folder_path) from error
            raise
        return folders, articles


__all__ = [
    "CONTENT_TYPE",
    "META_ACTOR",
    "META_KIND",
    "META_MOVED_FROM",
    "AccessDenied",
    "ArticleStore",
    "PreconditionFailed",
    "StoredObject",
    "folder_prefix",
    "key_for",
    "path_for",
]
