"""``archive_article`` (HANDOFF §5.2, §8.3, §10.12).

The listing index is another increment's; ``ListingIndex.refresh_child`` is replaced
with a recorder so these tests pin what archive *asks* of it, not how it answers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_WRITE
from app.mcp.protocol import ToolContext
from app.mcp.tools.archive_article import ARCHIVED_FROM_SEQ, TOOL
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.read_article import TOOL as READ
from app.mcp.tools.update_article import TOOL as UPDATE
from app.storage.articles import META_ACTOR, META_KIND, AccessDenied, ArticleStore
from app.storage.listings import ListingChild, ListingIndex
from app.storage.markdown import parse
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

PATH = "/racing/setup/rear-bar.md"
PARENT = "/racing/setup"
NAME = "rear-bar.md"
FM = {"type": "doc", "title": "Rear bar", "tags": ["racing"], "custom": "kept"}

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
    listing_calls.clear()  # only what archive asks for is under test
    return created


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "archive_article"
    assert TOOL.scope == SCOPE_WRITE
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["path", "if_version"]
    assert descriptor["outputSchema"]["properties"]["archived"] == {"const": True}
    assert TOOL.description.startswith("Remove an article from listings and search.")


def test_happy_path_writes_tombstone(
    ctx: ToolContext,
    existing: dict[str, Any],
    minter: FakeMinter,
    head_raw: Callable[..., Any],
    get_raw: Callable[..., Any],
    listing_calls: list[ListingCall],
) -> None:
    result = call(TOOL, ctx, path=PATH, if_version=existing["version"])
    assert result == {
        "path": PATH,
        "version": result["version"],
        "seq": 2,
        "archived": True,
    }
    assert result["version"] != existing["version"]

    head = head_raw(PATH)
    assert head["ETag"].strip('"') == result["version"]
    assert head["Metadata"] == {META_ACTOR: f"human:{OWNER}", META_KIND: "archive"}

    stored = parse(get_raw(PATH))
    assert stored.body == ""
    assert stored.frontmatter == {**FM, "type": "archived", ARCHIVED_FROM_SEQ: 1, "seq": 2}

    assert (OWNER, Shape.WRITE, PATH) in minter.calls
    assert listing_calls == [(PARENT, None, NAME)]
    assert (OWNER, Shape.MAINTAIN, PARENT) in minter.calls


def test_archived_article_reads_as_404(ctx: ToolContext, existing: dict[str, Any]) -> None:
    call(TOOL, ctx, path=PATH, if_version=existing["version"])
    expect_error(READ, ctx, 404, "not_found", path=PATH)


def test_create_at_archived_path_is_409_archived_with_tombstone_version(
    ctx: ToolContext, existing: dict[str, Any]
) -> None:
    """§5.2, §10.9: the caller unarchives with this version rather than overwriting."""
    tombstone = call(TOOL, ctx, path=PATH, if_version=existing["version"])
    error = expect_error(
        CREATE, ctx, 409, "archived", path=PATH, content="again", frontmatter={"type": "doc"}
    )
    assert error.extra["current_version"] == tombstone["version"]
    assert "unarchive_article" in error.message


def test_archive_follows_updates(ctx: ToolContext, existing: dict[str, Any]) -> None:
    v2 = call(UPDATE, ctx, path=PATH, content="v2", if_version=existing["version"])
    result = call(TOOL, ctx, path=PATH, if_version=v2["version"])
    assert result["seq"] == 3


def test_quoted_if_version_is_accepted(ctx: ToolContext, existing: dict[str, Any]) -> None:
    result = call(TOOL, ctx, path=PATH, if_version=f'"{existing["version"]}"')
    assert result["archived"] is True


def test_stale_if_version_is_409_and_writes_nothing(
    ctx: ToolContext,
    existing: dict[str, Any],
    head_raw: Callable[..., Any],
    get_raw: Callable[..., Any],
    listing_calls: list[ListingCall],
) -> None:
    v2 = call(UPDATE, ctx, path=PATH, content="v2 body\n", if_version=existing["version"])
    listing_calls.clear()
    before = head_raw(PATH)
    error = expect_error(TOOL, ctx, 409, "conflict", path=PATH, if_version=existing["version"])
    assert error.extra["current_version"] == v2["version"]
    assert error.extra["current_body"] == "v2 body\n"
    after = head_raw(PATH)
    assert after["VersionId"] == before["VersionId"]
    assert parse(get_raw(PATH)).type == "doc"
    assert listing_calls == []


def test_lost_race_at_s3_is_409_with_current_state(
    ctx: ToolContext, existing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_put = ArticleStore.put_if_match
    winner: dict[str, str] = {}

    def racing_put(
        self: ArticleStore, s3: Any, path: str, body: bytes, etag: str, meta: Any
    ) -> Any:
        if not winner:
            won = real_put(self, s3, path, b"---\ntype: doc\nseq: 2\n---\nwinner", etag, meta)
            winner["v"] = won.version
        return real_put(self, s3, path, body, etag, meta)

    monkeypatch.setattr(ArticleStore, "put_if_match", racing_put)
    error = expect_error(TOOL, ctx, 409, "conflict", path=PATH, if_version=existing["version"])
    assert error.extra["current_version"] == winner["v"]
    assert error.extra["current_body"] == "winner"


def test_missing_article_is_404(ctx: ToolContext, listing_calls: list[ListingCall]) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, if_version="abc")
    assert listing_calls == []


def test_already_archived_is_404(ctx: ToolContext, existing: dict[str, Any]) -> None:
    tombstone = call(TOOL, ctx, path=PATH, if_version=existing["version"])
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, if_version=tombstone["version"])


def test_pointer_is_404(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    version = put_raw(PATH, {"type": "pointer", "moved_to": "/racing/rear-bar.md", "seq": 2})
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, if_version=version)


@pytest.mark.parametrize("path", ["/racing/x", "/Racing/x.md", "/_x.md", "", None])
def test_bad_path_is_400(ctx: ToolContext, minter: FakeMinter, path: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path, if_version="v")
    assert minter.mint_count == 0


@pytest.mark.parametrize("if_version", [None, "", '""', 7, "x" * 257])
def test_missing_or_bad_if_version_is_400(
    ctx: ToolContext, minter: FakeMinter, if_version: Any
) -> None:
    args: dict[str, Any] = {"path": PATH}
    if if_version is not None:
        args["if_version"] = if_version
    expect_error(TOOL, ctx, 400, "bad_request", **args)
    assert minter.mint_count == 0


def test_zero_grant_subject_is_403_and_nothing_minted(
    existing: dict[str, Any], nobody: ToolContext, minter: FakeMinter
) -> None:
    minted_before = minter.mint_count
    expect_error(TOOL, nobody, 403, "forbidden", path=PATH, if_version=existing["version"])
    assert minter.mint_count == minted_before


def test_read_grant_is_403(
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
) -> None:
    seed_grant("user_reader", "/racing", Permission.READ)
    minted_before = minter.mint_count
    expect_error(
        TOOL, make_ctx("user_reader"), 403, "forbidden", path=PATH, if_version=existing["version"]
    )
    assert minter.mint_count == minted_before


def test_article_write_grant_suffices_but_skips_listing(
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    listing_calls: list[ListingCall],
) -> None:
    """An article-only writer cannot maintain the folder listing (§4.6); the archive
    still lands and no MAINTAIN credential is minted."""
    seed_grant("user_editor", PATH, Permission.WRITE)
    editor = make_ctx("user_editor")
    minted_before = len(minter.calls)
    result = call(TOOL, editor, path=PATH, if_version=existing["version"])
    assert result["archived"] is True
    assert listing_calls == []
    assert minter.calls[minted_before:] == [("user_editor", Shape.WRITE, PATH)]


def test_grants_used_are_audited(ctx: ToolContext, existing: dict[str, Any]) -> None:
    call(TOOL, ctx, path=PATH, if_version=existing["version"])
    assert ("/", "own") in ctx.audit.grants_used


def test_s3_access_denied_is_403(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 403, "forbidden", path=PATH, if_version="v")


def test_s3_access_denied_on_put_is_403(
    ctx: ToolContext, existing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*_: Any, **__: Any) -> Any:
        raise AccessDenied(PATH)

    monkeypatch.setattr(ArticleStore, "put_if_match", deny)
    expect_error(TOOL, ctx, 403, "forbidden", path=PATH, if_version=existing["version"])


def test_listing_failure_never_fails_the_archive(
    ctx: ToolContext,
    existing: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    get_raw: Callable[..., Any],
) -> None:
    def explode(*_: Any, **__: Any) -> None:
        raise RuntimeError("listing store down")

    monkeypatch.setattr(ListingIndex, "refresh_child", explode)
    result = call(TOOL, ctx, path=PATH, if_version=existing["version"])
    assert result["archived"] is True
    assert parse(get_raw(PATH)).type == "archived"
