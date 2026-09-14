"""Per-folder listing projection — HANDOFF §8.6, increment A.

``a/<folder>/_listing.json`` holds the projected frontmatter of a folder's immediate
children so one ``GetObject`` renders ``list_folder`` and feeds ``search``. It lives
inside the folder so it inherits the folder's grants.

Rules (all from §8.6):
* Rewritten whenever a child is created, updated, archived, unarchived or moved.
  Children whose ``type`` is ``pointer`` or ``archived`` are excluded; that is how
  §5.2/§5.3 "leaves listings" is implemented.
* **Conditional write.** Read ETag → modify → ``PutObject If-Match``; on 412 re-read
  and retry, bounded. A missing listing is created with ``If-None-Match: *``.
* **Tagged** ``wiki:listing=true`` (config ``LISTING_TAG``) for the lifecycle rule.
* **Self-healing on read.** ``list_folder`` compares the listing's children
  ``(name, etag)`` pairs with a live ``ListObjectsV2`` and rebuilds on mismatch, so
  a listing that ever went stale repairs itself on the next read.
* **Folder visibility.** A child folder appears only while it has ≥ 1 visible child.
  When a refresh finds a folder's visible-child count crossed zero, it refreshes the
  parent listing too (bounded by depth).

Credentials: refresh/rebuild run under ``Shape.MAINTAIN`` for the folder — the caller
already holds ``write`` on the folder or an ancestor. An article-only writer cannot
refresh the parent listing; the write still succeeds and the next folder-level read
repairs the listing (documented cost of §4.6's "searchable, not listable").

Two details the wire format carries beyond the §8.6 sketch:

* ``excluded`` — the ``(name, etag)`` of every ``.md`` child the rebuild dropped for
  carrying a reserved type. The self-heal comparison is then exact: the listing knows
  every live ``.md`` key, so a folder holding tombstones is not rebuilt on every read,
  and a genuinely new key (or a changed one) is the only thing that triggers one.
* Folder entries are stored whatever their ``visible_children``; ``list_folder``
  filters to the visible ones. Storing them all keeps the folder name set comparable
  with the live ``CommonPrefixes`` and lets a later refresh flip the bit in place.

Propagation to the parent needs a credential for the *parent* folder, which the
credential handed in cannot reach (it is scoped to this folder's prefix). The index
takes an optional ``client_for(folder)`` factory for that; when it returns ``None``
(the caller lacks ``write`` on the parent) propagation stops and is logged. Without a
factory the same client is reused, which works against moto and is refused by S3 in
production — also logged, never raised.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from app.config import LISTING_NAME, LISTING_TAG, RESERVED_TYPES
from app.errors import internal
from app.mcp.tools._common import trust_of
from app.storage.articles import AccessDenied, PreconditionFailed, folder_prefix, path_for
from app.storage.markdown import Article, parse

KIND_ARTICLE = "article"
KIND_FOLDER = "folder"
ROOT = "/"

LISTING_CONTENT_TYPE = "application/json"
#: Bytes fetched per child article on rebuild; frontmatter that does not close inside
#: this window costs one more, unranged, read.
FRONTMATTER_RANGE_BYTES = 16 * 1024
#: Conditional-write attempts before giving up (§8.6 "bounded to a handful").
MAX_WRITE_ATTEMPTS = 5
#: Parent-propagation bound; path depth is bounded by the grammar long before this.
MAX_PROPAGATION_DEPTH = 64

_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NotFound", "404"})
_DENIED_CODES = frozenset({"AccessDenied", "403"})
_PRECONDITION_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict", "412"})

_fallback_log = logging.getLogger(__name__)

ClientFor = Callable[[str], Any | None]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def listing_key(folder: str) -> str:
    """``/racing`` → ``a/racing/_listing.json``; ``/`` → ``a/_listing.json``."""
    return folder_prefix(folder) + LISTING_NAME


def parent_of(folder: str) -> str:
    """``/racing/setup`` → ``/racing``; ``/racing`` → ``/``; ``/`` → ``/``."""
    head = folder.rstrip("/").rsplit("/", 1)[0]
    return head or ROOT


def basename(path: str) -> str:
    """Last segment: ``/racing/setup`` → ``setup``; ``/racing/x.md`` → ``x.md``."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def join(folder: str, name: str) -> str:
    """``/racing`` + ``setup`` → ``/racing/setup``; root + ``x.md`` → ``/x.md``."""
    return f"{folder.rstrip('/')}/{name}"


