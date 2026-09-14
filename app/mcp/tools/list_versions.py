"""``list_versions`` — ``wiki.read`` · ``read`` on path (HANDOFF §10.7).

Sequence: validate → grant check on the path → mint a READ credential for the exact
key → ``ListObjectVersions`` → one ranged ``GetObject?versionId=`` per entry.

§8.3 budgets one ``HeadObject`` per entry for actor and kind. A ``version_entry``
also requires ``seq``, which lives in the frontmatter (§8.4) and which no head can
return — so each entry is read with a ranged ``GetObject`` instead: the first
``PREFIX_BYTES`` carry the frontmatter block and the response carries the same
user metadata a head would. Same call count, one more field. The reads fan out
over a small thread pool; the S3 client is thread-safe.

**Returns this path's chain only.** When the oldest version on the last page is a
``moved_in``, ``continues_at`` names the path the article came from — a link the
caller may follow with a fresh ``list_versions`` call, authorized against that
path. This tool never touches the other key (§5.3, §10.7).

A subject without ``read`` gets ``404``, as does a 403 from S3: absence and denial
are indistinguishable (§10.1, §10.15).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from botocore.exceptions import ClientError

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ
from app.errors import ToolError, bad_request, not_found
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ACTOR_SCHEMA,
    ARTICLE_PATH_SCHEMA,
    KIND_ARCHIVE,
    KIND_MOVED_IN,
    KIND_MOVED_OUT,
    KIND_UNARCHIVE,
    KIND_WRITE,
    TYPE_POINTER,
    article_path,
    store,
)
from app.storage.articles import (
    META_ACTOR,
    META_KIND,
    META_MOVED_FROM,
    AccessDenied,
    ArticleStore,
    StoredObject,
)
from app.storage.markdown import Article, parse

DESCRIPTION = (
    "The version chain of one path, newest first. Returns that path's chain only. "
    "When the oldest entry is moved_in, earlier history lives at the path named in "
    "continues_at and reaching it is a separate call, authorized against that path."
)

LIMIT_DEFAULT = 20
LIMIT_MAX = 100
CURSOR_MAX_LENGTH = 2048
VERSION_ID_MAX_LENGTH = 1024

# Enough for any frontmatter §10.2 allows (title 200, description 500, twenty tags,
# sources, attestations) with room to spare. A block that somehow overruns it is
# read in full — correctness over the byte budget.
PREFIX_BYTES = 16 * 1024
MAX_WORKERS = 8

# What an entry says when the object carries no server metadata — objects written
# before the metadata existed, or restored from a backup without it.
ACTOR_UNKNOWN = "process:unknown"

VERSION_ID_SCHEMA: dict[str, Any] = {
    "type": "string",
    "maxLength": VERSION_ID_MAX_LENGTH,
    "description": (
        "Opaque identifier for one historical version. Used only to pin a read; "
        "not valid as if_version."
    ),
}

VERSION_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["version_id", "seq", "kind", "actor", "at"],
    "properties": {
        "version_id": VERSION_ID_SCHEMA,
        "seq": {"type": "integer"},
        "kind": {"enum": [KIND_WRITE, KIND_ARCHIVE, KIND_UNARCHIVE, KIND_MOVED_IN, KIND_MOVED_OUT]},
        "actor": ACTOR_SCHEMA,
        "at": {"type": "string", "format": "date-time"},
        "size_bytes": {"type": "integer"},
        "moved_to": ARTICLE_PATH_SCHEMA,
        "moved_from": ARTICLE_PATH_SCHEMA,
    },
}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "limit": {"type": "integer", "minimum": 1, "maximum": LIMIT_MAX, "default": LIMIT_DEFAULT},
        "cursor": {"type": "string", "maxLength": CURSOR_MAX_LENGTH},
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["path", "versions", "truncated"],
    "properties": {
        "path": ARTICLE_PATH_SCHEMA,
        "versions": {"type": "array", "items": VERSION_ENTRY_SCHEMA},
        "truncated": {"type": "boolean"},
        "cursor": {"type": "string"},
        "continues_at": {
            **ARTICLE_PATH_SCHEMA,
            "description": (
                "Present when this chain begins with a move. Earlier history is at "
                "this path, under its own grants."
            ),
        },
    },
}


def _limit_arg(args: dict[str, Any]) -> int:
    value = args.get("limit", LIMIT_DEFAULT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= LIMIT_MAX:
        raise bad_request(f"'limit' must be an integer from 1 to {LIMIT_MAX}.")
    return value


def _cursor_arg(args: dict[str, Any]) -> str | None:
    value = args.get("cursor")
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > CURSOR_MAX_LENGTH:
        raise bad_request("'cursor' must be a cursor returned by a previous list_versions call.")
    return value


def _is_denied(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    return code in ("AccessDenied", "403") or status == 403


def _is_gone(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    return code in ("NoSuchKey", "NoSuchVersion", "NotFound", "404") or status == 404


def _peek(st: ArticleStore, s3: Any, path: str, entry: StoredObject) -> StoredObject | None:
    """``GetObject?versionId=`` for the first ``PREFIX_BYTES`` of one version.

    Returns the version with its metadata and a body holding at least the whole
    frontmatter block, or ``None`` when the version is no longer there (nothing
    deletes versions; this is a race with an operator, not a normal path).

    Raises:
        AccessDenied: on 403.
    """
    request: dict[str, Any] = {"Bucket": st.bucket, "Key": entry.key, "VersionId": entry.version_id}
    if entry.size:
        # A range on a zero-byte object is 416 InvalidRange; there is nothing to skip.
        request["Range"] = f"bytes=0-{PREFIX_BYTES - 1}"
    try:
        response = s3.get_object(**request)
    except ClientError as error:
        if _is_gone(error):
            return None
        if _is_denied(error):
            raise AccessDenied(path) from error
        raise
    body = response["Body"].read()
    peeked = StoredObject(
        key=entry.key,
        etag=response.get("ETag", entry.etag),
        body=body,
        size=entry.size,
        version_id=response.get("VersionId") or entry.version_id,
        last_modified=response.get("LastModified") or entry.last_modified,
        metadata=dict(response.get("Metadata") or {}),
    )
    if _frontmatter_cut_short(peeked):
        # The block outran the prefix. Read the whole version rather than guess.
        whole = st.get_version(s3, path, entry.version_id or "")
        return whole if whole is not None else peeked
    return peeked


def _frontmatter_cut_short(peeked: StoredObject) -> bool:
    """True when the prefix opens a frontmatter block that does not close inside it."""
    if peeked.size is None or len(peeked.body) >= peeked.size:
        return False
    if not peeked.body.startswith(b"---"):
        return False
    return not parse(peeked.body).frontmatter


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _entry(version: StoredObject, article: Article) -> dict[str, Any]:
    """One ``version_entry`` (§10.2) from a version's metadata and frontmatter."""
    kind = version.metadata.get(META_KIND) or KIND_WRITE
    entry: dict[str, Any] = {
        "version_id": version.version_id,
        "seq": article.seq,
        "kind": kind,
        "actor": version.metadata.get(META_ACTOR) or ACTOR_UNKNOWN,
    }
    at = _iso(version.last_modified)
    if at is not None:
        entry["at"] = at
    if version.size is not None:
        entry["size_bytes"] = version.size
    if kind == KIND_MOVED_OUT or article.type == TYPE_POINTER:
        moved_to = article.frontmatter.get("moved_to")
        if isinstance(moved_to, str) and moved_to:
            entry["moved_to"] = moved_to
    moved_from = version.metadata.get(META_MOVED_FROM)
    if moved_from:
        entry["moved_from"] = moved_from
    return entry


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path = article_path(args.get("path"))
    limit = _limit_arg(args)
    cursor = _cursor_arg(args)

    try:
        ctx.require(path, Permission.READ)
    except ToolError as error:
        if error.status == 403:
            raise not_found() from None
        raise

    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    st = store(ctx)
    try:
        listed, next_cursor = st.list_versions(s3, path, limit=limit, cursor=cursor)
    except ValueError:
        raise bad_request(
            "'cursor' is not a cursor this path issued. Start again without one."
        ) from None
    except AccessDenied:
        raise not_found() from None
    if not listed:
        # A cursor is only issued when another version of this key was seen, so an
        # empty page means the key has no versions at all: no such path.
        raise not_found()

    try:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(listed))) as pool:
            peeked = list(pool.map(lambda entry: _peek(st, s3, path, entry), listed))
    except AccessDenied:
        raise not_found() from None

    versions: list[dict[str, Any]] = []
    for version in peeked:
        if version is None:
            continue
        versions.append(_entry(version, parse(version.body)))

    result: dict[str, Any] = {
        "path": path,
        "versions": versions,
        "truncated": next_cursor is not None,
    }
    if next_cursor is not None:
        result["cursor"] = next_cursor
    elif versions:
        # This page holds the oldest version. If the chain began with a move, say
        # where it came from — and go no further (§5.3: history is per path).
        oldest = versions[-1]
        if oldest["kind"] == KIND_MOVED_IN and oldest.get("moved_from"):
            result["continues_at"] = oldest["moved_from"]
    return result


TOOL = Tool(
    name="list_versions",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_READ,
    handler=handle,
)

__all__ = ["TOOL", "VERSION_ENTRY_SCHEMA", "VERSION_ID_SCHEMA", "handle"]
