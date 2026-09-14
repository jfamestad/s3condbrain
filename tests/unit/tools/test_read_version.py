"""``read_version`` (HANDOFF §10.8)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ
from app.mcp.protocol import ToolContext
from app.mcp.tools.list_versions import ACTOR_UNKNOWN
from app.mcp.tools.list_versions import TOOL as LIST
from app.mcp.tools.read_article import TOOL as READ
from app.mcp.tools.read_version import TOOL
from app.storage.articles import META_ACTOR, META_KIND
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

PATH = "/racing/setup/rear-bar.md"
OTHER = "/racing/setup/front-bar.md"

FM_V1 = {"type": "doc", "title": "Rear bar", "tags": ["old"], "seq": 1}
FM_V2 = {"type": "doc", "title": "Rear bar (revised)", "tags": ["new"], "seq": 2}


def _versions(ctx: ToolContext, path: str = PATH) -> list[dict[str, Any]]:
    """Newest first, as ``list_versions`` reports them."""
    return call(LIST, ctx, path=path)["versions"]


@pytest.fixture
def two_versions(ctx: ToolContext, put_raw: Callable[..., str]) -> list[dict[str, Any]]:
    put_raw(PATH, FM_V1, "first body\n", **{META_ACTOR: "human:alice", META_KIND: "write"})
    put_raw(PATH, FM_V2, "second body\n", **{META_ACTOR: "human:bob", META_KIND: "write"})
    return _versions(ctx)


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "read_version"
    assert TOOL.scope == SCOPE_READ
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["path", "version_id"]
    assert "section" not in descriptor["inputSchema"]["properties"]
    assert descriptor["outputSchema"]["required"] == [
        "path",
        "version_id",
        "frontmatter",
        "content",
        "actor",
        "at",
        "total_bytes",
        "returned_bytes",
        "truncated",
    ]
    assert TOOL.description.startswith("One historical version, pinned by its version_id")


def test_returns_the_historical_body_and_frontmatter_exactly(
    ctx: ToolContext, two_versions: list[dict[str, Any]], minter: FakeMinter
) -> None:
    newest, oldest = two_versions
    result = call(TOOL, ctx, path=PATH, version_id=oldest["version_id"])
    assert result == {
        "path": PATH,
        "version_id": oldest["version_id"],
        "seq": 1,
        "frontmatter": FM_V1,
        "content": "first body\n",
        "actor": "human:alice",
        "at": oldest["at"],
        "total_bytes": len("first body\n"),
        "returned_bytes": len("first body\n"),
        "truncated": False,
    }
    assert minter.calls[-1] == (OWNER, Shape.READ, PATH)

    current = call(TOOL, ctx, path=PATH, version_id=newest["version_id"])
    assert current["frontmatter"] == FM_V2
    assert current["content"] == "second body\n"
    assert current["actor"] == "human:bob"
    assert current["seq"] == 2
    # The live read agrees with the newest version.
    assert call(READ, ctx, path=PATH)["content"] == "second body\n"


def test_history_is_immutable_after_a_later_write(
    ctx: ToolContext, two_versions: list[dict[str, Any]], put_raw: Callable[..., str]
) -> None:
    _, oldest = two_versions
    put_raw(PATH, {"type": "doc", "seq": 3}, "third body\n")
    result = call(TOOL, ctx, path=PATH, version_id=oldest["version_id"])
    assert result["content"] == "first body\n"
    assert result["frontmatter"] == FM_V1


def test_object_without_metadata_reports_unknown_actor(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, "x")
    [only] = _versions(ctx)
    result = call(TOOL, ctx, path=PATH, version_id=only["version_id"])
    assert result["actor"] == ACTOR_UNKNOWN
    assert result["seq"] == 0


# --- pointers and tombstones are history, not forward references ----------------------


def test_pointer_version_is_returned_as_stored(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, FM_V1, "before the move\n", **{META_ACTOR: "human:a", META_KIND: "write"})
    pointer = {"type": "pointer", "moved_to": OTHER, "moved_at": "2026-02-03T04:05:06Z", "seq": 2}
    put_raw(PATH, pointer, "", **{META_ACTOR: "human:mover", META_KIND: "moved_out"})
    newest, oldest = _versions(ctx)

    result = call(TOOL, ctx, path=PATH, version_id=newest["version_id"])
    assert result["frontmatter"] == pointer
    assert result["content"] == ""
    assert result["actor"] == "human:mover"
    assert "kind" not in result  # never a forward_reference
    assert "moved_to" not in result

    # The version beneath the pointer is still readable at this path.
    assert call(TOOL, ctx, path=PATH, version_id=oldest["version_id"])["content"] == (
        "before the move\n"
    )
    # Whereas the live read answers with the forward reference.
    assert call(READ, ctx, path=PATH)["kind"] == "forward_reference"


def test_archived_version_is_returned_as_stored(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, FM_V1, "live\n", **{META_ACTOR: "human:a", META_KIND: "write"})
    put_raw(
        PATH, {"type": "archived", "seq": 2}, "", **{META_ACTOR: "human:b", META_KIND: "archive"}
    )
    newest, oldest = _versions(ctx)
    tombstone = call(TOOL, ctx, path=PATH, version_id=newest["version_id"])
    assert tombstone["frontmatter"] == {"type": "archived", "seq": 2}
    assert tombstone["actor"] == "human:b"
    assert call(TOOL, ctx, path=PATH, version_id=oldest["version_id"])["content"] == "live\n"
    expect_error(READ, ctx, 404, "not_found", path=PATH)


# --- byte_range ---------------------------------------------------------------------


def _one(ctx: ToolContext, put_raw: Callable[..., str], body: str) -> str:
    put_raw(PATH, {"type": "doc", "seq": 1}, body)
    return _versions(ctx)[0]["version_id"]


def test_byte_range_is_inclusive_and_truncated(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    vid = _one(ctx, put_raw, "0123456789")
    result = call(TOOL, ctx, path=PATH, version_id=vid, byte_range=[2, 5])
    assert result["content"] == "2345"
    assert result["returned_bytes"] == 4
    assert result["total_bytes"] == 10
    assert result["truncated"] is True


def test_byte_range_clamps_to_body(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    vid = _one(ctx, put_raw, "0123456789")
    result = call(TOOL, ctx, path=PATH, version_id=vid, byte_range=[7, 500])
    assert result["content"] == "789"
    assert result["returned_bytes"] == 3
    assert result["truncated"] is True


def test_byte_range_covering_whole_body_is_not_truncated(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    vid = _one(ctx, put_raw, "0123456789")
    result = call(TOOL, ctx, path=PATH, version_id=vid, byte_range=[0, 9])
    assert result["content"] == "0123456789"
    assert result["truncated"] is False


def test_byte_range_past_end_is_empty(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    vid = _one(ctx, put_raw, "0123456789")
    result = call(TOOL, ctx, path=PATH, version_id=vid, byte_range=[50, 60])
    assert result["content"] == ""
    assert result["returned_bytes"] == 0
    assert result["truncated"] is True


def test_byte_range_splitting_a_multibyte_char_decodes_with_replacement(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    vid = _one(ctx, put_raw, "a☕b")  # ☕ is 3 bytes
    result = call(TOOL, ctx, path=PATH, version_id=vid, byte_range=[0, 1])
    assert result["returned_bytes"] == 2
    assert result["content"].startswith("a")
    assert "�" in result["content"]


@pytest.mark.parametrize(
    "byte_range", [[5, 2], [-1, 3], [0], [0, 1, 2], ["0", "5"], [True, 5], "0-5", [0.5, 2]]
)
def test_bad_byte_range_is_400(
    ctx: ToolContext, minter: FakeMinter, put_raw: Callable[..., str], byte_range: Any
) -> None:
    vid = _one(ctx, put_raw, "0123456789")
    minted = minter.mint_count
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, version_id=vid, byte_range=byte_range)
    assert minter.mint_count == minted


# --- not found ------------------------------------------------------------------------


def test_version_of_another_path_is_404(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, FM_V1, "mine")
    put_raw(OTHER, {"type": "doc", "seq": 1}, "theirs")
    [theirs] = _versions(ctx, OTHER)
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, version_id=theirs["version_id"])
    # Sanity: the id is fine at its own path.
    assert call(TOOL, ctx, path=OTHER, version_id=theirs["version_id"])["content"] == "theirs"


def test_unknown_version_id_is_404(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, FM_V1, "mine")
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, version_id="no-such-version")


def test_missing_path_is_404(ctx: ToolContext) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, version_id="anything")


# --- authorization --------------------------------------------------------------------


def test_zero_grant_subject_is_404_and_nothing_minted(
    ctx: ToolContext, nobody: ToolContext, minter: FakeMinter, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, FM_V1, "secret")
    [only] = _versions(ctx)
    minted = minter.mint_count
    expect_error(TOOL, nobody, 404, "not_found", path=PATH, version_id=only["version_id"])
    expect_error(TOOL, nobody, 404, "not_found", path="/racing/nope.md", version_id="v")
    assert minter.mint_count == minted


def test_grant_elsewhere_is_404_and_nothing_minted(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    put_raw: Callable[..., str],
) -> None:
    put_raw(PATH, FM_V1, "secret")
    seed_grant("user_other", "/other", Permission.OWN)
    expect_error(TOOL, make_ctx("user_other"), 404, "not_found", path=PATH, version_id="v")
    assert minter.mint_count == 0


def test_article_read_grant_reads_that_articles_history(
    ctx: ToolContext,
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    put_raw: Callable[..., str],
) -> None:
    put_raw(PATH, FM_V1, "old text\n")
    put_raw(PATH, FM_V2, "new text\n")
    _, oldest = _versions(ctx)
    seed_grant("user_one", PATH, Permission.READ)
    result = call(TOOL, make_ctx("user_one"), path=PATH, version_id=oldest["version_id"])
    assert result["content"] == "old text\n"


def test_s3_access_denied_is_404(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 404, "not_found", path=PATH, version_id="v")


@pytest.mark.parametrize("path", ["/Racing/x.md", "/x", "/_x.md", None])
def test_bad_path_is_400(ctx: ToolContext, minter: FakeMinter, path: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path, version_id="v")
    assert minter.mint_count == 0


@pytest.mark.parametrize("version_id", [None, "", 5, "x" * 1025])
def test_bad_version_id_is_400(ctx: ToolContext, minter: FakeMinter, version_id: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, version_id=version_id)
    assert minter.mint_count == 0