def is_stale(stale_after: str | None, now: datetime | None = None) -> bool | None:
    """``True`` when ``stale_after`` has passed; ``None`` when absent or unparseable."""
    if not stale_after:
        return None
    text = stale_after.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when <= (now or datetime.now(UTC))


def _strip(etag: str) -> str:
    return etag.strip('"')


def _quoted(etag: str) -> str:
    return etag if etag.startswith('"') else f'"{etag}"'


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _status(error: ClientError) -> int:
    return int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def _is_not_found(error: ClientError) -> bool:
    return _code(error) in _NOT_FOUND_CODES or _status(error) == 404


def _is_denied(error: ClientError) -> bool:
    return _code(error) in _DENIED_CODES or _status(error) == 403


def _is_precondition(error: ClientError) -> bool:
    return _code(error) in _PRECONDITION_CODES or _status(error) == 412


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


@dataclass
class ListingChild:
    """One projected child (§8.6 schema)."""

    name: str  # last path segment, e.g. ``rear-bar.md`` or ``setup`` for a folder
    kind: str  # ``article`` | ``folder``
    etag: str = ""  # article: object ETag (quotes stripped); folder: ""
    type: str = "doc"
    title: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str | None = None
    stale_after: str | None = None
    size: int = 0
    seq: int = 0
    trust: str = "unverified"
    visible_children: int = 0  # folders only

    @property
    def is_folder(self) -> bool:
        return self.kind == KIND_FOLDER

    @property
    def visible(self) -> bool:
        """Articles are always visible once listed; folders only with children."""
        return not self.is_folder or self.visible_children > 0

    @classmethod
    def article(
        cls, name: str, etag: str, size: int, frontmatter: dict[str, Any]
    ) -> ListingChild:
        """Project an article's frontmatter. The caller has already checked ``type``."""
        tags = frontmatter.get("tags")
        return cls(
            name=name,
            kind=KIND_ARTICLE,
            etag=_strip(etag),
            type=str(frontmatter.get("type", "doc")),
            title=_optional_str(frontmatter.get("title")),
            description=_optional_str(frontmatter.get("description")),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            status=_optional_str(frontmatter.get("status")),
            stale_after=_optional_str(frontmatter.get("stale_after")),
            size=int(size),
            seq=_int(frontmatter.get("seq")),
            trust=trust_of(frontmatter),
        )

    @classmethod
    def folder(cls, name: str, visible_children: int) -> ListingChild:
        return cls(name=name, kind=KIND_FOLDER, visible_children=int(visible_children))

    def to_json(self) -> dict[str, Any]:
        if self.is_folder:
            return {
                "name": self.name,
                "kind": KIND_FOLDER,
                "visible_children": self.visible_children,
            }
        out: dict[str, Any] = {
            "name": self.name,
            "kind": KIND_ARTICLE,
            "etag": self.etag,
            "type": self.type,
            "tags": list(self.tags),
            "size": self.size,
            "seq": self.seq,
            "trust": self.trust,
        }
        for key in ("title", "description", "status", "stale_after"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ListingChild:
        kind = str(data.get("kind", KIND_ARTICLE))
        if kind == KIND_FOLDER:
            return cls.folder(str(data.get("name", "")), _int(data.get("visible_children")))
        tags = data.get("tags")
        return cls(
            name=str(data.get("name", "")),
            kind=KIND_ARTICLE,
            etag=_strip(str(data.get("etag", ""))),
            type=str(data.get("type", "doc")),
            title=_optional_str(data.get("title")),
            description=_optional_str(data.get("description")),
            tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            status=_optional_str(data.get("status")),
            stale_after=_optional_str(data.get("stale_after")),
            size=_int(data.get("size")),
            seq=_int(data.get("seq")),
            trust=str(data.get("trust", "unverified")),
        )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class Listing:
    folder: str  # absolute folder path, ``/`` for root
    children: list[ListingChild]
    generated_at: str
    etag: str = ""  # of the listing object itself, for If-Match
    excluded: dict[str, str] = field(default_factory=dict)  # name → etag of hidden .md keys

    @property
    def articles(self) -> list[ListingChild]:
        return [c for c in self.children if not c.is_folder]

    @property
    def folders(self) -> list[ListingChild]:
        return [c for c in self.children if c.is_folder]

    @property
    def visible_count(self) -> int:
        """Articles plus folders that are themselves visible (§8.6 folder visibility)."""
        return sum(1 for c in self.children if c.visible)

    def child(self, name: str) -> ListingChild | None:
        for c in self.children:
            if c.name == name:
                return c
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "generated_at": self.generated_at,
            "children": [c.to_json() for c in _sorted(self.children)],
            "excluded": [{"name": n, "etag": e} for n, e in sorted(self.excluded.items())],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], etag: str) -> Listing:
        raw_children = data.get("children")
        children = (
            [ListingChild.from_json(c) for c in raw_children if isinstance(c, dict)]
            if isinstance(raw_children, list)
            else []
        )
        raw_excluded = data.get("excluded")
        excluded: dict[str, str] = {}
        if isinstance(raw_excluded, list):
            for entry in raw_excluded:
                if isinstance(entry, dict) and entry.get("name"):
                    excluded[str(entry["name"])] = _strip(str(entry.get("etag", "")))
        return cls(
            folder=str(data.get("folder", ROOT)),
            children=children,
            generated_at=str(data.get("generated_at", "")),
            etag=_strip(etag),
            excluded=excluded,
        )


