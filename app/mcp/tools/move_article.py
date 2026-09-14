"""``move_article`` — ``wiki.write`` · ``write`` on both (HANDOFF §4.6, §5.3, §8.3, §10.11).

Four S3 operations, **pointer first**:

1. ``HeadObject to`` — anything there (article, pointer, tombstone) is ``409``.
2. ``GetObject from`` — must be live content; ``if_version`` must match.
3. The impact report is computed here, before any write, so a failure leaves
   nothing changed and a success reports exactly what it changed (§4.6).
4. ``PutObject`` the pointer at ``from`` with ``If-Match: <if_version>``. A stale
   token fails here with **nothing written anywhere** — no content crosses a
   permission boundary for a move that was refused (§8.3, §13.2).
5. ``PutObject`` the content at ``to`` with ``If-None-Match: *``, ``seq + 1``,
   ``moved_from`` in frontmatter and ``kind: moved_in`` / ``moved-from`` in metadata.
6. Refresh both parent listings, best effort.

S3 has no multi-object transaction, so the mitigation is idempotence: a crash between
4 and 5 leaves a pointer at ``from`` whose target answers 404 and the content intact
one version beneath it. Retrying the same call finds that pointer naming ``to``, finds
``to`` empty, and completes step 5 from the version beneath the pointer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission, Resolution
from app.config import SCOPE_WRITE
from app.errors import ToolError, bad_request, conflict, forbidden, internal, not_found
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_PATH_SCHEMA,
    KIND_MOVED_IN,
    KIND_MOVED_OUT,
    TYPE_ARCHIVED,
    TYPE_POINTER,
    VERSION_SCHEMA,
    article_path,
    metadata,
    reject_reserved_name,
    store,
    version_arg,
)
from app.mcp.tools._listings import refresh_parent
from app.mcp.tools.archive_article import log_event, stale
from app.mcp.tools.unarchive_article import last_content_version
from app.storage.articles import (
    META_MOVED_FROM,
    AccessDenied,
    ArticleStore,
    PreconditionFailed,
    StoredObject,
)
from app.storage.listings import ListingChild, basename
from app.storage.markdown import Article, parse, serialize

DESCRIPTION = (
    "Relocate an article, leaving a permanent forward pointer at the old path. Returns "
    "who gains and who loses access. Because grants are positional, a move re-evaluates "
    "readership in both directions — surface access_changes to the person before "
    "treating the move as done."
)

ACCESS_CHANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["subject", "direction", "permission"],
    "properties": {
        "subject": {"type": "string"},
        "display_name": {"type": "string"},
        "direction": {"enum": ["gains", "loses"]},
        "permission": {"enum": ["read", "write", "own"]},
        "via": {
            "type": "string",
            "description": "The granted path the change derives from.",
        },
    },
}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from", "to", "if_version"],
    "properties": {
        "from": ARTICLE_PATH_SCHEMA,
        "to": ARTICLE_PATH_SCHEMA,
        "if_version": VERSION_SCHEMA,
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from", "to", "version", "seq", "access_changes"],
    "properties": {
        "from": ARTICLE_PATH_SCHEMA,
        "to": ARTICLE_PATH_SCHEMA,
        "version": {
            **VERSION_SCHEMA,
            "description": "The article's version at its new path.",
        },
        "seq": {"type": "integer"},
        "access_changes": {
            "type": "array",
            "items": ACCESS_CHANGE_SCHEMA,
            "description": "Empty when the move crosses no permission boundary.",
        },
        "history_note": {
            "type": "string",
            "description": (
                "'Earlier versions remain at {from} and are not readable from the new location.'"
            ),
        },
    },
}

HISTORY_NOTE = "Earlier versions remain at {from_path} and are not readable from the new location."
POINTER_BODY = "This article moved to {to}."

_PROFILE_SK = "PROFILE"
_SUBJECT_PREFIX = "U#"


# ---------------------------------------------------------------------------
# Impact report (§4.6 "a move is a boundary decision")
# ---------------------------------------------------------------------------


def _display_name(ctx: ToolContext, subject: str) -> str | None:
    """``display_name`` from the subject's PROFILE row, or ``None``. Never raises:
    the report is still correct without names."""
    try:
        item = ctx.grants.table.get_item(
            Key={"pk": f"{_SUBJECT_PREFIX}{subject}", "sk": _PROFILE_SK}
        ).get("Item")
    except Exception as exc:  # noqa: BLE001 — decoration only; the report must not fail on it
        log_event(ctx, "warning", "profile_lookup_failed", exc_class=type(exc).__name__)
        return None
    name = item.get("display_name") if isinstance(item, dict) else None
    return name if isinstance(name, str) and name else None


def _deciding_node(resolution: Resolution, permission: Permission) -> str | None:
    """The node of the shallowest grant carrying ``permission``."""
    for grant in resolution.grants_used:
        if grant.permission is permission:
            return grant.node
    return None


def _permission(resolution: Resolution | None) -> Permission | None:
    return resolution.permission if resolution is not None else None


def _change(
    ctx: ToolContext, subject: str, direction: str, resolution: Resolution, permission: Permission
) -> dict[str, Any]:
    change: dict[str, Any] = {
        "subject": subject,
        "direction": direction,
        "permission": permission.value,
    }
    via = _deciding_node(resolution, permission)
    if via is not None:
        change["via"] = via
    display_name = _display_name(ctx, subject)
    if display_name is not None:
        change["display_name"] = display_name
    return change


def access_changes(ctx: ToolContext, from_path: str, to_path: str) -> list[dict[str, Any]]:
    """Who gains and who loses by relocating ``from_path`` to ``to_path``.

    Both sides are resolved through GSI1 (§8.5) before any write. For each subject
    who can reach either path, a rise in effective permission is a ``gains`` entry
    and a drop a ``loses`` entry; ``permission`` is the higher of the two and ``via``
    the granted path that confers it. A subject whose permission is unchanged does
    not appear — the common case for the mover, who holds write on both ends.
    """
    before = ctx.grants.subjects_reaching(from_path)
    after = ctx.grants.subjects_reaching(to_path)
    changes: list[dict[str, Any]] = []
    for subject in sorted(set(before) | set(after)):
        old, new = before.get(subject), after.get(subject)
        old_permission, new_permission = _permission(old), _permission(new)
        old_rank = old_permission.rank if old_permission is not None else 0
        new_rank = new_permission.rank if new_permission is not None else 0
        if new is not None and new_permission is not None and new_rank > old_rank:
            changes.append(_change(ctx, subject, "gains", new, new_permission))
        elif old is not None and old_permission is not None and old_rank > new_rank:
            changes.append(_change(ctx, subject, "loses", old, old_permission))
    return changes


# ---------------------------------------------------------------------------
# The move
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _destination_occupied() -> ToolError:
    return conflict(
        "Something already occupies the destination — an article, a move pointer or an "
        "archive tombstone. Choose a different 'to'."
    )


def _source(st: ArticleStore, s3: Any, from_path: str, to_path: str) -> tuple[StoredObject, bool]:
    """The current object at ``from`` and whether this call is resuming a half-done move.

    Raises:
        ToolError: 404 when nothing live is there — including a pointer that names
            some *other* destination, which is a retired path like any other; 403
            when S3 refuses.
    """
    try:
        current = st.get(s3, from_path)
    except AccessDenied:
        raise forbidden() from None
    if current is None:
        raise not_found()
    article = parse(current.body)
    if article.type == TYPE_POINTER:
        if article.frontmatter.get("moved_to") == to_path:
            return current, True
        raise not_found()
    if article.type == TYPE_ARCHIVED:
        raise not_found()
    return current, False


def _write_pointer(
    ctx: ToolContext, st: ArticleStore, s3: Any, current: StoredObject, to_path: str
) -> int:
    """Step 4. Returns the pointer's ``seq``.

    Raises:
        ToolError: 409 (nothing written) when the ETag no longer matches; 403 when
            S3 refuses.
    """
    seq = parse(current.body).seq + 1
    frontmatter = {
        "type": TYPE_POINTER,
        "moved_to": to_path,
        "moved_at": _now(),
        "seq": seq,
    }
    body = serialize(frontmatter, POINTER_BODY.format(to=to_path))
    try:
        st.put_if_match(s3, current.path, body, current.etag, metadata(ctx, KIND_MOVED_OUT))
    except PreconditionFailed:
        # Lost the race between our read and our write: report what is there now.
        # A pointer or tombstone now on top means someone else retired the path.
        now, _ = _source(st, s3, current.path, to_path)
        raise stale(now) from None
    except AccessDenied:
        raise forbidden() from None
    return seq


def _write_destination(
    ctx: ToolContext,
    st: ArticleStore,
    s3: Any,
    from_path: str,
    to_path: str,
    content: Article,
    seq: int,
) -> tuple[dict[str, Any], StoredObject]:
    """Step 5. Returns the destination frontmatter and the written object.

    Raises:
        ToolError: 409 when the destination filled between step 1 and now, 403 when
            S3 refuses. Either way the pointer at ``from`` already stands, so the
            message says so — the move is half-complete and a retry finishes it.
    """
    frontmatter = dict(content.frontmatter)
    frontmatter["moved_from"] = from_path
    frontmatter["seq"] = seq
    body = serialize(frontmatter, content.body)
    meta = metadata(ctx, KIND_MOVED_IN, **{META_MOVED_FROM: from_path})
    try:
        written = st.put_new(s3, to_path, body, meta)
    except PreconditionFailed:
        log_event(ctx, "warning", "move_half_complete", from_path=from_path, to_path=to_path)
        raise conflict(
            f"The destination filled while the move was in progress. The pointer at "
            f"{from_path} already names {to_path}, so the move is half-complete: retry "
            "once the destination is clear, or ask an owner."
        ) from None
    except AccessDenied:
        log_event(ctx, "warning", "move_half_complete", from_path=from_path, to_path=to_path)
        raise forbidden(
            f"Storage refused the write at {to_path}. The pointer at {from_path} already "
            f"names {to_path}, so the move is half-complete: ask an owner."
        ) from None
    return frontmatter, written


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from_path = article_path(args.get("from"), "from")
    to_path = article_path(args.get("to"), "to")
    reject_reserved_name(to_path)
    if from_path == to_path:
        raise bad_request("'from' and 'to' must be different paths.")
    if_version = version_arg(args)

    ctx.require(from_path, Permission.WRITE)
    ctx.require(to_path, Permission.WRITE)

    st = store(ctx)

    # 1. The destination must be empty — a pointer or tombstone there is occupied too.
    s3_to = ctx.minter.s3(ctx.subject, Shape.WRITE, to_path)
    try:
        occupant = st.head(s3_to, to_path)
    except AccessDenied:
        raise forbidden() from None
    if occupant is not None:
        raise _destination_occupied()

    # 2. The source must be live content at the version the caller saw — or the
    #    pointer this same move already wrote, in which case we finish the job.
    s3_from = ctx.minter.s3(ctx.subject, Shape.WRITE, from_path)
    current, resuming = _source(st, s3_from, from_path, to_path)
    if not resuming and current.version != if_version:
        raise stale(current)

    # 3. Impact, before anything is written.
    changes = access_changes(ctx, from_path, to_path)

    if resuming:
        log_event(ctx, "info", "move_resumed", from_path=from_path, to_path=to_path)
        try:
            beneath = last_content_version(st, s3_from, from_path)
        except AccessDenied:
            raise forbidden() from None
        if beneath is None:
            raise internal(
                f"The pointer at {from_path} has no content version beneath it to move. "
                "Ask an owner to inspect the history."
            )
        content = parse(beneath.body)
        seq = parse(current.body).seq
    else:
        # 4. Pointer first. A stale token fails here with nothing written anywhere.
        content = parse(current.body)
        seq = _write_pointer(ctx, st, s3_from, current, to_path)

    # 5. Content lands at the destination only after the source has committed.
    dest_frontmatter, written = _write_destination(ctx, st, s3_to, from_path, to_path, content, seq)

    # 6. Listings: the child leaves one folder and joins another.
    refresh_parent(ctx, from_path, None)
    refresh_parent(
        ctx,
        to_path,
        ListingChild.article(basename(to_path), written.etag, len(written.body), dest_frontmatter),
    )

    return {
        "from": from_path,
        "to": to_path,
        "version": written.version,
        "seq": seq,
        "access_changes": changes,
        "history_note": HISTORY_NOTE.format(from_path=from_path),
    }


TOOL = Tool(
    name="move_article",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_WRITE,
    handler=handle,
)

__all__ = ["ACCESS_CHANGE_SCHEMA", "HISTORY_NOTE", "TOOL", "access_changes", "handle"]
