"""``read_article`` — ``wiki.read`` · ``read`` (HANDOFF §10.5).

Sequence: validate → grant check on the path → mint a READ credential for the exact
key → ``GetObject``. A ``type: pointer`` body becomes a forward reference and a
``type: archived`` body a 404 (§8.3) — recognised now so increment B changes no read
path.

A subject without ``read`` gets ``404``, as does a 403 from S3: absence and denial
are indistinguishable (§10.1, §12.8 item 5). §10.5's error line says ``403 without
read``; the zero-grant security test and §10.1 say a path you may not see looks like
no path, and that is what ships. The pointer ``note`` still tells a caller who
lands on a 404 after a move to ask an owner (§5.3).

The 403 is also what real S3 answers for a *missing* key under a credential without
``s3:ListBucket`` (§8.5), which a READ credential for one key is — so the read asks
``ArticleStore`` to treat it as absence (``absent_on_denied``).
"""

from __future__ import annotations

import re
from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ
from app.errors import ToolError, bad_request, not_found
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_PATH_SCHEMA,
    FORWARD_REFERENCE_SCHEMA,
    FRONTMATTER_SCHEMA,
    SECTION_MAX_LENGTH,
    TRUST_SCHEMA,
    TYPE_ARCHIVED,
    TYPE_POINTER,
    VERSION_SCHEMA,
    article_path,
    require_grant,
    store,
    trust_of,
)
from app.mcp.tools._links import LINK_REFERENCE_SCHEMA, TYPE_LINK, link_reference
from app.storage.markdown import Article, parse

DESCRIPTION = (
    "Current live version of one article: frontmatter plus body. When the path holds "
    "a move pointer or a link, returns a reference instead of content — one hop; the "
    "server does not follow it, the caller does, and the next call is authorized "
    "against the target."
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "section": {
            "type": "string",
            "maxLength": SECTION_MAX_LENGTH,
            "description": (
                "Return only the body under this markdown heading, matched "
                "case-insensitively on the heading text."
            ),
        },
        "byte_range": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {"type": "integer", "minimum": 0},
            "description": (
                "Inclusive [start, end] byte offsets into the body. Ignored when "
                "'section' is given."
            ),
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "required": [
                "path",
                "frontmatter",
                "content",
                "version",
                "total_bytes",
                "returned_bytes",
                "truncated",
            ],
            "properties": {
                "path": ARTICLE_PATH_SCHEMA,
                "frontmatter": FRONTMATTER_SCHEMA,
                "content": {"type": "string"},
                "version": VERSION_SCHEMA,
                "trust": TRUST_SCHEMA,
                "total_bytes": {"type": "integer"},
                "returned_bytes": {"type": "integer"},
                "truncated": {"type": "boolean"},
            },
        },
        FORWARD_REFERENCE_SCHEMA,
        LINK_REFERENCE_SCHEMA,
    ]
}

POINTER_NOTE = "This article moved. You may not have access at its new location — ask an owner."

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(```|~~~)")


def _section_arg(args: dict[str, Any]) -> str | None:
    value = args.get("section")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > SECTION_MAX_LENGTH:
        raise bad_request("'section' must be a non-empty heading text.")
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


def section_span(body: str, heading: str) -> tuple[int, int] | None:
    """Character offsets ``(start, end)`` of a section in ``body``: from its heading
    line up to the next heading of the same or higher level, or the end of the body.
    Headings inside fenced code blocks are ignored. Matched case-insensitively on
    the heading text; ``None`` when no heading matches.
    """
    wanted = heading.strip().casefold()
    in_fence = False
    start: int | None = None
    level = 0
    offset = 0
    for line in body.splitlines(keepends=True):
        line_start, offset = offset, offset + len(line)
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line.rstrip("\r\n"))
        if match is None:
            continue
        depth = len(match.group(1))
        if start is None:
            if match.group(2).strip().casefold() == wanted:
                start, level = line_start, depth
        elif depth <= level:
            return start, line_start
    return None if start is None else (start, len(body))


def extract_section(body: str, heading: str) -> str | None:
    """The heading line and everything beneath it, up to the next heading of the
    same or higher level (``section_span``); ``None`` when no heading matches.
    """
    span = section_span(body, heading)
    return None if span is None else body[span[0] : span[1]]


def _forward_reference(article: Article) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "forward_reference",
        "moved_to": str(article.frontmatter.get("moved_to", "")),
        "note": POINTER_NOTE,
    }
    moved_at = article.frontmatter.get("moved_at")
    if moved_at is not None:
        result["moved_at"] = str(moved_at)
    return result


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = article_path(args.get("path"))
    section = _section_arg(args)
    byte_range = _byte_range_arg(args)

    try:
        require_grant(ctx, path, Permission.READ)
    except ToolError as error:
        if error.status == 403:
            raise not_found() from None
        raise

    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    current = store(ctx).get(s3, path, absent_on_denied=True)
    if current is None:
        raise not_found()

    article = parse(current.body)
    if article.type == TYPE_POINTER:
        return _forward_reference(article)
    if article.type == TYPE_LINK:
        return link_reference(path, article.frontmatter)
    if article.type == TYPE_ARCHIVED:
        raise not_found()

    body_bytes = article.body.encode("utf-8")
    total = len(body_bytes)
    if section is not None:
        piece = extract_section(article.body, section)
        if piece is None:
            raise not_found(
                f"No heading matching '{section}' in {path}. Read without 'section' "
                "to see the headings."
            )
        content = piece
        returned = len(piece.encode("utf-8"))
    elif byte_range is not None:
        start, end = byte_range
        chunk = body_bytes[start : end + 1]
        content = chunk.decode("utf-8", errors="replace")
        returned = len(chunk)
    else:
        content = article.body
        returned = total

    return {
        "path": path,
        "frontmatter": article.frontmatter,
        "content": content,
        "version": current.version,
        "trust": trust_of(article.frontmatter),
        "total_bytes": total,
        "returned_bytes": returned,
        "truncated": returned < total,
    }


TOOL = Tool(
    name="read_article",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_READ,
    handler=handle,
)

__all__ = ["TOOL", "extract_section", "handle", "section_span"]
