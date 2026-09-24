"""``create_article`` (HANDOFF §10.9)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import MAX_ARTICLE_BYTES, SCOPE_WRITE
from app.mcp.protocol import ToolContext
from app.mcp.tools._common import MAX_FRONTMATTER_BYTES
from app.mcp.tools.create_article import TOOL
from app.mcp.tools.read_article import TOOL as READ
from app.storage.articles import META_ACTOR, META_KIND
from app.storage.markdown import parse, serialize
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

PATH = "/racing/setup/rear-bar.md"
FM = {"type": "doc", "title": "Rear bar", "tags": ["racing"], "custom": {"k": [1, 2]}}


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "create_article"
    assert TOOL.scope == SCOPE_WRITE
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["path", "content", "frontmatter"]
    assert TOOL.description.startswith("Create a new article.")


def test_create_writes_seq_1_and_metadata(
    ctx: ToolContext, minter: FakeMinter, head_raw: Callable[..., Any], get_raw: Callable[..., Any]
) -> None:
    result = call(TOOL, ctx, path=PATH, content="# Rear bar\n\nbody\n", frontmatter=FM)
    assert result["path"] == PATH
    assert result["seq"] == 1
    assert result["version"] and '"' not in result["version"]

    head = head_raw(PATH)
    assert head["ETag"].strip('"') == result["version"]
    assert head["Metadata"] == {META_ACTOR: f"human:{OWNER}", META_KIND: "write"}
    assert head["ContentType"] == "text/markdown; charset=utf-8"

    stored = parse(get_raw(PATH))
    assert stored.frontmatter == {**FM, "seq": 1}
    assert stored.body == "# Rear bar\n\nbody\n"
    # The write itself, then listing upkeep (§8.6): MAINTAIN on the parent, and on each
    # ancestor as the first article makes the folder chain visible.
    assert minter.calls[0] == (OWNER, Shape.WRITE, PATH)
    assert minter.calls[1:] == [
        (OWNER, Shape.MAINTAIN, "/racing/setup"),
        (OWNER, Shape.MAINTAIN, "/racing"),
        (OWNER, Shape.MAINTAIN, "/"),
    ]


def test_create_then_read_round_trip(ctx: ToolContext) -> None:
    created = call(TOOL, ctx, path=PATH, content="body\n", frontmatter=FM)
    read = call(READ, ctx, path=PATH)
    assert read["version"] == created["version"]
    assert read["frontmatter"] == {**FM, "seq": 1}
    assert read["content"] == "body\n"
    assert read["trust"] == "unverified"


def test_create_does_not_mutate_caller_frontmatter(ctx: ToolContext) -> None:
    frontmatter = dict(FM)
    call(TOOL, ctx, path=PATH, content="", frontmatter=frontmatter)
    assert "seq" not in frontmatter


def test_create_twice_is_409_exists(ctx: ToolContext, get_raw: Callable[..., Any]) -> None:
    call(TOOL, ctx, path=PATH, content="one", frontmatter=FM)
    expect_error(TOOL, ctx, 409, "exists", path=PATH, content="two", frontmatter=FM)
    assert parse(get_raw(PATH)).body == "one"


def test_create_at_pointer_is_409_retired_pointer(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "pointer", "moved_to": "/racing/rear-bar.md", "seq": 3})
    expect_error(TOOL, ctx, 409, "retired_pointer", path=PATH, content="x", frontmatter=FM)


def test_create_at_archived_is_409_with_tombstone_version(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    tombstone = put_raw(PATH, {"type": "archived", "seq": 4})
    error = expect_error(TOOL, ctx, 409, "archived", path=PATH, content="x", frontmatter=FM)
    assert error.extra["current_version"] == tombstone
    assert "unarchive_article" in error.message


@pytest.mark.parametrize("path", ["/racing/index.md", "/log.md", "/a/b/index.md"])
def test_reserved_name_is_400(ctx: ToolContext, minter: FakeMinter, path: str) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path, content="x", frontmatter=FM)
    assert minter.mint_count == 0


@pytest.mark.parametrize(
    "path",
    [
        "/Racing/x.md",
        "/racing/x",
        "racing/x.md",
        "/racing/_private/x.md",
        "/racing//x.md",
        "/racing/../x.md",
        "/racing/x.md/",
        "/racing/x.md/y.md",  # ".md" folder segment: would let an article grant cascade
        "/",
        "",
        None,
        42,
    ],
)
def test_bad_path_is_400(ctx: ToolContext, path: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path, content="x", frontmatter=FM)


def test_caller_seq_is_400(ctx: ToolContext, minter: FakeMinter) -> None:
    expect_error(
        TOOL, ctx, 400, "bad_request", path=PATH, content="x", frontmatter={**FM, "seq": 9}
    )
    assert minter.mint_count == 0


@pytest.mark.parametrize("article_type", ["pointer", "archived"])
def test_reserved_type_is_400(ctx: ToolContext, article_type: str) -> None:
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        content="x",
        frontmatter={"type": article_type, "moved_to": "/elsewhere.md"},
    )


@pytest.mark.parametrize("frontmatter", [None, "doc", [], {}, {"type": ""}, {"type": 3}])
def test_missing_or_invalid_type_is_400(ctx: ToolContext, frontmatter: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, content="x", frontmatter=frontmatter)


def test_content_over_limit_is_400(ctx: ToolContext, minter: FakeMinter) -> None:
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        content="x" * (MAX_ARTICLE_BYTES + 1),
        frontmatter=FM,
    )
    assert minter.mint_count == 0


def test_content_limit_counts_utf8_bytes(ctx: ToolContext) -> None:
    # 3 bytes per character: fewer characters than the limit, more bytes.
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        content="☕" * (MAX_ARTICLE_BYTES // 2),
        frontmatter=FM,
    )


def _overhead() -> int:
    """Bytes the frontmatter block adds to the stored object, ``seq`` included."""
    return len(serialize({**FM, "seq": 1}, ""))


def test_object_at_limit_is_accepted(ctx: ToolContext) -> None:
    content = "x" * (MAX_ARTICLE_BYTES - _overhead())
    result = call(TOOL, ctx, path=PATH, content=content, frontmatter=FM)
    assert result["seq"] == 1


def test_object_over_limit_is_400_even_when_content_fits(
    ctx: ToolContext, minter: FakeMinter
) -> None:
    """The 1 MiB ceiling is on the stored object (§6.3), frontmatter included."""
    content = "x" * (MAX_ARTICLE_BYTES - _overhead() + 1)
    assert len(content.encode()) <= MAX_ARTICLE_BYTES
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, content=content, frontmatter=FM)
    assert minter.mint_count == 0


@pytest.mark.parametrize(
    "frontmatter",
    [
        {**FM, "title": "t" * 201},
        {**FM, "tags": ["x"] * 21},
        {**FM, "status": "published"},
        {**FM, "verified": [{"by": "not-an-actor"}]},
        {**FM, "notes": "n" * MAX_FRONTMATTER_BYTES},
    ],
)
def test_frontmatter_over_limits_is_400_and_mints_nothing(
    ctx: ToolContext, minter: FakeMinter, frontmatter: dict[str, Any]
) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, content="x", frontmatter=frontmatter)
    assert minter.mint_count == 0


@pytest.mark.parametrize("content", [None, 7, ["x"]])
def test_non_string_content_is_400(ctx: ToolContext, content: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, content=content, frontmatter=FM)


def test_zero_grant_subject_is_403_and_nothing_minted(
    nobody: ToolContext, minter: FakeMinter
) -> None:
    expect_error(TOOL, nobody, 403, "forbidden", path=PATH, content="x", frontmatter=FM)
    assert minter.mint_count == 0


def test_read_only_grant_is_403(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None], minter: FakeMinter
) -> None:
    seed_grant("user_reader", "/racing", Permission.READ)
    reader = make_ctx("user_reader")
    expect_error(TOOL, reader, 403, "forbidden", path=PATH, content="x", frontmatter=FM)
    assert minter.mint_count == 0


def test_write_grant_on_ancestor_suffices(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None]
) -> None:
    seed_grant("user_writer", "/racing", Permission.WRITE)
    writer = make_ctx("user_writer")
    result = call(TOOL, writer, path=PATH, content="x", frontmatter=FM)
    assert result["seq"] == 1


def test_write_grant_elsewhere_is_403(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None], minter: FakeMinter
) -> None:
    seed_grant("user_writer", "/other", Permission.WRITE)
    writer = make_ctx("user_writer")
    expect_error(TOOL, writer, 403, "forbidden", path=PATH, content="x", frontmatter=FM)
    assert minter.mint_count == 0


def test_s3_access_denied_is_403(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 403, "forbidden", path=PATH, content="x", frontmatter=FM)


# --- audit (AS-10) -----------------------------------------------------------------------


def test_grants_used_are_audited(ctx: ToolContext) -> None:
    call(TOOL, ctx, path=PATH, content="x", frontmatter=FM)
    assert ("/", "own") in ctx.audit.grants_used


def test_ancestor_write_grant_is_audited(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None]
) -> None:
    seed_grant("user_writer", "/racing", Permission.WRITE)
    writer = make_ctx("user_writer")
    call(TOOL, writer, path=PATH, content="x", frontmatter=FM)
    assert writer.audit.grants_used == [("/racing", "write")]


def test_insufficient_grant_is_audited_on_denial(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None], minter: FakeMinter
) -> None:
    """AS-10 wants the grants the decision depended on — a denial included."""
    seed_grant("user_reader", "/racing", Permission.READ)
    reader = make_ctx("user_reader")
    expect_error(TOOL, reader, 403, "forbidden", path=PATH, content="x", frontmatter=FM)
    assert ("/racing", "read") in reader.audit.grants_used
    assert minter.mint_count == 0


def test_zero_grant_denial_audits_nothing(nobody: ToolContext) -> None:
    expect_error(TOOL, nobody, 403, "forbidden", path=PATH, content="x", frontmatter=FM)
    assert nobody.audit.grants_used == []
