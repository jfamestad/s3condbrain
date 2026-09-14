"""``read_version`` — ``wiki.read`` · ``read`` on path (HANDOFF §10.8).

Sequence: validate → grant check on the path → mint a READ credential for the exact
key → ``GetObject?versionId=``. Same shape as ``read_article`` but immutable and
**never a forward reference**: a pointer or archive tombstone version is history and
comes back exactly as stored, frontmatter and body, with no conversion. Attribution
(``actor``, ``at``) comes from the object's metadata and timestamp, never from the
frontmatter (§8.2).

A version id that belongs to another key is a ``404`` from S3, which is the §10.8
"version does not belong to that path" case with nothing extra to check. A subject
without ``read`` gets ``404`` too, as does a 403 from S3 (§10.1, §10.15).

``byte_range`` follows ``read_article``: inclusive offsets into the body, clamped to
its end, decoded with replacement when a boundary splits a character.
"""

from __future__ import annotations

from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ
from app.errors import ToolError, bad_request, not_found
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ACTOR_SCHEMA,
    ARTICLE_PATH_SCHEMA,
    FRONTMATTER_SCHEMA,
    article_path,
    store,
)
from app.mcp.tools.list_versions import ACTOR_UNKNOWN, VERSION_ID_MAX_LENGTH, VERSION_ID_SCHEMA
from app.storage.articles import META_ACTOR
from app.storage.markdown import parse

DESCRIPTION = (
    "One historical version, pinned by its version_id. Same shape as read_article "
    "but immutable and never a forward reference."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "version_id"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "version_id": VERSION_ID_SCHEMA,
        "byte_range": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {"type": "integer", "minimum": 0},
            "description": "Inclusive [start, end] byte offsets into the body.",
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "path",
        "version_id",
        "frontmatter",
        "content",
        "actor",
        "at",
        "total_bytes",
        "returned_bytes",
        "truncated",
    ],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "version_id": VERSION_ID_SCHEMA,
        "seq": {"type": "integer"},
        "frontmatter": FRONTMATTER_SCHEMA,
        "content": {"type": "string"},
        "actor": ACTOR_SCHEMA,
        "at": {"type": "string", "format": "date-time"},
        "total_bytes": {"type": "integer"},
        "returned_bytes": {"type": "integer"},
        "truncated": {"type": "boolean"},
    },
}


def _version_id_arg(args: dict[str, Any]) -> str:
    value = args.get("version_id")
    if not isinstance(value, str) or not value or len(value) > VERSION_ID_MAX_LENGTH:
        raise bad_request("'version_id' is required: pass a version_id from list_versions.")
    return value


def _byte_range_arg(args: dict[str, Any]) -> tuple[int, int] | None:
    value = args.get("byte_range")
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value)
        or value[0] > value[1]
    ):
        raise bad_request("'byte_range' must be [start, end] with 0 <= start <= end.")
    return value[0], value[1]


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = article_path(args.get("path"))
    version_id = _version_id_arg(args)
    byte_range = _byte_range_arg(args)

    try:
        ctx.require(path, Permission.READ)
    except ToolError as error:
        if error.status == 403:
            raise not_found() from None
        raise

    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    # A READ credential for exactly this key: S3's 403 on a missing key or version
    # is absence (§8.5), and the caller sees 404 either way.
    stored = store(ctx).get_version(s3, path, version_id, absent_on_denied=True)
    if stored is None:
        raise not_found("No such version at this path.")

    article = parse(stored.body)
    body_bytes = article.body.encode("utf-8")
    total = len(body_bytes)
    if byte_range is not None:
        start, end = byte_range
        chunk = body_bytes[start : end + 1]
        content = chunk.decode("utf-8", errors="replace")
        returned = len(chunk)
    else:
        content = article.body
        returned = total

    result: dict[str, Any] = {
        "path": path,
        "version_id": stored.version_id or version_id,
        "seq": article.seq,
        "frontmatter": article.frontmatter,
        "content": content,
        "actor": stored.metadata.get(META_ACTOR) or ACTOR_UNKNOWN,
        "total_bytes": total,
        "returned_bytes": returned,
        "truncated": returned < total,
    }
    if stored.last_modified is not None:
        result["at"] = stored.last_modified.isoformat()
    return result


TOOL = Tool(
    name="read_version",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_READ,
    handler=handle,
)

__all__ = ["TOOL", "handle"]
