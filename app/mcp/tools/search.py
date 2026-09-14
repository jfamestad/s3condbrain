"""``search`` — ``wiki.read`` · ``read`` on each hit (HANDOFF §10.3, §8.7).

Metadata matching, filtered by path against the caller's grants. The grant set *is*
the searchable area:

* a **folder grant** contributes everything beneath it — the walk reads that folder's
  ``_listing.json`` and descends through the child folders each listing names, under
  one LIST credential minted for the folder (or for ``prefix`` when it narrows the
  grant, which is strictly inside it);
* an **article grant** contributes that one article — one ranged ``GetObject`` on
  the key under a READ credential minted for exactly that key.

Nothing outside the grant set is fetched, matched or ranked, so there is no
post-filter: a path the caller cannot reach never enters the candidate set (§4.8).
``prefix`` narrows and never widens; a prefix outside every grant yields an empty
result, indistinguishable from nothing matching. A subject with no grants mints
nothing (§12.8 item 5).

Match: every query token (lowercased, split on non-alphanumerics) is looked for as
a substring of the title, description, tags and the article's own name. Score is
the weighted count of matched tokens — title 3, name 2, tags 2, description 1.
Hits carry an ``article_summary`` plus ``snippet`` (description, else title) and
``score``, sorted by score then path.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from app.auth.credentials import Shape
from app.auth.types import Grant, Permission, Resolution
from app.config import SCOPE_READ
from app.errors import bad_request
from app.mcp.protocol import Tool, ToolContext
from app.mcp.tools._common import (
    ARTICLE_SUMMARY_SCHEMA,
    FOLDER_PATH_SCHEMA,
    folder_path,
)
from app.mcp.tools._listings import index
from app.mcp.tools.list_folder import summary_of
from app.storage.articles import AccessDenied
from app.storage.listings import ListingChild, ListingIndex, join, parent_of

DESCRIPTION = (
    "Find articles by words in their title, description or tags, within everything "
    "you can see — folder grants contribute their subtrees, article grants contribute "
    "themselves. Returns ranked summaries with a snippet, never article bodies. Use "
    "this first when you do not already know a path."
)

QUERY_MAX_LENGTH = 400
SNIPPET_MAX_LENGTH = 400
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
#: Folders visited per search before the walk stops and ``truncated`` is set.
MAX_FOLDERS_VISITED = 1_000

WEIGHT_TITLE = 3
WEIGHT_NAME = 2
WEIGHT_TAGS = 2
WEIGHT_DESCRIPTION = 1

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": QUERY_MAX_LENGTH},
        "prefix": {
            **FOLDER_PATH_SCHEMA,
            "description": "Restrict to this folder and everything beneath it.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_LIMIT,
        },
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["hits", "truncated"],
    "properties": {
        "hits": {
            "type": "array",
            "items": {
                **ARTICLE_SUMMARY_SCHEMA,
                "properties": {
                    **ARTICLE_SUMMARY_SCHEMA["properties"],
                    "snippet": {"type": "string", "maxLength": SNIPPET_MAX_LENGTH},
                    "score": {"type": "number"},
                },
            },
        },
        "truncated": {"type": "boolean"},
    },
}

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _query_arg(args: dict[str, Any]) -> str:
    value = args.get("query")
    if not isinstance(value, str) or not value.strip():
        raise bad_request("'query' is required and must be a non-empty string.")
    if len(value) > QUERY_MAX_LENGTH:
        raise bad_request(f"'query' must be at most {QUERY_MAX_LENGTH} characters.")
    return value


def _limit_arg(args: dict[str, Any]) -> int:
    value = args.get("limit", DEFAULT_LIMIT)
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise bad_request(f"'limit' must be an integer between 1 and {MAX_LIMIT}.")
    return value


def tokens(query: str) -> list[str]:
    """Lowercased alphanumeric tokens, de-duplicated, order kept."""
    seen: dict[str, None] = {}
    for token in _TOKEN_RE.split(query.lower()):
        if token:
            seen.setdefault(token, None)
    return list(seen)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def score(child: ListingChild, query_tokens: list[str]) -> int:
    """Weighted count of query tokens found in the child's projected fields."""
    title = (child.title or "").lower()
    description = (child.description or "").lower()
    tags = [t.lower() for t in child.tags]
    name = child.name[:-3].lower() if child.name.endswith(".md") else child.name.lower()
    total = 0
    for token in query_tokens:
        if token in title:
            total += WEIGHT_TITLE
        if token in name:
            total += WEIGHT_NAME
        if any(token in tag for tag in tags):
            total += WEIGHT_TAGS
        if token in description:
            total += WEIGHT_DESCRIPTION
    return total


