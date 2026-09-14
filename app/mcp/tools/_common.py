"""Helpers shared by the tool modules — HANDOFF §10.1, §10.2.

Argument validation mirrors the §10.2 ``$defs`` so a malformed call is a ``400``
naming the field, whatever the transport did or did not validate. The schema
fragments here are the same definitions, inlined into every tool's schema because
MCP clients do not resolve ``$ref`` across documents (§10.1).
"""

from __future__ import annotations

import re
from typing import Any

from app.config import MAX_ARTICLE_BYTES, RESERVED_NAMES, RESERVED_TYPES
from app.errors import bad_request
from app.mcp.protocol import ToolContext
from app.storage.articles import META_ACTOR, META_KIND, ArticleStore

# Object-metadata ``kind`` values (§8.2).
KIND_WRITE = "write"
KIND_ARCHIVE = "archive"
KIND_UNARCHIVE = "unarchive"
KIND_MOVED_IN = "moved_in"
KIND_MOVED_OUT = "moved_out"

TYPE_POINTER = "pointer"
TYPE_ARCHIVED = "archived"

TRUST_UNVERIFIED = "unverified"
TRUST_MACHINE = "machine-confirmed"
TRUST_HUMAN = "human-reviewed"

HUMAN_ACTOR_PREFIX = "human:"

# ---------------------------------------------------------------------------
# §10.2 path grammar
# ---------------------------------------------------------------------------

ARTICLE_PATH_PATTERN = r"^/(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*\.md$"
FOLDER_PATH_PATTERN = r"^/$|^/(?:[a-z0-9][a-z0-9._-]*)(?:/[a-z0-9][a-z0-9._-]*)*$"
PATH_MAX_LENGTH = 512  # §10.2; a LIST session policy carries the path twice and must stay < 2 KB
VERSION_MAX_LENGTH = 256
SECTION_MAX_LENGTH = 200

_ARTICLE_PATH_RE = re.compile(ARTICLE_PATH_PATTERN)
_FOLDER_PATH_RE = re.compile(FOLDER_PATH_PATTERN)


def article_path(value: Any, field: str = "path") -> str:
    """Validate an absolute, lowercase, ``.md``-suffixed article path.

    Raises:
        ToolError: 400 when the value is not a string matching the grammar.
    """
    if not isinstance(value, str) or not value:
        raise bad_request(f"'{field}' is required and must be an absolute article path.")
    if len(value) > PATH_MAX_LENGTH or not _ARTICLE_PATH_RE.match(value):
        raise bad_request(
            f"'{field}' must be an absolute, lowercase article path ending in '.md', "
            "e.g. '/racing/setup/rear-bar.md'. Segments may not begin with '_'."
        )
    return value


def folder_path(value: Any, field: str = "path") -> str:
    """Validate an absolute folder path with no trailing slash; ``/`` is the root.

    Raises:
        ToolError: 400 when the value is not a string matching the grammar.
    """
    if not isinstance(value, str) or not value:
        raise bad_request(f"'{field}' is required and must be an absolute folder path.")
    if len(value) > PATH_MAX_LENGTH or not _FOLDER_PATH_RE.match(value):
        raise bad_request(
            f"'{field}' must be an absolute, lowercase folder path with no trailing "
            "slash, e.g. '/racing/setup' ('/' is the root)."
        )
    return value


def reject_reserved_name(path: str) -> None:
    """``index.md`` and ``log.md`` are generated, never stored (§3.1, §3.4).

    Raises:
        ToolError: 400 when the last segment is a reserved name.
    """
    name = path.rsplit("/", 1)[-1]
    if name in RESERVED_NAMES:
        raise bad_request(
            f"'{name}' is a reserved name and cannot be created; it is generated per request."
        )


def parent_folder(path: str) -> str:
    """``/racing/setup/rear-bar.md`` → ``/racing/setup``; ``/x.md`` → ``/``."""
    head = path.rsplit("/", 1)[0]
    return head or "/"


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------


def content_arg(args: dict[str, Any], field: str = "content") -> str:
    """The markdown body: a string of at most ``MAX_ARTICLE_BYTES`` UTF-8 bytes.

    Raises:
        ToolError: 400 when missing, not a string, or too large.
    """
    value = args.get(field)
    if not isinstance(value, str):
        raise bad_request(f"'{field}' is required and must be a string (markdown body).")
    if len(value.encode("utf-8")) > MAX_ARTICLE_BYTES:
        raise bad_request(
            f"'{field}' exceeds the {MAX_ARTICLE_BYTES} byte article limit. Split the article."
        )
    return value


