"""``list_folder`` — ``wiki.read`` · ``read`` on path (HANDOFF §10.4).

Skeleton behaviour (§11.4): ``ListObjectsV2`` under a LIST credential scoped to the
folder, returning child folders and article summaries with ``path``, ``type``
(``doc``), ``version``, ``trust`` (``unverified``) and ``size_bytes`` — **no titles,
descriptions or tags, and no pointer/tombstone filtering**; those need per-child
metadata, which increment A supplies through the ``_listing.json`` projection.

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
    TRUST_UNVERIFIED,
    folder_path,
    store,
    summary_from,
)
from app.storage.articles import AccessDenied

DESCRIPTION = (
    "Immediate contents of one folder — child folders by name, articles as summaries. "
    "Does not recurse. Archived articles and move pointers never appear."
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
DEFAULT_TYPE = "doc"


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = folder_path(args.get("path"))

    try:
        ctx.grants.require(ctx.subject, path, Permission.READ)
    except ToolError as error:
        if error.status == 403:
            raise not_found() from None
        raise

    s3 = ctx.minter.s3(ctx.subject, Shape.LIST, path)
    try:
        folders, articles = store(ctx).list_children(s3, path)
    except AccessDenied:
        raise not_found() from None

    if not folders and not articles and path != ROOT:
        raise not_found()

    return {
        "path": path,
        "folders": folders,
        "articles": [
            summary_from(
                entry.path,
                type=DEFAULT_TYPE,
                version=entry.version,
                trust=TRUST_UNVERIFIED,
                size_bytes=entry.size,
            )
            for entry in articles
        ],
        "truncated": False,
    }


TOOL = Tool(
    name="list_folder",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_READ,
    handler=handle,
)

__all__ = ["TOOL", "handle"]
