"""``unarchive_article`` (HANDOFF §5.2, §8.3, §10.13).

``ListingIndex.refresh_child`` is replaced with a recorder (see test_archive_article).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_WRITE
from app.mcp.protocol import ToolContext
from app.mcp.tools import unarchive_article
from app.mcp.tools.archive_article import ARCHIVED_FROM_SEQ
from app.mcp.tools.archive_article import TOOL as ARCHIVE
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.read_article import TOOL as READ
from app.mcp.tools.unarchive_article import TOOL, last_content_version
from app.mcp.tools.update_article import TOOL as UPDATE
from app.storage.articles import META_ACTOR, META_KIND, AccessDenied, ArticleStore
from app.storage.listings import ListingChild, ListingIndex
from app.storage.markdown import parse
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

PATH = "/racing/setup/rear-bar.md"
PARENT = "/racing/setup"
NAME = "rear-bar.md"
FM = {"type": "doc", "title": "Rear bar", "tags": ["racing"], "status": "draft", "custom": 1}

ListingCall = tuple[str, ListingChild | None, str]


@pytest.fixture(autouse=True)
def listing_calls(monkeypatch: pytest.MonkeyPatch) -> list[ListingCall]:
    calls: list[ListingCall] = []

    def record(self: ListingIndex, s3: Any, folder: str, child: Any, name: str) -> None:
        calls.append((folder, child, name))

    monkeypatch.setattr(ListingIndex, "refresh_child", record)
    return calls


@pytest.fixture
def existing(ctx: ToolContext, listing_calls: list[ListingCall]) -> dict[str, Any]:
    created = call(CREATE, ctx, path=PATH, content="v1 body\n", frontmatter=FM)
    listing_calls.clear()
    return created


@pytest.fixture
def archived(
    ctx: ToolContext, existing: dict[str, Any], listing_calls: list[ListingCall]
) -> dict[str, Any]:
    tombstone = call(ARCHIVE, ctx, path=PATH, if_version=existing["version"])
    listing_calls.clear()
    return tombstone


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "unarchive_article"
    assert TOOL.scope == SCOPE_WRITE
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["path"]
    assert "if_version" in descriptor["inputSchema"]["properties"]
    assert descriptor["outputSchema"]["properties"]["archived"] == {"const": False}
    assert TOOL.description.startswith("Restore an archived article to listings.")


def test_restores_exact_body_and_frontmatter_with_seq_bumped_twice(
    ctx: ToolContext,
    existing: dict[str, Any],
    archived: dict[str, Any],
    minter: FakeMinter,
    head_raw: Callable[..., Any],
    get_raw: Callable[..., Any],
    listing_calls: list[ListingCall],
) -> None:
    result = call(TOOL, ctx, path=PATH)
    assert result == {"path": PATH, "version": result["version"], "seq": 3, "archived": False}
    assert result["version"] not in (existing["version"], archived["version"])

    head = head_raw(PATH)
    assert head["ETag"].strip('"') == result["version"]
    assert head["Metadata"] == {META_ACTOR: f"human:{OWNER}", META_KIND: "unarchive"}

    stored = parse(get_raw(PATH))
    assert stored.body == "v1 body\n"
    assert stored.frontmatter == {**FM, "seq": 3}
    assert ARCHIVED_FROM_SEQ not in stored.frontmatter

    read = call(READ, ctx, path=PATH)
    assert read["content"] == "v1 body\n"
    assert read["version"] == result["version"]

    assert (OWNER, Shape.WRITE, PATH) in minter.calls
    assert (OWNER, Shape.MAINTAIN, PARENT) in minter.calls
    assert len(listing_calls) == 1
    folder, child, name = listing_calls[0]
    assert (folder, name) == (PARENT, NAME)
    assert isinstance(child, ListingChild)
    assert (child.name, child.kind, child.etag) == (NAME, "article", result["version"])
    assert (child.title, child.status, child.tags, child.seq) == (
        "Rear bar",
        "draft",
        ["racing"],
        3,
    )


def test_restores_the_last_content_version_not_the_first(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    v2 = call(
        UPDATE,
        ctx,
        path=PATH,
        content="v2 body\n",
        if_version=existing["version"],
        frontmatter={"type": "doc", "title": "Renamed"},
    )
    call(ARCHIVE, ctx, path=PATH, if_version=v2["version"])
    result = call(TOOL, ctx, path=PATH)
    assert result["seq"] == 4
    stored = parse(get_raw(PATH))
    assert stored.body == "v2 body\n"
    assert stored.frontmatter == {"type": "doc", "title": "Renamed", "seq": 4}


def test_archive_unarchive_cycle_keeps_one_chain(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    """§5.2: ``[content, archived, content, archived, content]`` — one chain, seq 1..5."""
    t1 = call(ARCHIVE, ctx, path=PATH, if_version=existing["version"])
    r1 = call(TOOL, ctx, path=PATH)
    t2 = call(ARCHIVE, ctx, path=PATH, if_version=r1["version"])
    r2 = call(TOOL, ctx, path=PATH)
    assert (t1["seq"], r1["seq"], t2["seq"], r2["seq"]) == (2, 3, 4, 5)
    assert parse(get_raw(PATH)).body == "v1 body\n"


def test_if_version_matching_tombstone_is_accepted(
    ctx: ToolContext, archived: dict[str, Any]
) -> None:
    result = call(TOOL, ctx, path=PATH, if_version=archived["version"])
    assert result["archived"] is False


def test_if_version_from_create_409_works_end_to_end(
    ctx: ToolContext, archived: dict[str, Any]
) -> None:
    """§10.15: the create 409 carries the tombstone's version so restore costs no
    extra call."""
    error = expect_error(
        CREATE, ctx, 409, "archived", path=PATH, content="x", frontmatter={"type": "doc"}
    )
    result = call(TOOL, ctx, path=PATH, if_version=error.extra["current_version"])
    assert result["seq"] == 3


def test_stale_if_version_is_409_and_writes_nothing(
    ctx: ToolContext,
    existing: dict[str, Any],
    archived: dict[str, Any],
    head_raw: Callable[..., Any],
    listing_calls: list[ListingCall],
) -> None:
    before = head_raw(PATH)
    error = expect_error(TOOL, ctx, 409, "conflict", path=PATH, if_version=existing["version"])
    assert error.extra["current_version"] == archived["version"]
    assert head_raw(PATH)["VersionId"] == before["VersionId"]
    assert listing_calls == []


def test_lost_race_at_s3_is_409(
    ctx: ToolContext, archived: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents restore at once: the second conditional put sees a changed ETag."""
    real_put = ArticleStore.put_if_match
    winner: dict[str, str] = {}

    def racing_put(
        self: ArticleStore, s3: Any, path: str, body: bytes, etag: str, meta: Any
    ) -> Any:
        if not winner:
            won = real_put(self, s3, path, b"---\ntype: doc\nseq: 3\n---\nwinner", etag, meta)
            winner["v"] = won.version
        return real_put(self, s3, path, body, etag, meta)

    monkeypatch.setattr(ArticleStore, "put_if_match", racing_put)
    error = expect_error(TOOL, ctx, 409, "conflict", path=PATH)
    assert error.extra["current_version"] == winner["v"]


