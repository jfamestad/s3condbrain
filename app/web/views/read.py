"""The read view — HANDOFF §11.6 "Read view".

Browse folders, read articles, walk a path's version history, search: for people who
would rather look something up than ask an agent. This is a second consumer of the
storage and auth layer beside the MCP tools, and it keeps their rules exactly:

* every S3 read goes through a credential minted for the operation's target, with
  the shape the corresponding tool uses — ``READ`` for one article, ``LIST`` for a
  folder listing, ``MAINTAIN`` when the caller may write the folder so a rebuilt
  listing can be persisted (§4.8, §8.5, §8.6);
* the grant check comes before the mint, and a denial renders ``404`` — absence and
  denial are indistinguishable from outside (§10.1, §10.15);
* history is per path: ``continues_at`` is a link, never a traversal (§5.3);
* a pointer is one hop: the moved page names the destination and reads nothing
  there (§5.3, §10.5).

The tools' handlers are not imported — the web app must stay independent of tool
schemas — but their public helpers are: the §10.2 path validators and ``trust_of``
from ``_common``, and the search area/scoring from ``search`` so both surfaces rank
the same way. The per-version ranged read of ``list_versions`` is reimplemented
here (``_peek``) because it is private there.

URL captures are user input: nothing touches storage until the path has passed the
§10.2 grammar (lowercase, no ``.``/``..``, no ``_``-prefixed segment).
"""

from __future__ import annotations

import html as html_mod
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from botocore.exceptions import ClientError

from app.auth.credentials import Shape
from app.auth.types import Grant, Permission
from app.errors import ToolError
from app.mcp.tools._common import (
    HUMAN_ACTOR_PREFIX,
    KIND_MOVED_IN,
    KIND_MOVED_OUT,
    KIND_WRITE,
    TYPE_ARCHIVED,
    TYPE_POINTER,
    article_path,
    folder_path,
    trust_of,
)
from app.mcp.tools._links import TYPE_LINK, is_local, link_target
from app.mcp.tools.search import score, searchable_area, tokens
from app.storage.articles import (
    META_ACTOR,
    META_KIND,
    META_MOVED_FROM,
    AccessDenied,
    StoredObject,
)
from app.storage.listings import Listing, ListingChild, basename, is_stale, join, parent_of
from app.storage.markdown import Article, parse
from app.web.app import PREFIX, page, view
from app.web.context import WebContext
from app.web.http import HttpError, Request, Response, Router
from app.web.render import render, sections

ROOT = "/"

#: Versions per history page (the tool's default).
HISTORY_PAGE = 20
CURSOR_MAX_LENGTH = 2048
VERSION_ID_MAX_LENGTH = 1024
#: Bytes fetched per version on the history page — enough for any §10.2 frontmatter.
PREFIX_BYTES = 16 * 1024
MAX_WORKERS = 8
ACTOR_UNKNOWN = "process:unknown"

QUERY_MAX_LENGTH = 400
SEARCH_LIMIT = 50
#: Folders visited per search before the walk stops and the page says so.
SEARCH_FOLDER_BUDGET = 1_000

#: §4.5: read covers history, so editing is not redaction. Shown wherever people read.
HISTORY_NOTE = (
    "Every version of this page stays readable to anyone who can read this page. "
    "Editing does not remove text from history; moving the page does."
)

# Frontmatter keys the panel renders on their own; anything else is listed as-is.
_PANEL_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "tags",
        "status",
        "stale_after",
        "sources",
        "generated",
        "verified",
        "seq",
    }
)

