"""Listing upkeep from inside a tool — HANDOFF §8.6, increment A.

Every mutating tool refreshes the parent folder's ``_listing.json`` after its own
write lands. Three rules, all here so no tool re-derives them:

* **Grant before mint.** A MAINTAIN credential for a folder is minted only when the
  subject holds ``write`` on that folder (an article-only writer does not; §4.6).
  Otherwise the refresh is skipped and logged — the next folder-level read repairs
  the listing.
* **Never fail the write.** Whatever the refresh raises is logged as
  ``listing_refresh_failed`` and swallowed; the article write already succeeded and
  the listing is a cache with S3 as its authority.
* **Propagation stays inside the grant set.** The index asks ``maintainer(ctx)`` for
  each ancestor it wants to touch and stops at the first one the subject cannot write.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.mcp.protocol import ToolContext
from app.mcp.tools._common import parent_folder
from app.storage.listings import ListingChild, ListingIndex, basename


def maintainer(ctx: ToolContext) -> Callable[[str], Any | None]:
    """A ``client_for`` for ``ListingIndex``: a MAINTAIN client for ``folder`` when the
    subject may write there, else ``None``. Records the grant used for the audit line."""

    def client_for(folder: str) -> Any | None:
        resolution = ctx.grants.resolve(ctx.subject, folder)
        if not resolution.allows(Permission.WRITE):
            return None
        ctx.audit.note(resolution)
        return ctx.minter.s3(ctx.subject, Shape.MAINTAIN, folder)

    return client_for


def index(ctx: ToolContext) -> ListingIndex:
    """The listing index for this deployment, wired to the caller's grants."""
    return ListingIndex(ctx.settings.bucket, client_for=maintainer(ctx), log=ctx.log)


def refresh_parent(ctx: ToolContext, path: str, child: ListingChild | None) -> bool:
    """Upsert (``child``) or drop (``None``) ``path`` in its parent folder's listing.

    Returns ``True`` when a refresh was attempted and succeeded, ``False`` when it was
    skipped (no folder-level write) or failed (logged, never raised).
    """
    folder = parent_folder(path)
    s3 = maintainer(ctx)(folder)
    if s3 is None:
        _log(ctx, "listing_refresh_skipped", folder=folder, path=path)
        return False
    try:
        index(ctx).refresh_child(s3, folder, child, basename(path))
    except Exception as error:  # noqa: BLE001 — a listing failure never fails the write
        _log(ctx, "listing_refresh_failed", folder=folder, path=path, error=repr(error))
        return False
    return True


def _log(ctx: ToolContext, event: str, **fields: Any) -> None:
    log = ctx.log
    if log is None:
        return
    try:
        log.info(event, extra=fields)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["index", "maintainer", "refresh_parent"]
