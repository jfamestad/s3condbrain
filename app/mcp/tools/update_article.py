"""``update_article`` — ``wiki.write`` · ``write`` (HANDOFF §10.10).

Sequence: validate → grant check on the path → mint a WRITE credential for the exact
key → ``GetObject`` → compare ``if_version`` → ``PutObject`` with ``If-Match``. A
stale token fails **without writing** and returns the current state (§5.1); losing
the race at S3 (412) re-reads and reports the same way.

Frontmatter, when given, replaces the stored block entirely except for the
server-maintained ``seq`` (§10.15). Omit it to leave the block alone.

The parent listing is refreshed after **every** successful write, not only when a
projected field changed: the listing carries the child's ETag (its ``version``, and
the self-heal comparison key — §8.6), and every write changes that.
"""

from __future__ import annotations

from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import MAX_ARTICLE_BYTES, RESERVED_TYPES, SCOPE_WRITE
from app.errors import ToolError, conflict, forbidden, not_found
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_PATH_SCHEMA,
    FRONTMATTER_SCHEMA,
    KIND_WRITE,
    VERSION_SCHEMA,
    WRITE_RESULT_SCHEMA,
    article_path,
    content_arg,
    metadata,
    require_grant,
    revalidate_stored,
    serialize_article,
    store,
    validate_frontmatter,
    version_arg,
)
from app.mcp.tools._links import check_link
from app.mcp.tools._listings import refresh_parent
from app.storage.articles import AccessDenied, ArticleStore, PreconditionFailed, StoredObject
from app.storage.listings import ListingChild, basename
from app.storage.markdown import parse

DESCRIPTION = (
    "Replace the body, and optionally the frontmatter, of an existing article. Requires "
    "the version from your most recent read; a mismatch writes nothing and returns the "
    "current state. To link a shared folder or article into your tree, create an "
    "article with type 'link' and 'link_to'."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "content", "if_version"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "content": {"type": "string", "maxLength": MAX_ARTICLE_BYTES},
        "if_version": VERSION_SCHEMA,
        "frontmatter": {
            **FRONTMATTER_SCHEMA,
            "description": (
                "Omit to leave frontmatter unchanged. When given it replaces the "
                "previous block entirely, except for server-maintained fields."
            ),
        },
    },
}

# The §10.14 envelope carries the current body "up to the response cap" (§6.3).
CURRENT_BODY_CAP = 100_000


def _stale(current: StoredObject) -> ToolError:
    body = parse(current.body).body
    return conflict(
        "The article changed since you read it. Merge your edit into current_body "
        "and retry with if_version = current_version.",
        current_version=current.version,
        current_body=body[:CURRENT_BODY_CAP],
    )


def _live(st: ArticleStore, s3: Any, path: str) -> StoredObject:
    """The current object, or the §10.10 error for why it cannot be updated.

    ``s3`` is the WRITE credential for exactly this key, so S3's 403 on a missing
    key (no ``s3:ListBucket`` — §8.5) is read as absence: 404, not 403.
    """
    current = st.get(s3, path, absent_on_denied=True)
    if current is None:
        raise not_found()
    if parse(current.body).type in RESERVED_TYPES:
        # A pointer or tombstone: nothing live to update (§5.2, §5.3).
        raise not_found()
    return current


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = article_path(args.get("path"))
    content = content_arg(args)
    if_version = version_arg(args)
    replacement = args.get("frontmatter")
    if replacement is not None:
        replacement = dict(validate_frontmatter(replacement, drop_seq=True))

    require_grant(ctx, path, Permission.WRITE)

    s3 = ctx.minter.s3(ctx.subject, Shape.WRITE, path)
    st = store(ctx)
    current = _live(st, s3, path)
    if current.version != if_version:
        raise _stale(current)

    stored = parse(current.body)
    # A kept block is re-checked against §10.2 exactly as a supplied one is.
    frontmatter = replacement if replacement is not None else revalidate_stored(stored.frontmatter)
    check_link(frontmatter)
    frontmatter["seq"] = stored.seq + 1
    body = serialize_article(frontmatter, content)
    try:
        written = st.put_if_match(s3, path, body, current.etag, metadata(ctx, KIND_WRITE))
    except PreconditionFailed:
        # Lost the race between our read and our write: report what is there now.
        raise _stale(_live(st, s3, path)) from None
    except AccessDenied:
        raise forbidden() from None
    refresh_parent(
        ctx, path, ListingChild.article(basename(path), written.etag, len(body), frontmatter)
    )
    return {"path": path, "version": written.version, "seq": frontmatter["seq"]}


TOOL = Tool(
    name="update_article",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=WRITE_RESULT_SCHEMA,
    scope=SCOPE_WRITE,
    handler=handle,
)

__all__ = ["TOOL", "handle"]