_HEADING_HTML_RE = re.compile(r"<h([1-6])>(.*?)</h\1>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Template rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Crumb:
    name: str
    href: str


@dataclass(frozen=True)
class FolderRow:
    name: str
    href: str


@dataclass(frozen=True)
class ArticleRow:
    """One article in a folder listing or a search result."""

    path: str
    href: str
    name: str
    title: str
    description: str
    tags: list[str]
    status: str
    stale: bool
    trust: str
    seq: int
    size: int
    score: int = 0


@dataclass(frozen=True)
class TocEntry:
    level: int
    text: str
    anchor: str


@dataclass(frozen=True)
class Attestation:
    by: str
    at: str


@dataclass(frozen=True)
class Source:
    resource: str
    title: str
    author: str
    id: str
    href: str  # empty unless the resource is an http(s) URL


@dataclass(frozen=True)
class VersionRow:
    version_id: str
    href: str
    seq: int
    kind: str
    actor: str
    actor_label: str
    at: str
    size: int | None
    moved_to: str
    moved_from: str


@dataclass(frozen=True)
class GrantRow:
    """One grant, as a sentence fragment for the home page."""

    node: str
    href: str
    permission: str
    is_article: bool


@dataclass
class PageVars:
    """What ``article.html`` and ``version.html`` render from one parsed object."""

    path: str
    title: str
    crumbs: list[Crumb]
    fm: dict[str, Any]
    type: str
    description: str
    tags: list[str]
    status: str
    stale: bool | None
    trust: str
    generated: Attestation | None
    verified: list[Attestation]
    sources: list[Source]
    other: list[tuple[str, str]]
    body_html: str
    toc: list[TocEntry]
    history_href: str
    article_href: str
    moved_to: str = ""
    moved_to_href: str = ""
    moved_at: str = ""
    link_to: str = ""
    link_to_href: str = ""


# ---------------------------------------------------------------------------
# Paths, links, errors
# ---------------------------------------------------------------------------


def _gone() -> HttpError:
    """The one answer for absent, archived and denied alike (§10.1)."""
    return HttpError(404, "No such page.")


def _article_href(path: str) -> str:
    return f"{PREFIX}/a{path}"


def _folder_href(folder: str) -> str:
    return f"{PREFIX}/a" if folder == ROOT else f"{PREFIX}/a{folder}"


def _history_href(path: str) -> str:
    return f"{PREFIX}/history{path}"


def _version_href(path: str, version_id: str) -> str:
    return f"{PREFIX}/v{path}?version={quote(version_id, safe='')}"


def _from_capture(raw: str) -> str:
    """URL capture → absolute path. One trailing slash is tolerated (URL convention);
    anything else the §10.2 grammar decides."""
    return "/" + raw.removesuffix("/")


def _validated(raw: str) -> tuple[str, bool]:
    """``(path, is_article)`` after the §10.2 grammar, or 404.

    Raises:
        HttpError: 404 for anything the grammar rejects.
    """
    path = _from_capture(raw)
    try:
        if path.endswith(".md"):
            return article_path(path), True
        return folder_path(path), False
    except ToolError:
        raise _gone() from None


def _article_only(raw: str) -> str:
    path, is_article = _validated(raw)
    if not is_article:
        raise _gone()
    return path


def _crumbs(path: str) -> list[Crumb]:
    """Wiki › each folder › the leaf; every folder linked to its listing."""
    out = [Crumb("Wiki", _folder_href(ROOT))]
    if path == ROOT:
        return out
    current = ""
    for segment in path.strip("/").split("/"):
        current = f"{current}/{segment}"
        href = _article_href(current) if current.endswith(".md") else _folder_href(current)
        out.append(Crumb(segment, href))
    return out


def _require_read(ctx: WebContext, path: str) -> Permission | None:
    """Grant check before any mint. A denial is a 404, never a 403.

    Returns:
        The effective permission, for callers that pick a credential shape by it.

    Raises:
        HttpError: 404 when the subject lacks ``read``.
    """
    try:
        return ctx.grants.require(ctx.subject, path, Permission.READ).permission
    except ToolError as error:
        if error.status == 403:
            raise _gone() from None
        raise


def _when(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Folder listings
# ---------------------------------------------------------------------------


def _listing(ctx: WebContext, folder: str) -> Listing:
    """Grant check → LIST (or MAINTAIN when the caller may write the folder) →
    ``read_or_rebuild``. Mirrors ``list_folder``.

    Raises:
        HttpError: 404 on denial, from the grant table or from S3.
    """
    permission = _require_read(ctx, folder)
    writable = permission is not None and permission.satisfies(Permission.WRITE)
    s3 = ctx.minter.s3(ctx.subject, Shape.MAINTAIN if writable else Shape.LIST, folder)
    try:
        return ctx.listings.read_or_rebuild(s3, folder, writable=writable)
    except AccessDenied:
        raise _gone() from None


def _article_row(folder: str, child: ListingChild, points: int = 0) -> ArticleRow:
    path = join(folder, child.name)
    return ArticleRow(
        path=path,
        href=_article_href(path),
        name=child.name,
        title=child.title or child.name,
        description=child.description or "",
        tags=list(child.tags),
        status=child.status or "stable",
        stale=bool(is_stale(child.stale_after)),
        trust=child.trust,
        seq=child.seq,
        size=child.size,
        score=points,
    )


def _folder_vars(folder: str, listing: Listing) -> dict[str, Any]:
    folders = [
        FolderRow(c.name, _folder_href(join(folder, c.name)))
        for c in sorted(listing.folders, key=lambda c: c.name)
        if c.visible
    ]
    articles = [_article_row(folder, c) for c in sorted(listing.articles, key=lambda c: c.name)]
    return {
        "folder": folder,
        "crumbs": _crumbs(folder),
        "folders": folders,
        "articles": articles,
    }


def _folder_page(ctx: WebContext, folder: str) -> Response:
    listing = _listing(ctx, folder)
    data = _folder_vars(folder, listing)
    if folder != ROOT and not data["folders"] and not data["articles"]:
        # Nothing visible: no such folder, or one holding only pointers and
        # tombstones. Both read as absence (§10.4).
        raise _gone()
    return page("folder.html", ctx, **data)


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------


def _fetch(ctx: WebContext, path: str) -> StoredObject:
    """Grant check → READ credential for exactly this key → ``GetObject``.

    A 403 from S3 under that credential is what a missing key answers without
    ``s3:ListBucket`` (§8.5), so the store reads it as absence.

    Raises:
        HttpError: 404 when denied (grant table) or absent.
    """
    _require_read(ctx, path)
    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    current = ctx.store.get(s3, path, absent_on_denied=True)
    if current is None:
        raise _gone()
    return current


def _slug(text: str) -> str:
    plain = html_mod.unescape(_TAG_RE.sub("", text)).lower()
    return _SLUG_RE.sub("-", plain).strip("-") or "section"


class _Anchors:
    """Unique heading ids in document order, the same way for the body and the TOC."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def next(self, text: str) -> str:
        base = _slug(text)
        n = self._seen.get(base, 0) + 1
        self._seen[base] = n
        return base if n == 1 else f"{base}-{n}"


def _anchored(body_html: str) -> str:
    """Give every rendered heading an ``id`` the table of contents can point at.

    The rendered HTML comes from ``render`` with raw HTML disabled, so a heading's
    inner content can never contain a closing heading tag; the slug is ``[a-z0-9-]``
    only, so the attribute needs no quoting care.
    """
    anchors = _Anchors()

    def sub(match: re.Match[str]) -> str:
        level, inner = match.group(1), match.group(2)
        return f'<h{level} id="{anchors.next(inner)}">{inner}</h{level}>'

    return _HEADING_HTML_RE.sub(sub, body_html)


def _toc(body: str) -> list[TocEntry]:
    anchors = _Anchors()
    return [
        TocEntry(level, html_mod.unescape(text), anchors.next(text))
        for level, text in sections(body)
    ]


def _attestation(value: Any) -> Attestation | None:
    if not isinstance(value, dict):
        return None
    by = value.get("by")
    if not isinstance(by, str) or not by:
        return None
    return Attestation(by, str(value.get("at") or ""))


def _sources(value: Any) -> list[Source]:
    if not isinstance(value, list):
        return []
    out: list[Source] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        resource = str(entry.get("resource") or "")
        if not resource:
            continue
        href = resource if resource.startswith(("https://", "http://")) else ""
        out.append(
            Source(
                resource=resource,
                title=str(entry.get("title") or ""),
                author=str(entry.get("author") or ""),
                id=str(entry.get("id") or ""),
                href=href,
            )
        )
    return out


def _page_vars(path: str, article: Article) -> PageVars:
    fm = article.frontmatter
    tags = fm.get("tags")
    verified_raw = fm.get("verified")
    verified = (
        [a for a in (_attestation(v) for v in verified_raw) if a is not None]
        if isinstance(verified_raw, list)
        else []
    )
    other = [
        (key, value if isinstance(value, str) else json.dumps(value, default=str))
        for key, value in fm.items()
        if key not in _PANEL_KEYS
    ]
    title = fm.get("title")
    return PageVars(
        path=path,
        title=str(title) if title else basename(path),
        crumbs=_crumbs(path),
        fm=fm,
        type=article.type,
        description=str(fm.get("description") or ""),
        tags=[str(t) for t in tags] if isinstance(tags, list) else [],
        status=str(fm.get("status") or ""),
        stale=is_stale(str(fm["stale_after"])) if fm.get("stale_after") else None,
        trust=trust_of(fm),
        generated=_attestation(fm.get("generated")),
        verified=verified,
        sources=_sources(fm.get("sources")),
        other=other,
        body_html=_anchored(render(article.body, PREFIX)),
        toc=_toc(article.body),
        history_href=_history_href(path),
        article_href=_article_href(path),
    )


def _link_vars(path: str, article: Article) -> PageVars:
    """A link's page: names the target, reads nothing there (s3condbrain S7).

    Only a valid local target becomes a link; a foreign reference or a target a raw
    write left malformed is shown as text.
    """
    data = _page_vars(path, article)
    target = str(article.frontmatter.get("link_to") or "")
    data.link_to = target
    try:
        valid = link_target(target)
    except ToolError:
        valid = ""
    if valid and is_local(valid):
        data.link_to_href = _article_href(valid) if valid.endswith(".md") else _folder_href(valid)
    data.body_html = ""
    data.toc = []
    return data


def _moved_vars(path: str, article: Article) -> PageVars:
    """The one-hop page for a pointer: names the destination, reads nothing there."""
    data = _page_vars(path, article)
    moved_to = str(article.frontmatter.get("moved_to") or "")
    data.moved_to = moved_to
    data.moved_to_href = _article_href(moved_to) if moved_to.startswith("/") else ""
    data.moved_at = str(article.frontmatter.get("moved_at") or "")
    data.body_html = ""
    data.toc = []
    return data


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _is_denied(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    return code in ("AccessDenied", "403") or status == 403


def _is_gone_error(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    return code in ("NoSuchKey", "NoSuchVersion", "NotFound", "404") or status == 404


def _peek(ctx: WebContext, s3: Any, path: str, entry: StoredObject) -> StoredObject | None:
    """``GetObject?versionId=`` for the first ``PREFIX_BYTES`` of one version: the
    metadata a head would give plus the frontmatter block (``seq`` lives there).
    ``None`` when the version is no longer there.

    Raises:
        AccessDenied: on 403.
    """
    request: dict[str, Any] = {
        "Bucket": ctx.store.bucket,
        "Key": entry.key,
        "VersionId": entry.version_id,
    }
    if entry.size:
        # A range on a zero-byte object is 416 InvalidRange; there is nothing to skip.
        request["Range"] = f"bytes=0-{PREFIX_BYTES - 1}"
    try:
        response = s3.get_object(**request)
    except ClientError as error:
        if _is_gone_error(error):
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
    cut_short = (
        peeked.size is not None
        and len(body) < peeked.size
        and body.startswith(b"---")
        and not parse(body).frontmatter
    )
    if cut_short:
        # The block outran the prefix. Read the whole version rather than guess.
        whole = ctx.store.get_version(s3, path, entry.version_id or "")
        return whole if whole is not None else peeked
    return peeked


class _Actors:
    """``human:<sub>`` → display name from the PROFILE row, once per subject."""

    def __init__(self, ctx: WebContext) -> None:
        self._ctx = ctx
        self._names: dict[str, str] = {}

    def label(self, actor: str) -> str:
        if not actor.startswith(HUMAN_ACTOR_PREFIX):
            return actor
        subject = actor[len(HUMAN_ACTOR_PREFIX) :]
        if subject not in self._names:
            profile = self._ctx.admin.get_profile(subject)
            name = profile.display_name if profile is not None else ""
            self._names[subject] = name or subject
        return self._names[subject]


def _version_row(path: str, version: StoredObject, article: Article, actors: _Actors) -> VersionRow:
    kind = version.metadata.get(META_KIND) or KIND_WRITE
    actor = version.metadata.get(META_ACTOR) or ACTOR_UNKNOWN
    moved_to = ""
    if kind == KIND_MOVED_OUT or article.type == TYPE_POINTER:
        raw = article.frontmatter.get("moved_to")
        if isinstance(raw, str):
            moved_to = raw
    version_id = version.version_id or ""
    return VersionRow(
        version_id=version_id,
        href=_version_href(path, version_id),
        seq=article.seq,
        kind=kind,
        actor=actor,
        actor_label=actors.label(actor),
        at=_when(version.last_modified),
        size=version.size,
        moved_to=moved_to,
        moved_from=version.metadata.get(META_MOVED_FROM) or "",
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _walk(
    ctx: WebContext,
    root: str,
    query_tokens: list[str],
    hits: list[ArticleRow],
    budget: int,
) -> int:
    """Breadth-first over listings from ``root`` under one LIST credential, exactly
    as the search tool walks. Reads at most ``budget`` folders; returns what is left."""
    s3 = ctx.minter.s3(ctx.subject, Shape.LIST, root)
    queue = [root]
    while queue and budget > 0:
        budget -= 1
        folder = queue.pop(0)
        try:
            listing = ctx.listings.read(s3, folder) or ctx.listings.rebuild(
                s3, folder, persist=False
            )
        except AccessDenied:
            continue  # the credential decides what is reachable; nothing to add
        for child in listing.children:
            if child.is_folder:
                queue.append(join(folder, child.name))
                continue
            points = score(child, query_tokens)
            if points > 0:
                hits.append(_article_row(folder, child, points))
    return budget if not queue else 0


def _granted_article(ctx: WebContext, path: str, query_tokens: list[str]) -> ArticleRow | None:
    """An article grant contributes that one article: a ranged read under READ."""
    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    try:
        child = ctx.listings.project(s3, path)
    except AccessDenied:
        return None
    if child is None:
        return None
    points = score(child, query_tokens)
    return _article_row(parent_of(path), child, points) if points > 0 else None


def _search(ctx: WebContext, query: str, prefix: str | None) -> tuple[list[ArticleRow], bool]:
    """``(hits, truncated)`` over the caller's grant set (§8.7)."""
    query_tokens = tokens(query)
    if not query_tokens:
        return [], False
    grants = ctx.grants.all_grants(ctx.subject)
    if not grants:
        return [], False
    walk, articles = searchable_area(grants, prefix)
    hits: list[ArticleRow] = []
    budget = SEARCH_FOLDER_BUDGET
    for root in walk:
        budget = _walk(ctx, root, query_tokens, hits, budget)
    for path in articles:
        hit = _granted_article(ctx, path, query_tokens)
        if hit is not None:
            hits.append(hit)
    hits.sort(key=lambda h: (-h.score, h.path))
    truncated = len(hits) > SEARCH_LIMIT or budget <= 0
    return hits[:SEARCH_LIMIT], truncated


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------


def _grant_rows(grants: list[Grant]) -> list[GrantRow]:
    rows = [
        GrantRow(
            node=g.node,
            href=_article_href(g.node) if g.is_article else _folder_href(g.node),
            permission=g.permission.value,
            is_article=g.is_article,
        )
        for g in grants
    ]
    return sorted(rows, key=lambda r: (r.node, r.permission))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def home(request: Request, ctx: WebContext) -> Response:
    """The root listing when the caller may read ``/``; otherwise the places they
    can start from. Either way, a note on what "what you can see" means (§3.3)."""
    grants = ctx.grants.all_grants(ctx.subject)
    data: dict[str, Any] = {
        "grant_rows": _grant_rows(grants),
        "root_visible": False,
        "folder": ROOT,
        "crumbs": [],
        "folders": [],
        "articles": [],
    }
    if ctx.grants.resolve(ctx.subject, ROOT).allows(Permission.READ):
        listing = _listing(ctx, ROOT)
        data.update(_folder_vars(ROOT, listing))
        data["root_visible"] = True
    return page("home.html", ctx, **data)


def root(request: Request, ctx: WebContext) -> Response:
    return _folder_page(ctx, ROOT)


def node(request: Request, ctx: WebContext) -> Response:
    """``/a/<folder>`` or ``/a/<article>.md``."""
    path, is_article = _validated(request.params["path"])
    if not is_article:
        return _folder_page(ctx, path)
    current = _fetch(ctx, path)
    article = parse(current.body)
    if article.type == TYPE_POINTER:
        return page("article.html", ctx, page=_moved_vars(path, article), history_note=HISTORY_NOTE)
    if article.type == TYPE_LINK:
        return page("article.html", ctx, page=_link_vars(path, article), history_note=HISTORY_NOTE)
    if article.type == TYPE_ARCHIVED:
        raise _gone()
    return page("article.html", ctx, page=_page_vars(path, article), history_note=HISTORY_NOTE)


def _owner_links(ctx: WebContext, path: str) -> dict[str, str]:
    """Audit and hard-delete runbook links when the viewer owns the path (§8.9, §11.6)."""
    try:
        roots = ctx.admin.owned_roots(ctx.subject)
    except Exception:  # noqa: BLE001 — a link is a convenience, never a failure
        return {}
    if not any(path == r or (r == "/") or path.startswith(r + "/") for r in roots):
        return {}
    prefix = ctx.app_prefix
    return {
        "audit": f"{prefix}/admin/audit?path={quote(path, safe='/')}",
        "delete": f"{prefix}/admin/delete{path}",
    }


def history(request: Request, ctx: WebContext) -> Response:
    """The version chain of one path, newest first — this path's chain only."""
    path = _article_only(request.params["path"])
    cursor = request.query.get("cursor") or None
    if cursor is not None and len(cursor) > CURSOR_MAX_LENGTH:
        raise _gone()
    _require_read(ctx, path)
    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    try:
        listed, next_cursor = ctx.store.list_versions(s3, path, limit=HISTORY_PAGE, cursor=cursor)
    except ValueError:
        raise HttpError(
            400, "That history link is not one this page issued. Start from the newest version."
        ) from None
    except AccessDenied:
        raise _gone() from None
    if not listed:
        raise _gone()
    try:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(listed))) as pool:
            peeked = list(pool.map(lambda entry: _peek(ctx, s3, path, entry), listed))
    except AccessDenied:
        raise _gone() from None

    actors = _Actors(ctx)
    rows = [_version_row(path, v, parse(v.body), actors) for v in peeked if v is not None]
    continues_at = ""
    if next_cursor is None and rows:
        # This page holds the oldest version. If the chain began with a move, say
        # where it came from — and go no further (§5.3: history is per path).
        oldest = rows[-1]
        if oldest.kind == KIND_MOVED_IN and oldest.moved_from:
            continues_at = oldest.moved_from
    return page(
        "history.html",
        ctx,
        path=path,
        crumbs=_crumbs(path),
        article_href=_article_href(path),
        rows=rows,
        next_cursor=next_cursor or "",
        next_href=f"{_history_href(path)}?cursor={next_cursor}" if next_cursor else "",
        continues_at=continues_at,
        continues_href=_history_href(continues_at) if continues_at else "",
        history_note=HISTORY_NOTE,
        # §8.9: the audit log is reachable from the read view, for the people who
        # own the path. Owners of nothing see no admin links here.
        owner_links=_owner_links(ctx, path),
    )


