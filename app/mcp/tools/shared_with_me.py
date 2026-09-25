"""``shared_with_me`` — ``wiki.read`` · none (s3condbrain S7).

Every grant row the caller holds: the places they can reach, and so the places they
can link into their own tree. It reads the caller's own partition of the grant table
and nothing else — no other subject's rows, no S3 — so it needs no permission beyond
a valid token.
"""

from __future__ import annotations

from typing import Any

from app.auth.types import Permission, Resolution
from app.config import SCOPE_READ
from app.mcp.protocol import Tool, ToolContext

DESCRIPTION = (
    "Everything shared with you: each folder or article you hold a grant on, with the "
    "permission and who granted it. Use it to find shares to link into your own tree "
    "(create_article with type 'link')."
)

INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["grants"],
    "properties": {
        "grants": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "permission", "folder", "granted_by", "granted_at"],
                "properties": {
                    "path": {"type": "string"},
                    "permission": {"enum": ["read", "write", "own"]},
                    "folder": {"type": "boolean"},
                    "granted_by": {"type": "string"},
                    "granted_at": {"type": "string"},
                },
            },
        }
    },
}


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    grants = sorted(ctx.grants.all_grants(ctx.subject), key=lambda g: g.node)
    if grants:
        ctx.audit.note(Resolution(Permission.READ, tuple(grants)))
    return {
        "grants": [
            {
                "path": g.node,
                "permission": g.permission.value,
                "folder": not g.is_article,
                "granted_by": g.granted_by,
                "granted_at": g.granted_at,
            }
            for g in grants
        ]
    }


TOOL = Tool(
    name="shared_with_me",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_READ,
    handler=handle,
)

__all__ = ["TOOL", "handle"]