def version_arg(args: dict[str, Any], field: str = "if_version") -> str:
    """A required version token, returned with surrounding quotes stripped (§10.1).

    Raises:
        ToolError: 400 when missing or malformed.
    """
    value = args.get(field)
    if not isinstance(value, str) or not value.strip('"'):
        raise bad_request(f"'{field}' is required: pass the 'version' from your most recent read.")
    if len(value) > VERSION_MAX_LENGTH:
        raise bad_request(f"'{field}' is not a version token.")
    return value.strip('"')


def validate_frontmatter(value: Any, *, drop_seq: bool = False) -> dict[str, Any]:
    """Check caller-supplied frontmatter against §10.2.

    ``type`` is required and must not be a server-written value. ``seq`` is
    server-maintained: rejected on create (there is nothing to echo), silently
    dropped on update (an agent that read the article and hands the block back
    will naturally include it — §10.2, §10.15).

    Args:
        value: The caller's ``frontmatter`` argument.
        drop_seq: When true, remove a supplied ``seq`` instead of rejecting it.

    Raises:
        ToolError: 400 on any violation.
    """
    if not isinstance(value, dict):
        raise bad_request("'frontmatter' is required and must be an object with a 'type'.")
    article_type = value.get("type")
    if not isinstance(article_type, str) or not article_type.strip():
        raise bad_request("'frontmatter.type' is required and must be a non-empty string.")
    if article_type in RESERVED_TYPES:
        raise bad_request(
            f"'frontmatter.type' may not be '{article_type}'; it is written only by the server."
        )
    if "seq" in value:
        if not drop_seq:
            raise bad_request(
                "'frontmatter.seq' is server-maintained and may not be supplied. "
                "Remove it and retry."
            )
        value = {k: v for k, v in value.items() if k != "seq"}
    return value


# ---------------------------------------------------------------------------
# Derived fields and result builders
# ---------------------------------------------------------------------------


def trust_of(frontmatter: dict[str, Any]) -> str:
    """Derive the trust tier from ``verified`` (§10.2 ``trust``). Never stored."""
    verified = frontmatter.get("verified")
    if not isinstance(verified, list) or not verified:
        return TRUST_UNVERIFIED
    for entry in verified:
        by = entry.get("by") if isinstance(entry, dict) else None
        if isinstance(by, str) and by.startswith(HUMAN_ACTOR_PREFIX):
            return TRUST_HUMAN
    return TRUST_MACHINE


def summary_from(
    path: str,
    *,
    type: str,
    version: str,
    trust: str,
    size_bytes: int | None = None,
    seq: int | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    stale: bool | None = None,
) -> dict[str, Any]:
    """Build an ``article_summary`` (§10.2), omitting fields that are ``None``."""
    summary: dict[str, Any] = {"path": path, "type": type, "version": version, "trust": trust}
    optional = {
        "title": title,
        "description": description,
        "tags": tags,
        "status": status,
        "stale": stale,
        "size_bytes": size_bytes,
        "seq": seq,
    }
    summary.update({k: v for k, v in optional.items() if v is not None})
    return summary


def metadata(ctx: ToolContext, kind: str, **extra: str) -> dict[str, str]:
    """Object metadata for a ``PutObject`` (§8.2): actor, kind, plus any extra keys."""
    return {META_ACTOR: ctx.actor, META_KIND: kind, **extra}


def store(ctx: ToolContext) -> ArticleStore:
    """The article store for this deployment's bucket."""
    return ArticleStore(ctx.settings.bucket)


# ---------------------------------------------------------------------------
# §10.2 schema fragments, for inlining into tool schemas
# ---------------------------------------------------------------------------

ARTICLE_PATH_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": ARTICLE_PATH_PATTERN,
    "maxLength": PATH_MAX_LENGTH,
    "description": (
        "Absolute article path, lowercase, '.md' suffixed. Example "
        "'/racing/setup/rear-bar.md'. Segments may not begin with '_' (reserved for "
        "system objects) and the names 'index.md' and 'log.md' are reserved."
    ),
}

FOLDER_PATH_SCHEMA: dict[str, Any] = {
    "type": "string",
    "pattern": FOLDER_PATH_PATTERN,
    "maxLength": PATH_MAX_LENGTH,
    "description": "Absolute folder path with no trailing slash; '/' is the root.",
}

VERSION_SCHEMA: dict[str, Any] = {
    "type": "string",
    "maxLength": VERSION_MAX_LENGTH,
    "description": (
        "Opaque concurrency token (the object ETag). Compare verbatim; never parse. "
        "Pass back unchanged as if_version."
    ),
}