def version(request: Request, ctx: WebContext) -> Response:
    """One historical version, pinned by id. Pointer and tombstone versions are
    history and render as stored, with a note — never a forward reference."""
    path = _article_only(request.params["path"])
    version_id = (request.query.get("version") or "").strip()
    if not version_id or len(version_id) > VERSION_ID_MAX_LENGTH:
        raise _gone()
    _require_read(ctx, path)
    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    # READ for exactly this key: a 403 on a missing key or version is absence (§8.5).
    stored = ctx.store.get_version(s3, path, version_id, absent_on_denied=True)
    if stored is None:
        raise _gone()
    article = parse(stored.body)
    data = _page_vars(path, article)
    note = ""
    if article.type == TYPE_POINTER:
        note = (
            "This version is a move pointer: at this point the page had been moved "
            "away and this path held only a forward reference."
        )
    elif article.type == TYPE_ARCHIVED:
        note = "This version is an archive tombstone: at this point the page had been archived."
    actor = stored.metadata.get(META_ACTOR) or ACTOR_UNKNOWN
    return page(
        "version.html",
        ctx,
        page=data,
        history_note=HISTORY_NOTE,
        version_id=stored.version_id or version_id,
        version_seq=article.seq,
        version_at=_when(stored.last_modified),
        version_actor=actor,
        version_actor_label=_Actors(ctx).label(actor),
        version_kind=stored.metadata.get(META_KIND) or KIND_WRITE,
        version_note=note,
    )