def test_live_article_is_404(
    ctx: ToolContext, existing: dict[str, Any], listing_calls: list[ListingCall]
) -> None:
    error = expect_error(TOOL, ctx, 404, "not_found", path=PATH)
    assert "not archived" in error.message
    assert listing_calls == []


def test_second_unarchive_is_404(ctx: ToolContext, archived: dict[str, Any]) -> None:
    call(TOOL, ctx, path=PATH)
    expect_error(TOOL, ctx, 404, "not_found", path=PATH)


def test_missing_path_is_404(ctx: ToolContext) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path=PATH)


def test_pointer_is_404(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, {"type": "pointer", "moved_to": "/racing/rear-bar.md", "seq": 2})
    expect_error(TOOL, ctx, 404, "not_found", path=PATH)


def test_tombstone_with_nothing_beneath_is_500(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "archived", "seq": 1})
    error = expect_error(TOOL, ctx, 500, "internal", path=PATH)
    assert "No content version" in error.message


def test_version_scan_is_bounded(
    ctx: ToolContext,
    existing: dict[str, Any],
    put_raw: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content exists but sits beneath more reserved versions than the bound allows."""
    monkeypatch.setattr(unarchive_article, "VERSION_SCAN_LIMIT", 2)
    for seq in (2, 3, 4):
        put_raw(PATH, {"type": "archived", "seq": seq})
    expect_error(TOOL, ctx, 500, "internal", path=PATH)
    monkeypatch.setattr(unarchive_article, "VERSION_SCAN_LIMIT", 3)
    assert call(TOOL, ctx, path=PATH)["seq"] == 5


def test_last_content_version_skips_reserved_types(
    ctx: ToolContext,
    existing: dict[str, Any],
    put_raw: Callable[..., str],
    bucket: Any,
) -> None:
    put_raw(PATH, {"type": "pointer", "moved_to": "/x.md", "seq": 2})
    put_raw(PATH, {"type": "archived", "seq": 3})
    found = last_content_version(ArticleStore(ctx.settings.bucket), bucket, PATH)
    assert found is not None
    assert found.version == existing["version"]
    assert parse(found.body).body == "v1 body\n"


def test_last_content_version_ignores_longer_keys(
    ctx: ToolContext, put_raw: Callable[..., str], bucket: Any
) -> None:
    """``/x.md`` is a legal folder name; its children share our key as a prefix."""
    put_raw("/racing/setup/rear-bar.md/child.md", {"type": "doc", "seq": 1}, "child")
    put_raw(PATH, {"type": "archived", "seq": 1})
    assert last_content_version(ArticleStore(ctx.settings.bucket), bucket, PATH) is None


@pytest.mark.parametrize("path", ["/racing/x", "/Racing/x.md", "/_x.md", "", None])
def test_bad_path_is_400(ctx: ToolContext, minter: FakeMinter, path: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path)
    assert minter.mint_count == 0


@pytest.mark.parametrize("if_version", ["", '""', 7, "x" * 257])
def test_bad_if_version_is_400(ctx: ToolContext, minter: FakeMinter, if_version: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, if_version=if_version)
    assert minter.mint_count == 0


def test_zero_grant_subject_is_403_and_nothing_minted(
    archived: dict[str, Any], nobody: ToolContext, minter: FakeMinter
) -> None:
    minted_before = minter.mint_count
    expect_error(TOOL, nobody, 403, "forbidden", path=PATH)
    assert minter.mint_count == minted_before


def test_read_grant_is_403(
    archived: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
) -> None:
    seed_grant("user_reader", "/racing", Permission.READ)
    minted_before = minter.mint_count
    expect_error(TOOL, make_ctx("user_reader"), 403, "forbidden", path=PATH)
    assert minter.mint_count == minted_before


def test_article_write_grant_suffices_but_skips_listing(
    archived: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    listing_calls: list[ListingCall],
) -> None:
    seed_grant("user_editor", PATH, Permission.WRITE)
    minted_before = len(minter.calls)
    result = call(TOOL, make_ctx("user_editor"), path=PATH)
    assert result["archived"] is False
    assert listing_calls == []
    assert minter.calls[minted_before:] == [("user_editor", Shape.WRITE, PATH)]


def test_s3_access_denied_is_403(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 403, "forbidden", path=PATH)


def test_s3_access_denied_on_put_is_403(
    ctx: ToolContext, archived: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*_: Any, **__: Any) -> Any:
        raise AccessDenied(PATH)

    monkeypatch.setattr(ArticleStore, "put_if_match", deny)
    expect_error(TOOL, ctx, 403, "forbidden", path=PATH)


def test_listing_failure_never_fails_the_restore(
    ctx: ToolContext,
    archived: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    get_raw: Callable[..., Any],
) -> None:
    def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("listing store down")

    monkeypatch.setattr(ListingIndex, "refresh_child", explode)
    result = call(TOOL, ctx, path=PATH)
    assert result["archived"] is False
    assert parse(get_raw(PATH)).type == "doc"
