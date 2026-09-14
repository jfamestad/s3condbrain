"""The move page — HANDOFF §4.6 "a move is a boundary decision", §4.7, §11.6.

A move through which anyone *gains* access is the one write the tool surface refuses
(``403 boundary_change``): text an agent has read can instruct it, and a move that
discloses cannot be undone by moving back. This page is where such a move happens.
A person previews the impact — who gains and who loses, as sentences — and confirms.
The confirm runs the same ``perform`` the tool runs, pointer first, with the same
``if_version`` guard and ``allow_widening=True``.

Who may confirm: ``write`` on both ends, as the tool requires, and — because this is
the human surface for boundary changes — ``own`` on the destination's folder or an
ancestor (§4.10), checked through ``GrantAdmin.assert_owner`` like every other
privilege change in the console. Moves that widen nobody's access work here too; the
tool surface is simply the other place they can be done.

Nothing here is an MCP tool, and the impact report is computed the same way on both
surfaces (``move_article.access_changes``), so what the person confirms is exactly
what the agent was refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.auth.admin import NotAnOwner
from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ, SCOPE_WRITE
from app.errors import ToolError
from app.mcp.protocol import AuditTrail, ToolContext
from app.mcp.tools._common import (
    PATH_MAX_LENGTH,
    TYPE_ARCHIVED,
    TYPE_POINTER,
    VERSION_MAX_LENGTH,
    article_path,
    parent_folder,
    reject_reserved_name,
)
from app.mcp.tools.move_article import access_changes, perform
from app.storage.articles import AccessDenied, StoredObject
from app.storage.markdown import parse
from app.web.app import page, view
from app.web.context import WebContext
from app.web.http import HttpError, Request, Response, Router
from app.web.views.admin import Scope

TEMPLATE = "admin/move.html"

NO_SUCH_SOURCE = "No such article at the source path."
NEED_WRITE = "You need write access on both paths to move an article."
NOT_PREVIEWED = "Preview the move first."
STALE = (
    "The article changed since you previewed this move, so nothing was moved. "
    "Preview again to see its current state."
)
OCCUPIED = (
    "Something already occupies the destination — an article, a move pointer or an "
    "archive tombstone. Choose a different destination."
)


@dataclass(frozen=True)
class ChangeRow:
    """One access change as a sentence: *"Dana gains read (via /family/shared)"*."""

    text: str
    direction: str


# --- validation and authorization --------------------------------------------------------


def _scope(ctx: WebContext) -> Scope:
    return Scope(tuple(ctx.admin.owned_roots(ctx.subject)))


def _paths(form: dict[str, str]) -> tuple[str, str]:
    """Both paths through the §10.2 grammar, reserved names refused on the destination.

    Raises:
        HttpError: 400 with the validator's own sentence.
    """
    try:
        from_path = article_path((form.get("from") or "").strip(), "from")
        to_path = article_path((form.get("to") or "").strip(), "to")
        reject_reserved_name(to_path)
    except ToolError as error:
        raise HttpError(400, error.message) from None
    if from_path == to_path:
        raise HttpError(400, "The article is already there. Give a different destination.")
    return from_path, to_path


def _authorize(ctx: WebContext, from_path: str, to_path: str) -> None:
    """``write`` on both ends (the tool's rule) and ``own`` over the destination's
    folder (this page's rule, §4.10). Both before anything touches storage.

    Raises:
        HttpError: 403 naming which rule failed.
    """
    try:
        ctx.grants.require(ctx.subject, from_path, Permission.WRITE)
        ctx.grants.require(ctx.subject, to_path, Permission.WRITE)
    except ToolError:
        raise HttpError(403, NEED_WRITE) from None
    folder = parent_folder(to_path)
    try:
        ctx.admin.assert_owner(ctx.subject, folder)
    except NotAnOwner:
        raise HttpError(
            403,
            f"You do not own {folder}, so you cannot move an article there from here. "
            "Moving an article can give people access, and only an owner of the "
            "destination may do that.",
        ) from None


def _source(ctx: WebContext, from_path: str, to_path: str) -> tuple[StoredObject, bool]:
    """The current object at ``from`` under a READ credential, and whether it is the
    pointer of a half-complete move to this same destination (§8.3).

    Raises:
        HttpError: 404 when nothing live is there — absence, an archive tombstone,
            a pointer elsewhere, or S3 refusing, all alike (§10.1).
    """
    s3 = ctx.minter.s3(ctx.subject, Shape.READ, from_path)
    try:
        current = ctx.store.get(s3, from_path)
    except AccessDenied:
        raise HttpError(404, NO_SUCH_SOURCE) from None
    if current is None:
        raise HttpError(404, NO_SUCH_SOURCE)
    article = parse(current.body)
    if article.type == TYPE_POINTER:
        if article.frontmatter.get("moved_to") == to_path:
            return current, True
        raise HttpError(404, NO_SUCH_SOURCE)
    if article.type == TYPE_ARCHIVED:
        raise HttpError(404, NO_SUCH_SOURCE)
    return current, False


def _destination_clear(ctx: WebContext, to_path: str) -> None:
    """Say so at preview time rather than at confirm. The tool re-checks under its
    own WRITE credential; this head is a courtesy under READ.

    Raises:
        HttpError: 409 when anything occupies the destination.
    """
    s3 = ctx.minter.s3(ctx.subject, Shape.READ, to_path)
    try:
        occupant = ctx.store.head(s3, to_path)
    except AccessDenied:
        occupant = None  # the confirm will say so under the credential that matters
    if occupant is not None:
        raise HttpError(409, OCCUPIED)


# --- the report --------------------------------------------------------------------------


def _tool_context(ctx: WebContext) -> ToolContext:
    """The tool's context, built from the session: same grants, same minter, same
    per-operation credentials (§4.8). Identity is the signed-in person's."""
    return ToolContext(
        subject=ctx.subject,
        scopes=frozenset({SCOPE_READ, SCOPE_WRITE}),
        grants=ctx.grants,
        minter=ctx.minter,
        settings=ctx.settings,
        request_id=ctx.request_id,
        log=ctx.log,
        audit=AuditTrail(),
    )


def _name(ctx: WebContext, change: dict[str, Any]) -> str:
    """The report already carries ``display_name`` from the PROFILE row when there is
    one; fall back to the profile's email, then the subject."""
    name = change.get("display_name")
    if isinstance(name, str) and name:
        return name
    profile = ctx.admin.get_profile(change["subject"])
    if profile is not None and (profile.display_name or profile.email):
        return profile.display_name or profile.email
    return str(change["subject"])


def _rows(ctx: WebContext, changes: list[dict[str, Any]]) -> list[ChangeRow]:
    rows: list[ChangeRow] = []
    for change in changes:
        text = f"{_name(ctx, change)} {change['direction']} {change['permission']}"
        if change.get("via"):
            text += f" (via {change['via']})"
        rows.append(ChangeRow(text, change["direction"]))
    return rows


def _gains(changes: list[dict[str, Any]]) -> int:
    return sum(1 for c in changes if c["direction"] == "gains")


# --- handlers ----------------------------------------------------------------------------


def form(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    from_path = (request.query.get("from") or "")[:PATH_MAX_LENGTH]
    return page(TEMPLATE, ctx, scope=scope, from_path=from_path, to_path="")


def _preview(ctx: WebContext, scope: Scope, form: dict[str, str]) -> Response:
    from_path, to_path = _paths(form)
    _authorize(ctx, from_path, to_path)
    current, resuming = _source(ctx, from_path, to_path)
    _destination_clear(ctx, to_path)
    changes = access_changes(_tool_context(ctx), from_path, to_path)
    return page(
        TEMPLATE,
        ctx,
        scope=scope,
        from_path=from_path,
        to_path=to_path,
        preview=True,
        resuming=resuming,
        if_version=current.version,
        changes=_rows(ctx, changes),
        gains=_gains(changes),
    )


def _confirm(ctx: WebContext, scope: Scope, form: dict[str, str]) -> Response:
    from_path, to_path = _paths(form)
    if_version = (form.get("if_version") or "").strip()
    if not if_version or len(if_version) > VERSION_MAX_LENGTH:
        raise HttpError(400, NOT_PREVIEWED)
    _authorize(ctx, from_path, to_path)
    try:
        result = perform(_tool_context(ctx), from_path, to_path, if_version, allow_widening=True)
    except ToolError as error:
        stale = error.status == 409 and "current_version" in error.extra
        return page(
            TEMPLATE,
            ctx,
            error.status,
            scope=scope,
            from_path=from_path,
            to_path=to_path,
            error=STALE if stale else error.message,
        )
    changes = result["access_changes"]
    ctx.log.info(
        "console_move",
        request_id=ctx.request_id,
        subject=ctx.subject,
        from_path=from_path,
        to_path=to_path,
        gains=_gains(changes),
    )
    return page(
        TEMPLATE,
        ctx,
        scope=scope,
        done=True,
        from_path=from_path,
        to_path=to_path,
        version=result["version"],
        changes=_rows(ctx, changes),
        gains=_gains(changes),
        history_note=result["history_note"],
    )


def submit(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    form = request.form()
    action = (form.get("action") or "").strip()
    if action == "preview":
        return _preview(ctx, scope, form)
    if action == "confirm":
        return _confirm(ctx, scope, form)
    raise HttpError(400, NOT_PREVIEWED)


def register(router: Router) -> None:
    router.add("GET", "/admin/move", view(form))
    router.add("POST", "/admin/move", view(submit))


__all__ = ["NEED_WRITE", "NO_SUCH_SOURCE", "STALE", "ChangeRow", "register"]