def search(request: Request, ctx: WebContext) -> Response:
    """Form plus results. The grant set is the searchable area; ``prefix`` narrows it
    and never widens it (§8.7)."""
    query = (request.query.get("q") or "").strip()[:QUERY_MAX_LENGTH]
    raw_prefix = (request.query.get("prefix") or "").strip()
    data: dict[str, Any] = {
        "q": query,
        "folder_prefix": raw_prefix,
        "searched": False,
        "hits": [],
        "truncated": False,
        "prefix_error": "",
    }
    if not query:
        return page("search.html", ctx, **data)
    prefix: str | None = None
    if raw_prefix:
        try:
            prefix = folder_path(raw_prefix, "prefix")
        except ToolError:
            data["prefix_error"] = (
                "The folder must be an absolute, lowercase path with no trailing slash, "
                "like /racing/setup."
            )
            return page("search.html", ctx, **data)
    hits, truncated = _search(ctx, query, prefix)
    data.update({"searched": True, "hits": hits, "truncated": truncated})
    return page("search.html", ctx, **data)


def register(router: Router) -> None:
    router.add("GET", "", view(home))
    router.add("GET", "/a", view(root))
    router.add("GET", "/a/{path:path}", view(node))
    router.add("GET", "/history/{path:path}", view(history))
    router.add("GET", "/v/{path:path}", view(version))
    router.add("GET", "/search", view(search))


__all__ = ["HISTORY_NOTE", "register"]
