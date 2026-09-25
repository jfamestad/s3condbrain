"""Links — a mount as a symlink (s3condbrain S7).

A link is an ordinary article whose frontmatter carries ``type: link`` and
``link_to``: a local article or folder path, or an ``https://<host>/a/<article
path>`` reference on another instance (§7). It is deliberately *not* a reserved
type, so listings, search, archive, unarchive, move and history treat it like any
other article.

A link grants nothing. ``read_article`` answers one with a ``link`` reference and
stops — one hop, never followed by the server, never resolved through — and the
caller's next call is authorized against the target like any other (D8, D10).
"""

from __future__ import annotations

from typing import Any

from app.errors import ToolError, bad_request
from app.mcp.tools._common import article_path, folder_path
from app.mcp.tools.resolve_reference import parse_reference

TYPE_LINK = "link"
LINK_TO = "link_to"

LINK_NOTE = (
    "This is a link. Access is checked at the target: call list_folder for a folder "
    "or read_article for an article. If you cannot reach it, the link is unresolved — "
    "ask the owner of the target, or archive the link."
)

_BAD_TARGET = (
    "'frontmatter.link_to' must be an absolute article path ('/racing/setup.md'), a "
    "folder path ('/racing'), or a reference 'https://<host>/a/<article path>'."
)


def link_target(value: Any) -> str:
    """Validate a link target and return it unchanged.

    Args:
        value: The ``link_to`` value from frontmatter.

    Returns:
        The target: a local article path, a local folder path, or an https reference.

    Raises:
        ToolError: 400 when the value is none of those.
    """
    if not isinstance(value, str) or not value:
        raise bad_request(_BAD_TARGET)
    if value.startswith("https://"):
        try:
            parse_reference(value)
        except ToolError:
            raise bad_request(_BAD_TARGET) from None
        return value
    for check in (article_path, folder_path):
        try:
            return check(value, "frontmatter.link_to")
        except ToolError:
            continue
    raise bad_request(_BAD_TARGET)


def is_local(target: str) -> bool:
    """A target on this instance (a path) rather than a reference to another.

    Args:
        target: A validated ``link_to`` value.

    Returns:
        True for a local path, False for an https reference.
    """
    return target.startswith("/")


def check_link(frontmatter: dict[str, Any]) -> None:
    """``link_to`` is required on a link and forbidden on anything else.

    Args:
        frontmatter: The validated frontmatter about to be written.

    Raises:
        ToolError: 400 naming the field.
    """
    if frontmatter.get("type") == TYPE_LINK:
        link_target(frontmatter.get(LINK_TO))
    elif LINK_TO in frontmatter:
        raise bad_request("'frontmatter.link_to' is only allowed with 'type: link'.")


def link_reference(path: str, frontmatter: dict[str, Any]) -> dict[str, Any]:
    """What ``read_article`` returns in place of content for a link.

    Args:
        path: The link's own path.
        frontmatter: The link's stored frontmatter.

    Returns:
        The one-hop ``link`` reference; the target is not read.
    """
    return {
        "kind": "link",
        "path": path,
        "link_to": str(frontmatter.get(LINK_TO, "")),
        "note": LINK_NOTE,
    }


LINK_REFERENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind", "path", "link_to"],
    "properties": {
        "kind": {"const": "link"},
        "path": {"type": "string"},
        "link_to": {
            "type": "string",
            "maxLength": 2048,
            "description": (
                "Target path (folder or article) on this instance, or an absolute "
                "reference URL on another."
            ),
        },
        "note": {"type": "string"},
    },
    "description": (
        "Returned in place of content when the path holds a link. The server does not "
        "follow it; the caller does, and that call is authorized against the target."
    ),
}

__all__ = [
    "LINK_NOTE",
    "LINK_REFERENCE_SCHEMA",
    "LINK_TO",
    "TYPE_LINK",
    "check_link",
    "is_local",
    "link_reference",
    "link_target",
]
