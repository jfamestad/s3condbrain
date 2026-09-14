"""Per-folder listing projection — HANDOFF §8.6, increment A.

``a/<folder>/_listing.json`` holds the projected frontmatter of a folder's immediate
children so one ``GetObject`` renders ``list_folder`` and feeds ``search``. It lives
inside the folder so it inherits the folder's grants.

Rules (all from §8.6):
* Rewritten whenever a child is created, updated in a projected field, archived,
  unarchived or moved. Children whose ``type`` is ``pointer`` or ``archived`` are
  excluded; that is how §5.2/§5.3 "leaves listings" is implemented.
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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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

    def to_json(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ListingChild:
        raise NotImplementedError


@dataclass
class Listing:
    folder: str  # absolute folder path, ``/`` for root
    children: list[ListingChild]
    generated_at: str
    etag: str = ""  # of the listing object itself, for If-Match

    def to_json(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_json(cls, data: dict[str, Any], etag: str) -> Listing:
        raise NotImplementedError


class ListingIndex:
    """Reads, refreshes and rebuilds ``_listing.json`` objects.

    Args:
        bucket: Bucket name.
    """

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    def read(self, s3: Any, folder: str) -> Listing | None:
        """``GetObject`` on the folder's listing. ``None`` when absent."""
        raise NotImplementedError

    def rebuild(self, s3: Any, folder: str) -> Listing:
        """Authority: ``ListObjectsV2`` + one ``GetObject`` (ranged, frontmatter
        only) per child article, filtering reserved types; child folders get their
        ``visible_children`` from their own listing (read, or rebuilt if absent).
        Writes the result conditionally and returns it."""
        raise NotImplementedError

    def read_or_rebuild(self, s3: Any, folder: str) -> Listing:
        """Read; rebuild when absent **or** when the live ``ListObjectsV2``
        ``(name, etag)`` set disagrees with the listing's (self-healing)."""
        raise NotImplementedError

    def refresh_child(self, s3: Any, folder: str, child: ListingChild | None, name: str) -> None:
        """Upsert (``child``) or remove (``None``) one entry by ``name`` with the
        conditional-write retry loop; propagate to the parent when this folder's
        visible-child count crosses zero (§8.6 folder visibility)."""
        raise NotImplementedError


__all__ = ["Listing", "ListingChild", "ListingIndex"]
