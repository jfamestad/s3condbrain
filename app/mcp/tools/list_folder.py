"""``list_folder`` — ``wiki.read`` · ``read`` on path (HANDOFF §10.4).

Renders one folder from its ``_listing.json`` projection (§8.6): child folders that
hold at least one visible child, articles as full summaries — titles, descriptions,
tags, status, trust — with pointers and archive tombstones already filtered out by
the projection.

The read self-heals: the listing's ``(name, etag)`` set is compared with a live
``ListObjectsV2`` and rebuilt on mismatch. A rebuild is persisted only when the
caller holds ``write`` on the folder (the credential is then MAINTAIN, which can put
the tagged listing); a reader's rebuild is computed in memory and returned.

A subject without ``read`` on the folder gets ``404``, never ``403`` (§10.4, §10.15):
a ``403`` would confirm the folder exists.
"""

from __future__ import annotations

from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ
from app.errors import ToolError, not_found
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_SUMMARY_SCHEMA,
    FOLDER_PATH_SCHEMA,
    folder_path,
    summary_from,
)
from app.mcp.tools._links import is_local, link_target
from app.mcp.tools._listings import index
from app.storage.articles import AccessDenied
from app.storage.listings import Listing, ListingChild, is_stale, join

DESCRIPTION = (
    "Immediate contents of one folder — child folders by name, articles as summaries. "
    "Does not recurse. Links show their target and whether you can reach it. "
    "Archived articles and move pointers never appear."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path"],
    "properties": {"path": FOLDER_PATH_SCHEMA},
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "folders", "articles", "truncated"],
    "properties": {
        "path": FOLDER_PATH_SCHEMA,
        "folders": {"type": "array", "items": FOLDER_PATH_SCHEMA},
        "articles": {"type": "array", "items": ARTICLE_SUMMARY_SCHEMA},
        "truncated": {"type": "boolean"},
    },
}

ROOT = "/"


def summary_of(folder: str, child: ListingChild) -> dict[str, Any]:
    """An ``article_summary`` (§10.2) for one projected article child."""
    return summary_from(
        join(folder, child.name),
        type=child.type,
        version=child.etag,
        trust=child.trust,
        size_bytes=child.size,
        seq=child.seq,
        title=child.title,
        description=child.description,
        tags=list(child.tags) if child.tags else None,
        status=child.status,
        stale=is_stale(child.stale_after),
        link_to=child.link_to,
    )


def render(folder: str, listing: Listing) -> dict[str, Any]:
    """The §10.4 result for a listing: visible folders as paths, articles as summaries."""
    return {
        "path": folder,
        "folders": sorted(join(folder, c.name) for c in listing.folders if c.visible),
        "articles": [summary_of(folder, c) for c in sorted(listing.articles, key=lambda c: c.name)],
        "truncated": False,
    }


def _reachable(ctx: ToolContext, target: str) -> bool:
    """Whether the caller holds ``read`` on a local link target — a grant lookup only.

    Args:
        ctx: The calling context.
        target: A stored ``link_to`` value, which a raw write may have left malformed.

    Returns:
        True when the caller can read the target; False otherwise, including when the
        stored target is not a valid path.
    """
    try:
        link_target(target)
    except ToolError:
        return False
    return ctx.grants.resolve(ctx.subject, target).allows(Permission.READ)


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = folder_path(args.get("path"))

    try:
        resolution = ctx.require(path, Permission.READ)
    except ToolError as error:
        if error.status == 403:
            raise not_found() from None
        raise

    writable = resolution.allows(Permission.WRITE)
    s3 = ctx.minter.s3(ctx.subject, Shape.MAINTAIN if writable else Shape.LIST, path)
    try:
        listing = index(ctx).read_or_rebuild(s3, path, writable=writable)
    except AccessDenied:
        raise not_found() from None

    result = render(path, listing)
    for summary in result["articles"]:
        target = summary.get("link_to")
        if target is not None and is_local(target):
            # A grant lookup only: nothing is read at the target, and no access
            # decision is taken, so nothing is recorded for the audit line.
            summary["resolved"] = _reachable(ctx, target)
    if path != ROOT and not result["folders"] and not result["articles"]:
        # Nothing visible here: a folder that does not exist, or one holding only
        # pointers and tombstones. Both read as absence (§10.4).
        raise not_found()
    return result


TOOL = Tool(
    name="list_folder",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_READ,
    handler=handle,
)

__all__ = ["TOOL", "handle", "render", "summary_of"]
