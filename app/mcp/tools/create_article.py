"""``create_article`` — ``wiki.write`` · ``write`` on any ancestor (HANDOFF §10.9).

Sequence: validate → grant check on the parent folder (the cascade resolves every
ancestor) → mint a WRITE credential for the exact key → ``PutObject`` with
``If-None-Match: *`` → refresh the parent folder's listing (§8.3, §8.6). The grant
check precedes minting so a subject with no grants never causes an ``AssumeRole``
(§11.3 step 9). The listing refresh runs under a MAINTAIN credential for the parent
and can never fail the write (``_listings.refresh_parent``).
"""

from __future__ import annotations

from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import MAX_ARTICLE_BYTES, SCOPE_WRITE
from app.errors import ToolError, archived, exists, forbidden, retired_pointer
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_PATH_SCHEMA,
    FRONTMATTER_SCHEMA,
    KIND_WRITE,
    TYPE_ARCHIVED,
    TYPE_POINTER,
    WRITE_RESULT_SCHEMA,
    article_path,
    content_arg,
    metadata,
    parent_folder,
    reject_reserved_name,
    require_grant,
    serialize_article,
    store,
    validate_frontmatter,
)
from app.mcp.tools._listings import refresh_parent
from app.storage.articles import AccessDenied, ArticleStore, PreconditionFailed
from app.storage.listings import ListingChild, basename
from app.storage.markdown import parse

DESCRIPTION = (
    "Create a new article. Fails if anything already occupies the path, including a "
    "move pointer or an archive tombstone — a vacated path is retired permanently, and "
    "an archived one is restored with unarchive_article rather than overwritten. "
    "Ancestor folders are created implicitly."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "content", "frontmatter"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "content": {
            "type": "string",
            "maxLength": MAX_ARTICLE_BYTES,
            "description": "Markdown body without frontmatter.",
        },
        "frontmatter": FRONTMATTER_SCHEMA,
    },
}


def _occupied(st: ArticleStore, s3: Any, path: str) -> ToolError:
    """Explain a failed ``If-None-Match: *`` by looking at what sits there (§10.9)."""
    try:
        current = st.get(s3, path)
    except AccessDenied:
        return forbidden()
    if current is None:
        # Vanished between the put and the look — nothing more precise to say.
        return exists()
    article = parse(current.body)
    if article.type == TYPE_POINTER:
        return retired_pointer()
    if article.type == TYPE_ARCHIVED:
        return archived(
            "This path holds an archived article. Call unarchive_article with this "
            "current_version, then update it.",
            current_version=current.version,
        )
    return exists("An article already exists at this path. Read it and call update_article.")


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = article_path(args.get("path"))
    reject_reserved_name(path)
    content = content_arg(args)
    frontmatter = dict(validate_frontmatter(args.get("frontmatter")))

    # The parent folder: ``write`` on any ancestor suffices (§10.9), and the cascade
    # resolves every ancestor from that one path. Recorded for the audit line (AS-10).
    require_grant(ctx, parent_folder(path), Permission.WRITE)

    frontmatter["seq"] = 1
    body = serialize_article(frontmatter, content)
    s3 = ctx.minter.s3(ctx.subject, Shape.WRITE, path)
    st = store(ctx)
    try:
        written = st.put_new(s3, path, body, metadata(ctx, KIND_WRITE))
    except PreconditionFailed:
        raise _occupied(st, s3, path) from None
    except AccessDenied:
        raise forbidden() from None
    refresh_parent(
        ctx, path, ListingChild.article(basename(path), written.etag, len(body), frontmatter)
    )
    return {"path": path, "version": written.version, "seq": 1}


TOOL = Tool(
    name="create_article",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=WRITE_RESULT_SCHEMA,
    scope=SCOPE_WRITE,
    handler=handle,
)

__all__ = ["TOOL", "handle"]