ACTOR_SCHEMA: dict[str, Any] = {
    "type": "string",
    "maxLength": 256,
    "pattern": r"^(human:[A-Za-z0-9._@-]+|process:[A-Za-z0-9._-]+|[^/]+/[^/]+)$",
    "description": (
        "OKF actor convention: 'human:<id>' for a person, 'process:<id>' for an "
        "automated process, '<producer>/<version>' for an agent."
    ),
}

SOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["resource"],
    "properties": {
        "resource": {"type": "string", "maxLength": 2048},
        "id": {"type": "string", "maxLength": 64},
        "title": {"type": "string", "maxLength": 200},
        "author": {"type": "string", "maxLength": 200},
    },
}

_ATTESTATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["by"],
    "properties": {
        "by": ACTOR_SCHEMA,
        "at": {"type": "string", "format": "date-time"},
    },
}

FRONTMATTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type"],
    "additionalProperties": True,
    "properties": {
        "type": {
            "type": "string",
            "description": (
                "OKF concept type. Starting vocabulary is 'doc'. 'pointer' and "
                "'archived' are server-written and rejected on input."
            ),
        },
        "title": {"type": "string", "maxLength": 200},
        "description": {
            "type": "string",
            "maxLength": 500,
            "description": "One sentence. Shown in listings and search.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string", "maxLength": 60},
            "maxItems": 20,
        },
        "status": {"enum": ["draft", "stable", "deprecated"], "default": "stable"},
        "stale_after": {"type": "string", "format": "date-time"},
        "sources": {"type": "array", "items": SOURCE_SCHEMA},
        "generated": _ATTESTATION_SCHEMA,
        "verified": {"type": "array", "items": _ATTESTATION_SCHEMA},
        "seq": {
            "type": "integer",
            "minimum": 1,
            "readOnly": True,
            "description": (
                "Server-maintained. Increments on every write. Rejected if supplied by a caller."
            ),
        },
    },
}

TRUST_SCHEMA: dict[str, Any] = {
    "enum": [TRUST_UNVERIFIED, TRUST_MACHINE, TRUST_HUMAN],
    "description": (
        "Derived, never stored. No 'verified' entries is unverified; entries by "
        "non-human actors only is machine-confirmed; any 'human:' actor makes it "
        "human-reviewed."
    ),
}

ARTICLE_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "type", "version", "trust"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "type": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "status": {"enum": ["draft", "stable", "deprecated"]},
        "stale": {"type": "boolean", "description": "True when stale_after has passed."},
        "trust": TRUST_SCHEMA,
        "size_bytes": {"type": "integer"},
        "seq": {"type": "integer"},
        "version": VERSION_SCHEMA,
    },
}

FORWARD_REFERENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind", "moved_to"],
    "properties": {
        "kind": {"const": "forward_reference"},
        "moved_to": {
            "type": "string",
            "maxLength": 2048,
            "description": (
                "Destination path, or an absolute URL when the destination is on another instance."
            ),
        },
        "moved_at": {"type": "string", "format": "date-time"},
        "note": {
            "type": "string",
            "description": (
                "Human-readable, e.g. 'This article moved. You may not have access at "
                "its new location — ask an owner.'"
            ),
        },
    },
    "description": (
        "Returned in place of content when a path holds a pointer. Naming the "
        "destination is deliberate and safe: only a caller who could reach the old "
        "path can see this, and they could already read the article."
    ),
}

WRITE_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "version", "seq"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "version": VERSION_SCHEMA,
        "seq": {"type": "integer"},
    },
}


__all__ = [
    "ACTOR_SCHEMA",
    "ARTICLE_PATH_SCHEMA",
    "ARTICLE_SUMMARY_SCHEMA",
    "FOLDER_PATH_SCHEMA",
    "FORWARD_REFERENCE_SCHEMA",
    "FRONTMATTER_SCHEMA",
    "HUMAN_ACTOR_PREFIX",
    "KIND_ARCHIVE",
    "KIND_MOVED_IN",
    "KIND_MOVED_OUT",
    "KIND_UNARCHIVE",
    "KIND_WRITE",
    "MAX_ARTICLE_BYTES",
    "SECTION_MAX_LENGTH",
    "SOURCE_SCHEMA",
    "TRUST_HUMAN",
    "TRUST_MACHINE",
    "TRUST_SCHEMA",
    "TRUST_UNVERIFIED",
    "TYPE_ARCHIVED",
    "TYPE_POINTER",
    "VERSION_SCHEMA",
    "WRITE_RESULT_SCHEMA",
    "article_path",
    "content_arg",
    "folder_path",
    "metadata",
    "parent_folder",
    "reject_reserved_name",
    "store",
    "summary_from",
    "trust_of",
    "validate_frontmatter",
    "version_arg",
]
