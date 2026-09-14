"""``archive_article`` — ``wiki.write`` · ``write`` (HANDOFF §5.2, §10.12).

Sequence: validate → grant check on the path → mint a WRITE credential for the exact
key → ``GetObject`` → compare ``if_version`` → ``PutObject`` tombstone with
``If-Match`` → refresh the parent listing so the child leaves it (§8.3).

The tombstone is the next version of the same key: the current frontmatter with
``type: archived``, ``seq`` incremented and ``archived_from_seq`` recording the
version it retired, an empty body, and ``kind: archive`` in object metadata. Nothing
is deleted and no delete marker is written (§13.2) — prior versions stay reachable
to anyone holding ``read`` on the path, and ``unarchive_article`` restores the last
content version by writing it again on top.

This module also holds the helpers the lifecycle trio shares (archive, unarchive,
move): the stale-``if_version`` 409, the live-content read and a log shim. Listing
upkeep goes through ``_listings.refresh_parent`` like every other mutating tool.
"""

from __future__ import annotations

from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import RESERVED_TYPES, SCOPE_WRITE
from app.errors import ToolError, conflict, forbidden, not_found
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_PATH_SCHEMA,
    KIND_ARCHIVE,
    TYPE_ARCHIVED,
    VERSION_SCHEMA,
    article_path,
    metadata,
    store,
    version_arg,
)
from app.mcp.tools._listings import refresh_parent
from app.mcp.tools.update_article import CURRENT_BODY_CAP
from app.storage.articles import AccessDenied, ArticleStore, PreconditionFailed, StoredObject
from app.storage.markdown import parse, serialize

DESCRIPTION = (
    "Remove an article from listings and search. Nothing is destroyed — prior versions "
    "stay reachable to anyone who can read that path."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "if_version"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "if_version": VERSION_SCHEMA,
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "version", "seq", "archived"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "version": VERSION_SCHEMA,
        "seq": {"type": "integer"},
        "archived": {"const": True},
    },
}

# Frontmatter key on a tombstone naming the ``seq`` it retired (§5.2).
ARCHIVED_FROM_SEQ = "archived_from_seq"


# ---------------------------------------------------------------------------
# Helpers shared by archive / unarchive / move
# ---------------------------------------------------------------------------


def log_event(ctx: ToolContext, level: str, event: str, **fields: Any) -> None:
    """One Powertools-style structured line; a no-op when the context has no logger."""
    if ctx.log is None:
        return
    getattr(ctx.log, level)(event, request_id=ctx.request_id, subject=ctx.subject, **fields)


def stale(current: StoredObject) -> ToolError:
    """The §10.14 conflict for a stale ``if_version``: current version and body."""
    return conflict(
        "The article changed since you read it. Re-read, then retry with "
        "if_version = current_version.",
        current_version=current.version,
        current_body=parse(current.body).body[:CURRENT_BODY_CAP],
    )


def live(st: ArticleStore, s3: Any, path: str) -> StoredObject:
    """The current content object at ``path``, or the error for why there is none.

    ``s3`` must be the WRITE credential for exactly this key: S3's 403 on a missing
    key (no ``s3:ListBucket`` — §8.5) is then absence, and reads as 404.

    Raises:
        ToolError: 404 when nothing is there or the top version is a pointer or a
            tombstone.
    """
    current = st.get(s3, path, absent_on_denied=True)
    if current is None:
        raise not_found()
    if parse(current.body).type in RESERVED_TYPES:
        raise not_found()
    return current


# ---------------------------------------------------------------------------
# archive_article
# ---------------------------------------------------------------------------


def _tombstone(current: StoredObject) -> tuple[dict[str, Any], bytes]:
    """Frontmatter and bytes of the tombstone that retires ``current``."""
    stored = parse(current.body)
    frontmatter = dict(stored.frontmatter)
    frontmatter["type"] = TYPE_ARCHIVED
    frontmatter[ARCHIVED_FROM_SEQ] = stored.seq
    frontmatter["seq"] = stored.seq + 1
    return frontmatter, serialize(frontmatter, "")


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = article_path(args.get("path"))
    if_version = version_arg(args)

    ctx.require(path, Permission.WRITE)

    s3 = ctx.minter.s3(ctx.subject, Shape.WRITE, path)
    st = store(ctx)
    current = live(st, s3, path)
    if current.version != if_version:
        raise stale(current)

    frontmatter, body = _tombstone(current)
    try:
        written = st.put_if_match(s3, path, body, current.etag, metadata(ctx, KIND_ARCHIVE))
    except PreconditionFailed:
        # Lost the race between our read and our write: report what is there now.
        raise stale(live(st, s3, path)) from None
    except AccessDenied:
        raise forbidden() from None

    refresh_parent(ctx, path, None)  # the child leaves its folder's listing (§8.3)
    return {"path": path, "version": written.version, "seq": frontmatter["seq"], "archived": True}


TOOL = Tool(
    name="archive_article",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_WRITE,
    handler=handle,
)

__all__ = ["ARCHIVED_FROM_SEQ", "TOOL", "handle", "live", "log_event", "stale"]
