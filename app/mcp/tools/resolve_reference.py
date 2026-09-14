"""``resolve_reference`` — no scope · no grant (HANDOFF §7, §10.6, §10.15).

Parse a wiki URL and say whether it can be read from here. **Reads nothing** — not
across instances, and not locally either. It does not touch S3, does not consult the
grant store, does not follow pointers, and returns the same answer to every caller,
so it reveals nothing about the tree: a foreign URL gets the same reply from anyone.

The reference grammar (§7)::

    https://wiki.acme.com/a/standards/torque-spec.md
    └──────────┬─────────┘│└───────────┬────────────┘
          root pointer    │       resource path
                          └── article namespace

The MCP endpoint for any reference is ``{root}/mcp``. Resolution happens on the
client, by choosing a connector; this tool only tells it which one.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.errors import ToolError, bad_request
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import article_path

DESCRIPTION = (
    "Parse a wiki URL and say whether it can be read from here. Reads nothing — not "
    "across instances, and not locally either; it does not touch storage, does not "
    "follow pointers, and returns the same answer to every caller. Resolution happens "
    "on the client, by choosing a connector. Call this before following a link found "
    "inside an article."
)

URL_MAX_LENGTH = 2048
ARTICLE_NAMESPACE = "/a/"
MCP_SUFFIX = "/mcp"

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["url"],
    "properties": {"url": {"type": "string", "format": "uri", "maxLength": URL_MAX_LENGTH}},
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["url", "root", "path", "local", "resolvable"],
    "properties": {
        "url": {"type": "string"},
        "root": {"type": "string", "description": "Instance root, e.g. 'https://wiki.acme.com'."},
        "path": {"type": "string", "description": "Article path within that instance."},
        "local": {"type": "boolean", "description": "True when the root is this instance."},
        "resolvable": {
            "type": "boolean",
            "description": (
                "True only when local. A foreign reference is resolvable by the agent if "
                "it holds a connector for that instance, which this server cannot know."
            ),
        },
        "mcp_endpoint": {"type": "string", "description": "'{root}/mcp' — the connector to use."},
        "note": {"type": "string"},
    },
}

LOCAL_NOTE = "This reference is on this instance. Read it with read_article."
FOREIGN_NOTE = (
    "Use the connector for {root} if you hold one; otherwise this is an unresolved citation."
)

_NOT_A_REFERENCE = (
    "'url' is not a wiki reference. Expected 'https://<host>/a/<article path>', "
    "e.g. 'https://wiki.acme.com/a/standards/torque-spec.md'."
)


def local_root(settings: Settings) -> str:
    """This instance's root pointer: ``canonical_mcp_url`` without its ``/mcp`` suffix."""
    url = settings.canonical_mcp_url.rstrip("/")
    return url[: -len(MCP_SUFFIX)] if url.endswith(MCP_SUFFIX) else url


def parse_reference(url: Any) -> tuple[str, str]:
    """Split a reference into ``(root, article path)``.

    The host is lowercased (DNS is case-insensitive); the path is not (§3.1 — paths
    are rejected, never normalised). Query and fragment are ignored.

    Raises:
        ToolError: 400 when ``url`` is not an ``https`` URL whose path lies in the
            article namespace and matches the article grammar.
    """
    if not isinstance(url, str) or not url or len(url) > URL_MAX_LENGTH:
        raise bad_request("'url' is required and must be a string of at most 2048 characters.")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or "@" in parts.netloc:
        raise bad_request(_NOT_A_REFERENCE)
    if not parts.path.startswith(ARTICLE_NAMESPACE):
        raise bad_request(_NOT_A_REFERENCE)
    try:
        path = article_path(parts.path[len(ARTICLE_NAMESPACE) - 1 :], "url")
    except ToolError:
        raise bad_request(_NOT_A_REFERENCE) from None
    return f"https://{parts.netloc.lower()}", path


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    url = args.get("url")
    root, path = parse_reference(url)
    local = root == local_root(ctx.settings)
    return {
        "url": url,
        "root": root,
        "path": path,
        "local": local,
        "resolvable": local,
        "mcp_endpoint": f"{root}{MCP_SUFFIX}",
        "note": LOCAL_NOTE if local else FOREIGN_NOTE.format(root=root),
    }


TOOL = Tool(
    name="resolve_reference",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=None,
    handler=handle,
)

__all__ = ["TOOL", "handle", "local_root", "parse_reference"]
