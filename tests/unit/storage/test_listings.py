"""Listing projection (HANDOFF §8.6, increment A) against moto S3.

moto honours what the index leans on: ``Tagging`` on ``PutObject`` (readable back with
``GetObjectTagging``), ``IfNoneMatch="*"`` and a wrong ``IfMatch`` both answer 412
``PreconditionFailed``, ``IfMatch`` on an absent key answers 404 ``NoSuchKey``, and a
``Range`` read answers 206 with ``ContentRange``. What moto does *not* reproduce is the
SSE-KMS ETag: here an ETag is the content MD5, so two listings with identical bytes
share one — tests that race the listing make the racing write differ in content.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.config import LISTING_TAG, Settings
from app.errors import ToolError
from app.storage.articles import AccessDenied
from app.storage.listings import (
    FRONTMATTER_RANGE_BYTES,
    MAX_WRITE_ATTEMPTS,
    Listing,
    ListingChild,
    ListingIndex,
    basename,
    is_stale,
    join,
    listing_key,
    parent_of,
)
from app.storage.markdown import MAX_FRONTMATTER_BYTES, parse, serialize

BUCKET = "wiki-test"


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def index() -> ListingIndex:
    return ListingIndex(BUCKET)


@pytest.fixture
def put_md(bucket: Any) -> Callable[..., str]:
    """Write an article directly; returns its ETag without quotes."""

    def _put(path: str, frontmatter: dict[str, Any], body: str = "body") -> str:
        response = bucket.put_object(
            Bucket=BUCKET, Key="a" + path, Body=serialize(frontmatter, body)
        )
        return response["ETag"].strip('"')

    return _put


@pytest.fixture
def get_listing(bucket: Any) -> Callable[[str], dict[str, Any] | None]:
    def _get(folder: str) -> dict[str, Any] | None:
        try:
            response = bucket.get_object(Bucket=BUCKET, Key=listing_key(folder))
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        return json.loads(response["Body"].read())

    return _get


def names(listing: Listing, kind: str) -> list[str]:
    return sorted(c.name for c in listing.children if c.kind == kind)


# --- pure helpers ------------------------------------------------------------------------


class TestHelpers:
    def test_listing_key(self) -> None:
        assert listing_key("/") == "a/_listing.json"
        assert listing_key("/racing") == "a/racing/_listing.json"
        assert listing_key("/racing/setup") == "a/racing/setup/_listing.json"

    def test_parent_of(self) -> None:
        assert parent_of("/racing/setup") == "/racing"
        assert parent_of("/racing") == "/"
        assert parent_of("/") == "/"

    def test_basename_and_join(self) -> None:
        assert basename("/racing/setup/rear-bar.md") == "rear-bar.md"
        assert basename("/racing/setup") == "setup"
        assert join("/", "x.md") == "/x.md"
        assert join("/racing", "setup") == "/racing/setup"

    def test_is_stale(self) -> None:
        assert is_stale(None) is None
        assert is_stale("not a date") is None
        assert is_stale("2000-01-01T00:00:00Z") is True
        assert is_stale("2999-01-01T00:00:00Z") is False
        assert is_stale("2000-01-01") is True


class TestWireShapes:
    def test_article_child_round_trips(self) -> None:
        child = ListingChild.article(
            "rear-bar.md",
            '"abc"',
            1234,
            {
                "type": "doc",
                "title": "Rear bar",
                "description": "Sway bar notes",
                "tags": ["racing", "setup"],
                "status": "stable",
                "stale_after": "2027-01-01T00:00:00Z",
                "seq": 3,
                "verified": [{"by": "human:josh"}],
            },
        )
        assert child.etag == "abc"
        assert child.trust == "human-reviewed"
        data = child.to_json()
        assert data["kind"] == "article"
        assert ListingChild.from_json(data) == child

    def test_article_child_omits_absent_optionals(self) -> None:
        data = ListingChild.article("x.md", "e", 1, {"type": "doc"}).to_json()
        assert "title" not in data and "description" not in data and "status" not in data
        assert data["tags"] == []
        assert data["trust"] == "unverified"

    def test_folder_child_round_trips(self) -> None:
        child = ListingChild.folder("setup", 2)
        assert child.visible is True
        assert ListingChild.from_json(child.to_json()) == child
        assert ListingChild.folder("empty", 0).visible is False

    def test_listing_round_trips_with_excluded(self) -> None:
        listing = Listing(
            folder="/racing",
            children=[ListingChild.folder("setup", 1), ListingChild.article("n.md", "e1", 5, {})],
            generated_at="2026-09-13T00:00:00Z",
            excluded={"old.md": "e2"},
        )
        data = listing.to_json()
        assert [c["name"] for c in data["children"]] == ["n.md", "setup"]  # articles first
        assert data["excluded"] == [{"name": "old.md", "etag": "e2"}]
        back = Listing.from_json(data, '"listing-etag"')
        assert back.etag == "listing-etag"
        assert back.excluded == {"old.md": "e2"}
        assert back.visible_count == 2

    def test_from_json_tolerates_garbage(self) -> None:
        back = Listing.from_json({"children": "nope", "excluded": 3}, "e")
        assert back.children == [] and back.excluded == {}


# --- rebuild -----------------------------------------------------------------------------


class TestRebuild:
    def test_rebuild_projects_frontmatter_and_writes_tagged_json(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        etag = put_md(
            "/racing/notes.md",
            {"type": "doc", "title": "Notes", "tags": ["a"], "description": "d", "seq": 2},
        )
        listing = index.rebuild(bucket, "/racing")
        assert listing.folder == "/racing"
        assert names(listing, "article") == ["notes.md"]
        child = listing.child("notes.md")
        assert child is not None
        assert (child.etag, child.title, child.tags, child.description, child.seq) == (
            etag,
            "Notes",
            ["a"],
            "d",
            2,
        )
        assert child.size == len(
            serialize(
                {"type": "doc", "title": "Notes", "tags": ["a"], "description": "d", "seq": 2},
                "body",
            )
        )
        assert listing.etag

        head = bucket.head_object(Bucket=BUCKET, Key="a/racing/_listing.json")
        assert head["ContentType"] == "application/json"
        assert head["ETag"].strip('"') == listing.etag
        tags = bucket.get_object_tagging(Bucket=BUCKET, Key="a/racing/_listing.json")["TagSet"]
        key, value = LISTING_TAG.split("=")
        assert tags == [{"Key": key, "Value": value}]

    def test_rebuild_filters_reserved_types_into_excluded(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/live.md", {"type": "doc"})
        p = put_md("/racing/moved.md", {"type": "pointer", "moved_to": "/x.md"})
        a = put_md("/racing/gone.md", {"type": "archived"})
        listing = index.rebuild(bucket, "/racing")
        assert names(listing, "article") == ["live.md"]
        assert listing.excluded == {"moved.md": p, "gone.md": a}

    def test_rebuild_skips_system_keys_and_non_markdown(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/notes.md", {"type": "doc"})
        bucket.put_object(Bucket=BUCKET, Key="a/racing/photo.png", Body=b"x")
        bucket.put_object(Bucket=BUCKET, Key="a/racing/_scratch/x.md", Body=b"x")
        bucket.put_object(Bucket=BUCKET, Key="a/racing/_notes.md", Body=b"x")
        listing = index.rebuild(bucket, "/racing")
        assert names(listing, "article") == ["notes.md"]
        assert names(listing, "folder") == []

    def test_rebuild_counts_child_folder_visibility(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/setup/rear-bar.md", {"type": "doc"})
        put_md("/racing/history/2025.md", {"type": "archived"})
        put_md("/racing/deep/er/x.md", {"type": "doc"})
        listing = index.rebuild(bucket, "/racing")
        by_name = {c.name: c for c in listing.folders}
        assert by_name["setup"].visible_children == 1
        assert by_name["history"].visible_children == 0
        assert by_name["deep"].visible_children == 1  # one visible folder beneath it
        assert listing.visible_count == 2
        assert sorted(c.name for c in listing.folders if c.visible) == ["deep", "setup"]

    def test_rebuild_never_writes_descendant_listings(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        put_md("/racing/setup/rear-bar.md", {"type": "doc"})
        index.rebuild(bucket, "/racing")
        assert get_listing("/racing") is not None
        assert get_listing("/racing/setup") is None

    def test_rebuild_of_an_empty_folder_is_not_persisted(
        self, bucket: Any, index: ListingIndex, get_listing: Any
    ) -> None:
        listing = index.rebuild(bucket, "/nothing")
        assert listing.children == []
        assert get_listing("/nothing") is None
        assert index.rebuild(bucket, "/").children == []
        assert get_listing("/") is None

    def test_rebuild_reads_only_the_frontmatter_range(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/big/x.md", {"type": "doc", "title": "Big"}, "z" * (FRONTMATTER_RANGE_BYTES * 4))
        seen: list[dict[str, Any]] = []
        real = bucket.get_object

        def spy(**kwargs: Any) -> Any:
            seen.append(kwargs)
            return real(**kwargs)

        bucket.get_object = spy  # type: ignore[method-assign]
        listing = index.rebuild(bucket, "/big")
        child = listing.child("x.md")
        assert child is not None and child.title == "Big"
        assert child.size == len(
            serialize({"type": "doc", "title": "Big"}, "z" * (FRONTMATTER_RANGE_BYTES * 4))
        )
        article_reads = [k for k in seen if k["Key"] == "a/big/x.md"]
        assert len(article_reads) == 1
        assert article_reads[0]["Range"] == f"bytes=0-{FRONTMATTER_RANGE_BYTES - 1}"

    def test_frontmatter_longer_than_the_range_falls_back_to_a_full_read(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        # The largest block ``parse`` accepts: with its fences the header overruns the
        # ranged read, so the projection has to fetch the whole object.
        probe = serialize({"type": "doc", "title": "Long", "notes": "n"}, "")
        notes = "n" * (MAX_FRONTMATTER_BYTES - (len(probe) - len(b"---\n---\n")) + 1)
        frontmatter = {"type": "doc", "title": "Long", "notes": notes}
        stored = serialize(frontmatter, "body")
        assert len(stored) - len(b"body") > FRONTMATTER_RANGE_BYTES
        assert parse(stored).frontmatter == frontmatter
        put_md("/big/y.md", frontmatter)
        listing = index.rebuild(bucket, "/big")
        child = listing.child("y.md")
        assert child is not None
        assert child.title == "Long"

    def test_frontmatter_over_the_parse_cap_projects_as_none(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        # Storage treats a block over ``MAX_FRONTMATTER_BYTES`` as malformed (empty
        # frontmatter, whole text as body); the listing reflects that, not an error.
        put_md(
            "/big/z.md",
            {"type": "doc", "title": "Long", "notes": "n" * (FRONTMATTER_RANGE_BYTES * 2)},
        )
        listing = index.rebuild(bucket, "/big")
        child = listing.child("z.md")
        assert child is not None
        assert child.title is None and child.type == "doc" and child.seq == 0

    def test_empty_object_is_projected_without_error(
        self, bucket: Any, index: ListingIndex
    ) -> None:
        bucket.put_object(Bucket=BUCKET, Key="a/e/empty.md", Body=b"")
        listing = index.rebuild(bucket, "/e")
        child = listing.child("empty.md")
        assert child is not None and child.type == "doc" and child.size == 0

    def test_rebuild_with_if_match_over_an_existing_listing(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        first = index.rebuild(bucket, "/racing")
        put_md("/racing/b.md", {"type": "doc"})
        second = index.rebuild(bucket, "/racing")
        assert second.etag != first.etag
        assert names(second, "article") == ["a.md", "b.md"]
        versions = bucket.list_object_versions(Bucket=BUCKET, Prefix="a/racing/_listing.json")
        assert len(versions["Versions"]) == 2

    def test_unparseable_listing_is_rebuilt_in_place(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        bucket.put_object(Bucket=BUCKET, Key="a/racing/_listing.json", Body=b"not json")
        listing = index.read_or_rebuild(bucket, "/racing")
        assert names(listing, "article") == ["a.md"]
        assert get_listing("/racing")["children"][0]["name"] == "a.md"


# --- read / self-heal --------------------------------------------------------------------


class TestReadOrRebuild:
    def test_absent_listing_is_built(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        put_md("/racing/a.md", {"type": "doc", "title": "A"})
        listing = index.read_or_rebuild(bucket, "/racing")
        assert names(listing, "article") == ["a.md"]
        assert get_listing("/racing") is not None

    def test_matching_listing_is_returned_without_rebuild(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        built = index.rebuild(bucket, "/racing")
        seen: list[str] = []
        real = bucket.get_object

        def spy(**kwargs: Any) -> Any:
            seen.append(kwargs["Key"])
            return real(**kwargs)

        bucket.get_object = spy  # type: ignore[method-assign]
        again = index.read_or_rebuild(bucket, "/racing")
        assert again.etag == built.etag
        assert seen == ["a/racing/_listing.json"]  # no per-child reads

    def test_article_added_behind_the_listing_triggers_rebuild(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        index.rebuild(bucket, "/racing")
        put_md("/racing/b.md", {"type": "doc", "title": "B"})
        listing = index.read_or_rebuild(bucket, "/racing")
        assert names(listing, "article") == ["a.md", "b.md"]

    def test_article_changed_behind_the_listing_triggers_rebuild(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc", "title": "Old"})
        index.rebuild(bucket, "/racing")
        new_etag = put_md("/racing/a.md", {"type": "doc", "title": "New"})
        listing = index.read_or_rebuild(bucket, "/racing")
        child = listing.child("a.md")
        assert child is not None and (child.title, child.etag) == ("New", new_etag)

    def test_tombstones_do_not_cause_repeated_rebuilds(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        put_md("/racing/gone.md", {"type": "archived"})
        built = index.rebuild(bucket, "/racing")
        assert index.read_or_rebuild(bucket, "/racing").etag == built.etag

    def test_article_archived_behind_the_listing_drops_it(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        put_md("/racing/b.md", {"type": "doc"})
        index.rebuild(bucket, "/racing")
        put_md("/racing/b.md", {"type": "archived"})
        listing = index.read_or_rebuild(bucket, "/racing")
        assert names(listing, "article") == ["a.md"]
        assert "b.md" in listing.excluded

    def test_new_child_folder_triggers_rebuild(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        index.rebuild(bucket, "/racing")
        put_md("/racing/setup/x.md", {"type": "doc"})
        listing = index.read_or_rebuild(bucket, "/racing")
        assert names(listing, "folder") == ["setup"]

    def test_unwritable_read_returns_fresh_listing_without_persisting(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        put_md("/racing/a.md", {"type": "doc", "title": "A"})
        listing = index.read_or_rebuild(bucket, "/racing", writable=False)
        assert names(listing, "article") == ["a.md"]
        assert listing.etag == ""
        assert get_listing("/racing") is None

    def test_access_denied_surfaces(self, index: ListingIndex) -> None:
        class Denying:
            def get_object(self, **_: Any) -> Any:
                raise ClientError(
                    {
                        "Error": {"Code": "AccessDenied"},
                        "ResponseMetadata": {"HTTPStatusCode": 403},
                    },
                    "GetObject",
                )

        with pytest.raises(AccessDenied):
            index.read(Denying(), "/racing")


# --- refresh_child and the conditional write ---------------------------------------------


class TestRefreshChild:
    def test_upsert_into_absent_listing_rebuilds(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        etag = put_md("/racing/a.md", {"type": "doc", "title": "A"})
        index.refresh_child(
            bucket, "/racing", ListingChild.article("a.md", etag, 10, {"title": "A"}), "a.md"
        )
        data = get_listing("/racing")
        assert [c["name"] for c in data["children"]] == ["a.md"]

    def test_upsert_replaces_and_remove_excludes(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        put_md("/racing/a.md", {"type": "doc", "title": "A"})
        index.rebuild(bucket, "/racing")
        new_etag = put_md("/racing/a.md", {"type": "doc", "title": "A2", "seq": 2})
        index.refresh_child(
            bucket,
            "/racing",
            ListingChild.article("a.md", new_etag, 10, {"type": "doc", "title": "A2", "seq": 2}),
            "a.md",
        )
        listing = index.read(bucket, "/racing")
        assert listing is not None
        child = listing.child("a.md")
        assert child is not None and (child.title, child.etag, child.seq) == ("A2", new_etag, 2)

        tomb = put_md("/racing/a.md", {"type": "archived", "seq": 3})
        index.refresh_child(bucket, "/racing", None, "a.md")
        listing = index.read(bucket, "/racing")
        assert listing is not None
        assert listing.children == []
        assert listing.excluded == {"a.md": tomb}
        # And the self-heal comparison is satisfied: no rebuild needed afterwards.
        assert index.read_or_rebuild(bucket, "/racing").etag == listing.etag

    def test_unchanged_upsert_writes_nothing(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        etag = put_md("/racing/a.md", {"type": "doc", "title": "A"})
        built = index.rebuild(bucket, "/racing")
        child = built.child("a.md")
        assert child is not None and child.etag == etag
        index.refresh_child(bucket, "/racing", ListingChild.from_json(child.to_json()), "a.md")
        assert index.read(bucket, "/racing").etag == built.etag  # type: ignore[union-attr]

    def test_remove_of_unknown_name_writes_nothing(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        built = index.rebuild(bucket, "/racing")
        index.refresh_child(bucket, "/racing", None, "never.md")
        assert index.read(bucket, "/racing").etag == built.etag  # type: ignore[union-attr]

    def test_lost_race_is_retried_and_merged(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        """Two writers add different children: the second read sees the first's write
        via 412 and merges rather than overwriting (§8.6)."""
        put_md("/racing/a.md", {"type": "doc"})
        index.rebuild(bucket, "/racing")
        b = put_md("/racing/b.md", {"type": "doc", "title": "B"})
        c = put_md("/racing/c.md", {"type": "doc", "title": "C"})

        real_put = bucket.put_object
        raced = {"done": False}

        def racing_put(**kwargs: Any) -> Any:
            if not raced["done"] and kwargs["Key"] == "a/racing/_listing.json":
                raced["done"] = True
                # Someone else lands their child between our read and our write.
                other = ListingIndex(BUCKET)
                other.refresh_child(
                    bucket, "/racing", ListingChild.article("c.md", c, 1, {"title": "C"}), "c.md"
                )
            return real_put(**kwargs)

        bucket.put_object = racing_put  # type: ignore[method-assign]
        index.refresh_child(
            bucket, "/racing", ListingChild.article("b.md", b, 1, {"title": "B"}), "b.md"
        )
        listing = index.read(bucket, "/racing")
        assert listing is not None
        assert names(listing, "article") == ["a.md", "b.md", "c.md"]
        assert raced["done"]

    def test_persistent_race_gives_up_with_500(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        index.rebuild(bucket, "/racing")
        attempts = {"n": 0}
        real_put = bucket.put_object

        def always_losing(**kwargs: Any) -> Any:
            if kwargs["Key"] == "a/racing/_listing.json":
                attempts["n"] += 1
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            return real_put(**kwargs)

        bucket.put_object = always_losing  # type: ignore[method-assign]
        with pytest.raises(ToolError) as info:
            index.refresh_child(bucket, "/racing", ListingChild.article("z.md", "e", 1, {}), "z.md")
        assert info.value.status == 500
        assert attempts["n"] == MAX_WRITE_ATTEMPTS

    def test_conditional_create_uses_if_none_match(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/a.md", {"type": "doc"})
        seen: list[dict[str, Any]] = []
        real_put = bucket.put_object

        def spy(**kwargs: Any) -> Any:
            seen.append(kwargs)
            return real_put(**kwargs)

        bucket.put_object = spy  # type: ignore[method-assign]
        index.rebuild(bucket, "/racing")
        index.rebuild(bucket, "/racing")
        assert seen[0].get("IfNoneMatch") == "*" and "IfMatch" not in seen[0]
        assert "IfMatch" in seen[1] and "IfNoneMatch" not in seen[1]
        assert all(k["Tagging"] == LISTING_TAG for k in seen)


# --- folder visibility propagation -------------------------------------------------------


class TestPropagation:
    def test_first_child_makes_folder_visible_up_the_chain(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        etag = put_md("/racing/setup/rear-bar.md", {"type": "doc"})
        index.refresh_child(
            bucket,
            "/racing/setup",
            ListingChild.article("rear-bar.md", etag, 1, {"type": "doc"}),
            "rear-bar.md",
        )
        racing = get_listing("/racing")
        assert racing["children"] == [{"name": "setup", "kind": "folder", "visible_children": 1}]
        root = get_listing("/")
        assert root["children"] == [{"name": "racing", "kind": "folder", "visible_children": 1}]

    def test_removing_last_child_hides_folder_and_readding_restores_it(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        etag = put_md("/racing/setup/rear-bar.md", {"type": "doc"})
        child = ListingChild.article("rear-bar.md", etag, 1, {"type": "doc"})
        index.refresh_child(bucket, "/racing/setup", child, "rear-bar.md")
        assert [c.name for c in index.read(bucket, "/racing").folders if c.visible] == ["setup"]  # type: ignore[union-attr]

        # Archive-like removal: the tombstone lands, then the child is dropped.
        put_md("/racing/setup/rear-bar.md", {"type": "archived"})
        index.refresh_child(bucket, "/racing/setup", None, "rear-bar.md")
        racing = index.read(bucket, "/racing")
        assert racing is not None
        assert [c.name for c in racing.folders if c.visible] == []
        assert racing.visible_count == 0
        root = index.read(bucket, "/")
        assert root is not None and root.visible_count == 0

        # Unarchive-like return.
        etag2 = put_md("/racing/setup/rear-bar.md", {"type": "doc", "seq": 3})
        index.refresh_child(
            bucket,
            "/racing/setup",
            ListingChild.article("rear-bar.md", etag2, 1, {"type": "doc", "seq": 3}),
            "rear-bar.md",
        )
        racing = index.read(bucket, "/racing")
        assert racing is not None
        assert [c.name for c in racing.folders if c.visible] == ["setup"]
        root = index.read(bucket, "/")
        assert root is not None and root.visible_count == 1

    def test_second_child_does_not_touch_the_parent(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        a = put_md("/racing/setup/a.md", {"type": "doc"})
        index.refresh_child(bucket, "/racing/setup", ListingChild.article("a.md", a, 1, {}), "a.md")
        parent_etag = index.read(bucket, "/racing").etag  # type: ignore[union-attr]
        b = put_md("/racing/setup/b.md", {"type": "doc"})
        index.refresh_child(bucket, "/racing/setup", ListingChild.article("b.md", b, 1, {}), "b.md")
        assert index.read(bucket, "/racing").etag == parent_etag  # type: ignore[union-attr]

    def test_propagation_stops_where_client_for_says_no(
        self, bucket: Any, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        asked: list[str] = []

        def client_for(folder: str) -> Any | None:
            asked.append(folder)
            return bucket if folder == "/racing" else None

        index = ListingIndex(BUCKET, client_for=client_for)
        etag = put_md("/racing/setup/x.md", {"type": "doc"})
        index.refresh_child(
            bucket, "/racing/setup", ListingChild.article("x.md", etag, 1, {}), "x.md"
        )
        assert asked == ["/racing", "/"]
        assert get_listing("/racing") is not None
        assert get_listing("/") is None

    def test_propagation_survives_a_denied_parent(
        self, bucket: Any, put_md: Callable[..., str], get_listing: Any
    ) -> None:
        class Denying:
            def get_object(self, **_: Any) -> Any:
                raise ClientError(
                    {
                        "Error": {"Code": "AccessDenied"},
                        "ResponseMetadata": {"HTTPStatusCode": 403},
                    },
                    "GetObject",
                )

        index = ListingIndex(BUCKET, client_for=lambda folder: Denying())
        etag = put_md("/racing/setup/x.md", {"type": "doc"})
        index.refresh_child(
            bucket, "/racing/setup", ListingChild.article("x.md", etag, 1, {}), "x.md"
        )
        assert get_listing("/racing/setup") is not None
        assert get_listing("/racing") is None


# --- project (article grants in search) --------------------------------------------------


class TestProject:
    def test_projects_one_article(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        etag = put_md("/racing/a.md", {"type": "doc", "title": "A", "tags": ["t"]})
        child = index.project(bucket, "/racing/a.md")
        assert child is not None
        assert (child.name, child.etag, child.title, child.tags) == ("a.md", etag, "A", ["t"])

    def test_reserved_and_absent_are_none(
        self, bucket: Any, index: ListingIndex, put_md: Callable[..., str]
    ) -> None:
        put_md("/racing/p.md", {"type": "pointer", "moved_to": "/x.md"})
        assert index.project(bucket, "/racing/p.md") is None
        assert index.project(bucket, "/racing/nope.md") is None


def test_settings_bucket_matches(settings: Settings) -> None:
    assert settings.bucket == BUCKET
