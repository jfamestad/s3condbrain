"""Helpers shared by the tool modules — HANDOFF §10.1, §10.2.

Argument validation mirrors the §10.2 ``$defs`` so a malformed call is a ``400``
naming the field, whatever the transport did or did not validate. The schema
fragments here are the same definitions, inlined into every tool's schema because
MCP clients do not resolve ``$ref`` across documents (§10.1).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from app.auth.types import Permission, Resolution
from app.config import MAX_ARTICLE_BYTES, RESERVED_NAMES, RESERVED_TYPES
from app.errors import ToolError, bad_request
from app.mcp.protocol import ToolContext
from app.storage.articles import META_ACTOR, META_KIND, ArticleStore
from app.storage.markdown import MAX_FRONTMATTER_BYTES, MAX_FRONTMATTER_DEPTH, serialize

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

# A folder segment is any legal segment that does not end in ".md". Without that
# exclusion "/x.md/y.md" is a legal article under a folder named "x.md", and because
# grant resolution walks path strings, a grant on the *article* "/x.md" would then
# reach everything created beneath it (security review 2026-09-24).
_FOLDER_SEGMENT = r"(?![a-z0-9._-]*\.md(?:/|$))[a-z0-9][a-z0-9._-]*"
ARTICLE_PATH_PATTERN = rf"^/(?:{_FOLDER_SEGMENT}/)*[a-z0-9][a-z0-9._-]*\.md$"
FOLDER_PATH_PATTERN = rf"^/$|^/{_FOLDER_SEGMENT}(?:/{_FOLDER_SEGMENT})*$"
PATH_MAX_LENGTH = 512  # §10.2; a LIST session policy carries the path twice and must stay < 2 KB
VERSION_MAX_LENGTH = 256
SECTION_MAX_LENGTH = 200

_ARTICLE_PATH_RE = re.compile(ARTICLE_PATH_PATTERN)
_FOLDER_PATH_RE = re.compile(FOLDER_PATH_PATTERN)

# ---------------------------------------------------------------------------
# §10.2 frontmatter limits, enforced server-side (the schema only advertises them)
# ---------------------------------------------------------------------------

ACTOR_PATTERN = r"^(human:[A-Za-z0-9._@-]+|process:[A-Za-z0-9._-]+|[^/]+/[^/]+)$"
ACTOR_MAX_LENGTH = 256
TITLE_MAX_LENGTH = 200
DESCRIPTION_MAX_LENGTH = 500
TAGS_MAX_ITEMS = 20
TAG_MAX_LENGTH = 60
STATUS_VALUES = frozenset({"draft", "stable", "deprecated"})
SOURCES_MAX_ITEMS = 50
SOURCE_FIELD_MAX_LENGTH = {"resource": 2048, "id": 64, "title": 200, "author": 200}
VERIFIED_MAX_ITEMS = 50
# Top-level keys, unknown ones included (``additionalProperties: true`` is not "unbounded").
MAX_FRONTMATTER_KEYS = 50
# ``MAX_FRONTMATTER_BYTES`` and ``MAX_FRONTMATTER_DEPTH`` come from ``app.storage.markdown``:
# the write side must never accept a block the read side will treat as malformed.
# The block is measured with a ``seq`` line at least as long as any the server will
# write, so the stored object is never larger than what was checked.
_SEQ_UPPER_BOUND = 2**63 - 1
_FENCE_BYTES = len(b"---\n---\n")

_ACTOR_RE = re.compile(ACTOR_PATTERN)


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
            "e.g. '/racing/setup/rear-bar.md'. Segments may not begin with '_', and "
            "only the last segment may end in '.md'."
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
            "slash, e.g. '/racing/setup' ('/' is the root). No segment may end in '.md'."
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


def _string_field(value: Any, field: str, max_length: int, *, required: bool = False) -> None:
    """``field`` is a string of at most ``max_length`` characters (absent is fine
    unless ``required``)."""
    if value is None:
        if required:
            raise bad_request(f"'{field}' is required and must be a string.")
        return
    if not isinstance(value, str) or len(value) > max_length:
        raise bad_request(f"'{field}' must be a string of at most {max_length} characters.")


def _actor_field(value: Any, field: str) -> None:
    if not isinstance(value, str) or len(value) > ACTOR_MAX_LENGTH or not _ACTOR_RE.match(value):
        raise bad_request(
            f"'{field}' must be an actor: 'human:<id>', 'process:<id>' or '<producer>/<version>'."
        )


def _attestation(value: Any, field: str) -> None:
    """``{"by": <actor>, "at"?: <string>}`` — the ``generated`` / ``verified`` shape."""
    if not isinstance(value, dict):
        raise bad_request(f"'{field}' must be an object with a 'by' actor.")
    _actor_field(value.get("by"), f"{field}.by")
    _string_field(value.get("at"), f"{field}.at", ACTOR_MAX_LENGTH)


def _list_field(value: Any, field: str, max_items: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > max_items:
        raise bad_request(f"'{field}' must be an array of at most {max_items} items.")
    return value


def _depth(value: Any) -> int:
    """Nesting depth of mappings/sequences, a bare scalar being 0."""
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def _block_bytes(frontmatter: dict[str, Any]) -> int:
    """Bytes of the YAML block ``serialize`` will store, with the server's ``seq``
    line counted at its longest."""
    try:
        return len(serialize({**frontmatter, "seq": _SEQ_UPPER_BOUND}, "")) - _FENCE_BYTES
    except yaml.YAMLError as error:
        raise bad_request(f"'frontmatter' cannot be stored as YAML: {error}") from None


def _check_documented_fields(value: dict[str, Any]) -> None:
    """Every §10.2 property at its advertised limit; unknown keys pass untouched."""
    _string_field(value.get("title"), "frontmatter.title", TITLE_MAX_LENGTH)
    _string_field(value.get("description"), "frontmatter.description", DESCRIPTION_MAX_LENGTH)
    if (tags := value.get("tags")) is not None:
        for tag in _list_field(tags, "frontmatter.tags", TAGS_MAX_ITEMS):
            if not isinstance(tag, str) or len(tag) > TAG_MAX_LENGTH:
                raise bad_request(
                    f"'frontmatter.tags' items must be strings of at most {TAG_MAX_LENGTH} "
                    "characters."
                )
    if (status := value.get("status")) is not None and status not in STATUS_VALUES:
        raise bad_request(
            f"'frontmatter.status' must be one of {sorted(STATUS_VALUES)} when present."
        )
    _string_field(value.get("stale_after"), "frontmatter.stale_after", ACTOR_MAX_LENGTH)
    if (sources := value.get("sources")) is not None:
        for source in _list_field(sources, "frontmatter.sources", SOURCES_MAX_ITEMS):
            if not isinstance(source, dict):
                raise bad_request("'frontmatter.sources' items must be objects with a 'resource'.")
            for name, max_length in SOURCE_FIELD_MAX_LENGTH.items():
                _string_field(
                    source.get(name),
                    f"frontmatter.sources[].{name}",
                    max_length,
                    required=name == "resource",
                )
    if (generated := value.get("generated")) is not None:
        _attestation(generated, "frontmatter.generated")
    if (verified := value.get("verified")) is not None:
        for entry in _list_field(verified, "frontmatter.verified", VERIFIED_MAX_ITEMS):
            _attestation(entry, "frontmatter.verified[]")


def validate_frontmatter(value: Any, *, drop_seq: bool = False) -> dict[str, Any]:
    """Check caller-supplied frontmatter against §10.2 — shape *and* limits.

    ``type`` is required and must not be a server-written value. ``seq`` is
    server-maintained: rejected on create (there is nothing to echo), silently
    dropped on update (an agent that read the article and hands the block back
    will naturally include it — §10.2, §10.15). Every documented property is
    checked at its advertised limit; unknown keys are kept but bounded in number,
    nesting depth and serialized size, so nothing is stored that ``parse`` would
    later refuse to read.

    Args:
        value: The caller's ``frontmatter`` argument.
        drop_seq: When true, remove a supplied ``seq`` instead of rejecting it.

    Returns:
        The validated mapping (a copy when ``seq`` was dropped; the caller's own
        object otherwise — callers copy before mutating).

    Raises:
        ToolError: 400 on any violation, naming the field.
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
    if len(value) > MAX_FRONTMATTER_KEYS:
        raise bad_request(f"'frontmatter' may carry at most {MAX_FRONTMATTER_KEYS} keys.")
    _check_documented_fields(value)
    if _depth(value) > MAX_FRONTMATTER_DEPTH:
        raise bad_request(f"'frontmatter' may nest at most {MAX_FRONTMATTER_DEPTH} levels deep.")
    if _block_bytes(value) > MAX_FRONTMATTER_BYTES:
        raise bad_request(
            f"'frontmatter' exceeds {MAX_FRONTMATTER_BYTES} bytes when serialized. "
            "Move long material into the article body."
        )
    return value