def _hit(folder: str, child: ListingChild, points: int) -> dict[str, Any]:
    snippet = child.description or child.title or ""
    return {
        **summary_of(folder, child),
        "snippet": snippet[:SNIPPET_MAX_LENGTH],
        "score": points,
    }


# ---------------------------------------------------------------------------
# The searchable area
# ---------------------------------------------------------------------------


def _within(path: str, folder: str) -> bool:
    """``path`` is ``folder`` or beneath it."""
    return folder == "/" or path == folder or path.startswith(folder + "/")


def searchable_area(grants: list[Grant], prefix: str | None) -> tuple[list[str], list[str]]:
    """``(folder roots, article paths)`` to read, after collapsing nested folder grants
    and applying ``prefix``.

    A folder grant above ``prefix`` starts the walk at ``prefix``; one beneath it
    starts at the grant; one disjoint from it contributes nothing. Article grants
    count only when inside ``prefix`` and not already under a folder root.
    """
    folders = sorted({g.node for g in grants if not g.is_article}, key=len)
    roots: list[str] = []
    for folder in folders:
        if any(_within(folder, root) for root in roots):
            continue  # a broader grant already covers this subtree
        roots.append(folder)

    walk: list[str] = []
    for root in roots:
        if prefix is None or _within(prefix, root):
            walk.append(prefix if prefix is not None else root)
        elif _within(root, prefix):
            walk.append(root)
    walk = sorted(set(walk))

    articles: list[str] = []
    for grant in grants:
        if not grant.is_article:
            continue
        path = grant.node
        if prefix is not None and not _within(path, prefix):
            continue
        if any(_within(path, root) for root in walk):
            continue
        articles.append(path)
    return walk, sorted(set(articles))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _walk(
    ctx: ToolContext,
    idx: ListingIndex,
    root: str,
    query_tokens: list[str],
    hits: list[dict[str, Any]],
    budget: int,
) -> int:
    """Breadth-first over listings from ``root`` under one LIST credential. Reads at
    most ``budget`` folders; returns what is left of it."""
    s3 = ctx.minter.s3(ctx.subject, Shape.LIST, root)
    queue: deque[str] = deque([root])
    while queue and budget > 0:
        budget -= 1
        folder = queue.popleft()
        try:
            listing = idx.read(s3, folder) or idx.rebuild(s3, folder, persist=False)
        except AccessDenied:
            continue  # the credential decides what is reachable; nothing to add
        for child in listing.children:
            if child.is_folder:
                queue.append(join(folder, child.name))
                continue
            points = score(child, query_tokens)
            if points > 0:
                hits.append(_hit(folder, child, points))
    return budget if not queue else 0


def _granted_article(
    ctx: ToolContext, idx: ListingIndex, path: str, query_tokens: list[str]
) -> dict[str, Any] | None:
    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    try:
        child = idx.project(s3, path)
    except AccessDenied:
        return None
    if child is None:
        return None
    points = score(child, query_tokens)
    return _hit(parent_of(path), child, points) if points > 0 else None


def handle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    query = _query_arg(args)
    prefix = folder_path(args["prefix"], "prefix") if args.get("prefix") is not None else None
    limit = _limit_arg(args)
    query_tokens = tokens(query)

    empty = {"hits": [], "truncated": False}
    if not query_tokens:
        return empty
    grants = ctx.grants.all_grants(ctx.subject)
    if not grants:
        return empty
    # The whole grant set defines the area, so the whole set is what this call used.
    ctx.audit.note(Resolution(Permission.READ, tuple(grants)))

    walk, articles = searchable_area(grants, prefix)
    idx = index(ctx)
    hits: list[dict[str, Any]] = []
    budget = MAX_FOLDERS_VISITED
    for root in walk:
        budget = _walk(ctx, idx, root, query_tokens, hits, budget)
    for path in articles:
        hit = _granted_article(ctx, idx, path, query_tokens)
        if hit is not None:
            hits.append(hit)

    hits.sort(key=lambda h: (-h["score"], h["path"]))
    truncated = len(hits) > limit or budget <= 0
    return {"hits": hits[:limit], "truncated": truncated}


TOOL = Tool(
    name="search",
    description=DESCRIPTION,
    input_schema=INPUT_SCHEMA,
    output_schema=OUTPUT_SCHEMA,
    scope=SCOPE_READ,
    handler=handle,
)

__all__ = ["TOOL", "handle", "score", "searchable_area", "tokens"]
