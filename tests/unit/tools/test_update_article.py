"""``update_article`` (HANDOFF §5.1, §10.10)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import MAX_ARTICLE_BYTES, SCOPE_WRITE
from app.mcp.protocol import ToolContext
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.read_article import TOOL as READ
from app.mcp.tools.update_article import CURRENT_BODY_CAP, TOOL
from app.storage.articles import META_ACTOR, META_KIND, AccessDenied, ArticleStore
from app.storage.markdown import parse, serialize
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

PATH = "/racing/setup/rear-bar.md"
FM = {"type": "doc", "title": "Rear bar", "tags": ["racing"], "custom": "kept"}


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "update_article"
    assert TOOL.scope == SCOPE_WRITE
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["path", "content", "if_version"]
    assert TOOL.description.startswith("Replace the body, and optionally the frontmatter")


@pytest.fixture
def existing(ctx: ToolContext) -> dict[str, Any]:
    return call(CREATE, ctx, path=PATH, content="v1 body\n", frontmatter=FM)


def test_happy_path_bumps_seq_and_records_actor(
    ctx: ToolContext,
    existing: dict[str, Any],
    minter: FakeMinter,
    head_raw: Callable[..., Any],
    get_raw: Callable[..., Any],
) -> None:
    result = call(TOOL, ctx, path=PATH, content="v2 body\n", if_version=existing["version"])
    assert result["path"] == PATH
    assert result["seq"] == 2
    assert result["version"] != existing["version"]

    head = head_raw(PATH)
    assert head["ETag"].strip('"') == result["version"]
    assert head["Metadata"] == {META_ACTOR: f"human:{OWNER}", META_KIND: "write"}

    stored = parse(get_raw(PATH))
    assert stored.body == "v2 body\n"
    assert stored.frontmatter == {**FM, "seq": 2}
    # The write, then the parent listing refresh (§8.6); no ancestor flipped visibility.
    assert minter.calls[-2:] == [
        (OWNER, Shape.WRITE, PATH),
        (OWNER, Shape.MAINTAIN, "/racing/setup"),
    ]


def test_repeated_updates_chain(ctx: ToolContext, existing: dict[str, Any]) -> None:
    v2 = call(TOOL, ctx, path=PATH, content="two", if_version=existing["version"])
    v3 = call(TOOL, ctx, path=PATH, content="three", if_version=v2["version"])
    assert (v2["seq"], v3["seq"]) == (2, 3)
    read = call(READ, ctx, path=PATH)
    assert read["content"] == "three"
    assert read["version"] == v3["version"]
    assert read["frontmatter"]["seq"] == 3


def test_quoted_if_version_is_accepted(ctx: ToolContext, existing: dict[str, Any]) -> None:
    result = call(TOOL, ctx, path=PATH, content="two", if_version=f'"{existing["version"]}"')
    assert result["seq"] == 2


def test_frontmatter_omitted_leaves_block_unchanged(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    call(TOOL, ctx, path=PATH, content="two", if_version=existing["version"])
    assert parse(get_raw(PATH)).frontmatter == {**FM, "seq": 2}


def test_frontmatter_given_replaces_block_but_keeps_seq(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    replacement = {"type": "doc", "title": "New title", "status": "draft"}
    call(
        TOOL,
        ctx,
        path=PATH,
        content="two",
        if_version=existing["version"],
        frontmatter=replacement,
    )
    stored = parse(get_raw(PATH))
    assert stored.frontmatter == {**replacement, "seq": 2}
    assert "tags" not in stored.frontmatter
    assert "custom" not in stored.frontmatter
    assert "seq" not in replacement


def test_stale_if_version_is_409_and_writes_nothing(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any], head_raw: Any
) -> None:
    v2 = call(TOOL, ctx, path=PATH, content="v2 body\n", if_version=existing["version"])
    before = head_raw(PATH)
    error = expect_error(
        TOOL, ctx, 409, "conflict", path=PATH, content="v3", if_version=existing["version"]
    )
    assert error.extra["current_version"] == v2["version"]
    assert error.extra["current_body"] == "v2 body\n"
    assert "current_version" in error.structured()
    after = head_raw(PATH)
    assert after["ETag"] == before["ETag"]
    assert after["VersionId"] == before["VersionId"]
    assert parse(get_raw(PATH)).body == "v2 body\n"


def test_conflict_body_is_capped(ctx: ToolContext) -> None:
    big = "y" * (CURRENT_BODY_CAP + 500)
    created = call(CREATE, ctx, path=PATH, content=big, frontmatter=FM)
    call(TOOL, ctx, path=PATH, content=big, if_version=created["version"])
    error = expect_error(
        TOOL, ctx, 409, "conflict", path=PATH, content="x", if_version=created["version"]
    )
    assert len(error.extra["current_body"]) == CURRENT_BODY_CAP


def test_garbage_if_version_is_409(ctx: ToolContext, existing: dict[str, Any]) -> None:
    error = expect_error(TOOL, ctx, 409, "conflict", path=PATH, content="x", if_version="nope")
    assert error.extra["current_version"] == existing["version"]


def test_lost_race_at_s3_is_409_with_current_state(
    ctx: ToolContext, existing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our read sees v1; someone writes v2 before our conditional put; S3 says 412."""
    real_put = ArticleStore.put_if_match
    winner_version: dict[str, str] = {}

    def racing_put(
        self: ArticleStore, s3: Any, path: str, body: bytes, etag: str, meta: Any
    ) -> Any:
        if not winner_version:
            winner = real_put(self, s3, path, b"---\ntype: doc\nseq: 2\n---\nwinner", etag, meta)
            winner_version["v"] = winner.version
        return real_put(self, s3, path, body, etag, meta)

    monkeypatch.setattr(ArticleStore, "put_if_match", racing_put)
    error = expect_error(
        TOOL, ctx, 409, "conflict", path=PATH, content="loser", if_version=existing["version"]
    )
    assert error.extra["current_version"] == winner_version["v"]
    assert error.extra["current_body"] == "winner"
    assert call(READ, ctx, path=PATH)["content"] == "winner"


