"""ArticleStore against moto (HANDOFF §8.2–§8.4).

moto 5 honours ``If-None-Match: *`` and ``If-Match`` on ``PutObject``, so the 412
paths run against moto. The 403 paths use a stub client raising ``ClientError``,
because moto has no IAM to deny with.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.config import Settings
from app.storage.articles import (
    CONTENT_TYPE,
    META_ACTOR,
    META_KIND,
    AccessDenied,
    ArticleStore,
    PreconditionFailed,
    StoredObject,
    folder_prefix,
    key_for,
    path_for,
)

META = {META_ACTOR: "human:alice", META_KIND: "write"}


@pytest.fixture
def store(settings: Settings) -> ArticleStore:
    return ArticleStore(settings.bucket)


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class DenyingClient:
    """Every S3 verb answers 403, as a minted credential outside its prefix would."""

    def get_object(self, **_: Any) -> Any:
        raise _client_error("AccessDenied", 403, "GetObject")

    def head_object(self, **_: Any) -> Any:
        raise _client_error("403", 403, "HeadObject")

    def put_object(self, **_: Any) -> Any:
        raise _client_error("AccessDenied", 403, "PutObject")

    def get_paginator(self, _name: str) -> Any:
        return self

    def paginate(self, **_: Any) -> Any:
        raise _client_error("AccessDenied", 403, "ListObjectsV2")


# --- key mapping -------------------------------------------------------------


def test_key_and_path_mapping() -> None:
    assert key_for("/racing/x.md") == "a/racing/x.md"
    assert path_for("a/racing/x.md") == "/racing/x.md"
    assert folder_prefix("/") == "a/"
    assert folder_prefix("/racing") == "a/racing/"
    assert folder_prefix("/racing/setup") == "a/racing/setup/"


def test_stored_object_version_strips_quotes() -> None:
    obj = StoredObject(key="a/x.md", etag='"abc123"')
    assert obj.version == "abc123"
    assert obj.path == "/x.md"


# --- get / head ----------------------------------------------------------------


def test_get_missing_returns_none(bucket: Any, store: ArticleStore) -> None:
    assert store.get(bucket, "/nope.md") is None
    assert store.head(bucket, "/nope.md") is None


def test_put_new_then_get_and_head(bucket: Any, store: ArticleStore, settings: Settings) -> None:
    written = store.put_new(bucket, "/racing/x.md", b"---\ntype: doc\n---\nhi\n", META)
    assert written.key == "a/racing/x.md"
    assert written.etag.startswith('"') and written.etag.endswith('"')
    assert written.version == written.etag.strip('"')
    assert written.size == len(b"---\ntype: doc\n---\nhi\n")
    assert written.version_id

    got = store.get(bucket, "/racing/x.md")
    assert got is not None
    assert got.body == b"---\ntype: doc\n---\nhi\n"
    assert got.etag == written.etag
    assert got.size == written.size
    assert got.metadata == META

    head = store.head(bucket, "/racing/x.md")
    assert head is not None
    assert head.etag == written.etag
    assert head.metadata == META
    assert head.body == b""

    raw = bucket.head_object(Bucket=settings.bucket, Key="a/racing/x.md")
    assert raw["ContentType"] == CONTENT_TYPE
    assert raw["Metadata"] == META


# --- conditional writes ----------------------------------------------------------


def test_put_new_refuses_occupied_key(bucket: Any, store: ArticleStore) -> None:
    store.put_new(bucket, "/x.md", b"one", META)
    with pytest.raises(PreconditionFailed):
        store.put_new(bucket, "/x.md", b"two", META)
    got = store.get(bucket, "/x.md")
    assert got is not None and got.body == b"one"


def test_put_if_match_with_current_etag_writes_new_version(
    bucket: Any, store: ArticleStore
) -> None:
    first = store.put_new(bucket, "/x.md", b"one", META)
    second = store.put_if_match(bucket, "/x.md", b"two", first.etag, META)
    assert second.etag != first.etag
    assert second.version_id != first.version_id
    got = store.get(bucket, "/x.md")
    assert got is not None and got.body == b"two"


def test_put_if_match_accepts_unquoted_etag(bucket: Any, store: ArticleStore) -> None:
    first = store.put_new(bucket, "/x.md", b"one", META)
    second = store.put_if_match(bucket, "/x.md", b"two", first.version, META)
    got = store.get(bucket, "/x.md")
    assert got is not None and got.etag == second.etag


def test_put_if_match_with_stale_etag_writes_nothing(bucket: Any, store: ArticleStore) -> None:
    first = store.put_new(bucket, "/x.md", b"one", META)
    store.put_if_match(bucket, "/x.md", b"two", first.etag, META)
    with pytest.raises(PreconditionFailed):
        store.put_if_match(bucket, "/x.md", b"three", first.etag, META)
    got = store.get(bucket, "/x.md")
    assert got is not None and got.body == b"two"


def test_put_if_match_on_missing_key_is_precondition_failed(
    bucket: Any, store: ArticleStore
) -> None:
    with pytest.raises(PreconditionFailed):
        store.put_if_match(bucket, "/absent.md", b"x", '"deadbeef"', META)
    assert store.get(bucket, "/absent.md") is None


# --- access denied --------------------------------------------------------------


def test_denied_client_raises_access_denied(store: ArticleStore) -> None:
    denied = DenyingClient()
    with pytest.raises(AccessDenied):
        store.get(denied, "/x.md")
    with pytest.raises(AccessDenied):
        store.head(denied, "/x.md")
    with pytest.raises(AccessDenied):
        store.put_new(denied, "/x.md", b"x", META)
    with pytest.raises(AccessDenied):
        store.put_if_match(denied, "/x.md", b"x", '"e"', META)
    with pytest.raises(AccessDenied):
        store.list_children(denied, "/racing")


def test_unrelated_client_error_propagates(store: ArticleStore) -> None:
    class Broken:
        def get_object(self, **_: Any) -> Any:
            raise _client_error("InternalError", 500, "GetObject")

    with pytest.raises(ClientError):
        store.get(Broken(), "/x.md")


# --- listing ----------------------------------------------------------------------


def _seed(bucket: Any, settings: Settings, *keys: str) -> None:
    for key in keys:
        bucket.put_object(Bucket=settings.bucket, Key=key, Body=b"x")


def test_list_children_splits_folders_and_articles(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    _seed(
        bucket,
        settings,
        "a/racing/notes.md",
        "a/racing/setup/rear-bar.md",
        "a/racing/setup/front-bar.md",
        "a/racing/history/old.md",
        "a/racing/_listing.json",
        "a/racing/_sys/hidden.md",
        "a/racing/readme.txt",
        "a/other/x.md",
    )
    folders, articles = store.list_children(bucket, "/racing")
    assert folders == ["/racing/history", "/racing/setup"]
    assert [a.path for a in articles] == ["/racing/notes.md"]
    entry = articles[0]
    assert entry.key == "a/racing/notes.md"
    assert entry.etag.startswith('"')
    assert entry.size == 1
    assert entry.body == b""
    assert entry.metadata == {}


def test_list_children_root(bucket: Any, store: ArticleStore, settings: Settings) -> None:
    _seed(bucket, settings, "a/top.md", "a/racing/x.md", "a/_listing.json", "sys/config.json")
    folders, articles = store.list_children(bucket, "/")
    assert folders == ["/racing"]
    assert [a.path for a in articles] == ["/top.md"]


def test_list_children_empty(bucket: Any, store: ArticleStore) -> None:
    assert store.list_children(bucket, "/nothing") == ([], [])


def test_list_children_follows_pagination(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    keys = [f"a/big/{i:04d}.md" for i in range(1_050)]
    _seed(bucket, settings, *keys)
    folders, articles = store.list_children(bucket, "/big")
    assert folders == []
    assert len(articles) == 1_050
    assert articles[0].path == "/big/0000.md"
    assert articles[-1].path == "/big/1049.md"


# --- versions (increment C: §8.3, §10.7) --------------------------------------------


def _write_versions(bucket: Any, settings: Settings, key: str, count: int) -> list[str]:
    """``count`` versions of ``key``, oldest first; returns their version ids in that order."""
    ids: list[str] = []
    for i in range(count):
        response = bucket.put_object(
            Bucket=settings.bucket,
            Key=key,
            Body=f"v{i}".encode(),
            Metadata={META_ACTOR: f"human:u{i}", META_KIND: "write"},
        )
        ids.append(response["VersionId"])
    return ids


def _decode_cursor(cursor: str) -> dict[str, Any]:
    import base64
    import json

    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def test_list_versions_newest_first(bucket: Any, store: ArticleStore, settings: Settings) -> None:
    ids = _write_versions(bucket, settings, "a/x.md", 3)
    entries, cursor = store.list_versions(bucket, "/x.md")
    assert cursor is None
    assert [e.version_id for e in entries] == list(reversed(ids))
    for entry in entries:
        assert entry.key == "a/x.md"
        assert entry.etag.startswith('"')
        assert entry.size == 2
        assert entry.last_modified is not None
        assert entry.body == b""
        assert entry.metadata == {}


def test_list_versions_empty_key(bucket: Any, store: ArticleStore) -> None:
    assert store.list_versions(bucket, "/never.md") == ([], None)


def test_list_versions_prefix_collision_does_not_leak(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    # ``Prefix=a/x.md`` also matches ``a/x.mdx.md``; neither listing may show the other.
    short = _write_versions(bucket, settings, "a/x.md", 2)
    longer = _write_versions(bucket, settings, "a/x.mdx.md", 3)

    entries, cursor = store.list_versions(bucket, "/x.md", limit=10)
    assert cursor is None
    assert [e.version_id for e in entries] == list(reversed(short))
    assert {e.key for e in entries} == {"a/x.md"}

    entries, cursor = store.list_versions(bucket, "/x.mdx.md", limit=10)
    assert cursor is None
    assert [e.version_id for e in entries] == list(reversed(longer))

    # With ``limit`` equal to the short key's count, the spill-over check must look
    # past the longer key's versions without counting them or issuing a cursor.
    entries, cursor = store.list_versions(bucket, "/x.md", limit=2)
    assert cursor is None
    assert len(entries) == 2


def test_list_versions_cursor_walks_all_versions_one_at_a_time(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    ids = _write_versions(bucket, settings, "a/x.md", 3)
    _write_versions(bucket, settings, "a/x.md.bak.md", 1)  # a colliding neighbour
    seen: list[str] = []
    cursors: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        entries, cursor = store.list_versions(bucket, "/x.md", limit=1, cursor=cursor)
        assert len(entries) == 1
        seen.append(entries[0].version_id or "")
        if cursor is None:
            break
        cursors.append(cursor)
    assert seen == list(reversed(ids))
    assert len(cursors) == 2
    # Cursor format: URL-safe base64 of ``{"k": key_marker, "v": version_id_marker}``.
    assert _decode_cursor(cursors[0]) == {"k": "a/x.md", "v": ids[2]}
    assert _decode_cursor(cursors[1]) == {"k": "a/x.md", "v": ids[1]}


def test_list_versions_limit_exact_count_has_no_cursor(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    _write_versions(bucket, settings, "a/x.md", 3)
    entries, cursor = store.list_versions(bucket, "/x.md", limit=3)
    assert len(entries) == 3
    assert cursor is None
    entries, cursor = store.list_versions(bucket, "/x.md", limit=2)
    assert len(entries) == 2
    assert cursor is not None


def test_list_versions_ignores_delete_markers(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    ids = _write_versions(bucket, settings, "a/x.md", 2)
    bucket.delete_object(Bucket=settings.bucket, Key="a/x.md")  # a delete marker on top
    ids += _write_versions(bucket, settings, "a/x.md", 1)
    bucket.delete_object(Bucket=settings.bucket, Key="a/x.md")  # and another
    entries, cursor = store.list_versions(bucket, "/x.md", limit=10)
    assert cursor is None
    assert [e.version_id for e in entries] == list(reversed(ids))
    # Delete markers count toward MaxKeys; paging through them still yields every version.
    walked: list[str] = []
    cursor = None
    while True:
        page, cursor = store.list_versions(bucket, "/x.md", limit=1, cursor=cursor)
        walked += [e.version_id or "" for e in page]
        if cursor is None:
            break
    assert walked == list(reversed(ids))


@pytest.mark.parametrize("cursor", ["", "not base64!", "eyJ4IjoxfQ", "e30"])
def test_list_versions_rejects_malformed_cursor(
    bucket: Any, store: ArticleStore, cursor: str
) -> None:
    with pytest.raises(ValueError):
        store.list_versions(bucket, "/x.md", cursor=cursor)


def test_list_versions_rejects_cursor_from_another_path(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    _write_versions(bucket, settings, "a/x.md", 2)
    _, cursor = store.list_versions(bucket, "/x.md", limit=1)
    assert cursor is not None
    with pytest.raises(ValueError):
        store.list_versions(bucket, "/y.md", cursor=cursor)


def test_head_version_returns_metadata(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    ids = _write_versions(bucket, settings, "a/x.md", 2)
    head = store.head_version(bucket, "/x.md", ids[0])
    assert head is not None
    assert head.key == "a/x.md"
    assert head.version_id == ids[0]
    assert head.metadata == {META_ACTOR: "human:u0", META_KIND: "write"}
    assert head.size == 2
    assert head.etag.startswith('"')
    assert head.last_modified is not None
    assert head.body == b""
    assert store.head_version(bucket, "/x.md", "no-such-version") is None
    # A version id that belongs to another key is not this path's.
    other = _write_versions(bucket, settings, "a/y.md", 1)
    assert store.head_version(bucket, "/x.md", other[0]) is None


def test_get_version_from_another_key_is_none(
    bucket: Any, store: ArticleStore, settings: Settings
) -> None:
    _write_versions(bucket, settings, "a/x.md", 1)
    other = _write_versions(bucket, settings, "a/y.md", 1)
    assert store.get_version(bucket, "/x.md", other[0]) is None


def test_versions_denied_client_raises_access_denied(store: ArticleStore) -> None:
    class DenyingVersions(DenyingClient):
        def list_object_versions(self, **_: Any) -> Any:
            raise _client_error("AccessDenied", 403, "ListObjectVersions")

    denied = DenyingVersions()
    with pytest.raises(AccessDenied):
        store.list_versions(denied, "/x.md")
    with pytest.raises(AccessDenied):
        store.head_version(denied, "/x.md", "v1")
