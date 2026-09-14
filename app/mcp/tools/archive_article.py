"""``archive_article`` — placeholder until its increment lands (HANDOFF §10)."""

from __future__ import annotations

from typing import Any

from app.errors import internal
from app.mcp.protocol import Tool, ToolContext


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    raise internal("archive_article is not implemented in this build.")


TOOL = Tool(
    name="archive_article",
    description="Remove an article from listings and search. Nothing is destroyed.",
    input_schema={"type": "object", "properties": {}},
    output_schema=None,
    scope="wiki.write",
    handler=handle,
)