def _sorted(children: list[ListingChild]) -> list[ListingChild]:
    return sorted(children, key=lambda c: (c.is_folder, c.name))


@dataclass(frozen=True)
class _Live:
    """What ``ListObjectsV2`` says is under a folder right now."""

    folders: list[str]  # child folder names (no ``_``-prefixed ones)
    articles: dict[str, tuple[str, int]]  # name → (etag without quotes, size)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


class ListingIndex:
    """Reads, refreshes and rebuilds ``_listing.json`` objects.

    Args:
        bucket: Bucket name.
        client_for: Returns an S3 client able to maintain the named folder's listing
            (a MAINTAIN credential for exactly that folder), or ``None`` when the
            caller may not — parent propagation stops there. Optional; see the module
            docstring for what happens without one.
        log: Anything with ``info``/``warning`` taking ``(message, extra=...)``; a
            Powertools ``Logger`` or a stdlib one. Defaults to the module logger.
    """

    def __init__(
        self,
        bucket: str,
        *,
        client_for: ClientFor | None = None,
        log: Any = None,
    ) -> None:
        self.bucket = bucket
        self._client_for = client_for
        self._log = log if log is not None else _fallback_log

    # --- reads ---------------------------------------------------------------

    def read(self, s3: Any, folder: str) -> Listing | None:
        """``GetObject`` on the folder's listing. ``None`` when absent.

        A listing that exists but does not parse comes back **empty with its ETag**,
        so the caller's comparison finds it stale and the rebuild overwrites it with
        ``If-Match`` rather than tripping over ``If-None-Match``.

        Raises:
            AccessDenied: on 403.
        """
        key = listing_key(folder)
        try:
            response = s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                return None
            if _is_denied(error):
                raise AccessDenied(folder) from error
            raise
        etag = _strip(response["ETag"])
        try:
            data = json.loads(response["Body"].read())
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self._info("listing_unreadable", folder=folder)
            return Listing(folder=folder, children=[], generated_at="", etag=etag)
        listing = Listing.from_json(data, etag)
        listing.folder = folder
        return listing

    def project(self, s3: Any, path: str) -> ListingChild | None:
        """One article's projection from a ranged ``GetObject`` (§8.7 article grants).

        ``None`` when the key is absent or holds a reserved type.

        Raises:
            AccessDenied: on 403.
        """
        got = self._frontmatter(s3, folder_prefix(parent_of(path)) + basename(path))
        if got is None:
            return None
        etag, size, article = got
        if article.type in RESERVED_TYPES:
            return None
        return ListingChild.article(basename(path), etag, size, article.frontmatter)

    def read_or_rebuild(self, s3: Any, folder: str, *, writable: bool = True) -> Listing:
        """Read; rebuild when absent **or** when the live ``ListObjectsV2`` disagrees
        with the listing (self-healing).

        Args:
            s3: A LIST client (``writable=False``) or a MAINTAIN one (``writable=True``).
            folder: Absolute folder path.
            writable: Whether a rebuild may be persisted. Under a LIST credential the
                rebuilt listing is returned without being written and the event is
                logged as ``listing_rebuilt_unpersisted``.

        Raises:
            AccessDenied: on 403 from any read.
        """
        listing = self.read(s3, folder)
        live = self._list(s3, folder)
        if listing is not None and not self._stale(listing, live):
            return listing
        if listing is not None:
            self._info("listing_stale", folder=folder)
        return self._rebuild(s3, folder, persist=writable, live=live)

    def rebuild(self, s3: Any, folder: str, *, persist: bool = True) -> Listing:
        """Authority: ``ListObjectsV2`` + one ranged ``GetObject`` per child article,
        filtering reserved types; child folders get their ``visible_children`` from
        their own listing (read, or computed in memory if absent — a credential for
        this folder cannot tag a descendant's listing, so descendants are never
        written from here).

        Writes the result conditionally (``persist=True``) and returns it.

        Raises:
            AccessDenied: on 403.
            ToolError: 500 when the conditional write keeps losing races.
        """
        return self._rebuild(s3, folder, persist=persist, live=None)

    # --- writes --------------------------------------------------------------

    def refresh_child(self, s3: Any, folder: str, child: ListingChild | None, name: str) -> None:
        """Upsert (``child``) or remove (``None``) one entry by ``name`` with the
        conditional-write retry loop; propagate to the parent when this folder's
        visible-child count crosses zero (§8.6 folder visibility).

        Args:
            s3: A MAINTAIN client for ``folder``.
            folder: Absolute folder path whose listing changes.
            child: The new projection, or ``None`` to drop ``name`` (archive, move-out).
            name: The child's last path segment (``rear-bar.md``, or a folder name when
                a parent is being told about a child folder's visibility).

        Raises:
            AccessDenied: when ``s3`` cannot reach this folder's listing.
            ToolError: 500 when the conditional write keeps losing races.
        """
        self._refresh(s3, folder, child, name, depth=0)

    def _refresh(
        self, s3: Any, folder: str, child: ListingChild | None, name: str, *, depth: int
    ) -> None:
        before, after = self._upsert(s3, folder, child, name)
        if folder == ROOT or depth >= MAX_PROPAGATION_DEPTH:
            return
        if before is not None and (before == 0) == (after == 0):
            return  # visibility did not flip; the parent's bit is still right
        self._propagate(s3, folder, after, depth)

    def _propagate(self, s3: Any, folder: str, visible: int, depth: int) -> None:
        parent = parent_of(folder)
        parent_s3 = self._client_for(parent) if self._client_for is not None else s3
        if parent_s3 is None:
            self._info("listing_propagation_skipped", folder=folder, parent=parent)
            return
        entry = ListingChild.folder(basename(folder), visible) if visible > 0 else None
        try:
            self._refresh(parent_s3, parent, entry, basename(folder), depth=depth + 1)
        except AccessDenied:
            self._info("listing_propagation_denied", folder=folder, parent=parent)

    def _upsert(
        self, s3: Any, folder: str, child: ListingChild | None, name: str
    ) -> tuple[int | None, int]:
        """Apply one change; returns ``(visible before, visible after)``. ``before`` is
        ``None`` when the listing did not exist and had to be rebuilt."""
        for _attempt in range(MAX_WRITE_ATTEMPTS):
            current = self.read(s3, folder)
            if current is None:
                # Authority is S3, and the child is already there: a rebuild includes it.
                rebuilt = self._rebuild(s3, folder, persist=True, live=None)
                return None, rebuilt.visible_count
            before = current.visible_count
            updated = self._apply(s3, current, child, name)
            if updated is None:
                return before, before  # nothing changed; no write, no race
            try:
                written = self._write(s3, updated, current.etag)
            except PreconditionFailed:
                self._info("listing_write_raced", folder=folder)
                continue
            return before, written.visible_count
        raise internal("The folder listing could not be updated; retry the call.")

    def _apply(
        self, s3: Any, current: Listing, child: ListingChild | None, name: str
    ) -> Listing | None:
        """The new listing, or ``None`` when the change is already reflected."""
        children = [c for c in current.children if c.name != name]
        excluded = dict(current.excluded)
        if child is not None:
            children.append(replace(child, name=name, tags=list(child.tags)))
            excluded.pop(name, None)
        elif name.endswith(".md"):
            # The key still exists (tombstone, pointer): remember its ETag so the
            # self-heal comparison knows this hidden key and does not rebuild for it.
            etag = self._head_etag(s3, folder_prefix(current.folder) + name)
            if etag is None:
                excluded.pop(name, None)
            else:
                excluded[name] = etag
        updated = Listing(
            folder=current.folder,
            children=children,
            generated_at=_now_iso(),
            etag=current.etag,
            excluded=excluded,
        )
        if _same(current, updated):
            return None
        return updated

    def _write(self, s3: Any, listing: Listing, etag: str | None) -> Listing:
        """``PutObject`` the listing: ``If-Match`` over ``etag``, ``If-None-Match: *``
        when creating. Tagged (§8.9).

        Raises:
            PreconditionFailed: the condition did not hold; re-read and retry.
            AccessDenied: on 403.
        """
        condition = {"IfMatch": _quoted(etag)} if etag else {"IfNoneMatch": "*"}
        body = json.dumps(listing.to_json(), separators=(",", ":")).encode()
        try:
            response = s3.put_object(
                Bucket=self.bucket,
                Key=listing_key(listing.folder),
                Body=body,
                ContentType=LISTING_CONTENT_TYPE,
                Tagging=LISTING_TAG,
                **condition,
            )
        except ClientError as error:
            if _is_precondition(error) or (etag and _is_not_found(error)):
                raise PreconditionFailed(listing.folder) from error
            if _is_denied(error):
                raise AccessDenied(listing.folder) from error
            raise
        listing.etag = _strip(response["ETag"])
        return listing

    # --- rebuild internals ---------------------------------------------------

    def _rebuild(self, s3: Any, folder: str, *, persist: bool, live: _Live | None) -> Listing:
        for _attempt in range(MAX_WRITE_ATTEMPTS):
            current = self.read(s3, folder)
            if live is None:
                live = self._list(s3, folder)
            fresh = self._compute(s3, folder, live)
            empty = not live.folders and not live.articles
            live = None  # a retry re-lists: something changed underneath us
            if not persist or (current is None and empty):
                # A LIST credential cannot put the listing; and a folder with nothing
                # under it does not exist, so never leave a listing object behind for
                # a path someone merely asked about.
                self._info("listing_rebuilt_unpersisted", folder=folder)
                return fresh
            try:
                written = self._write(s3, fresh, current.etag if current else None)
            except PreconditionFailed:
                self._info("listing_write_raced", folder=folder)
                continue
            self._info("listing_rebuilt", folder=folder, children=len(written.children))
            return written
        raise internal("The folder listing could not be rebuilt; retry the call.")

    def _compute(self, s3: Any, folder: str, live: _Live | None) -> Listing:
        """The listing S3 says is right, in memory. Descends into child folders whose
        listing is absent (computing, never writing, theirs)."""
        if live is None:
            live = self._list(s3, folder)
        prefix = folder_prefix(folder)
        children: list[ListingChild] = []
        excluded: dict[str, str] = {}
        for name, (etag, size) in live.articles.items():
            got = self._frontmatter(s3, prefix + name)
            if got is None:
                continue  # vanished between the list and the read; the next read heals
            etag, size, article = got
            if article.type in RESERVED_TYPES:
                excluded[name] = _strip(etag)
                continue
            children.append(ListingChild.article(name, etag, size, article.frontmatter))
        for name in live.folders:
            child_folder = join(folder, name)
            sub = self.read(s3, child_folder)
            if sub is None:
                sub = self._compute(s3, child_folder, None)
            children.append(ListingChild.folder(name, sub.visible_count))
        return Listing(
            folder=folder, children=children, generated_at=_now_iso(), excluded=excluded
        )

    def _stale(self, listing: Listing, live: _Live) -> bool:
        known = {c.name: c.etag for c in listing.articles}
        known.update(listing.excluded)
        actual = {name: etag for name, (etag, _size) in live.articles.items()}
        if known != actual:
            return True
        return {c.name for c in listing.folders} != set(live.folders)

    def _list(self, s3: Any, folder: str) -> _Live:
        """``ListObjectsV2`` with ``Prefix`` + ``Delimiter``: every ``.md`` child (name,
        etag, size) and every child folder name, ``_``-prefixed names skipped.

        Raises:
            AccessDenied: on 403.
        """
        prefix = folder_prefix(folder)
        folders: list[str] = []
        articles: dict[str, tuple[str, int]] = {}
        paginator = s3.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
                for common in page.get("CommonPrefixes") or []:
                    name = common["Prefix"][len(prefix) :].rstrip("/")
                    if name and not name.startswith("_"):
                        folders.append(name)
                for item in page.get("Contents") or []:
                    name = item["Key"][len(prefix) :]
                    if not name or name.startswith("_") or not name.endswith(".md"):
                        continue
                    articles[name] = (_strip(item.get("ETag", "")), int(item.get("Size", 0)))
        except ClientError as error:
            if _is_denied(error):
                raise AccessDenied(folder) from error
            raise
        return _Live(folders=sorted(folders), articles=dict(sorted(articles.items())))

    def _frontmatter(self, s3: Any, key: str) -> tuple[str, int, Article] | None:
        """Ranged ``GetObject`` covering the frontmatter; the whole object when the
        block does not close inside the range. ``None`` when the key is absent.

        Returns ``(etag, total size, parsed article)`` — the body is whatever fell
        inside the range and is not meaningful.

        Raises:
            AccessDenied: on 403.
        """
        try:
            response = s3.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes=0-{FRONTMATTER_RANGE_BYTES - 1}"
            )
        except ClientError as error:
            if _is_not_found(error):
                return None
            if _is_denied(error):
                raise AccessDenied(path_for(key)) from error
            if _code(error) != "InvalidRange":
                raise
            # An empty object: nothing to project, but the key exists.
            return self._whole(s3, key)
        data = response["Body"].read()
        total = _total_size(response, len(data))
        article = parse(data)
        if total > len(data) and not article.frontmatter and data.startswith(b"---"):
            # The block opened but did not close inside the window: read it all.
            return self._whole(s3, key)
        return response["ETag"], total, article

    def _whole(self, s3: Any, key: str) -> tuple[str, int, Article] | None:
        try:
            response = s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                return None
            if _is_denied(error):
                raise AccessDenied(path_for(key)) from error
            raise
        data = response["Body"].read()
        return response["ETag"], int(response.get("ContentLength", len(data))), parse(data)

    def _head_etag(self, s3: Any, key: str) -> str | None:
        try:
            response = s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if _is_not_found(error):
                return None
            if _is_denied(error):
                raise AccessDenied(path_for(key)) from error
            raise
        return _strip(response["ETag"])

    def _info(self, event: str, **fields: Any) -> None:
        try:
            self._log.info(event, extra=fields)
        except Exception:  # noqa: BLE001 — logging must never break a listing write
            _fallback_log.info("%s %s", event, fields)


def _total_size(response: dict[str, Any], fallback: int) -> int:
    """Object size from ``Content-Range: bytes 0-16383/100027``; else ``ContentLength``."""
    content_range = response.get("ContentRange") or ""
    if "/" in content_range:
        try:
            return int(content_range.rsplit("/", 1)[1])
        except ValueError:
            pass
    return int(response.get("ContentLength", fallback))


def _same(a: Listing, b: Listing) -> bool:
    """Equal in everything but ``generated_at`` and the object ETag."""
    return (
        _sorted(a.children) == _sorted(b.children)
        and a.excluded == b.excluded
        and a.folder == b.folder
    )


__all__ = [
    "FRONTMATTER_RANGE_BYTES",
    "KIND_ARTICLE",
    "KIND_FOLDER",
    "MAX_WRITE_ATTEMPTS",
    "Listing",
    "ListingChild",
    "ListingIndex",
    "basename",
    "is_stale",
    "join",
    "listing_key",
    "parent_of",
]