def test_missing_article_is_404(ctx: ToolContext, minter: FakeMinter) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, content="x", if_version="abc")
    assert minter.mint_count == 1


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"type": "pointer", "moved_to": "/racing/rear-bar.md", "seq": 2},
        {"type": "archived", "seq": 2},
    ],
)
def test_pointer_or_tombstone_is_404(
    ctx: ToolContext, put_raw: Callable[..., str], frontmatter: dict[str, Any]
) -> None:
    version = put_raw(PATH, frontmatter, "")
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, content="x", if_version=version)


@pytest.mark.parametrize("if_version", [None, "", '""', 7, "x" * 257])
def test_missing_or_bad_if_version_is_400(
    ctx: ToolContext, minter: FakeMinter, if_version: Any
) -> None:
    args: dict[str, Any] = {"path": PATH, "content": "x"}
    if if_version is not None:
        args["if_version"] = if_version
    expect_error(TOOL, ctx, 400, "bad_request", **args)
    assert minter.mint_count == 0


def test_caller_seq_in_frontmatter_is_ignored(ctx: ToolContext, existing: dict[str, Any]) -> None:
    """An agent that read the block and hands it back includes ``seq``; the server
    overwrites it rather than refusing (§10.2, §10.15)."""
    result = TOOL.handler(
        ctx,
        {
            "path": PATH,
            "content": "x",
            "if_version": existing["version"],
            "frontmatter": {"type": "doc", "seq": 999},
        },
    )
    assert result["seq"] == existing["seq"] + 1


@pytest.mark.parametrize("article_type", ["pointer", "archived"])
def test_reserved_type_in_frontmatter_is_400(
    ctx: ToolContext, existing: dict[str, Any], article_type: str
) -> None:
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        content="x",
        if_version=existing["version"],
        frontmatter={"type": article_type},
    )


def test_frontmatter_without_type_is_400(ctx: ToolContext, existing: dict[str, Any]) -> None:
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        content="x",
        if_version=existing["version"],
        frontmatter={"title": "no type"},
    )