def revalidate_stored(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Re-check a stored block a tool is about to carry forward unchanged.

    ``validate_frontmatter`` runs on the caller's block; when a tool keeps the
    stored one instead (update without ``frontmatter``, archive, unarchive, move)
    the same §10.2 limits apply, or an object written under an older limit — or
    straight into the bucket — would be re-signed as a fresh version. Runs before
    the tool adds its own fields, so ``seq`` is dropped here and set by the caller.

    Args:
        frontmatter: The stored block as ``parse`` returned it, ``seq`` included.
            ``type`` must be a document type; a tool that retires the block sets
            ``archived`` afterwards.

    Returns:
        A copy without ``seq``, ready for the tool's server-written fields.

    Raises:
        ToolError: 400 naming the field, prefixed ``stored frontmatter:`` so a
            person knows the article's existing block is what failed, not input.
    """
    try:
        return dict(validate_frontmatter(frontmatter, drop_seq=True))
    except ToolError as error:
        if error.status != 400:
            raise
        raise bad_request(f"stored frontmatter: {error.message}") from None


def serialize_article(frontmatter: dict[str, Any], content: str) -> bytes:
    """``serialize`` with the §6.3 ceiling applied to the *stored object*.

    ``content_arg`` bounds the body alone; frontmatter adds up to
    ``MAX_FRONTMATTER_BYTES`` on top, and the published 1 MiB maximum is on the
    article as stored.

    Raises:
        ToolError: 400 when the serialized object exceeds ``MAX_ARTICLE_BYTES``.
    """
    body = serialize(frontmatter, content)
    if len(body) > MAX_ARTICLE_BYTES:
        raise bad_request(
            f"The article would be {len(body)} bytes with its frontmatter; the limit is "
            f"{MAX_ARTICLE_BYTES}. Shorten the content or split the article."
        )
    return body


def require_grant(ctx: ToolContext, path: str, needed: Permission) -> Resolution:
    """``ctx.require`` — kept as a name the tools already import. The resolve →
    record → enforce ordering (AS-10: a denial's contributing grants are logged too)
    lives in ``ToolContext.require`` so every tool gets it, not only these callers.
    """
    return ctx.require(path, needed)


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
    link_to: str | None = None,
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
        "link_to": link_to,
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
    "maxLength": ACTOR_MAX_LENGTH,
    "pattern": ACTOR_PATTERN,
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
                "OKF concept type. Starting vocabulary is 'doc'. 'link' makes the "
                "article a link to 'link_to' (it grants nothing; readers get a one-hop "
                "reference). 'pointer' and 'archived' are server-written and rejected "
                "on input."
            ),
        },
        "link_to": {
            "type": "string",
            "maxLength": 2048,
            "description": (
                "Links only (type 'link'): the target — an article path, a folder path, "
                "or 'https://<host>/a/<article path>'. Required on a link, rejected on "
                "anything else."
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
        "link_to": {
            "type": "string",
            "description": "Links only: the target path or reference URL.",
        },
        "resolved": {
            "type": "boolean",
            "description": (
                "list_folder only, local links only: whether you hold read on the "
                "target right now (a grant check — the target may still not exist). "
                "Absent from search hits."
            ),
        },
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
    "MAX_FRONTMATTER_BYTES",
    "MAX_FRONTMATTER_DEPTH",
    "MAX_FRONTMATTER_KEYS",
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
    "require_grant",
    "revalidate_stored",
    "serialize_article",
    "store",
    "summary_from",
    "trust_of",
    "validate_frontmatter",
    "version_arg",
]
