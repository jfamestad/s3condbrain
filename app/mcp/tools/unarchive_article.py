"""``unarchive_article`` — ``wiki.write`` · ``write`` (HANDOFF §5.2, §10.13).

Sequence: validate → grant check on the path → mint a WRITE credential for the exact
key → ``GetObject`` (must be a tombstone) → find the last content version beneath it
→ ``PutObject`` restoring version with ``If-Match: <tombstone etag>`` → refresh the
parent listing so the child returns (§8.3).

The restoring version is that content's frontmatter and body written again, ``seq``
incremented once more, with ``kind: unarchive`` in object metadata — so the chain
reads ``[…, content, archived, content]`` and both events stay visible in history.

Finding the version beneath the tombstone is a ``ListObjectVersions`` on the one key,
newest first, reading each entry until one whose ``type`` is not reserved. Ordinarily
that is the second entry; the scan is bounded so a key that somehow holds nothing but
tombstones and pointers fails clearly instead of walking forever.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import RESERVED_TYPES, SCOPE_WRITE
from app.errors import ToolError, conflict, forbidden, internal, not_found
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_PATH_SCHEMA,
    KIND_UNARCHIVE,
    TYPE_ARCHIVED,
    VERSION_SCHEMA,
    article_path,
    metadata,
    revalidate_stored,
    store,
    version_arg,
)
from app.mcp.tools._listings import refresh_parent
from app.mcp.tools.archive_article import ARCHIVED_FROM_SEQ, log_event
from app.storage.articles import (
    AccessDenied,
    ArticleStore,
    PreconditionFailed,
    StoredObject,
    key_for,
)
from app.storage.listings import ListingChild, basename
from app.storage.markdown import parse, serialize

DESCRIPTION = (
    "Restore an archived article to listings. Writes a restoring version — the last "
    "content version's body and frontmatter, seq incremented — so the chain reads "
    "[…, content, archived, content] and both events are visible in history."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "if_version": {
            **VERSION_SCHEMA,
            "description": (
                "Optional. The tombstone's version, as returned by list_versions or by "
                "the 409 from a create_article attempt on this path. Supply it to guard "
                "against a concurrent restore."
            ),
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "version", "seq", "archived"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "version": VERSION_SCHEMA,
        "seq": {"type": "integer"},
        "archived": {"const": False},
    },
}

# How many versions beneath the top one to inspect before giving up. A key's chain
# alternates content and tombstones/pointers in practice; a run of fifty reserved
# versions with no content beneath is a corrupted key, not a slow one.
VERSION_SCAN_LIMIT = 50


def _version_ids_newest_first(s3: Any, bucket: str, key: str, limit: int) -> list[str]:
    """Version ids of exactly ``key``, newest first, at most ``limit`` of them.

    ``ListObjectVersions`` is prefix-based and sorted by key then recency, so entries
    for a longer key (``a/x.md/y.md`` is a legal article under a folder named
    ``x.md``) follow ours; the walk stops at the first foreign key. Delete markers
    are skipped — nothing here writes them (§13.2).
    """
    ids: list[str] = []
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": key}
    while len(ids) < limit:
        try:
            response = s3.list_object_versions(**kwargs)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "AccessDenied":
                raise AccessDenied(key) from error
            raise
        for entry in response.get("Versions") or []:
            if entry.get("Key") != key:
                return ids
            ids.append(entry["VersionId"])
            if len(ids) >= limit:
                return ids
        if not response.get("IsTruncated"):
            return ids
        kwargs["KeyMarker"] = response.get("NextKeyMarker")
        kwargs["VersionIdMarker"] = response.get("NextVersionIdMarker")
    return ids


def last_content_version(st: ArticleStore, s3: Any, path: str) -> StoredObject | None:
    """The newest version of ``path`` whose ``type`` is not reserved, or ``None``.

    The top version is read too — a caller that already knows it is a tombstone or
    pointer pays one extra ``GetObject`` for a function that needs no caveats.

    Raises:
        AccessDenied: when the credential cannot list or read versions of the key.
    """
    key = key_for(path)
    for version_id in _version_ids_newest_first(s3, st.bucket, key, VERSION_SCAN_LIMIT + 1):
        stored = st.get_version(s3, path, version_id)
        if stored is None:
            continue
        if parse(stored.body).type not in RESERVED_TYPES:
            return stored
    return None


def _optional_version(args: dict[str, Any]) -> str | None:
    if args.get("if_version") is None:
        return None
    return version_arg(args)


def _tombstone_at(st: ArticleStore, s3: Any, path: str) -> StoredObject:
    """The current object, which must be an archive tombstone.

    ``s3`` is the WRITE credential for exactly this key, so S3's 403 on a missing
    key (no ``s3:ListBucket`` — §8.5) is absence.

    Raises:
        ToolError: 404 when the path was never used or holds anything but a
            tombstone (a live article, a move pointer).
    """
    current = st.get(s3, path, absent_on_denied=True)
    if current is None:
        raise not_found()
    if parse(current.body).type != TYPE_ARCHIVED:
        raise not_found("This path is not archived; there is nothing to restore.")
    return current


def _changed(current: StoredObject) -> ToolError:
    return conflict(
        "The path changed since you read it. Re-read, then retry with "
        "if_version = current_version.",
        current_version=current.version,
    )


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = article_path(args.get("path"))
    if_version = _optional_version(args)

    ctx.require(path, Permission.WRITE)

    s3 = ctx.minter.s3(ctx.subject, Shape.WRITE, path)
    st = store(ctx)
    tombstone = _tombstone_at(st, s3, path)
    if if_version is not None and tombstone.version != if_version:
        raise _changed(tombstone)

    try:
        content = last_content_version(st, s3, path)
    except AccessDenied:
        raise forbidden() from None
    if content is None:
        log_event(ctx, "error", "unarchive_no_content_version", path=path)
        raise internal(
            f"No content version was found beneath the tombstone at {path} within the "
            f"last {VERSION_SCAN_LIMIT} versions. Ask an owner to inspect the history."
        )

    restored = parse(content.body)
    # The restored block is re-checked against §10.2 before it goes back on top.
    frontmatter = revalidate_stored(
        {k: v for k, v in restored.frontmatter.items() if k != ARCHIVED_FROM_SEQ}
    )
    frontmatter["seq"] = parse(tombstone.body).seq + 1
    body = serialize(frontmatter, restored.body)
    try:
        written = st.put_if_match(s3, path, body, tombstone.etag, metadata(ctx, KIND_UNARCHIVE))
    except PreconditionFailed:
        # Someone restored or re-archived it between our read and our write.
        now = st.get(s3, path, absent_on_denied=True)
        if now is None:
            raise not_found() from None
        raise _changed(now) from None
    except AccessDenied:
        raise forbidden() from None

    # The child returns to its folder's listing (§8.3).
    refresh_parent(
        ctx, path, ListingChild.article(basename(path), written.etag, len(body), frontmatter)
    )
    return {"path": path, "version": written.version, "seq": frontmatter["seq"], "archived": False}


TOOL = Tool(
    name="unarchive_article",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_WRITE,
    handler=handle,
)

__all__ = ["TOOL", "VERSION_SCAN_LIMIT", "handle", "last_content_version"]