def test_content_over_limit_is_400(ctx: ToolContext, existing: dict[str, Any]) -> None:
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        content="x" * (MAX_ARTICLE_BYTES + 1),
        if_version=existing["version"],
    )


@pytest.mark.parametrize("path", ["/racing/x", "/Racing/x.md", "/_x.md", None])
def test_bad_path_is_400(ctx: ToolContext, path: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path, content="x", if_version="v")


def test_zero_grant_subject_is_403_and_nothing_minted(
    ctx: ToolContext, existing: dict[str, Any], nobody: ToolContext, minter: FakeMinter
) -> None:
    minted_before = minter.mint_count
    expect_error(
        TOOL, nobody, 403, "forbidden", path=PATH, content="x", if_version=existing["version"]
    )
    assert minter.mint_count == minted_before


def test_read_grant_is_403(
    ctx: ToolContext,
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
) -> None:
    seed_grant("user_reader", "/racing", Permission.READ)
    minted_before = minter.mint_count
    expect_error(
        TOOL,
        make_ctx("user_reader"),
        403,
        "forbidden",
        path=PATH,
        content="x",
        if_version=existing["version"],
    )
    assert minter.mint_count == minted_before


def test_article_write_grant_suffices(
    ctx: ToolContext,
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
) -> None:
    seed_grant("user_editor", PATH, Permission.WRITE)
    result = call(
        TOOL, make_ctx("user_editor"), path=PATH, content="edited", if_version=existing["version"]
    )
    assert result["seq"] == 2


def test_s3_access_denied_is_403(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 403, "forbidden", path=PATH, content="x", if_version="v")


def test_s3_access_denied_on_put_is_403(
    ctx: ToolContext, existing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(*_: Any, **__: Any) -> Any:
        raise AccessDenied(PATH)

    monkeypatch.setattr(ArticleStore, "put_if_match", deny)
    expect_error(
        TOOL, ctx, 403, "forbidden", path=PATH, content="x", if_version=existing["version"]
    )


# --- audit (AS-10) and the object-size cap (review findings #4, #6b) ---------------------


def test_grants_used_are_audited(ctx: ToolContext, existing: dict[str, Any]) -> None:
    call(TOOL, ctx, path=PATH, content="x", if_version=existing["version"])
    assert ("/", "own") in ctx.audit.grants_used


def test_read_grant_denial_is_audited(
    ctx: ToolContext,
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
) -> None:
    """The read grant is what the 403 depended on; AS-10 wants it in the audit line."""
    seed_grant("user_reader", "/racing", Permission.READ)
    reader = make_ctx("user_reader")
    expect_error(
        TOOL, reader, 403, "forbidden", path=PATH, content="x", if_version=existing["version"]
    )
    assert ("/racing", "read") in reader.audit.grants_used


def _overhead(seq: int) -> int:
    return len(serialize({**FM, "seq": seq}, ""))


def test_object_at_limit_is_accepted(ctx: ToolContext, existing: dict[str, Any]) -> None:
    content = "x" * (MAX_ARTICLE_BYTES - _overhead(2))
    result = call(TOOL, ctx, path=PATH, content=content, if_version=existing["version"])
    assert result["seq"] == 2


def test_object_over_limit_is_400_and_writes_nothing(
    ctx: ToolContext,
    existing: dict[str, Any],
    get_raw: Callable[..., Any],
    head_raw: Callable[..., Any],
) -> None:
    before = head_raw(PATH)
    content = "x" * (MAX_ARTICLE_BYTES - _overhead(2) + 1)
    assert len(content.encode()) <= MAX_ARTICLE_BYTES
    expect_error(
        TOOL, ctx, 400, "bad_request", path=PATH, content=content, if_version=existing["version"]
    )
    assert head_raw(PATH)["VersionId"] == before["VersionId"]
    assert parse(get_raw(PATH)).body == "v1 body\n"


def test_replacement_frontmatter_over_limits_is_400(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        content="x",
        if_version=existing["version"],
        frontmatter={"type": "doc", "tags": ["x"] * 21},
    )
    assert parse(get_raw(PATH)).frontmatter == {**FM, "seq": 1}
