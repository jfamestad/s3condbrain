"""``search_and_read`` — ``wiki.read`` · ``read`` on each hit (search, then read).

Calls the ``search`` handler, then the ``read_article`` handler for each of the top
``k`` hits, in search order. Nothing here touches S3 or the grant store directly:
every read is authorized, minted and audited exactly as a separate ``read_article``
call would be, so a batch can never return what individual reads would refuse. A
link or move pointer comes back as the read's one-hop reference and is not
followed.

Failures are per item: a ``ToolError`` from one read (a missing section, an article
gone since the search) becomes that item's ``error`` and the others are unaffected.

Content is held to ``max_bytes`` in total, counted in UTF-8 bytes. The item that
crosses the budget is cut on a character boundary and marked ``truncated``; items
after it are not read at all and carry ``skipped: "budget"`` with their summary, so
the agent can read them individually.
"""

from __future__ import annotations

from typing import Any

from app.config import SCOPE_READ
from app.errors import ToolError, bad_request
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools import read_article, search
from app.mcp.tools._common import (
    ARTICLE_SUMMARY_SCHEMA,
    FOLDER_PATH_SCHEMA,
    FORWARD_REFERENCE_SCHEMA,
    SECTION_MAX_LENGTH,
    VERSION_SCHEMA,
)
from app.mcp.tools._links import LINK_REFERENCE_SCHEMA

DESCRIPTION = (
    "Search, then return the top hits' content in the same call — use when you'd "
    "otherwise search and then read the best result. Each read is checked exactly as "
    "read_article; links and moved pages come back as references, not followed."
)

DEFAULT_K = 3
MAX_K = 10
DEFAULT_MAX_BYTES = 60_000
#: Keeps the result inside the 25k-token tool budget.
MAX_MAX_BYTES = 100_000

SKIPPED_BUDGET = "budget"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": search.QUERY_MAX_LENGTH},
        "prefix": {
            **FOLDER_PATH_SCHEMA,
            "description": "Restrict to this folder and everything beneath it.",
        },
        "k": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_K,
            "default": DEFAULT_K,
            "description": "How many of the top hits to read.",
        },
        "section": {
            "type": "string",
            "maxLength": SECTION_MAX_LENGTH,
            "description": (
                "Read only the body under this markdown heading, in every hit. A hit "
                "without it gets a per-item 404."
            ),
        },
        "max_bytes": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_MAX_BYTES,
            "default": DEFAULT_MAX_BYTES,
            "description": (
                "Total content bytes across all items. The item that crosses it is "
                "truncated; later items are skipped and can be read individually."
            ),
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["results", "search_truncated"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                **ARTICLE_SUMMARY_SCHEMA,
                "properties": {
                    **ARTICLE_SUMMARY_SCHEMA["properties"],
                    "score": {"type": "number"},
                    "content": {"type": "string"},
                    "version": VERSION_SCHEMA,
                    "total_bytes": {"type": "integer"},
                    "returned_bytes": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "reference": {"oneOf": [FORWARD_REFERENCE_SCHEMA, LINK_REFERENCE_SCHEMA]},
                    "error": {
                        "type": "object",
                        "required": ["status", "code"],
                        "properties": {
                            "status": {"type": "integer"},
                            "code": {"type": "string"},
                        },
                    },
                    "skipped": {"const": SKIPPED_BUDGET},
                },
            },
        },
        "search_truncated": {"type": "boolean"},
    },
}

_READ_FIELDS = ("content", "version", "total_bytes", "returned_bytes", "truncated")


def _int_arg(args: dict[str, Any], field: str, default: int, maximum: int) -> int:
    value = args.get(field, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise bad_request(f"'{field}' must be an integer between 1 and {maximum}.")
    return value


def _section_arg(args: dict[str, Any]) -> str | None:
    value = args.get("section")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > SECTION_MAX_LENGTH:
        raise bad_request("'section' must be a non-empty heading text.")
    return value


def _cut(content: str, budget: int) -> tuple[str, int]:
    """``content`` cut to at most ``budget`` UTF-8 bytes on a character boundary,
    and its byte length."""
    piece = content.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return piece, len(piece.encode("utf-8"))


def _summary(hit: dict[str, Any]) -> dict[str, Any]:
    """A search hit's summary fields and score; the snippet is dropped."""
    return {key: value for key, value in hit.items() if key != "snippet"}


def _item(ctx: ToolContext, hit: dict[str, Any], section: str | None) -> dict[str, Any]:
    """One hit read through ``read_article``; its summary plus the read's outcome."""
    item = _summary(hit)
    read_args: dict[str, Any] = {"path": hit["path"]}
    if section is not None:
        read_args["section"] = section
    try:
        read = read_article.handle(ctx, read_args)
    except ToolError as error:
        item["error"] = {"status": error.status, "code": error.code}
        return item
    if "kind" in read:
        item["reference"] = read
        return item
    item.update({field: read[field] for field in _READ_FIELDS})
    return item


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    k = _int_arg(args, "k", DEFAULT_K, MAX_K)
    max_bytes = _int_arg(args, "max_bytes", DEFAULT_MAX_BYTES, MAX_MAX_BYTES)
    section = _section_arg(args)

    found = search.handle(
        ctx, {"query": args.get("query"), "prefix": args.get("prefix"), "limit": k}
    )

    results: list[dict[str, Any]] = []
    remaining = max_bytes
    for hit in found["hits"]:
        if remaining <= 0:
            results.append({**_summary(hit), "skipped": SKIPPED_BUDGET})
            continue
        item = _item(ctx, hit, section)
        if "content" in item:
            if item["returned_bytes"] > remaining:
                # The item that crosses the budget is cut, and spends what is left.
                item["content"], item["returned_bytes"] = _cut(item["content"], remaining)
                item["truncated"] = True
                remaining = 0
            else:
                remaining -= item["returned_bytes"]
        results.append(item)
    return {"results": results, "search_truncated": found["truncated"]}


TOOL = Tool(
    name="search_and_read",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_READ,
    handler=handle,
)

__all__ = ["TOOL", "handle"]
